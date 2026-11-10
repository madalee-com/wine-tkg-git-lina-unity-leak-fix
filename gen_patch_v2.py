#!/usr/bin/env python3
"""
Generate patch for SIGUSR1 syscall-handler race fix (Unity/Mono GC leak).
Adapted for current proton_11.0 branch (Wine 11.0).
"""

import os
import subprocess

CLONE = ".wine-clone"
SX86 = os.path.join(CLONE, "dlls/ntdll/unix/signal_x86_64.c")

# Read source file
with open(SX86, 'r') as f:
    sx86 = f.read()

print('Step 0: Source file loaded.')
print(f'  signal_x86_64.c length: {len(sx86)}')

# Step 2: struct syscall_frame - replace DWORD align[2] with ULONG64 retval
import re
# Find DWORD align[2] line with flexible spacing
align_match = re.search(r'DWORD\s+align\[2\];\s+/\* 00b8 \*/', sx86)
assert align_match, "struct syscall_frame align[2] not found"
old_frame = align_match.group(0)
new_frame = 'ULONG64 retval;             /* 00b8 - syscall return value for deferred USR1 handling */'
sx86 = sx86[:align_match.start()] + new_frame + sx86[align_match.end():]
print('Step 2: Replaced DWORD align[2]; with ULONG64 retval; in struct syscall_frame.')

# Step 3: struct amd64_thread_data - add sigusr1 fields after last field before };
# Find the struct end dynamically
teb_start = sx86.find('struct amd64_thread_data\n{')
assert teb_start != -1, "struct amd64_thread_data not found"
teb_end = sx86.find('\n};', teb_start)
assert teb_end != -1, "struct amd64_thread_data end not found"

# Get the last field line before };
last_field_line = sx86[teb_start:teb_end].rsplit('\n', 1)[-1]
# Determine next offset based on last field position (find /* 0xxx */ comment).
# The mxcsr line has trailing text after the comment close, so use a tolerant pattern.
import re
offset_match = re.search(r'/\*\s*0x?([0-9a-fA-F]+)', last_field_line)
if offset_match:
    last_offset = int(offset_match.group(1), 16)
else:
    last_offset = 0x33c  # fallback (mxcsr offset)
next_off1 = last_offset + 4  # align to 4 bytes (after DWORD mxcsr at 0x33c -> 0x340)
next_off2 = next_off1 + 1

# Insert new fields before }; (preserve last_field_line)
sx86 = sx86[:teb_end] + \
       '\n    volatile char           sigusr1_blocked;   /* ' + f'{next_off1:04x}' + ' refcount of block_sigusr1() calls */\n' + \
       '    volatile char           sigusr1_pending;   /* ' + f'{next_off2:04x}' + ' SIGUSR1 pending while blocked */\n' + \
       '};' + sx86[teb_end:]
print(f'Step 3: Added sigusr1_blocked/pending fields at offset {next_off1:04x}/{next_off2:04x} to amd64_thread_data.')

# Add C_ASSERT lines - find existing C_ASSERT near the struct
c_assert_pattern = 'C_ASSERT( offsetof( TEB, GdiTebBatch ) + offsetof( struct amd64_thread_data,'
c_assert_idx = sx86.find(c_assert_pattern, teb_start)
if c_assert_idx != -1:
    # Find the end of this C_ASSERT line
    line_end = sx86.find('\n', c_assert_idx)
    new_c_asserts = sx86[:line_end] + '\n' + \
        f'C_ASSERT( offsetof( TEB, GdiTebBatch ) + offsetof( struct amd64_thread_data, sigusr1_blocked ) == 0x{next_off1:02x} );' + '\n' + \
        f'C_ASSERT( offsetof( TEB, GdiTebBatch ) + offsetof( struct amd64_thread_data, sigusr1_pending ) == 0x{next_off2:02x} );' + \
        sx86[line_end:]
    sx86 = new_c_asserts
print('Step 3b: Added C_ASSERT lines for new fields.')

# Step 4: Insert helper functions before usr1_handler comment block
usr1_comment = '\t\tusr1_handler'
usr1_idx = sx86.find(usr1_comment)
assert usr1_idx != -1, "usr1_handler comment not found"

