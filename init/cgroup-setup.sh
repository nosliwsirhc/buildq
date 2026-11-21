#!/bin/sh
# SPDX-License-Identifier: MIT
# Delegate a cgroup v2 subtree for buildq's `cgroup` runner on non-systemd Linux.
#
# Run as root once (or at boot from OpenRC/runit/s6/rc.local):
#   sudo ./init/cgroup-setup.sh <username> [memory_max_percent]
#
# Creates a two-level hierarchy following Linux's cgroup v2 delegation model (man 7 cgroups):
#   /sys/fs/cgroup/buildq-<user>/           (root-owned; holds unbypassable memory.high/memory.max)
#     └── user/                             (delegated to <user>; common ancestor for migration)
#           ├── daemon/                     (<user>-owned; buildqd runs here)
#           └── builds/                     (<user>-owned; slice_guard tightens memory.max here;
#                                            each build runs in builds/buildq-<id>)
set -eu

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  printf 'usage: %s <username> [memory_max_percent]\n' "$0" >&2
  exit 2
fi

user=$1
case "$user" in
  ''|*[!A-Za-z0-9_.-]*|-*)
    printf 'cgroup-setup.sh: invalid username %s\n' "$user" >&2
    exit 2
    ;;
esac

raw_pct=${2:-85}
case "$raw_pct" in
  ''|*[!0-9]*)
    printf 'cgroup-setup.sh: memory_max_percent must be an integer 0..99, got %s\n' "$raw_pct" >&2
    exit 2
    ;;
esac
# Strip leading zeros in POSIX sh so values like "08" don't fail octal arithmetic.
while [ "${#raw_pct}" -gt 1 ]; do
  case "$raw_pct" in
    0*) raw_pct=${raw_pct#0} ;;
    *) break ;;
  esac
done
max_pct=$((raw_pct))
if [ "$max_pct" -lt 0 ] || [ "$max_pct" -ge 100 ]; then
  printf 'cgroup-setup.sh: memory_max_percent must be between 0 and 99, got %s\n' "$max_pct" >&2
  exit 2
fi
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

enable_ctrls() {
  dir=$1
  avail=$(cat "$dir/cgroup.controllers" 2>/dev/null || true)
  for ctrl in cpu memory io pids; do
    case " $avail " in
      *" $ctrl "*)
        printf '+%s\n' "$ctrl" >"$dir/cgroup.subtree_control" 2>/dev/null || true
        ;;
    esac
  done
}

delegate_dir() {
  dir=$1
  chown "$user:$group" "$dir"
  for f in cgroup.procs cgroup.threads cgroup.subtree_control; do
    [ -e "$dir/$f" ] && chown "$user:$group" "$dir/$f" 2>/dev/null || true
  done
}

# Enable controllers down the hierarchy: /sys/fs/cgroup -> buildq-<user> -> user -> builds.
enable_ctrls "$cg_base"

outer="$cg_base/buildq-$user"
root="$outer/user"
daemon_cg="$root/daemon"
builds="$root/builds"

mkdir -p "$outer"
chown root:root "$outer"
chmod 0755 "$outer"
enable_ctrls "$outer"

# Host-wide aggregate memory ceiling stays root-owned on `buildq-<user>` so workloads under
# `$user` cannot raise their own ceiling above `max_pct`% of RAM.
mem_kb=$(awk '/^MemTotal:/ {print $2; exit}' /proc/meminfo 2>/dev/null || echo 0)
if [ "${mem_kb:-0}" -gt 0 ] && [ "$max_pct" -gt 0 ]; then
  high_bytes=$((mem_kb * 1024 * high_pct / 100))
  max_bytes=$((mem_kb * 1024 * max_pct / 100))
  [ -w "$outer/memory.high" ] && printf '%s\n' "$high_bytes" >"$outer/memory.high" 2>/dev/null || true
  [ -w "$outer/memory.max" ] && printf '%s\n' "$max_bytes" >"$outer/memory.max" 2>/dev/null || true
fi

mkdir -p "$root"
enable_ctrls "$root"
mkdir -p "$daemon_cg" "$builds"
enable_ctrls "$builds"

# Initialize builds/memory.{high,max} to the same ceiling so slice_guard knows its configured cap
# and can dynamically tighten/restore builds/memory.max under load (never exceeding outer's cap).
if [ "${mem_kb:-0}" -gt 0 ] && [ "$max_pct" -gt 0 ]; then
  [ -w "$builds/memory.high" ] && printf '%s\n' "$high_bytes" >"$builds/memory.high" 2>/dev/null || true
  [ -w "$builds/memory.max" ] && printf '%s\n' "$max_bytes" >"$builds/memory.max" 2>/dev/null || true
fi

# Delegate only the required cgroup v2 files per man 7 cgroups (never chown outer's limits).
delegate_dir "$root"
delegate_dir "$daemon_cg"
delegate_dir "$builds"
for f in memory.high memory.max; do
  [ -e "$builds/$f" ] && chown "$user:$group" "$builds/$f" 2>/dev/null || true
done

printf 'cgroup-setup.sh: delegated %s to %s:%s (ceiling on %s, daemon in %s, controllers: %s)\n' \
  "$root" "$user" "$group" "$outer" "$daemon_cg" "$(cat "$builds/cgroup.subtree_control" 2>/dev/null || echo none)"
