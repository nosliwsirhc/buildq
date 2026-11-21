# SPDX-License-Identifier: MIT
"""Linux /proc, PSI, cgroup v2, disk, flock, and agent-process inspection."""

from __future__ import annotations

import collections
import ctypes
import errno
import math
import os
import re
import shutil
import signal
import subprocess
import time

from .common import DEVNULL, KIB, MIB, UID

CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
PR_SET_CHILD_SUBREAPER = 36


def set_child_subreaper() -> bool:
    """Mark this process as a subreaper (Linux 3.4+) so orphaned grandchildren are reparented here
    instead of PID 1, preserving process-tree membership and cumulative CPU accounting."""
    try:
        libc = ctypes.CDLL(None)
        return libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0
    except (OSError, AttributeError):
        return False

def cpu_times() -> tuple[int, int]:
    """(all jiffies, idle + iowait jiffies) summed over every CPU, from /proc/stat."""
    with open("/proc/stat") as f:
        fields = [int(x) for x in f.readline().split()[1:9]]
    return sum(fields), fields[3] + fields[4]


def meminfo() -> dict[str, int]:
    out = {}
    with open("/proc/meminfo") as f:
        for line in f:
            name, _, rest = line.partition(":")
            parts = rest.split()
            if parts:
                out[name] = int(parts[0]) * (KIB if len(parts) > 1 else 1)
    return out


def pressure(resource: str, kind: str) -> float:
    """avg10 of /proc/pressure/<resource> for "some" or "full"; 0 where PSI is unavailable."""
    try:
        with open(f"/proc/pressure/{resource}") as f:
            for line in f:
                parts = line.split()
                if parts and parts[0] == kind:
                    for field in parts[1:]:
                        name, _, value = field.partition("=")
                        if name == "avg10":
                            return float(value)
    except (OSError, ValueError):
        pass
    return 0.0


def disk_space(path: str) -> tuple[int, int] | None:
    """(bytes free to an unprivileged user, the filesystem's size) where a path lives, or None."""
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize
    except OSError:
        return None


class Machine:
    """Machine-wide CPU and memory, sampled once a second by the daemon."""

    TAU = 10.0  # seconds; time constant of the busy-cores average

    def __init__(self):
        self.ncpu = os.cpu_count() or 1
        self.busy = 0.0
        self.busy_now = 0.0
        self.mem_total = self.mem_available = self.swap_used = 0
        self.psi_cpu = self.psi_mem = self.psi_mem_some = 0.0
        self._prev = None
        self._primed = False
        self.sample(time.monotonic())

    def sample(self, now: float) -> None:
        total, idle = cpu_times()
        if self._prev is not None:
            t0, total0, idle0 = self._prev
            if total > total0:
                self.busy_now = max(0.0, 1 - (idle - idle0) / (total - total0)) * self.ncpu
                if self._primed:
                    self.busy += (1 - math.exp(-max(now - t0, 0.0) / self.TAU)) * (self.busy_now - self.busy)
                else:
                    self.busy = self.busy_now
                    self._primed = True
        self._prev = (now, total, idle)
        info = meminfo()
        self.mem_total = info.get("MemTotal", 0)
        self.mem_available = info.get("MemAvailable", info.get("MemFree", 0))
        self.swap_used = info.get("SwapTotal", 0) - info.get("SwapFree", 0)
        self.psi_cpu = pressure("cpu", "some")
        self.psi_mem = pressure("memory", "full")
        self.psi_mem_some = pressure("memory", "some")

    def read_available(self) -> int:
        """MemAvailable now, read again: a build that ended since this tick's sample has given its
        memory back by now (see make_room_for_ci)."""
        info = meminfo()
        self.mem_available = info.get("MemAvailable", info.get("MemFree", self.mem_available))
        return self.mem_available

    def summary(self) -> dict:
        return {
            "cores": self.ncpu, "busy": round(self.busy, 2), "busy_now": round(self.busy_now, 2),
            "mem_total": self.mem_total, "mem_available": self.mem_available, "swap_used": self.swap_used,
            "psi_cpu_some_avg10": self.psi_cpu, "psi_mem_full_avg10": self.psi_mem,
            "psi_mem_some_avg10": self.psi_mem_some,
        }


def user_manager_cgroup() -> str:
    """The cgroup directory of this user's systemd manager (user@UID.service)."""
    try:
        with open("/proc/self/cgroup") as f:
            for line in f:
                if line.startswith("0::"):
                    match = re.match(r"(.*/user@\d+\.service)(?:/|$)", line[3:].strip())
                    if match:
                        return "/sys/fs/cgroup" + match.group(1)
    except OSError:
        pass
    return f"/sys/fs/cgroup/user.slice/user-{UID}.slice/user@{UID}.service"


def slice_path(name: str) -> str:
    """systemd nests a-b.slice inside a.slice: 'a-b.slice' -> 'a.slice/a-b.slice'."""
    parts = name[: -len(".slice")].split("-")
    return "/".join("-".join(parts[: i + 1]) + ".slice" for i in range(len(parts)))



def unreclaimable(stat: dict[str, int]) -> int:
    return stat.get("anon", 0) + stat.get("shmem", 0) + max(0, stat.get("kernel", 0) - stat.get("slab_reclaimable", 0))


