# lina-fix-v6 — AI Repeatable Generation Instructions

## Overview
This patch fixes a SIGUSR1 syscall-handler race condition that corrupts Unity/Mono GC thread state, causing extreme memory leaks in Wine 11 / Proton. The fix:
- Blocks SIGUSR1 around `NtSetContextThread` / `NtGetContextThread` via a refcount.
- If SIGUSR1 fires while blocked *inside* a syscall, sets a pending flag and returns; the real handler runs when unblocked.
- Inserts inline instrumentation so `frame->rip` (not `%r10`) is passed to `NtSetContextThread`.
- At the syscall return path, checks the pending flag and rewinds `frame->rip` so the syscall is re-executed after signal delivery.

Two target-specific variants are provided:

| Variant | Patch file | Target branch | Struct offsets | Generator |
|---------|-----------|---------------|----------------|-----------|
| proton_11.0 | `lina-fix-v6-proton.mypatch` | `proton_11.0` (Wine 11.0, commit e60892cc239) | 0x340/0x341 | `gen_patch_v2.py` |
| experimental_11.0 | `lina-fix-v6-experimental11.mypatch` | `experimental_11.0` (Wine 7b3ceaf8644) | 0x341/0x342 | `gen_patch_experimental11.py` |

**Why the offset differs:** `experimental_11.0` adds `char syscall_dispatch; /* 0340 */` (from `prctl(PR_SET_SYSCALL_USER_DISPATCH)`) as the last field of `struct amd64_thread_data`, shifting the free space to 0x341/0x342. The `proton_11.0` branch's last field is `DWORD mxcsr; /* 033c */`, leaving 0x340/0x341 free. All other anchors (inline instrumentation block, return-path `leaq 0x70(%rcx),%rsp`, NtSetContextThread/NtGetContextThread bodies, usr1_handler) are byte-identical between the two branches.

## Prerequisites
- Fork: `wine-tkg-git-lina-unity-leak-fix` (origin/master rebased onto upstream master at commit 65d846c8 "Rebase Proton patches against Wine 11.21")
- Clone ValveSoftware/wine to `.wine-clone/` (gitignored)
- **Checkout the `proton_11.0` branch in the clone** (NOT proton_10.0)
- For experimental_11.0 variant: prepare a temp checkout with `dlls/ntdll/unix/signal_x86_64.c` at the experimental_11.0 Wine HEAD (commit 7b3ceaf8644bb5d57140903e12897efac69be16a)

## Files Modified
- `dlls/ntdll/unix/signal_x86_64.c` — core fix (the earlier threadctx.obj/Makefile.in step was removed as a known bug; the fix is self-contained in signal_x86_64.c)

## Key Structural Anchors (proton_11.0 / Wine 11.0)

### struct syscall_frame (line ~496)
- `DWORD restore_flags; /* 00b4 */` followed by `DWORD align[2]; /* 00b8 */`
- Replace the **entire** `DWORD align[2];` line with:
  ```c
  ULONG64 retval;             /* 00b8 - syscall return value for deferred USR1 handling */
  ```

### struct amd64_thread_data (line ~520)
- Current fields end at `DWORD mxcsr; /* 033c Unix-side mxcsr register */` — this is the **last field** before `};`.
- Add two fields **after** mxcsr:
  ```c
  volatile char           sigusr1_blocked;   /* 0340 refcount of block_sigusr1() calls */
  volatile char           sigusr1_pending;   /* 0341 SIGUSR1 pending while blocked */
  ```
- Add C_ASSERT lines (matching existing style, no leading spaces):
  ```c
  C_ASSERT( offsetof( TEB, GdiTebBatch ) + offsetof( struct amd64_thread_data, sigusr1_blocked ) == 0x340 );
  C_ASSERT( offsetof( TEB, GdiTebBatch ) + offsetof( struct amd64_thread_data, sigusr1_pending ) == 0x341 );
  ```
- **For experimental_11.0:** the last field is `char syscall_dispatch; /* 0340 */`, so offsets become 0x341/0x342:
  ```c
  C_ASSERT( offsetof( TEB, GdiTebBatch ) + offsetof( struct amd64_thread_data, sigusr1_blocked ) == 0x341 );
  C_ASSERT( offsetof( TEB, GdiTebBatch ) + offsetof( struct amd64_thread_data, sigusr1_pending ) == 0x342 );
  ```

