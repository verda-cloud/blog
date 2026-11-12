#!/usr/bin/env python3
"""The three Linux file lock APIs on one machine, and the POSIX close() trap.

Run it on any filesystem: ./lock_basics.py [directory]
"""
import fcntl, os, struct, subprocess, sys

F_OFD_SETLK = 37  # from <fcntl.h>; Python's fcntl module has it as fcntl.F_OFD_SETLK since 3.9
path = os.path.join(sys.argv[1] if len(sys.argv) > 1 else ".", "demo.lock")
wrlck = struct.pack("hhqqi", fcntl.F_WRLCK, os.SEEK_SET, 0, 0, 0)  # whole file, exclusive

TRY = {
    "flock": "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)",
    "posix": "fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)",
    "ofd": f"fcntl.fcntl(fd, {F_OFD_SETLK}, {wrlck!r})",
}


def other_process(kind):
    """Can a second process take the same lock? Prints the answer."""
    code = (
        "import fcntl, os, sys\n"
        f"fd = os.open({path!r}, os.O_RDWR)\n"
        f"try:\n    {TRY[kind]}\n    print('acquired')\n"
        "except OSError as e:\n    print('blocked:', e.strerror)\n"
    )
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).stdout.strip()


def proc_locks(fd):
    ino = os.fstat(fd).st_ino
    return [l.strip() for l in open("/proc/locks") if l.split()[5].endswith(f":{ino}")]


for kind in TRY:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    exec(TRY[kind])
    print(f"{kind:5}  held by pid {os.getpid()}; second process: {other_process(kind)}")
    for line in proc_locks(fd):
        print("       /proc/locks:", line)
    os.close(fd)

# The trap: a POSIX lock belongs to the process, and closing *any* descriptor of
# the file drops it. Libraries that open and close the same file behind your back
# release your lock.
fd = os.open(path, os.O_RDWR)
fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
os.close(os.open(path, os.O_RDONLY))  # an unrelated open+close of the same file
print(f"posix  after closing another fd of the file; second process: {other_process('posix')}")

# flock and OFD locks belong to the open file description, so they survive that.
fd2 = os.open(path, os.O_RDWR)
fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
os.close(os.open(path, os.O_RDONLY))
print(f"flock  after closing another fd of the file; second process: {other_process('flock')}")
os.unlink(path)