def cgroup_stats(path: str):
    """(cpu usage in usec, unreclaimable memory in bytes, oom kills) of a cgroup, or None if gone.

    The memory is anon + shmem + kernel - slab_reclaimable from memory.stat. memory.current and
    memory.peak also count page cache, which a build fills with its own output and the kernel takes
    back when it needs to, so they say little about what a build needs.
    """
    try:
        usage = 0
        with open(os.path.join(path, "cpu.stat")) as f:
            for line in f:
                name, _, value = line.partition(" ")
                if name == "usage_usec":
                    usage = int(value)
                    break
        stat = {}
        with open(os.path.join(path, "memory.stat")) as f:
            for line in f:
                name, _, value = line.partition(" ")
                if name in ("anon", "kernel", "shmem", "slab_reclaimable"):
                    stat[name] = int(value)
        return usage, unreclaimable(stat), cgroup_oom_kills(path)
    except (OSError, ValueError):
        return None


def cgroup_events(path: str) -> dict[str, int]:
    out = {}
    try:
        with open(os.path.join(path, "memory.events.local")) as f:
            for line in f:
                name, _, value = line.partition(" ")
                out[name] = int(value)
    except (OSError, ValueError):
        pass
    return out


def cgroup_memory_waits(path: str) -> tuple[float, float, int] | None:
    """(seconds in which some of the cgroup's tasks waited on memory, seconds in which all of them
    did, times it went over its own MemoryHigh), or None if its memory.pressure can't be read.

    The waits are reclaim and swap-in, whatever caused them: the cgroup's own MemoryHigh, or a
    machine short of memory. Going over MemoryHigh says its own limit was one of the causes."""
    some = full = 0.0
    try:
        with open(os.path.join(path, "memory.pressure")) as f:
            for line in f:
                parts = line.split()
                total = next((int(p[6:]) for p in parts[1:] if p.startswith("total=")), 0) / 1e6
                if parts and parts[0] == "some":
                    some = total
                elif parts and parts[0] == "full":
                    full = total
    except (OSError, ValueError):
        return None
    return some, full, cgroup_events(path).get("high", 0)


def cgroup_held(path: str) -> tuple[int, int] | None:
    """(unreclaimable memory, swap) a cgroup and its descendants hold, in bytes, or None if its
    memory.stat can't be read (it has gone, say). Swap counts because with zram it is still RAM."""
    stat = {}
    try:
        with open(os.path.join(path, "memory.stat")) as f:
            for line in f:
                name, _, value = line.partition(" ")
                if name in ("anon", "kernel", "shmem", "slab_reclaimable"):
                    stat[name] = int(value)
    except (OSError, ValueError):
        return None
    swap = 0
    try:
        with open(os.path.join(path, "memory.swap.current")) as f:
            swap = int(f.read())
    except (OSError, ValueError):
        pass
    return unreclaimable(stat), swap


def cgroup_swap(path: str) -> int:
    """Bytes of a cgroup's memory in swap now (0 if unknown)."""
    try:
        with open(os.path.join(path, "memory.swap.current")) as f:
            return int(f.read())
    except (OSError, ValueError):
        return 0


class WalkBudget:
    """How many more cgroup directories a walk may read, and until when."""

    def __init__(self, dirs: int, deadline: float | None):
        self.dirs, self.deadline, self.complete = dirs, deadline, True

    def out(self) -> bool:
        if self.dirs <= 0 or (self.deadline is not None and time.monotonic() > self.deadline):
            self.complete = False
            return True
        return False


def biggest_process(path: str, skip=(), budget: WalkBudget | None = None, limit: int = 500) -> str:
    """The name of the largest process (by resident memory) in a cgroup or below it, leaving out the
    subtrees in `skip`: best effort, looking at no more than `limit` processes and within `budget`."""
    best, name, seen = -1, "", 0
    for directory, children, _ in os.walk(path):
        if budget is not None:
            if budget.out():
                return name
            budget.dirs -= 1
        children[:] = [c for c in children if os.path.join(directory, c) not in skip]
        try:
            with open(os.path.join(directory, "cgroup.procs")) as f:
                pids = f.read().split()
        except OSError:
            continue
        for pid in pids:
            if seen >= limit:
                if budget is not None:
                    budget.complete = False  # the largest may be among those not looked at
                return name
            if budget is not None and budget.out():
                return name
            seen += 1
            try:
                with open(f"/proc/{pid}/statm") as f:
                    rss = int(f.read().split()[1])
                if rss > best:
                    with open(f"/proc/{pid}/comm", errors="replace") as f:
                        best, name = rss, f.read().strip()
            except (OSError, ValueError, IndexError):
                continue
    return name


