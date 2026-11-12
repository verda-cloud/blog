#!/bin/bash
# Cross-node flock/fcntl exclusion on a shared filesystem.
# Each attempt: a holder takes the lock on node A; while it holds, a second
# process tries on node A (same-node control) and on node B (cross-node);
# then the holder releases and node B must be able to take the lock.
# Run from a directory on the shared filesystem, on a machine that can ssh
# to both nodes. Needs locktest.py next to this script.
# Usage: ./locktest.sh <node-a> <node-b> [attempts per kind and direction]
# Working locks give 0 cross-node violations.
a=${1:?node a}; b=${2:?node b}; n=${3:-20}
here=$(cd "$(dirname "$0")" && pwd)
d=$here/locktest.$$; mkdir -p "$d"
agent=$here/locktest.py
declare -A viol ctl_fail post_fail tries
for kind in flock fcntl; do
  for dir in "$a $b" "$b $a"; do
    set -- $dir; h=$1; o=$2
    for i in $(seq 1 "$n"); do
      lf=$d/$kind-$h-$i.lock; sen=$d/$kind-$h-$i.release
      coproc HOLD { ssh -o BatchMode=yes "$h" python3 "$agent" holder $kind "$lf" "$sen"; }
      read -r -t 30 st <&"${HOLD[0]}"
      if [ "$st" != LOCKED ]; then echo "$kind $h #$i: holder said '$st'"; exit 2; fi
      same=$(ssh -o BatchMode=yes "$h" python3 "$agent" attempter $kind "$lf" x)
      cross=$(ssh -o BatchMode=yes "$o" python3 "$agent" attempter $kind "$lf" x)
      touch "$sen"; wait "$HOLD_PID" 2>/dev/null
      post=$(ssh -o BatchMode=yes "$o" python3 "$agent" attempter $kind "$lf" x)
      k="$kind $h->$o"; tries[$k]=$(( ${tries[$k]:-0} + 1 ))
      [ "$same" = BLOCKED ] || ctl_fail[$k]=$(( ${ctl_fail[$k]:-0} + 1 ))
      [ "$cross" = ACQUIRED ] && viol[$k]=$(( ${viol[$k]:-0} + 1 ))
      [ "$post" = ACQUIRED ] || post_fail[$k]=$(( ${post_fail[$k]:-0} + 1 ))
    done
  done
done
for k in "${!tries[@]}"; do
  echo "$k: cross-node violations ${viol[$k]:-0}/${tries[$k]}, same-node control failures ${ctl_fail[$k]:-0}, post-release failures ${post_fail[$k]:-0}"
done | sort
rm -rf "$d"