helpers = '''/*
 * block_sigusr1: Increment refcount and block SIGUSR1 for current thread.
 * Call before any context access in syscall handler to prevent race.
 */
static void block_sigusr1(void)
{
    struct amd64_thread_data *data = amd64_thread_data();
    sigset_t mask;

    sigprocmask( SIG_BLOCK, &signal_usr1_mask, NULL );
    data->sigusr1_blocked++;
}

/*
 * unblock_sigusr1: Decrement refcount and unblock SIGUSR1.
 * If SIGUSR1 was pending while blocked, run deferred handler.
 */
static void unblock_sigusr1(void)
{
    struct amd64_thread_data *data = amd64_thread_data();

    if (data->sigusr1_blocked > 0)
        data->sigusr1_blocked--;
    if (data->sigusr1_blocked == 0 && data->sigusr1_pending)
    {
        data->sigusr1_pending = 0;
        deferred_sigusr1();
    }
    sigprocmask( SIG_UNBLOCK, &signal_usr1_mask, NULL );
}

/*
 * deferred_sigusr1: Rewind syscall RIP so the syscall is re-executed
 * after SIGUSR1 was handled. Called when a blocked SIGUSR1 needs
 * processing but we're inside a syscall handler.
 */
static void deferred_sigusr1(void)
{
    struct amd64_thread_data *data = amd64_thread_data();
    struct syscall_frame *frame = data->syscall_frame;
    if (!frame) return;
    data->sigusr1_pending = 0;  /* consume pending before re-entering */
    frame->rip -= 2;  /* rewind past syscall instruction */
}

'''
sx86 = sx86[:usr1_idx] + helpers + sx86[usr1_idx:]
print('Step 4: Inserted block/unblock/deferred helper functions before usr1_handler.')

# Step 5: Refactor usr1_handler to add re-entrancy guard
# Insert after "ucontext_t *ucontext = init_handler( sigcontext );" inside usr1_handler.
usr1_anchor = 'static void usr1_handler( int signal, siginfo_t *siginfo, void *sigcontext )\n{\n    ucontext_t *ucontext = init_handler( sigcontext );\n'
usr1_idx = sx86.find(usr1_anchor)
assert usr1_idx != -1, "usr1_handler entry anchor not found"
guard_block = (
    'static void usr1_handler( int signal, siginfo_t *siginfo, void *sigcontext )\n'
    '{\n'
    '    ucontext_t *ucontext = init_handler( sigcontext );\n'
    '\n'
    '    /* Prevent SIGUSR1 from running while we manipulate context structures. */\n'
    '    if (is_inside_syscall( ucontext ) && amd64_thread_data()->sigusr1_blocked)\n'
    '    {\n'
    '        amd64_thread_data()->sigusr1_pending = 1;\n'
    '        return;\n'
    '    }\n'
)
sx86 = sx86[:usr1_idx] + guard_block + sx86[usr1_idx + len(usr1_anchor):]
print('Step 5: Added re-entrancy guard to usr1_handler.')

# Step 6: NtSetContextThread - block/unblock and r10/Rip handling
setctx_start = sx86.find('NTSTATUS WINAPI NtSetContextThread(')
assert setctx_start != -1, "NtSetContextThread not found"
# Find opening brace after function signature
brace_idx = sx86.find('{', setctx_start)
sx86 = sx86[:brace_idx+1] + '\n    block_sigusr1();\n' + sx86[brace_idx+1:]
print('Step 6a: Added block_sigusr1() to NtSetContextThread.')

# Add unblock before returns in NtSetContextThread and r10/Rip handling
setctx_body = sx86[setctx_start:]
# Find the integer context handling block
r10_marker = 'frame->r10 = context->R10;'
if r10_marker in setctx_body:
    new_r10 = '''if (frame->restore_flags & RESTORE_FLAGS_INSTRUMENTATION)
            frame->r10 = context->Rip;
        else
            frame->r10 = context->R10;'''
    setctx_body = setctx_body.replace(r10_marker, new_r10, 1)
print('Step 6b: Added rip->r10 conditional handling in NtSetContextThread.')

# Add unblock_sigusr1() before the final return STATUS_SUCCESS in NtSetContextThread
# Find "return STATUS_SUCCESS;" after the r10 modification
success_marker = 'frame->restore_flags |= flags & ~CONTEXT_INTEGER;\n    return STATUS_SUCCESS;'
if success_marker in setctx_body:
    unblock_success = 'frame->restore_flags |= flags & ~CONTEXT_INTEGER;\n    unblock_sigusr1();\n    return STATUS_SUCCESS;'
    setctx_body = setctx_body.replace(success_marker, unblock_success, 1)
print('Step 6c: Added unblock_sigusr1() before final return in NtSetContextThread.')

sx86 = sx86[:setctx_start] + setctx_body + sx86[setctx_start+len(setctx_body):]

# Step 7: NtGetContextThread - block/unblock wrapping
getctx_start = sx86.find('NTSTATUS WINAPI NtGetContextThread(')
assert getctx_start != -1, "NtGetContextThread not found"
brace_idx = sx86.find('{', getctx_start)
sx86 = sx86[:brace_idx+1] + '\n    block_sigusr1();\n' + sx86[brace_idx+1:]
print('Step 7a: Added block_sigusr1() to NtGetContextThread.')

# Find the final return in NtGetContextThread and add unblock before it
getctx_body = sx86[getctx_start:]
# Look for the last "return" in this function (before next function or #endif)
next_func = sx86.find('\nstatic ', getctx_start + 100)
if next_func == -1:
    next_func = sx86.find('#endif', getctx_start)