def biggest_cgroups(root: str = "/sys/fs/cgroup", count: int = 6, at_least: int = 256 * MIB,
                    max_dirs: int = 5000, deadline: float | None = None):
    """Where the machine's memory is: the cgroups holding the most that the kernel can't simply drop
    (as cgroup_stats counts it, plus swap), largest first, as a list of (memory, swap, path below
    root, its largest process), and whether the walk finished.

    It looks into a cgroup only if its memory.current plus its swap is at least `at_least`. A cgroup
    none of whose children is that big is listed as a whole; one with big children is listed by
    those, plus "(the rest)" for what its small children hold together, when that is big enough, so
    many small services don't vanish. It reads at most `max_dirs` directories while walking, and
    as many again looking for process names, and stops at `deadline`, listing what it has not looked
    into yet as a whole."""
    found = []
    budget = WalkBudget(max_dirs, deadline)

    def big_children(path: str) -> tuple[list[tuple[str, tuple[int, int]]], bool]:
        """Its children big enough to look into, and whether the listing was cut off."""
        big = []
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    if budget.out():
                        return big, True
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    budget.dirs -= 1
                    try:
                        with open(os.path.join(entry.path, "memory.current")) as f:
                            size = int(f.read())
                    except (OSError, ValueError):
                        continue
                    if size < at_least:
                        try:  # swapped out, it holds little RAM but may hold a lot of swap
                            with open(os.path.join(entry.path, "memory.swap.current")) as f:
                                size += int(f.read())
                        except (OSError, ValueError):
                            pass
                        if size < at_least:
                            continue
                    held = cgroup_held(entry.path)
                    if held is not None:  # one that has gone counts in its parent's rest
                        big.append((entry.path, held))
        except OSError:
            pass
        return big, False

    def visit(path: str, depth: int, held: tuple[int, int] | None) -> None:
        big, cut = big_children(path) if depth < 8 and not budget.out() else ([], False)
        if cut and held is not None:  # its unread children would pass for "the rest": list it whole
            found.append((held[0], held[1], path, ()))
            return
        if held is not None:  # not the root, which has no memory of its own to list
            if not big:
                found.append((held[0], held[1], path, ()))
                return
            rest = (max(0, held[0] - sum(h[0] for _, h in big)), max(0, held[1] - sum(h[1] for _, h in big)))
            if sum(rest) >= at_least:
                found.append((rest[0], rest[1], path + " (the rest)", tuple(c for c, _ in big)))
        for child, child_held in big:
            if budget.out():
                found.append((child_held[0], child_held[1], child, ()))  # listed whole, not looked into
            else:
                visit(child, depth + 1, child_held)

    visit(root, 0, None)
    found.sort(key=lambda e: e[0] + e[1], reverse=True)
    names = WalkBudget(max_dirs, None if deadline is None else max(deadline, time.monotonic()) + 5)
    top = []
    for memory, swap, path, skip in found[:count]:
        directory = path[: -len(" (the rest)")] if path.endswith(" (the rest)") else path
        top.append((memory, swap, os.path.relpath(path, root), biggest_process(directory, skip, names)))
    return top, budget.complete and names.complete


def short_cgroup(rel: str) -> str:
    """The last part of a cgroup's path, with a container's 64-digit ID cut to Docker's 12."""
    return re.sub(r"^docker-([0-9a-f]{12})[0-9a-f]{52}\.scope", r"docker-\1.scope", rel.rsplit("/", 1)[-1])


def cgroup_oom_kills(path: str) -> int:
    try:
        with open(os.path.join(path, "memory.events")) as f:
            for line in f:
                name, _, value = line.partition(" ")
                if name == "oom_kill":
                    return int(value)
    except (OSError, ValueError):
        pass
    return 0


def cgroup_populated(path: str) -> bool:
    try:
        with open(os.path.join(path, "cgroup.events")) as f:
            return "populated 1" in f.read()
    except FileNotFoundError:
        return False
    except OSError:
        return True  # can't tell (out of descriptors, say): don't call a build gone on that


def proc_stat(pid, strict: bool = False):
    """(start time in clock ticks since boot, state letter, parent pid) of a process, from one read
    of its stat line, or None if it's gone. With `strict`, a read that fails for another reason
    than the process being gone (out of descriptors, say) is raised, not taken for that."""
    try:
        with open(f"/proc/{int(pid)}/stat") as f:
            data = f.read()
        fields = data.rsplit(")", 1)[1].split()
        return int(fields[19]), fields[0], int(fields[1])
    except (FileNotFoundError, ProcessLookupError, ValueError, IndexError, TypeError, OverflowError):
        return None
    except OSError:
        if strict:
            raise
        return None


def proc_stat_full(pid: int):
    """(start_ticks, state, ppid, pgrp, sid, utime, stime, cutime, cstime) from /proc/<pid>/stat."""
    try:
        with open(f"/proc/{int(pid)}/stat") as f:
            data = f.read()
        fields = data.rsplit(")", 1)[1].split()
        return (int(fields[19]), fields[0], int(fields[1]), int(fields[2]), int(fields[3]),
                int(fields[11]), int(fields[12]), int(fields[13]), int(fields[14]))
    except (OSError, ValueError, IndexError, TypeError, OverflowError):
        return None


