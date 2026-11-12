# Two nodes, one lock: file locks across VMs on virtio-fs

Code for the file locks post at [verda.com/blog](https://verda.com/blog). The draft of the post
itself is [article.md](article.md).

| File | What it does |
| --- | --- |
| `lock_basics.py` | The three Linux lock APIs (`flock`, POSIX, OFD) on one machine, what `/proc/locks` shows, and the POSIX close trap. |
| `locktest.sh`, `locktest.py` | Does an exclusive lock taken on one node exclude another node? Counts cross-node violations, with a same-node control. |
| `counter.py` | Lost-update test: processes on several nodes increment one counter under a lock. |

Everything needs only python3 (and ssh between nodes for `locktest.sh`).

```bash
./lock_basics.py /tmp

# from a directory on the shared filesystem
./locktest.sh node-1 node-2 20          # expect: cross-node violations 0/20

echo 0 > counter.txt
srun -N2 --ntasks-per-node=4 python3 counter.py counter.txt 50
cat counter.txt                         # expect: 400
```
