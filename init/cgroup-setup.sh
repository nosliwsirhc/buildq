#!/bin/sh
# SPDX-License-Identifier: MIT
# Delegate a cgroup v2 subtree for buildq's `cgroup` runner on non-systemd Linux.
#
# Run as root once (or at boot from OpenRC/runit/s6/rc.local):
#   sudo ./init/cgroup-setup.sh <username> [memory_max_pct]
#
# Creates /sys/fs/cgroup/buildq-<user> with cpu, memory (and io, pids when available) delegated
# to <user>, plus a `builds` child capped at 75%/85% of RAM by default. buildqd picks up
# /sys/fs/cgroup/buildq-<user> automatically under `runner = "auto"`.
set -eu

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  printf 'usage: %s <username> [memory_max_percent]\n' "$0" >&2
  exit 2
fi

user=$1
max_pct=${2:-85}
high_pct=$((max_pct > 10 ? max_pct - 10 : max_pct))
cg_base=/sys/fs/cgroup

if [ ! -f "$cg_base/cgroup.controllers" ]; then
  printf 'cgroup-setup.sh: %s is not a cgroup v2 unified hierarchy\n' "$cg_base" >&2
  exit 1
fi

if ! id "$user" >/dev/null 2>&1; then
  printf 'cgroup-setup.sh: unknown user %s\n' "$user" >&2
  exit 1
fi
group=$(id -gn "$user")

# Enable controllers at the root so child subtrees receive them.
avail=$(cat "$cg_base/cgroup.controllers" 2>/dev/null || true)
for ctrl in cpu memory io pids; do
  case " $avail " in
    *" $ctrl "*)
      printf '+%s\n' "$ctrl" >"$cg_base/cgroup.subtree_control" 2>/dev/null || true
      ;;
  esac
done

root="$cg_base/buildq-$user"
daemon_cg="$root/daemon"
builds="$root/builds"
mkdir -p "$root"

root_avail=$(cat "$root/cgroup.controllers" 2>/dev/null || true)
for ctrl in cpu memory io pids; do
  case " $root_avail " in
    *" $ctrl "*)
      printf '+%s\n' "$ctrl" >"$root/cgroup.subtree_control" 2>/dev/null || true
      ;;
  esac
done

mkdir -p "$daemon_cg" "$builds"
builds_avail=$(cat "$builds/cgroup.controllers" 2>/dev/null || true)
for ctrl in cpu memory io pids; do
  case " $builds_avail " in
    *" $ctrl "*)
      printf '+%s\n' "$ctrl" >"$builds/cgroup.subtree_control" 2>/dev/null || true
      ;;
  esac
done

# Optional aggregate memory backstop (matching systemd's 50-buildq-memory.conf 75%/85% drop-in).
mem_kb=$(awk '/^MemTotal:/ {print $2; exit}' /proc/meminfo 2>/dev/null || echo 0)
if [ "${mem_kb:-0}" -gt 0 ] && [ "$max_pct" -gt 0 ] && [ "$max_pct" -lt 100 ]; then
  high_bytes=$((mem_kb * 1024 * high_pct / 100))
  max_bytes=$((mem_kb * 1024 * max_pct / 100))
  [ -w "$builds/memory.high" ] && printf '%s\n' "$high_bytes" >"$builds/memory.high" 2>/dev/null || true
  [ -w "$builds/memory.max" ] && printf '%s\n' "$max_bytes" >"$builds/memory.max" 2>/dev/null || true
fi

chown -R "$user:$group" "$root"
printf 'cgroup-setup.sh: delegated %s to %s:%s (daemon in %s, controllers: %s)\n' \
  "$root" "$user" "$group" "$daemon_cg" "$(cat "$builds/cgroup.subtree_control" 2>/dev/null || echo none)"