def proc_memory(pid: int) -> tuple[int, int] | None:
    """(unreclaimable anon+shmem bytes, swap bytes) of one process.

    Reads /proc/<pid>/smaps_rollup (Pss_Anon + Pss_Shmem, SwapPss) so shared CoW pages aren't
    double-counted across forked workers and file-backed page cache is excluded (matching cgroup
    memory.stat). Falls back to /proc/<pid>/status (RssAnon + RssShmem, VmSwap) where smaps_rollup
    isn't available. Returns None if neither file could be read."""
    try:
        with open(f"/proc/{pid}/smaps_rollup") as f:
            vals: dict[str, int] = {}
            for line in f:
                name, _, rest = line.partition(":")
                if name in ("Pss_Anon", "Pss_Shmem", "SwapPss", "Swap", "Anonymous", "Pss"):
                    parts = rest.split()
                    if parts and parts[0].isdigit():
                        vals[name] = int(parts[0]) * KIB
        if "Pss_Anon" in vals or "Pss_Shmem" in vals:
            mem = vals.get("Pss_Anon", 0) + vals.get("Pss_Shmem", 0)
            swap = vals.get("SwapPss", vals.get("Swap", 0))
            return mem, swap
        if "Anonymous" in vals:
            return vals["Anonymous"], vals.get("SwapPss", vals.get("Swap", 0))
    except OSError:
        pass
    try:
        vals = {}
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                name, _, rest = line.partition(":")
                if name in ("RssAnon", "RssShmem", "VmRSS", "VmSwap"):
                    parts = rest.split()
                    if parts and parts[0].isdigit():
                        vals[name] = int(parts[0]) * KIB
        if "RssAnon" in vals or "RssShmem" in vals:
            return vals.get("RssAnon", 0) + vals.get("RssShmem", 0), vals.get("VmSwap", 0)
        if "VmRSS" in vals or "VmSwap" in vals:
            return vals.get("VmRSS", 0), vals.get("VmSwap", 0)
        return 0, 0
    except OSError:
        return None


def proc_io(pid: int) -> tuple[int, int] | None:
    """(read_bytes, write_bytes) from /proc/<pid>/io, or None."""
    try:
        rb = wb = 0
        with open(f"/proc/{pid}/io") as f:
            for line in f:
                name, _, val = line.partition(":")
                if name == "read_bytes":
                    rb = int(val.strip())
                elif name == "write_bytes":
                    wb = int(val.strip())
        return rb, wb
    except (OSError, ValueError):
        return None


def _scan_own_stats():
    """Single-pass scan of this user's /proc/<pid>/stat -> (stats, children_by_ppid, by_sid)."""
    stats: dict[int, tuple] = {}
    children: dict[int, list[int]] = collections.defaultdict(list)
    by_sid: dict[int, list[int]] = collections.defaultdict(list)
    for pid in own_pids():
        s = proc_stat_full(pid)
        if s is None:
            continue
        stats[pid] = s
        children[s[2]].append(pid)
        by_sid[s[4]].append(pid)
    return stats, children, by_sid


def _collect_tree(root_pid: int | None, root_start: int | None, job: str, stats: dict, children: dict,
                  by_sid: dict) -> tuple[list[int], bool]:
    """All PIDs in a build's process tree in strict parent-before-child topological order (rooted
    at supervisor `root_pid`, plus session members, falling back to BUILDQ_JOB-tagged processes if
    the supervisor is gone). Returns `(ordered_pids, sup_alive)`."""
    members: set[int] = set()
    sup_alive = False
    if root_pid and root_pid in stats and stats[root_pid][0] == root_start and stats[root_pid][1] not in ("Z", "X"):
        sup_alive = True
        queue_pids = collections.deque([root_pid])
        members.add(root_pid)
        for spid in by_sid.get(root_pid, ()):
            if spid not in members:
                members.add(spid)
                queue_pids.append(spid)
        while queue_pids:
            cur = queue_pids.popleft()
            for ch in children.get(cur, ()):
                if ch not in members:
                    members.add(ch)
                    queue_pids.append(ch)
    else:
        for pid in tagged_processes(job):
            if pid not in members:
                members.add(pid)
                queue_pids = collections.deque([pid])
                while queue_pids:
                    cur = queue_pids.popleft()
                    for ch in children.get(cur, ()):
                        if ch not in members:
                            members.add(ch)
                            queue_pids.append(ch)
    if not members:
        return [], sup_alive
    # Topologically sort `members` strictly parent-before-child so a parent's stat is always read
    # before its children's (preventing child-exit reap double-counting).
    roots = [p for p in members if (stats[p][2] not in members if p in stats else True)]
    if root_pid in roots:
        roots.remove(root_pid)
        roots.insert(0, root_pid)
    ordered: list[int] = []
    visited: set[int] = set()
    q = collections.deque(roots)
    while q:
        cur = q.popleft()
        if cur in visited:
            continue
        visited.add(cur)
        ordered.append(cur)
        for ch in children.get(cur, ()):
            if ch in members and ch not in visited:
                q.append(ch)
    for leftover in members:
        if leftover not in visited:
            ordered.append(leftover)
    return ordered, sup_alive


def proc_tree_descendants(root_pid: int) -> list[tuple[int, int]]:
    """All live descendant `(pid, start_time)` pairs of `root_pid` (excluding `root_pid`)."""
    stats, children, _ = _scan_own_stats()
    if root_pid not in stats:
        return []
    seen = {root_pid}
    queue_pids = collections.deque([root_pid])
    out: list[tuple[int, int]] = []
    while queue_pids:
        cur = queue_pids.popleft()
        for ch in children.get(cur, ()):
            if ch not in seen:
                seen.add(ch)
                queue_pids.append(ch)
                s = stats.get(ch)
                if s is not None and s[1] not in ("Z", "X"):
                    out.append((ch, s[0]))
    return out