### Inline instrumentation path (line ~3506, 8-space indent)
Anchor on this exact block:
```c
                   "2:\tmovq %gs:0x330,%r10\n\t"  /* amd64_thread_data()->instrumentation_callback */
                   "movq (%r10),%r10\n\t"
                   "test %r10,%r10\n\t"
                   "jz 3b\n\t"
                   "testl $0x2,%edx\n\t"          /* CONTEXT_INTEGER */
                   "jnz 1b\n\t"
                   "xchgq %r10,(%rsp)\n\t"
```
**Rewrite** to save rip to `%r11` before the gs-load, and restore rip into `%r10` right before `xchgq`:
```c
                   "2:\tmovq 0x70(%rcx),%r11\n\t"  /* save frame->rip */
                   "movq %gs:0x330,%r10\n\t"  /* amd64_thread_data()->instrumentation_callback */
                   "movq (%r10),%r10\n\t"
                   "test %r10,%r10\n\t"
                   "jz 3b\n\t"
                   "testl $0x2,%edx\n\t"          /* CONTEXT_INTEGER */
                   "jnz 1b\n\t"
                   "movq %r11,%r10\n\t"             /* restore frame->rip as r10 for NtSetContextThread */
                   "xchgq %r10,(%rsp)\n\t"
```

### Real return path (line ~3492)
Anchor on this exact line:
```c
                   "leaq 0x70(%rcx),%rsp\n\t"      /* %rsp > frame means no longer inside syscall */
```
Insert immediately after it (while `%rax` still holds the syscall result and `%rcx` = frame):
```c
                   "movq %rax,0xb8(%rcx)\n\t"      /* frame->retval */
                   "movb %gs:0x341,%al\n\t"        /* load sigusr1_pending (gs-relative) */
                   "testb %al,%al\n\t"             /* check if set */
                   "jz 5f\n\t"
                   "pushq %rcx\n\t"                /* save frame ptr across call */
                   "call " __ASM_NAME("deferred_sigusr1") "\n\t"
                   "popq %rcx\n\t"                 /* restore frame ptr */
                   "5:\n\t"
```
Then the existing `#ifdef __linux__` block continues unchanged.

> **Do NOT** use `__ASM_GLOBAL_POINTER`, `iretq`, or any exported-symbol anchors — these do not exist in proton_11.0. The real return path is the local label `__wine_syscall_dispatcher_return:` whose surrounding asm contains the `leaq 0x70(%rcx),%rsp` line above.

> **For experimental_11.0:** the gs-relative offset for `sigusr1_pending` is `%gs:0x342` (not `%gs:0x341`). All other return-path text is identical.

## Implementation Steps
1. syscall_frame: Replace entire `DWORD align[2]; /* 00b8 */` line with `ULONG64 retval; /* 00b8 - ... */`.
2. amd64_thread_data: Find the last field line (containing `/* 0xxx */` offset comment), compute next offsets based on the last field's size (char → +1, DWORD/larger → +4), insert two `volatile char` fields before `};`, then add two C_ASSERT lines after the existing C_ASSERT block.
3. Insert helper functions **before** the `usr1_handler` comment block (`static void quit_handler`):
   - `block_sigusr1()` — `sigprocmask(SIG_BLOCK, &signal_usr1_mask, NULL)` then `data->sigusr1_blocked++`
   - `unblock_sigusr1()` — decrement refcount (clamp at 0); if count hits 0 and `sigusr1_pending`, clear pending and call `deferred_sigusr1()`; then `sigprocmask(SIG_UNBLOCK, ...)`
   - `deferred_sigusr1()` — null-check `data->syscall_frame`; clear pending; `frame->rip -= 2` (rewind past syscall instruction)
4. usr1_handler: Add a **re-entrancy guard** at the very top (after `ucontext_t *ucontext = init_handler(sigcontext);`):
   ```c
   if (is_inside_syscall(ucontext) && amd64_thread_data()->sigusr1_blocked)
   {
       amd64_thread_data()->sigusr1_pending = 1;
       return;
   }
   ```
   This must be a **check**, NOT a `block_sigusr1()` call (calling it at entry would self-pend and skip all work).
