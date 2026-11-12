#!/usr/bin/env python3
"""Take an exclusive non-blocking lock. Called by locktest.sh on each node.

holder:    prints LOCKED and keeps the lock until the sentinel file exists.
attempter: prints ACQUIRED (the lock is not enforced) or BLOCKED.
"""
import fcntl, os, sys, time

mode, kind, lockfile, sentinel = sys.argv[1:5]
lock = fcntl.flock if kind == "flock" else fcntl.lockf
fd = os.open(lockfile, os.O_RDWR | os.O_CREAT, 0o644)
try:
    lock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print("BLOCKED" if mode == "attempter" else "HOLD-FAIL", flush=True)
    sys.exit(0)
if mode == "attempter":
    print("ACQUIRED", flush=True)
else:
    print("LOCKED", flush=True)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not os.path.exists(sentinel):
        time.sleep(0.05)