def proc_tree_info(root_pid: int | None, root_start: int | None, job: str) -> dict:
    """Inspect one non-cgroup build's process tree for freezing, stopping, or status checks."""
    stats, children, by_sid = _scan_own_stats()
    tree, sup_alive = _collect_tree(root_pid, root_start, job, stats, children, by_sid)
    live = [p for p in tree if p in stats and stats[p][1] not in ("Z", "X")]
    descendants = [p for p in live if p != root_pid]
    descendant_idents = {p: stats[p][0] for p in descendants}
    pgids = sorted({stats[p][3] for p in descendants if stats[p][3] > 1 and stats[p][3] != root_pid})
    sup_state = stats[root_pid][1] if sup_alive and root_pid in stats else None
    own_stopped = (sup_state in ("T", "t")) if sup_state is not None else None
    any_descendant_stopped = any(stats[p][1] in ("T", "t") for p in descendants)
    all_stopped = (bool(own_stopped) and all(stats[p][1] in ("T", "t") for p in descendants)) if own_stopped is not None else None
    return {
        "sup_alive": sup_alive, "pids": live, "descendants": descendants,
        "descendant_idents": descendant_idents, "pgids": pgids,
        "own_stopped": own_stopped, "all_stopped": all_stopped,
        "any_descendant_stopped": any_descendant_stopped,
    }