5. NtSetContextThread: `block_sigusr1();` at function start; before the final `return STATUS_SUCCESS;`, add `unblock_sigusr1();`; change `frame->r10 = context->R10;` to conditional: if `frame->restore_flags & RESTORE_FLAGS_INSTRUMENTATION` then `frame->r10 = context->Rip` else `frame->r10 = context->R10`.
6. NtGetContextThread: `block_sigusr1();` at function start; `unblock_sigusr1();` before final `return STATUS_SUCCESS;`.
7. Syscall dispatcher asm (two changes, see anchors above):
   - Inline instrumentation path: save rip to `%r11`, restore rip→`%r10` before `xchgq`.
   - Return path (`leaq 0x70(%rcx),%rsp`): store retval, read `%gs:<sigusr1_pending offset>`, call `deferred_sigusr1` if pending. Offset is 0x341 for proton_11.0, 0x342 for experimental_11.0.

## Regeneration Procedure — proton_11.0 variant
```bash
# 1. Ensure wine clone is on proton_11.0 and source files are clean
cd /home/agotenshi/build/wine-tkg-git-lina-unity-leak-fix/.wine-clone
git checkout proton_11.0
git checkout -- dlls/ntdll/unix/signal_x86_64.c

# 2. Run patch generator
cd /home/agotenshi/build/wine-tkg-git-lina-unity-leak-fix
python3 gen_patch_v2.py

# 3. Verify patch applies cleanly (from repo root)
git -C .wine-clone apply --check custom-proton/lina-fix-v6-proton.mypatch

# 4. Reset source tree so only the .mypatch is left as output
git -C .wine-clone checkout -- dlls/ntdll/unix/signal_x86_64.c

# 5. Output is at: custom-proton/lina-fix-v6-proton.mypatch
```

## Regeneration Procedure — experimental_11.0 variant
The `experimental_11.0` branch adds `char syscall_dispatch; /* 0340 */` as the last field of `struct amd64_thread_data`, shifting our new fields to 0x341/0x342. Use `gen_patch_experimental11.py` with a temp checkout:

```bash
# 1. Prepare a temp repo with experimental_11.0's signal_x86_64.c
rm -rf .wine-exp11 && mkdir -p .wine-exp11/dlls/ntdll/unix
cp sig_x86_64_experimental11.c .wine-exp11/dlls/ntdll/unix/signal_x86_64.c
cd .wine-exp11 && git init -q && git config user.email "fix@generator" && git config user.name "Generator"
git add . && git commit -q -m "experimental_11.0 base"

# 2. Run the experimental_11.0 generator
cd /home/agotenshi/build/wine-tkg-git-lina-unity-leak-fix
python3 gen_patch_experimental11.py

# 3. Verify patch applies cleanly against the temp repo
git -C .wine-exp11 apply --check custom-proton/lina-fix-v6-experimental11.mypatch

# 4. Reset the temp repo so only the .mypatch is left as output
git -C .wine-exp11 checkout -- dlls/ntdll/unix/signal_x86_64.c

# 5. Output is at: custom-proton/lina-fix-v6-experimental11.mypatch
```

## Constraints
- **Target branches:** `proton_11.0` (offsets 0x340/0x341) or `experimental_11.0` (offsets 0x341/0x342). Do NOT use proton_10.0 or older anchors.
- Do NOT commit the wine clone changes.
- Output only: new `.mypatch` file + these instructions.
- The patch is generated via `git diff` on the modified wine clone.

## Notes
- `gen_patch_v2.py` and `gen_patch_experimental11.py` extract patterns dynamically from the actual source file (robust against whitespace differences).
- All string replacements use exact pattern matching with `assert` failures if anchors are missing.
- `%gs:0x341` is the gs-relative offset for `sigusr1_pending` in proton_11.0; `%gs:0x342` in experimental_11.0. `%gs:0x330` is `instrumentation_callback` (identical in both).
- The inline instrumentation label `2:` uses 8-space indent inside the `__ASM_GLOBAL_FUNC` string literal.
- Both generators have been tested and produce patches that apply cleanly to their respective targets.
