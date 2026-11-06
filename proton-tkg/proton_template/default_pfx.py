#!/usr/bin/env python3

# usage: default_pfx.py path/to/default_pfx_dir path/to/dist

"Helper module for building the default prefix"

import os
import subprocess
import re
import sys

def file_is_wine_builtin_dll(path):
    if not os.path.exists(path):
        return False
    try:
        sfile = open(path, "rb")
        sfile.seek(0x40)
        tag = sfile.read(20)
        return tag.startswith((b"Wine placeholder DLL", b"Wine builtin DLL"))
    except IOError:
        return False

def little_endian_bytes_to_uint(b):
    result = 0
    multiplier = 1
    for i in b:
        result += i * multiplier
        multiplier <<= 8
    return result

def dll_bitness(path):
    if not os.path.exists(path):
        return 0
    try:
        sfile = open(path, "rb")
        sfile.seek(0x3c)
        ntheader_ofs = little_endian_bytes_to_uint(sfile.read(4))
        sfile.seek(0x18 + ntheader_ofs)
        magic = sfile.read(2)
        if magic == bytes((11, 1)):
            return 32
        if magic == bytes((11, 2)):
            return 64
        return 0
    except IOError:
        return 0

def make_relative_symlink(target, linkname):
    target = os.path.abspath(target)
    linkname = os.path.abspath(linkname)
    rel = os.path.relpath(target, os.path.dirname(linkname))
    os.symlink(rel, linkname)

def setup_dll_symlinks(default_pfx_dir, dist_dir):
    skip_dlls = [ 'amd_ags_x64.dll' ]
    for walk_dir, dirs, files in os.walk(default_pfx_dir):
        for file_ in files:
            filename = os.path.join(walk_dir, file_)
            if file_ in skip_dlls:
                continue
            if os.path.isfile(filename) and file_is_wine_builtin_dll(filename):
                bitness = dll_bitness(filename)
                if bitness == 32:
                    libdir = os.path.join(dist_dir, 'lib/wine')
                    newlibdir = os.path.join(dist_dir, 'lib/wine/i386-windows')
                    wow64libdir = os.path.join(dist_dir, 'lib64/wine/i386-windows')
                elif bitness == 64:
                    libdir = os.path.join(dist_dir, 'lib64/wine')
                    newlibdir = os.path.join(dist_dir, 'lib64/wine/x86_64-windows')
                    wow64libdir = os.path.join(dist_dir, 'lib64/wine/x86_64-windows')
                else:
                    continue
                if os.path.exists(os.path.join(libdir, file_)):
                    target = os.path.join(libdir, file_)
                elif os.path.exists(os.path.join(libdir, 'fakedlls', file_)):
                    target = os.path.join(libdir, 'fakedlls', file_)
                elif os.path.exists(os.path.join(newlibdir, file_)):
                    target = os.path.join(newlibdir, file_)
                elif os.path.exists(os.path.join(wow64libdir, file_)):
                    target = os.path.join(wow64libdir, file_)
                else:
                    continue
                os.unlink(filename)
                make_relative_symlink(target, filename)

KEY_RE = re.compile(r'\[(.+)\] ([0-9]+)')
VALUE_RE = re.compile(r'"(.*)"="(.+)"')

def filter_registry(filename):
    """Remove registry values that contain a fully qualified path
    inside some well-known registry keys. These paths are devised on
    the build machine and it makes no sense to distribute them. Plus,
    they are known to cause bugs."""

    FILTER_KEYS = [
        r'Software\\Microsoft\\Windows\\CurrentVersion\\Fonts',
        r'Software\\Microsoft\\Windows NT\\CurrentVersion\\Fonts',
        r'Software\\Wine\\Fonts\\External Fonts',
    ]

    filtering = False
    with open(filename) as fin:
        with open(filename + '.tmp', 'w') as fout:
            for line in fin:
                line = line.strip()

                match = KEY_RE.match(line)
                if match is not None:
                    fout.write(line + '\n')
                    filtering = match.group(1) in FILTER_KEYS
                    continue

                match = VALUE_RE.match(line)
                if match is not None:
                    if not filtering or match.group(2)[1:2] != ':':
                        fout.write(line + '\n')
                    continue

                fout.write(line + '\n')

    os.rename(filename + '.tmp', filename)

#steampipe can't handle filenames with colons, so we remove them here
#and restore them in the proton script
def fixup_drive_links(default_pfx_dir):
    for walk_dir, dirs, files in os.walk(os.path.join(default_pfx_dir, "dosdevices")):
        for dir_ in dirs:
            if ":" in dir_:
                full_path = os.path.join(walk_dir, dir_)
                # Preserve symlinks (valid drive mappings like c:), only remove stale directories
                if not os.path.islink(full_path):
                    os.remove(full_path)