def proc_trees_snapshot(jobs: dict[str, tuple[int | None, int | None]]) -> dict[str, dict]:
    """Single-pass /proc CPU, memory, swap, PID-list, and stop-state measurement for all active
    non-cgroup builds at once. Reads stat in top-down parent-before-child BFS order so a child
    reaped mid-scan is never double-counted in both its own utime and its parent's cutime."""
    if not jobs:
        return {}
    stats, children, by_sid = _scan_own_stats()
    clk = max(1, CLK_TCK)
    out: dict[str, dict] = {}
    for job_id, (root_pid, root_start) in jobs.items():
        tree, sup_alive = _collect_tree(root_pid, root_start, job_id, stats, children, by_sid)
        if not tree:
            continue
        ticks = 0
        mem = swap = 0
        mem_failed = 0
        live: list[int] = []
        descendants: list[int] = []
        descendant_idents: dict[int, int] = {}
        # Re-read stat strictly top-down (parent before child) so a concurrent child exit + reap
        # cannot be counted in both the child's utime/stime and the parent's cutime/cstime.
        fresh_states: dict[int, str] = {}
        for pid in tree:
            orig = stats.get(pid)
            if orig is None:
                continue
            s = proc_stat_full(pid)
            if s is None or s[0] != orig[0]:
                continue
            fresh_states[pid] = s[1]
            ticks += s[5] + s[6] + s[7] + s[8]
            if s[1] in ("Z", "X"):
                continue
            live.append(pid)
            if pid != root_pid:
                descendants.append(pid)
                descendant_idents[pid] = s[0]
                pm_ps = proc_memory(pid)
                if pm_ps is None:
                    # Process may have exited right between stat and smaps_rollup; only flag
                    # sample invalid if the exact same process is still alive.
                    after = proc_stat(pid)
                    if after is not None and after[0] == s[0] and after[1] not in ("Z", "X"):
                        mem_failed += 1
                else:
                    mem += pm_ps[0]
                    swap += pm_ps[1]
        sup_state = fresh_states.get(root_pid) if sup_alive and root_pid is not None else None
        own_stopped = (sup_state in ("T", "t")) if sup_state is not None else None
        all_stopped = (bool(own_stopped) and all(fresh_states.get(p) in ("T", "t") for p in descendants)) if own_stopped is not None else None
        out[job_id] = {
            "cpu_usec": int(ticks * 1_000_000 // clk),
            "mem": mem,
            "swap": swap,
            "mem_ok": mem_failed == 0,
            "pids": live,
            "descendants": descendants,
            "descendant_idents": descendant_idents,
            "own_stopped": own_stopped,
            "all_stopped": all_stopped,
        }
    return out


def biggest_processes(count: int = 6, at_least: int = 256 * MIB, deadline: float | None = None):
    """Fallback for the memory watch on machines without cgroup v2 sub-cgroups: top processes by
    unreclaimable memory + swap from /proc/<pid>/status."""
    found = []
    complete = True
    try:
        entries = os.listdir("/proc")
    except OSError:
        return [], False
    for entry in entries:
        if not entry.isdigit():
            continue
        if deadline is not None and time.monotonic() > deadline:
            complete = False
            break
        try:
            vals: dict[str, int] = {}
            name = ""
            with open(f"/proc/{entry}/status", errors="replace") as f:
                for line in f:
                    key, _, rest = line.partition(":")
                    if key == "Name":
                        name = rest.strip()
                    elif key in ("RssAnon", "RssShmem", "VmRSS", "VmSwap"):
                        parts = rest.split()
                        if parts and parts[0].isdigit():
                            vals[key] = int(parts[0]) * KIB
            mem = (vals.get("RssAnon", 0) + vals.get("RssShmem", 0)) if ("RssAnon" in vals or "RssShmem" in vals) else vals.get("VmRSS", 0)
            swap = vals.get("VmSwap", 0)
            if mem + swap >= at_least:
                found.append((mem, swap, f"pid {entry}", name))
        except OSError:
            continue
    found.sort(key=lambda e: e[0] + e[1], reverse=True)
    return found[:count], complete


def is_cgroup_v2(root: str = "/sys/fs/cgroup") -> bool:
    """Whether `root` is a mounted Linux cgroup v2 unified hierarchy."""
    return os.path.isfile(os.path.join(root, "cgroup.controllers"))


def cgroup_controllers(path: str) -> set[str]:
    try:
        with open(os.path.join(path, "cgroup.controllers")) as f:
            return set(f.read().split())
    except OSError:
        return set()


def cgroup_subtree_controllers(path: str) -> set[str]:
    try:
        with open(os.path.join(path, "cgroup.subtree_control")) as f:
            return set(f.read().split())
    except OSError:
        return set()


def cgroup_pids(path: str) -> list[int]:
    try:
        with open(os.path.join(path, "cgroup.procs")) as f:
            return [int(x) for x in f.read().split()]
    except (OSError, ValueError):
        return []


def cgroup_all_pids(path: str) -> list[int]:
    """All PIDs in `path` and every nested child cgroup beneath it (for legacy < 5.14 shutdown)."""
    found: list[int] = []
    seen: set[int] = set()
    try:
        for dirpath, _dirnames, _filenames in os.walk(path):
            for pid in cgroup_pids(dirpath):
                if pid not in seen:
                    seen.add(pid)
                    found.append(pid)
    except OSError:
        pass
    return found


def cgroup_memory_max(path: str) -> str | None:
    try:
        with open(os.path.join(path, "memory.max")) as f:
            val = f.read().strip()
        return val if val == "max" or val.isdigit() else None
    except OSError:
        return None


def boot_id() -> str | None:
    """Kernel boot UUID from /proc/sys/kernel/random/boot_id."""
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip() or None
    except OSError:
        return None


def pidfd_signal(pid: int, start: int | None, sig: int) -> bool:
    """Send `sig` to `(pid, start)` pinned via Linux pidfd_open + pidfd_send_signal (immune to PID
    reuse). Falls back to re-verified os.kill if the kernel lacks pidfd support."""
    if not isinstance(pid, int) or pid <= 1:
        return False
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send = getattr(signal, "pidfd_send_signal", None)
    if pidfd_open is not None and pidfd_send is not None:
        fd = -1
        try:
            fd = pidfd_open(pid, 0)
            if start is not None:
                st = proc_stat(pid)
                if st is None or st[0] != start:
                    return False
            pidfd_send(fd, sig)
            return True
        except ProcessLookupError:
            return False
        except OSError as e:
            if getattr(e, "errno", None) not in (errno.ENOSYS, errno.EPERM):
                return False
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
    if start is not None:
        st = proc_stat(pid)
        if st is None or st[0] != start:
            return False
    try:
        os.kill(pid, sig)
        return True
    except OSError:
        return False


def proc_ppid_comm(pid: int) -> tuple[int | None, str | None]:
    """(ppid, parent_comm) for `pid`, or (None, None)."""
    s = proc_stat_full(pid)
    if s is None or s[2] <= 0:
        return None, None
    ppid = s[2]
    try:
        with open(f"/proc/{ppid}/comm", errors="replace") as f:
            return ppid, f.read().strip()
    except OSError:
        return ppid, None


def is_descendant_of(pid: int, ancestor_pid: int, ancestor_start: int | None = None) -> bool:
    """Whether `pid` descends from `(ancestor_pid, ancestor_start)` via /proc/<p>/stat PPID chain."""
    if not isinstance(pid, int) or not isinstance(ancestor_pid, int) or ancestor_pid <= 1:
        return False
    cur = pid
    seen: set[int] = set()
    for _ in range(64):
        if cur <= 1 or cur in seen:
            return False
        seen.add(cur)
        s = proc_stat_full(cur)
        if s is None:
            return False
        if cur == ancestor_pid:
            return ancestor_start is None or s[0] == ancestor_start
        cur = s[2]
    return False


def _cmdline_is_buildqd(cmd: list[str]) -> bool:
    if not cmd or "--supervise" in cmd:
        return False
    base0 = os.path.basename(cmd[0])
    if base0 == "buildqd":
        return True
    if not re.fullmatch(r"python(\d+(\.\d+)*)?", base0):
        return False
    i = 1
    while i < len(cmd):
        arg = cmd[i]
        if arg == "-m":
            return i + 1 < len(cmd) and cmd[i + 1] == "buildq.daemon"
        if arg in ("-c", "-W", "-X"):
            if arg == "-c":
                return False
            i += 2
            continue
        if arg.startswith("-"):
            i += 1
            continue
        return os.path.basename(arg) == "buildqd"
    return False


def pid_is_buildqd(pid: int, start: int | None = None) -> bool:
    """Whether `pid` (optionally pinned to `start`) is a running buildqd daemon owned by UID."""
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        if os.stat(f"/proc/{pid}").st_uid != UID:
            return False
    except OSError:
        return False
    stat_info = proc_stat(pid)
    if stat_info is None or stat_info[1] in ("Z", "X"):
        return False
    if start is not None and stat_info[0] != start:
        return False
    return _cmdline_is_buildqd(proc_cmdline(pid))


def proc_identity(pid):
    """(start time in clock ticks since boot, state letter) of a process, or None if it's gone."""
    stat = proc_stat(pid)
    return stat[:2] if stat else None


def proc_gone(pid, start) -> bool:
    """Whether a process has certainly ended: nothing has that pid now, another process has it, or
    it is a zombie. A stat line that can't be read for another reason (out of descriptors, say)
    says nothing, so the process isn't called gone on that."""
    stat = proc_stat(pid)
    if stat is not None:
        return stat[0] != start or stat[1] in ("Z", "X")
    try:
        os.stat(f"/proc/{int(pid)}")
    except (FileNotFoundError, ProcessLookupError, ValueError, TypeError, OverflowError):
        return True
    except OSError:
        return False
    return False


def own_pids():
    """The pids of this user's processes, from /proc."""
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            try:
                if os.stat(f"/proc/{entry}").st_uid == UID:
                    yield int(entry)
            except OSError:
                continue


# Agent sessions (#71): Claude Code (the CLI, and the copies T3 and the desktop app run), Codex, and Antigravity (including its `agy` CLI).
AGENT_COMM = ("claude", "codex", "antigravity", "agy")
AGENT_EXE = re.compile(r"/\.?claude/(versions|remote/ccd-cli)/[^/]+$")
T3_THREAD_VAR = "T3CODE_THREAD_ID"  # T3 sets it for a thread's agent process, and so for all that runs


def is_agent(pid: int, strict: bool = False) -> bool:
    """Whether a process is an agent session's. With `strict`, a read that fails for another reason
    than the process being gone or closed to us is raised, not taken for "no"."""
    try:
        with open(f"/proc/{pid}/comm") as f:
            if f.read().strip() in AGENT_COMM:
                return True
        return bool(AGENT_EXE.search(os.readlink(f"/proc/{pid}/exe")))
    except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError):
        return False
    except OSError:
        if strict:
            raise
        return False