if next_func != -1:
    func_end = next_func
else:
    func_end = getctx_start + len(getctx_body)
func_tail = sx86[getctx_start:func_end]
# Find "return STATUS_SUCCESS;" in tail and add unblock before it
if 'return STATUS_SUCCESS;' in func_tail:
    func_tail = func_tail.replace('return STATUS_SUCCESS;', 'unblock_sigusr1();\n    return STATUS_SUCCESS;', 1)
    sx86 = sx86[:getctx_start] + func_tail + sx86[func_end:]
print('Step 7b: Added unblock_sigusr1() before final return in NtGetContextThread.')

# Step 8: Syscall dispatcher asm changes (proton_11.0 / Wine 11.0 layout)
#
# 8a: Instrumentation path fix (inline label `2:` at ~line 3600).
#     r10 is used for the instrumentation_callback load; we must not overwrite it
#     with frame->rip.  Save rip to r11 before the gs-load, and restore rip into
#     r10 immediately before xchgq so NtSetContextThread sees Rip in r10.
instr_anchor = '                   "2:\\tmovq %gs:0x330,%r10\\n\\t"  /* amd64_thread_data()->instrumentation_callback */\n'
instr_idx = sx86.find(instr_anchor)
assert instr_idx != -1, "inline instrumentation anchor (label 2:) not found"
new_instr_block = (
    '                   "2:\\tmovq 0x70(%rcx),%r11\\n\\t"  /* save frame->rip */\n'
    '                   "movq %gs:0x330,%r10\\n\\t"  /* amd64_thread_data()->instrumentation_callback */\n'
    '                   "movq (%r10),%r10\\n\\t"\n'
    '                   "test %r10,%r10\\n\\t"\n'
    '                   "jz 3b\\n\\t"\n'
    '                   "testl $0x2,%edx\\n\\t"          /* CONTEXT_INTEGER */\n'
    '                   "jnz 1b\\n\\t"\n'
    '                   "movq %r11,%r10\\n\\t"             /* restore frame->rip as r10 for NtSetContextThread */\n'
    '                   "xchgq %r10,(%rsp)\\n\\t"'
)
# Replace from the label line through the xchgq line (7 instructions)
lines_to_replace = instr_anchor
# find end of the xchgq line
xchg_line_end = sx86.find('"xchgq %r10,(%rsp)\\n\\t"', instr_idx) + len('"xchgq %r10,(%rsp)\\n\\t"')
sx86 = sx86[:instr_idx] + new_instr_block + sx86[xchg_line_end:]
print('Step 8a: Fixed inline instrumentation path (rip save to r11, restore rip->r10 before xchg).')

# 8b: Deferred USR1 check at the real return path (line ~3492).
#     Insert immediately after "leaq 0x70(%rcx),%rsp" where %rax still holds the
#     syscall result and %rcx = frame.  Store retval to frame->retval (0xb8), then
#     read sigusr1_pending via gs-relative offset (%gs:0x341) and call
#     deferred_sigusr1() if set.  deferred_sigusr1 rewinds rip so the syscall is
#     re-executed; the pending signal is now delivered before the dispatcher runs again.
ret_anchor = '                   "leaq 0x70(%rcx),%rsp\\n\\t"      /* %rsp > frame means no longer inside syscall */\n'
ret_idx = sx86.find(ret_anchor)
assert ret_idx != -1, "return-path anchor (leaq 0x70(%rcx),%rsp) not found"
check_block = (
    '                   "movq %rax,0xb8(%rcx)\\n\\t"      /* frame->retval */\n'
    '                   "movb %gs:0x341,%al\\n\\t"        /* load sigusr1_pending (gs-relative) */\n'
    '                   "testb %al,%al\\n\\t"             /* check if set */\n'
    '                   "jz 5f\\n\\t"\n'
    '                   "pushq %rcx\\n\\t"                /* save frame ptr across call */\n'
    '                   "call " __ASM_NAME("deferred_sigusr1") "\\n\\t"\n'
    '                   "popq %rcx\\n\\t"                 /* restore frame ptr */\n'
    '                   "5:\\n\\t"\n'
)
sx86 = sx86[:ret_idx + len(ret_anchor)] + check_block + sx86[ret_idx + len(ret_anchor):]
print('Step 8b: Inserted retval store + deferred USR1 check at return path.')

# Write modified files back to source tree
with open(SX86, 'w') as f:
    f.write(sx86)
print('\nModified files written back to source tree.')

# Generate patch
patch_path = '/home/agotenshi/build/wine-tkg-git-lina-unity-leak-fix/custom-proton/lina-fix-v6-proton.mypatch'
result = subprocess.run(
    ['git', '-C', CLONE, 'diff'],
    capture_output=True, text=True
)
with open(patch_path, 'w') as f:
    f.write(result.stdout)
print(f'Patch written to {patch_path}')
print(f'Patch size: {len(result.stdout)} bytes')
print('Done!')
