<!-- DRAFT for verda.com/blog. Code for this post: this directory. -->

# Two nodes, one lock: making flock() work across VMs on virtio-fs

Two processes on two nodes of a cluster ask for an exclusive lock on the same file in the shared
`/home`. Both get it.

That was the state of file locks on our Instant Clusters until this month. Nothing crashed and
nothing logged an error; `flock()` returned 0 on both nodes. The first visible symptom was corrupt
files in a shared Hugging Face cache when several nodes downloaded the same model at once.

This post is about why that happened, how we fixed it in `virtiofsd`, what the fix costs, and how
the same question is answered on other shared filesystems. The short version:

* A file lock is only as wide as the kernel that keeps it. Our VMs each kept their own.
* The fix is a patch to `virtiofsd` that takes the guest's locks on the host's CephFS mount, so
  the CephFS metadata server arbitrates them for every node. It is deployed, and
  [proposed upstream](https://gitlab.com/virtio-fs/virtiofsd/-/merge_requests/351).
* A lock now costs a network round trip, about 0.25 ms, instead of about 1 µs. Reads, writes and
  metadata are as fast as before.

## How a cluster node gets its shared filesystem

An Instant Cluster node is a KVM virtual machine. GPUs and the InfiniBand adapter are passed
through to it as PCI devices; the shared filesystem is not a device at all.

The storage behind a Shared Filesystem (SFS) is [CephFS](https://docs.ceph.com/en/latest/cephfs/).
CephFS splits a filesystem in two: file data lives as objects on the storage daemons (OSDs), and
the namespace (directories, inodes, permissions, and file locks) is owned by a metadata server
(MDS). Clients talk to both directly. The MDS hands out *capabilities* that say what a client may
cache, and it is the one place that sees every client, which makes it the natural arbiter for
anything that has to be consistent across machines.

We do not mount CephFS inside the VMs. The hypervisor mounts the customer's CephFS subvolume with
the kernel client, and shares that directory into the VM with
[virtio-fs](https://virtio-fs.gitlab.io/):

```
   node 1 (VM)                              node 2 (VM)
 ┌────────────────────┐                   ┌────────────────────┐
 │ application        │                   │ application        │
 │ guest kernel       │                   │ guest kernel       │
 │   virtiofs (FUSE)  │                   │   virtiofs (FUSE)  │
 └─────────┬──────────┘                   └─────────┬──────────┘
           │ virtqueue                              │ virtqueue
 ┌─────────┴──────────┐                   ┌─────────┴──────────┐
 │ virtiofsd          │                   │ virtiofsd          │
 │ host kernel        │                   │ host kernel        │
 │   CephFS client    │                   │   CephFS client    │
 └─────────┬──────────┘                   └─────────┬──────────┘
   hypervisor 1                             hypervisor 2
           └────────────────┐   ┌───────────────────┘
                        ┌───┴───┴───┐
                        │ CephFS    │
                        │ MDS + OSDs│
                        └───────────┘
```

virtio-fs is FUSE with the transport swapped out. The guest kernel's FUSE client sends its
requests (`LOOKUP`, `OPEN`, `READ`, ...) over a virtqueue instead of `/dev/fuse`, and a
vhost-user daemon on the host, [`virtiofsd`](https://gitlab.com/virtio-fs/virtiofsd), answers
them by making the matching system calls on the shared directory. We run one `virtiofsd` per VM
and filesystem.

Why this instead of a CephFS mount in the guest: the VM never joins the storage network and never
holds a Ceph key, the guest needs no Ceph client of a matching version, and the hypervisor decides
what the tenant can see (for example, the `ceph.*` extended attributes that control quotas and
layouts are filtered out). It is also fast: guest memory is shared with `virtiofsd`, whose thread pool
is confined to the same host cores as the VM, and a sequential read from inside a VM runs at about 10 GB/s (`fio`, 16 jobs, 4 MiB blocks, direct I/O: 9.9, 10.6 and 12.5 GB/s over three runs on a B300 node).

The price is one more kernel between the application and the filesystem, and that is where the
locks got lost.

## File locks in one paragraph each

Linux has three file lock APIs. All three are *advisory*: they only stop other processes that also
ask for the lock.

* **BSD locks**, [`flock(2)`](https://man7.org/linux/man-pages/man2/flock.2.html). Whole file,
  shared or exclusive. The lock belongs to the *open file description*: it is inherited over
  `fork()` and `dup()`, and it goes away when the last descriptor for that open is closed.
* **POSIX record locks**,
  [`fcntl(2)`](https://man7.org/linux/man-pages/man2/fcntl_locking.2.html) with `F_SETLK`,
  `F_SETLKW`, `F_GETLK` (and `lockf(3)` on top). Byte ranges. The lock belongs to the *process*,
  which has a famous consequence: closing **any** descriptor of the file drops every lock the
  process holds on it.
* **Open file description (OFD) locks**, `fcntl()` with `F_OFD_SETLK`. Byte ranges like POSIX
  locks, owned by the open file description like `flock`. Linux 3.15 and later.

The kernel's side of it is in
[Documentation/filesystems/locks.rst](https://docs.kernel.org/filesystems/locks.html).

### On a local filesystem

[`lock_basics.py`](lock_basics.py) takes each kind of lock, asks a second process to take it too,
and shows the lock in `/proc/locks`:

```
$ ./lock_basics.py /tmp
flock  held by pid 1526981; second process: blocked: Resource temporarily unavailable
       /proc/locks: 2: FLOCK  ADVISORY  WRITE 1526981 00:41:19165 0 EOF
posix  held by pid 1526981; second process: blocked: Resource temporarily unavailable
       /proc/locks: 37: POSIX  ADVISORY  WRITE 1526981 00:41:19165 0 EOF
ofd    held by pid 1526981; second process: blocked: Resource temporarily unavailable
       /proc/locks: 10: OFDLCK ADVISORY  WRITE -1 00:41:19165 0 EOF
posix  after closing another fd of the file; second process: acquired
flock  after closing another fd of the file; second process: blocked: Resource temporarily unavailable
```

The last two lines are the POSIX trap: the process opened and closed the same file once more, and
its lock was gone.

The thing to notice is where the locks live. `/proc/locks` is a table in *this* kernel. On a
local filesystem that is the whole story, because every process that can open the file runs on
this kernel. A network filesystem has to do more: its client must forward each lock to something
all clients share.

## What broke

FUSE lets the server choose. During the `FUSE_INIT` handshake the server may set
`FUSE_POSIX_LOCKS` and `FUSE_FLOCK_LOCKS`; the kernel then sends it `FUSE_GETLK`, `FUSE_SETLK` and
`FUSE_SETLKW` requests. If the server does not set them, the kernel treats the mount like a local
filesystem and keeps the locks in its own table.

`virtiofsd` did not set them. Its handlers for the three lock requests were stubs marked `TODO`,
and the [issue asking for them](https://gitlab.com/virtio-fs/virtiofsd/-/issues/53) has been open
for years. So in the picture above, every lock stopped in the guest kernel. The CephFS client on
the host, which knows perfectly well how to take a lock at the MDS, was never asked.

Within one VM everything worked, and fast: a lock+unlock took about a microsecond. Across VMs the
locks did not exist.

[`locktest.sh`](locktest.sh) shows it. A process on node A takes an exclusive lock and holds it; a
second process tries on node A (the control) and a third on node B:

```
$ ./locktest.sh node-1 node-2 20
fcntl node-1->node-2: cross-node violations 20/20, same-node control failures 0, post-release failures 0
fcntl node-2->node-1: cross-node violations 20/20, same-node control failures 0, post-release failures 0
flock node-1->node-2: cross-node violations 20/20, same-node control failures 0, post-release failures 0
flock node-2->node-1: cross-node violations 20/20, same-node control failures 0, post-release failures 0
```

80 out of 80. And [`counter.py`](counter.py), the classic lost-update test (lock, read a number,
write it back plus one, unlock), run as 4 processes on each of 2 nodes with 50 increments each,
ended somewhere around 270 instead of 400.

The real-world version: `huggingface_hub` up to 1.17 guards every file in its cache with a lock
file. Eight processes on two nodes downloading the same repository into one shared cache each
believed they owned the file they were writing. In our test 6 of the 8 processes failed and 4
files were left corrupt.

## The fix: take the guest's locks on the host

The patch makes `virtiofsd` advertise both lock flags and implement the three requests by taking
the lock on the host, on the file in the CephFS mount. The host's CephFS client sends it to the
MDS, and the MDS arbitrates it against every other client, which is every other hypervisor.

The mapping needs some care, because `virtiofsd` is a single host process standing in for many
guest processes:

* **`flock`** is the easy one. The guest's open file already has a host file descriptor in
  `virtiofsd`, and a BSD lock belongs to the open file. `flock()` on that descriptor has the
  right lifetime for free.
* **POSIX locks** cannot be taken as POSIX locks on the host: they belong to a process, and all
  guests' processes would collapse into the one `virtiofsd` process and never conflict with each
  other. FUSE sends a *lock owner* with each request, identifying the guest process. For each
  (inode, lock owner) pair `virtiofsd` opens a separate host file descriptor and takes **OFD
  locks** on it. When the guest process closes any descriptor of the file, the kernel sends
  `FUSE_FLUSH` with the same owner, `virtiofsd` closes that descriptor, and the locks drop:
  exactly the POSIX close semantics, including the trap.
* That extra descriptor is opened with the access mode of the guest's file. Opening it
  read-write for a file the guest has open read-only would make the MDS revoke every other
  client's permission to cache the file.

### Blocking locks, and why they time out

`F_SETLKW` and `flock()` without `LOCK_NB` wait. Two properties of virtio-fs make waiting awkward:

1. virtio-fs has no way to interrupt a request. FUSE over `/dev/fuse` sends `FUSE_INTERRUPT`
   when a signal arrives; virtio-fs does not. A guest process waiting for a lock cannot be
   killed, not even with `SIGKILL`, until `virtiofsd` replies.
2. A request that blocks in the host kernel occupies one thread of `virtiofsd`'s pool for as long
   as it waits. Enough waiters would stop all I/O for the VM.

So the patch never blocks in the host kernel. A blocking request retries a non-blocking lock with
backoff from 1 ms to 200 ms. At most `--max-lock-waiters` requests (half the thread pool by
default) may wait at once, and a wait gives up after `--lock-wait-timeout` seconds, 60 by default.
In both cases the caller gets `ENOLCK`.

Sixty seconds was chosen with Slurm in mind. A cancelled job that sits in an unkillable lock wait
keeps its node in `COMPLETING`, and after `UnkillableStepTimeout` Slurm drains the node. Sixty
seconds stays below that and below the guest kernel's 120-second hung-task warning. We tested it:
job A held a lock on one node, job B waited for it on another, and we cancelled B. B stayed in
`COMPLETING` until the timeout fired, then went away; the node stayed idle and nothing was logged
in the guest.

### What it does not do

* No deadlock detection across hosts; `EDEADLK` is never returned for a cross-node cycle.
* `F_GETLK` reports pid 0 for the holder of a conflicting lock. The holder may be on another
  machine, where the pid would mean nothing.
* A waiter learns about a release at its next retry, up to 200 ms late, and waiters are not
  queued fairly.
* Locks held by a VM do not survive a live migration.

## What it costs

We ran the same `virtiofsd` build on the same two hypervisors and VMs (2 × B300 nodes, Ubuntu
26.04 guests, virtiofsd 1.14.0) and changed only the lock flags.

| | locks in the guest (before) | locks on the host (after) |
| -- | -- | -- |
| cross-node lock tests | 0 / 25 pass | 25 / 25 pass |
| concurrent Hugging Face download, `huggingface_hub` 1.17 | 6 of 8 processes failed, 4 corrupt files | 0 failed, 0 corrupt |
| uncontended lock+unlock | ~1 µs | ~250 µs |
| waking a blocked waiter | immediate | next retry, 1–200 ms |
| killing a process that waits for a lock | immediate | after at most 60 s |
| `git clone` of pytorch onto the filesystem | 95 s | 90 s |
| creating 200,000 files | 51 s | 50 s |
| SQLite commits/s, default settings | 79–241 | 104–166 |

The cost is per lock call, not per I/O. Everything that does not lock is unchanged, and most
things that do lock do not notice either: a default SQLite commit waits about 9 ms for `fsync`,
which hides a quarter of a millisecond of locking.

Two cases do notice:

* **Lock-heavy code that used to work by luck is now correct and slower.** `huggingface_hub` 1.17
  takes a lock per file, so its processes now really wait for each other: two rounds of our
  download test took 66 minutes instead of an 18-minute run that corrupted files.
  `huggingface_hub` 1.18.0 and later write each file to a temporary name and rename it, need no
  cross-node lock, and finished in 25 minutes.
* **SQLite in WAL mode.** WAL takes several locks per transaction, and they were nearly free
  before. Commits with `synchronous=NORMAL` were about 11× slower, and `diskcache`, which uses
  WAL, went from about 3 µs to 250 µs per `get`. With `PRAGMA locking_mode=EXCLUSIVE` almost all
  of it comes back. But WAL across machines was never safe in the first place: SQLite's WAL index
  is shared memory, and [the SQLite documentation](https://www.sqlite.org/wal.html) says all
  processes must be on the same host. Put SQLite databases on node-local disk; that is 6–200×
  faster than any shared filesystem, with or without locks.

## How other shared filesystems answer the same question

"Does a lock on node A exclude node B?" is rarely a property of the filesystem alone. It is
usually a default, a mount option or a config key, and the failure mode is always the one we had:
the call succeeds on both nodes.

| Filesystem | Cross-node `flock` / `fcntl` | The fine print |
| -- | -- | -- |
| CephFS, kernel client or ceph-fuse | Yes | Both kinds go to the MDS, which keeps them as separate state, so they do not conflict with each other (as on a local filesystem). A client that is evicted loses its locks and gets `EIO` on lock calls. |
| NFSv3 | Yes, by a side protocol | NLM and NSM (`lockd`, `statd`) next to NFS itself. Mounting with `nolock` or `local_lock=` makes locks client-local. |
| NFSv4 | Yes, in the protocol | Lock state is tied to a client lease; after a server restart there is a grace period for reclaiming. The Linux client implements `flock` as a byte-range lock on the whole file, so over NFS `flock` and `fcntl` locks **do** conflict, and an exclusive `flock` needs the file open for writing. |
| NFS-Ganesha exporting CephFS | Yes | The Ceph backend passes locks to libcephfs and so to the MDS. Several active servers must coordinate their grace periods (the `rados_cluster` recovery backend). |
| Lustre | Depends on the mount option | `flock`: coherent across the cluster, the default since 2.12.3. `localflock`: per client only. `noflock`: lock calls fail with `ENOSYS`. Older documentation still names `noflock` as the default. |
| BeeGFS | Off by default | `tuneUseGlobalFileLocks = false` in the client config: locks are checked on the local machine only. |
| JuiceFS | Yes | `flock` and POSIX locks; OFD locks are not supported. |
| Amazon EFS | Yes (it is NFSv4) | At most 512 locks per file across all clients. |
| virtio-fs with upstream `virtiofsd` | No | Locks stay in each guest. The old C `virtiofsd` in QEMU could pass them through with `-o posix_lock,flock`, off by default and non-blocking only. |
| virtio-fs with our `virtiofsd` | Yes | This post. |

We did not find a statement about lock semantics in WEKA's public documentation, so it is not in
the table; if you depend on it, test it.

Application authors have noticed. The `huggingface_hub` 1.18.0
[release notes](https://github.com/huggingface/huggingface_hub/releases/tag/v1.18.0) say that on
some Lustre, GPFS and NFS mounts "`flock(2)` silently succeeds for every caller", and the library
stopped depending on it: every download now goes to a fresh temporary file. SQLite's
[advice](https://www.sqlite.org/lockingv3.html) is blunter: "Your best defense is to not use
SQLite for files on a network filesystem."

## Testing and rollout

Each bug we have hit on a shared filesystem is now a test in a small battery that runs from a
login node against two or more nodes. The lock group has 25 tests over the three APIs: exclusive
and shared locks, release on unlock, on close, on close of another descriptor, on `SIGKILL` of the
holder, blocking hand-off, `F_GETLK`, byte ranges, and the shared counter. Around them run
`fallocate`, first reads of files written on another node, git, `mmap`, ACLs, xattrs, concurrent
Hugging Face downloads, Elbencho and [pjdfstest](https://github.com/pjd/pjdfstest).

Two harnesses run it:

* **Two local VMs.** A script boots two cloud images, each on its own instance of a `virtiofsd`
  binary, both sharing one host directory. That is two VMs on two "hypervisors" without a storage
  cluster, good for testing `virtiofsd` on a laptop. Unpatched: all 25 lock tests fail. Patched:
  all pass.
* **A real cluster.** The same battery on two GPU nodes over CephFS, which is what tests the
  MDS path and gives the numbers above.

The fix ships as an ordinary package: we build `virtiofsd` 1.14.0 with our patches in CI and
install it on the hypervisors. `virtiofsd` is started with the VM, so a running VM keeps the
process it has. New clusters have cross-node locks from the start; an existing node gets them the
next time it is shut down and started again (a reboot from inside the node does not restart
`virtiofsd`).

## Upstream

We would rather not carry this patch forever. It is submitted to the virtiofsd project as draft
[merge request !351](https://gitlab.com/virtio-fs/virtiofsd/-/merge_requests/351), in three
commits: pass the lock parameters from the FUSE server to the filesystem (building on Mike
Sample's draft [!310](https://gitlab.com/virtio-fs/virtiofsd/-/merge_requests/310)), take the
locks on the host in the passthrough filesystem, and add the command line options.

One difference from what we run: upstream, locks are off unless `--posix-lock` and `--flock` are
given. That is the right default for a general-purpose daemon, where most shared directories are
used by one VM and guest-local locks are both correct and a thousand times faster. For a
filesystem whose whole point is to be shared between nodes, we turn them on.

## If you use locks on a shared filesystem

* On Verda cluster shared filesystems, `flock()` and `fcntl()` locks now hold across all nodes.
* Prefer non-blocking locks with your own retry loop. A blocking lock that waits longer than 60
  seconds fails with `ENOLCK`, and the process cannot be killed while it waits.
* A lock is a network round trip. Take one per unit of work, not one per record.
* Often you do not need a lock at all: write to a temporary file and `rename()` it into place.
  Rename is atomic on CephFS, as on any POSIX filesystem.
* Keep SQLite, and caches built on it, on local disk.
* Test your filesystem instead of trusting it. [`locktest.sh`](locktest.sh) takes a minute.

## References

* [`flock(2)`](https://man7.org/linux/man-pages/man2/flock.2.html),
  [`fcntl_locking(2)`](https://man7.org/linux/man-pages/man2/fcntl_locking.2.html),
  [`lockf(3)`](https://man7.org/linux/man-pages/man3/lockf.3.html)
* [File locking in the Linux kernel](https://docs.kernel.org/filesystems/locks.html)
* [virtio-fs](https://virtio-fs.gitlab.io/), and the kernel's
  [virtiofs documentation](https://docs.kernel.org/filesystems/virtiofs.html)
* [virtiofsd](https://gitlab.com/virtio-fs/virtiofsd): issue
  [#53](https://gitlab.com/virtio-fs/virtiofsd/-/issues/53), merge requests
  [!310](https://gitlab.com/virtio-fs/virtiofsd/-/merge_requests/310) and
  [!351](https://gitlab.com/virtio-fs/virtiofsd/-/merge_requests/351)
* [CephFS](https://docs.ceph.com/en/latest/cephfs/),
  [`mount.ceph(8)`](https://docs.ceph.com/en/latest/man/8/mount.ceph/)
* [`nfs(5)`](https://man7.org/linux/man-pages/man5/nfs.5.html),
  [`mount.lustre(8)`](https://github.com/lustre/lustre-release/blob/master/Documentation/man8/mount.lustre.8),
  [NFS-Ganesha rados_cluster design](https://github.com/nfs-ganesha/nfs-ganesha/blob/next/src/doc/man/ganesha-rados-cluster-design.rst),
  [BeeGFS client config](https://github.com/ThinkParQ/beegfs/blob/master/client_module/build/dist/etc/beegfs-client.conf),
  [JuiceFS POSIX compatibility](https://juicefs.com/docs/community/posix_compatibility/),
  [Amazon EFS quotas](https://docs.aws.amazon.com/efs/latest/ug/limits.html)
* [SQLite: Write-Ahead Logging](https://www.sqlite.org/wal.html),
  [How To Corrupt An SQLite Database File](https://www.sqlite.org/howtocorrupt.html)