def agents_above(pid) -> list[list[int]]:
    """The agent sessions a client runs under: [pid, start ticks] of the client and each of its
    ancestors that is a Claude Code, Codex or Antigravity process, nearest first (an agent may run
    another). Empty for CI, a terminal, a script."""
    found = []
    for _ in range(64):
        stat = proc_stat(pid) if isinstance(pid, int) and pid > 1 else None
        if stat is None:
            break
        if is_agent(pid):
            found.append([pid, stat[0]])
        pid = stat[2]
    return found


def live_threads() -> dict[str, list[list[int]]]:
    """Each T3 thread with agent processes of this user's now -> their [pid, start ticks]. An agent
    whose environment may not be read isn't listed (it can't be told which thread it serves); any
    other failure to read is raised, so that it isn't taken for there being no such process."""
    out: dict[str, list[list[int]]] = {}
    prefix = T3_THREAD_VAR.encode() + b"="
    for pid in own_pids():
        if not is_agent(pid, strict=True):
            continue
        try:
            with open(f"/proc/{pid}/environ", "rb") as f:
                entries = f.read().split(b"\0")
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        thread = next((v[len(prefix):] for v in entries if v.startswith(prefix)), b"")
        thread = thread.decode(errors="replace").strip()[:200]
        stat = proc_stat(pid, strict=True)
        if thread and stat and stat[1] not in ("Z", "X"):
            out.setdefault(thread, []).append([pid, stat[0]])
    return out


def sane_agents(value) -> dict[str, dict]:
    """A ticket's saved `agents`, whatever the file said: only records of the form
    {"pids": [[pid, start ticks], ...], "thread": text or None} are kept. A submission whose
    record is left out counts as dropped off by no known agent, so nothing is cancelled for it."""
    out = {}
    for who, record in value.items() if isinstance(value, dict) else ():
        pids = record.get("pids") if isinstance(record, dict) else None
        if not isinstance(who, str) or not isinstance(pids, list) or not pids:
            continue
        if not all(isinstance(p, list) and len(p) == 2
                   and all(isinstance(x, int) and not isinstance(x, bool) and 0 <= x < 2 ** 63 for x in p)
                   for p in pids):
            continue
        thread = record.get("thread")
        out[who] = {"pids": [list(p) for p in pids], "thread": thread[:200] if isinstance(thread, str) and thread else None}
    return out


def proc_alive(pid, start) -> bool:
    ident = proc_identity(pid) if pid else None
    return bool(ident) and ident[0] == start and ident[1] not in ("Z", "X")


def proc_cmdline(pid: int) -> list[str]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
    except OSError:
        return []