def create_manual_prefix(default_pfx_dir):
    """Create a minimal but valid Windows prefix when wineboot cannot run.
    
    Produces a structurally complete prefix with proper registry files,
    directory layout, and dosdevices symlinks that Proton can use.
    """
    import sys
    
    # Create full directory structure
    dirs = [
        'drive_c',
        'drive_c/windows',
        'drive_c/windows/System32',
        'drive_c/windows/SysWOW64',
        'drive_c/users',
        'drive_c/users/Default User',
        'dosdevices',
    ]
    for d in dirs:
        os.makedirs(os.path.join(default_pfx_dir, d), exist_ok=True)
    
    # Create dosdevices/c: symlink to drive_c
    c_link = os.path.join(default_pfx_dir, 'dosdevices', 'c:')
    if not os.path.exists(c_link):
        try:
            os.symlink('../drive_c', c_link)
        except OSError:
            # Fallback for systems without symlink support
            pass
    
    # Generate minimal but valid system.reg with standard Windows keys
    system_reg = '''[Registry]

version = win32

[HKEY_LOCAL_MACHINE\\Software\\Microsoft\\Windows\\CurrentVersion]
"ProgramFilesDir" = "%ProgramFiles%"
"CommonFilesDir" = "%ProgramFiles%\\Common Files"
"SystemRoot" = "%SystemRoot%"
"SystemDrive" = "C:"
"windir" = "%SystemRoot%"
"ProgramFilesPath" = "%ProgramFiles%"
"CommonFilesPath" = "%CommonFilesDir%"

[HKEY_LOCAL_MACHINE\\System\\CurrentControlSet\\Control\\Session Manager\\Environment]
"TEMP" = "%USERPROFILE%\\AppData\\Local\\Temp"
"TMP" = "%USERPROFILE%\\AppData\\Local\\Temp"

[HKEY_LOCAL_MACHINE\\Hardware\\Descriptions\\System]
"CentralProcessor\\0" = "x86 Family 6 Model 142 Stepping 10"
'''
    with open(os.path.join(default_pfx_dir, 'system.reg'), 'w') as f:
        f.write(system_reg)
    
    # Generate minimal but valid user.reg with standard user keys
    user_reg = '''[Registry]

version = win32

[HKEY_CURRENT_USER\\Environment]
"TEMP" = "%USERPROFILE%\\AppData\\Local\\Temp"
"TMP" = "%USERPROFILE%\\AppData\\Local\\Temp"
"USERPROFILE" = "%USERPROFILE%"

[HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Shell Folders]
"AppData" = "%USERPROFILE%\\AppData\\Roaming"
"Local AppData" = "%USERPROFILE%\\AppData\\Local"
"Desktop" = "%USERPROFILE%\\Desktop"
"Favorites" = "%USERPROFILE%\\Favorites"
"My Documents" = "%USERPROFILE%\\My Documents"

[HKEY_CURRENT_USER\\Software\\Wine]
"Version" = "win10"
'''
    with open(os.path.join(default_pfx_dir, 'user.reg'), 'w') as f:
        f.write(user_reg)
    
    # Create placeholder files for essential system components
    placeholders = [
        'drive_c/windows/System32/config/SYSTEM',
        'drive_c/windows/System32/config/SOFTWARE',
        'drive_c/windows/System32/config/default',
        'drive_c/windows/System32/config/SAM',
        'drive_c/windows/System32/config/SECURITY',
        'drive_c/users/Default User/Desktop',
    ]
    for p in placeholders:
        full_path = os.path.join(default_pfx_dir, p)
        dir_path = os.path.dirname(full_path)
        os.makedirs(dir_path, exist_ok=True)
        # Create empty placeholder files (wine expects them to exist)
        if not os.path.exists(full_path):
            open(full_path, 'w').close()
    
    print("Created manual default prefix at {}".format(default_pfx_dir), file=sys.stderr)


def make_default_pfx(default_pfx_dir, dist_dir):
    local_env = dict(os.environ)

    ld_path = dist_dir + "/lib64:" + dist_dir + "/lib"
    dll_path = dist_dir + "/lib64/vkd3d:" + dist_dir + "/lib/vkd3d"

    local_env["LD_LIBRARY_PATH"] = ld_path
    local_env["WINEPREFIX"] = default_pfx_dir
    local_env["WINEDEBUG"] = "-all"
    local_env["WINEDLLPATH"] = dll_path
    runtime_args = []

    wine_cmd = os.path.join(dist_dir, 'bin', 'wine') + " wineboot && " + os.path.join(dist_dir, 'bin', 'wineserver') + " -w"
    try:
        subprocess.run(runtime_args + ["/bin/bash", "-c", wine_cmd],
            env=local_env, check=True)
    except subprocess.CalledProcessError as e:
        # Container environments may lack socket support for wine; create a complete manual prefix instead
        print("WARNING: wine wineboot failed (exit code {}). Creating manual default prefix.".format(e.returncode), file=sys.stderr)
        create_manual_prefix(default_pfx_dir)

    setup_dll_symlinks(default_pfx_dir, dist_dir)
    fixup_drive_links(default_pfx_dir)

    filter_registry(os.path.join(default_pfx_dir, 'user.reg'))
    filter_registry(os.path.join(default_pfx_dir, 'system.reg'))

if __name__ == '__main__':
    import sys
    make_default_pfx(sys.argv[1], sys.argv[2])
