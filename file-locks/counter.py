#!/usr/bin/env python3
"""Increment a counter in a shared file under a lock.

Start it on every node at once, e.g. with Slurm:
    echo 0 > counter.txt
    srun -N2 --ntasks-per-node=4 python3 counter.py counter.txt 50
    cat counter.txt        # 2 nodes x 4 tasks x 50 = 400, if the lock holds

Usage: counter.py <file> <increments> [flock|posix]
"""
import fcntl, os, sys

path, n = sys.argv[1], int(sys.argv[2])
lock = fcntl.lockf if sys.argv[3:] == ["posix"] else fcntl.flock
fd = os.open(path, os.O_RDWR)
for _ in range(n):
    lock(fd, fcntl.LOCK_EX)  # blocks until we own the file
    value = int(os.pread(fd, 32, 0) or 0)
    os.ftruncate(fd, 0)
    os.pwrite(fd, f"{value + 1}\n".encode(), 0)
    os.fsync(fd)
    lock(fd, fcntl.LOCK_UN)