def proc_cgroup(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/cgroup") as f:
            for line in f:
                if line.startswith("0::"):
                    return "/sys/fs/cgroup" + line[3:].strip()
    except OSError:
        pass
    return None


def proc_children(pid: int) -> list[int]:
    try:
        with open(f"/proc/{pid}/task/{pid}/children") as f:
            return [int(x) for x in f.read().split()]
    except (OSError, ValueError):
        return []


def tagged_processes(job: str, exclude: int | None = None) -> list[int]:
    """This user's processes whose environment says they belong to build `job` (BUILDQ_JOB),
    including any that left the build's process group."""
    tag = f"BUILDQ_JOB={job}".encode()
    found = []
    for pid in own_pids():
        if pid == exclude or pid == os.getpid():
            continue
        try:
            with open(f"/proc/{pid}/environ", "rb") as f:
                if tag in f.read().split(b"\0"):
                    ident = proc_identity(pid)
                    if ident and ident[1] not in ("Z", "X"):
                        found.append(pid)
        except OSError:
            continue
    return found


# --------------------------------------------------------------------------------------------
# flock(2) slots of an older semaphore script


def locked_inodes() -> set[int]:
    """Inodes of every file with a flock(2) lock on it, from /proc/locks.

    The pid there is whoever took the lock, which for flock(1) on an inherited descriptor is a
    process that has already exited, and on btrfs the device differs from stat()'s. So this gives
    only candidates; holds_flock() confirms one.
    """
    inodes = set()
    try:
        with open("/proc/locks") as f:
            for line in f:
                parts = line.split()
                # "3: FLOCK  ADVISORY  WRITE 2234 00:56:67 0 EOF"; waiters read "3: -> FLOCK ...".
                if len(parts) >= 6 and parts[1] == "FLOCK":
                    try:
                        inodes.add(int(parts[5].rsplit(":", 1)[1]))
                    except (ValueError, IndexError):
                        continue
    except OSError:
        pass
    return inodes


def holds_flock(pid: int, path: str, inode: int) -> bool:
    """Whether `pid` holds the flock on `path`.

    A waiter has the file open too, so this looks for a descriptor of that file whose open file
    description carries the lock: the kernel lists the lock in that descriptor's fdinfo. It uses
    readlink, not stat, so a descriptor on a hung filesystem can't block it.
    """
    try:
        fds = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return False
    for fd in fds:
        try:
            if os.readlink(f"/proc/{pid}/fd/{fd}") != path:
                continue
            with open(f"/proc/{pid}/fdinfo/{fd}") as f:
                for line in f:
                    if line.startswith("lock:") and "FLOCK" in line and "->" not in line \
                            and line.split()[-3].endswith(f":{inode}"):
                        return True
        except (OSError, IndexError):
            continue
    return False


def fifo_in_use_pids(pids, path: str | None) -> bool:
    """Whether more than one process in `pids` has the jobserver FIFO open (beyond the supervisor)."""
    if not path or not pids:
        return False
    users = 0
    for pid in pids:
        try:
            for fd in os.listdir(f"/proc/{pid}/fd"):
                try:
                    if os.readlink(f"/proc/{pid}/fd/{fd}") == path:
                        users += 1
                        if users > 1:
                            return True
                        break
                except OSError:
                    continue
        except OSError:
            continue
    return users > 1  # the supervisor holds it open for the whole build


def fifo_in_use(cgroup: str, path: str | None) -> bool:
    """Whether a process of the build (other than its supervisor's own copy) has its jobserver
    FIFO open: a tool that opened it speaks the protocol, even before it takes a token."""
    if not path:
        return False
    return fifo_in_use_pids(cgroup_pids(cgroup), path)



def fs_device(path: str) -> int | None:
    """The device of the filesystem path is on, or None if it can't be read."""
    try:
        return os.stat(path).st_dev
    except OSError:
        return None


def reset_failed_unit(unit: str | None) -> bool:
    """Unload a failed service of that name, so a launch can use the name again."""
    try:
        subprocess.run(["systemctl", "--user", "reset-failed", unit or "-"], stdin=DEVNULL,
                       stdout=DEVNULL, stderr=DEVNULL, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass
    return True


def configured_memory_max(slice_name: str) -> str | None:
    """The MemoryMax systemd has for the slice, in bytes ("max" for none), or None if it can't say."""
    try:
        out = subprocess.run(["systemctl", "--user", "show", "-p", "MemoryMax", "--value", slice_name],
                             stdin=DEVNULL, capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if out == "infinity":
        return "max"
    return out if out.isdigit() else None


def find_gh() -> str:
    """gh, from PATH or from where a user install puts it: a systemd user service's PATH is often
    just /usr/local/bin:/usr/bin, without ~/.local/bin."""
    found = shutil.which("gh")
    if found:
        return found
    for where in ("~/.local/bin/gh", "~/bin/gh"):
        path = os.path.expanduser(where)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return "gh"


def find_flock_holder(path: str, inode: int) -> int | None:
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            if os.stat(f"/proc/{entry}").st_uid != UID:
                continue
        except OSError:
            continue
        if holds_flock(int(entry), path, inode):
            return int(entry)
    return None



def cgroup_freeze_set(path: str) -> bool | None:
    """Whether this cgroup itself is set frozen (its own cgroup.freeze), whatever its parents are.
    cgroup.events says whether it is frozen in effect, which a frozen parent also makes it."""
    try:
        with open(os.path.join(path, "cgroup.freeze")) as f:
            return f.read().strip() == "1"
    except (OSError, ValueError):
        return None


def parent_freeze_set(path: str) -> bool:
    """Whether the cgroup's parent (its slice) is set frozen: someone froze buildq.slice, say."""
    return bool(cgroup_freeze_set(os.path.dirname(path.rstrip("/"))))


def cgroup_frozen(path: str) -> bool | None:
    try:
        with open(os.path.join(path, "cgroup.events")) as f:
            for line in f:
                name, _, value = line.partition(" ")
                if name == "frozen":
                    return value.strip() == "1"
    except OSError:
        pass
    return None

