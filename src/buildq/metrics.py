# SPDX-License-Identifier: MIT
"""SQLite analytics writer, per-minute/hourly rollups, and memory consumers (metrics.db)."""

from __future__ import annotations

import collections
import fnmatch
import os
import queue
import threading
import time

from . import common, machine
from .common import CI_UNITS

METRIC_COLUMNS = [
    ("sampled", "REAL", "SUM"),
    ("cores", "INTEGER", "MAX"), ("cpu_target", "REAL", "AVG"),
    ("cpu_busy", "REAL", "AVG"), ("cpu_busy_max", "REAL", "MAX"),
    ("mem_total", "INTEGER", "MAX"), ("mem_floor", "INTEGER", "AVG"),
    ("mem_available", "INTEGER", "AVG"), ("mem_available_min", "INTEGER", "MIN"),
    ("swap_used", "INTEGER", "AVG"), ("psi_cpu", "REAL", "AVG"), ("psi_mem", "REAL", "AVG"),
    ("running", "REAL", "AVG"), ("running_max", "INTEGER", "MAX"),
    ("queued", "REAL", "AVG"), ("queued_max", "INTEGER", "MAX"),
    ("reserved", "INTEGER", "AVG"),  # the running builds' expected peaks, together
    ("builds_mem_avg", "INTEGER", "AVG"),  # what they used, read each second (builds_mem: once, as the minute ends)
    ("growth", "INTEGER", "AVG"),  # what is held for them to grow into
    ("free_mem", "INTEGER", "AVG"), ("free_mem_min", "INTEGER", "MIN"),  # room for new builds
    ("free_cpu", "REAL", "AVG"),
    ("wait_memory", "REAL", "AVG"), ("wait_cpu", "REAL", "AVG"), ("wait_held", "REAL", "AVG"),
    ("wait_turn", "REAL", "AVG"), ("wait_workspace", "REAL", "AVG"), ("wait_other", "REAL", "AVG"),
    # While at least one build waited on memory: for how long, and what sat idle or held meanwhile.
    ("mem_wait_seconds", "REAL", "SUM"),
    ("idle_cores_mem_wait", "REAL", "WAVG:mem_wait_seconds"),  # cores under the CPU target
    ("free_mem_mem_wait", "INTEGER", "WAVG:mem_wait_seconds"),  # memory above the floor
    ("growth_mem_wait", "INTEGER", "WAVG:mem_wait_seconds"),  # held for running builds to grow into
    # Who used the machine: buildq's builds, CI runners, containers, the user's apps (sessions,
    # editors; CI runners in app.slice not counted twice), and the rest. Cores are averages; memory
    # is one reading, taken as the minute ends (builds_mem_avg above is the builds' average).
    ("builds_cores", "REAL", "AVG"), ("builds_mem", "INTEGER", "AVG"),
    ("ci_cores", "REAL", "AVG"), ("ci_mem", "INTEGER", "AVG"),
    ("containers_cores", "REAL", "AVG"), ("containers_mem", "INTEGER", "AVG"),
    ("apps_cores", "REAL", "AVG"), ("apps_mem", "INTEGER", "AVG"),
    ("other_cores", "REAL", "AVG"),
]
WAIT_COLUMN = {"memory": "wait_memory", "memory-pressure": "wait_memory",
               "cpu": "wait_cpu", "cpu-pressure": "wait_cpu", "held": "wait_held", "max-jobs": "wait_turn",
               "workspace": "wait_workspace"}  # anything else (too big, disk, paused, new): wait_other
WAIT_COLUMNS = ("wait_memory", "wait_cpu", "wait_held", "wait_turn", "wait_workspace", "wait_other")
EXTREMES = {"cpu_busy": ("cpu_busy_max", max), "mem_available": ("mem_available_min", min),
            "running": ("running_max", max), "queued": ("queued_max", max), "free_mem": ("free_mem_min", min)}
BUILD_COLUMNS = [
    ("id", "TEXT PRIMARY KEY"), ("ended", "INTEGER"), ("submitted", "INTEGER"), ("key", "TEXT"),
    ("label", "TEXT"), ("name", "TEXT"), ("lane", "TEXT"), ("caller", "TEXT"), ("repo", "TEXT"),
    ("state", "TEXT"), ("exit", "INTEGER"), ("from_ci", "INTEGER"),
    ("wall", "REAL"), ("waited", "REAL"),
    ("waited_by", "TEXT"),  # JSON: seconds waited by reason (memory, cpu, held, max-jobs, ...)
    ("est_mem", "INTEGER"), ("est_src", "TEXT"),  # what was planned for it, and why
    ("mem_peak", "INTEGER"), ("peak_at", "REAL"),  # its peak, and how far into its run
    ("tail", "INTEGER"),  # the most it used from half a minute after its peak to its end
    ("profile", "TEXT"),  # JSON: its most memory in each tenth of its run, in MiB
    ("released", "INTEGER"),  # 1 if its reservation was let go after its peak
    ("release_at", "REAL"), ("release_hold", "INTEGER"),  # past the peak (also in shadow mode): when, what kept
    ("after_release_peak", "INTEGER"), ("release_revoked", "INTEGER"),  # the most it used after; taken back
    ("release_end", "REAL"),  # when that was (seconds in), if it was
    # Past the peak over all its attempts: how many were, how many then used more than would be kept
    # and by how much at most, the held room that would be freed (GB-hours), and how many can't say
    # (a daemon restart left them unwatched for a while).
    ("past_peak_attempts", "INTEGER"), ("past_peak_over", "INTEGER"), ("past_peak_most_over", "INTEGER"),
    ("past_peak_freed", "REAL"), ("past_peak_unknown", "INTEGER"), ("past_peak_released", "INTEGER"),
    ("observed_gap", "INTEGER"),  # picked up after a daemon restart
    ("cores_granted", "REAL"), ("avg_cores", "REAL"), ("peak_cores", "REAL"), ("cpu_seconds", "REAL"),
    ("requeues", "INTEGER"), ("paused", "REAL"), ("oom", "INTEGER"), ("mem_high", "INTEGER"),
    ("mem_wait", "REAL"),
    ("ci_stops", "INTEGER"),  # of its requeues, the times it was stopped to make room for a CI job's build
    ("attempt_wall", "REAL"),  # its last attempt's wall time, which its profile and avg_cores describe
    ("workspace", "TEXT"),  # what it wrote to (target folder or worktree): builds sharing one take turns
    # Its last attempt's memory plan at the start (est_mem is the plan at the end, raised if it passed
    # it or grew its cores), the cores it started with, when it first used more than that plan
    # (seconds into the attempt), and what the plan was made of: the peak behind it, the cores that
    # peak was measured at, and whether it was a cold cargo build. With these, what another
    # scale_mem_with_cores would have planned, and how often a plan was too small, can be counted.
    ("start_plan", "INTEGER"), ("start_cores", "INTEGER"), ("outgrown_at", "REAL"), ("plan_base", "INTEGER"),
    ("plan_cores", "INTEGER"), ("cold", "INTEGER"),
    # When its last attempt first used more than its running plan (est_mem then, which more cores may
    # have raised under "always": a real breach, re-planned at its cap), how many of its attempts did,
    # the setting its last attempt started under, the most cores it was given, and whether it ignores
    # its core count (rigid: its plan isn't scaled).
    ("breached_at", "REAL"), ("breaches", "INTEGER"), ("start_rule", "TEXT"), ("max_cores", "INTEGER"),
    ("rigid", "INTEGER"),
    # The setting when it ended (a row whose start_rule differs straddled a change: leave it out of a
    # comparison, #63), and its swap at its peak and its memory and swap together after it (#56).
    ("end_rule", "TEXT"), ("peak_swap", "INTEGER"), ("tail_used", "INTEGER"),
]


def memory_profile(trace: list, wall: float, buckets: int = 10) -> list[int] | None:
    """The most memory a build used in each tenth of its run, in MiB, from (seconds in, bytes)."""
    if not trace or wall <= 0:
        return None
    out = [0] * buckets
    for at, mem in trace:
        i = min(buckets - 1, max(0, int(at / wall * buckets)))
        out[i] = max(out[i], int(mem) >> 20)
    return out


def rollup_sql(column: str, how: str) -> str:
    """How a period's value of a column comes from its minutes (see METRIC_COLUMNS)."""
    if how in ("SUM", "MAX", "MIN"):
        return f"{how}({column})"
    weight = how.partition(":")[2] or "sampled"
    return f"SUM({column} * {weight}) / NULLIF(SUM(CASE WHEN {column} IS NOT NULL THEN {weight} END), 0)"


class Consumers:
    """Who uses the machine, read from the cgroups once a minute: CPU since the last reading, and
    the memory each group needs now (as cgroup_stats counts it: not page cache)."""

    def __init__(self):
        self.manager = machine.user_manager_cgroup()
        self.last: tuple[float, dict[str, int]] | None = None

    @staticmethod
    def children(path: str) -> list[str]:
        try:
            return sorted(os.listdir(path))
        except OSError:
            return []

    def groups(self, builds: str | None) -> dict[str, list[str]]:
        apps = os.path.join(self.manager, "app.slice")
        found = {"builds": [builds] if builds else [], "apps": [apps] if os.path.isdir(apps) else [],
                 "ci": [], "containers": []}
        for parent in (self.manager, apps):  # newer systemd puts user services in app.slice
            for name in self.children(parent):
                if name.endswith(".service") and any(fnmatch.fnmatch(name, p) for p in CI_UNITS):
                    found["ci"].append(os.path.join(parent, name))
        for name in self.children(os.path.join(self.manager, "user.slice")):
            if name.startswith("libpod-") and name.endswith(".scope"):
                found["containers"].append(os.path.join(self.manager, "user.slice", name))
        system = "/sys/fs/cgroup/system.slice"
        for name in self.children(system):
            if any(fnmatch.fnmatch(name, p) for p in CI_UNITS) and name.endswith(".service"):
                found["ci"].append(os.path.join(system, name))
            elif (name.startswith("docker-") and name.endswith(".scope")) or name in ("docker.service",
                                                                                      "containerd.service"):
                found["containers"].append(os.path.join(system, name))
        return found

    def read(self, builds: str | None, busy: float, builds_fallback: tuple[int, float] | None = None) -> dict:
        now = time.monotonic()
        usages: dict[str, int] = {}
        found = self.groups(builds)
        apps = os.path.join(self.manager, "app.slice")
        totals: dict[str, list] = {}
        inside_apps = [0, 0.0]  # CI runners within app.slice: taken off the apps' figures
        builds_measured = False
        for group, paths in found.items():
            mem, cores = 0, 0.0
            for path in paths:
                stats = machine.cgroup_stats(path)
                if stats is None:
                    if self.last is not None and path in self.last[1]:
                        usages[path] = self.last[1][path]  # unreadable this once: keep its baseline
                    continue
                if group == "builds":
                    builds_measured = True
                usage, need, _ = stats
                usages[path] = usage
                share = 0.0
                if self.last is not None and now > self.last[0]:
                    # A cgroup not seen last time is new since, and one whose count went down was
                    # started again: either way, all its time is in this minute.
                    before = self.last[1].get(path, 0)
                    delta = usage - before if usage >= before else usage
                    share = min(delta / 1e6 / (now - self.last[0]), float(os.cpu_count() or 1))
                mem += need
                cores += share
                if group == "ci" and path.startswith(apps + os.sep):
                    inside_apps[0] += need
                    inside_apps[1] += share
            totals[group] = [mem, cores]
        if not builds_measured and builds_fallback is not None:
            totals["builds"] = [int(builds_fallback[0]), float(builds_fallback[1])]
        totals["apps"][0] = max(0, totals["apps"][0] - inside_apps[0])
        totals["apps"][1] = max(0.0, totals["apps"][1] - inside_apps[1])
        out: dict = {}
        for group, (mem, cores) in totals.items():
            out[f"{group}_mem"] = mem
            if self.last is not None or (group == "builds" and not builds_measured and builds_fallback is not None):
                out[f"{group}_cores"] = round(cores, 3)
        if self.last is not None:
            out["other_cores"] = round(max(0.0, busy - sum(c for _, c in totals.values())), 3)
        self.last = (now, usages)
        return out


class Metrics:
    """The analytics file, metrics.db: one row a minute (kept MINUTE_DAYS), one an hour rolled up
    from them (HOUR_DAYS), and one per finished build (BUILD_DAYS, at most BUILD_ROWS). Rows past
    their age are deleted once an hour and SQLite reuses the space, so the file levels off. The
    daemon only adds up numbers it already has; a thread of its own writes the file and reads the
    cgroups, so the loop never waits on the disk. If the file can't be used, that is logged (at
    most once an hour) and the queue carries on without it."""

    MINUTE_DAYS, HOUR_DAYS, BUILD_DAYS, BUILD_ROWS = 14, 365, 180, 100_000
    WAL_LIMIT = 64 << 20  # the write-ahead log is cut back to this after a checkpoint
    WAL_MAX = 256 << 20  # past this (a reader holding it open), rows are dropped until it shrinks

    def __init__(self, path: str):
        self.path = path
        self.items: queue.Queue = queue.Queue(maxsize=10000)
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()
        self.stopping = False
        self.failed = False
        self.dropped = False
        self.complained_at = -1e18
        self.minute: int | None = None
        self.last: float | None = None
        self.seconds = 0.0
        self.sums: dict[str, float] = collections.defaultdict(float)
        self.extremes: dict[str, float] = {}
        self.builds_cgroup: str | None = ""
        self.builds_fallback: tuple[int, float] | None = None
        self._fb_mem_sum = 0.0
        self._fb_cpu_sum = 0.0
        self._fb_weight = 0.0

    def trouble(self, message: str) -> None:
        """Log a problem with the analytics, at most once an hour."""
        now = time.monotonic()
        if now - self.complained_at >= 3600:
            self.complained_at = now
            common.log(message, level=4)

    # ---- on the daemon's loop

    def _current_fallback(self) -> tuple[int, float] | None:
        if self._fb_weight > 0:
            return (int(self._fb_mem_sum / self._fb_weight), self._fb_cpu_sum / self._fb_weight)
        return self.builds_fallback

    def observe(self, now: float, values: dict, builds_cgroup: str | None,
                builds_fallback: tuple[int, float] | None = None) -> None:
        """Add one tick's numbers to the minute; a new minute sends the last one to be written."""
        minute = int(now // 60) * 60
        if self.minute is not None and minute != self.minute:
            self.emit(self.builds_cgroup, self._current_fallback())
        self.builds_cgroup = builds_cgroup
        self.builds_fallback = builds_fallback
        if minute != self.minute:
            self.minute, self.last, self.seconds = minute, None, 0.0
            self.sums.clear()
            self.extremes.clear()
            self._fb_mem_sum = self._fb_cpu_sum = self._fb_weight = 0.0
        weight = 1.0 if self.last is None else min(max(now - self.last, 0.0), 5.0)
        self.last = now
        if weight <= 0:
            return
        self.seconds += weight
        if builds_fallback is not None:
            self._fb_mem_sum += float(builds_fallback[0]) * weight
            self._fb_cpu_sum += float(builds_fallback[1]) * weight
            self._fb_weight += weight
        for name, value in values.items():
            self.sums[name] += float(value) * weight
            if name in EXTREMES:
                column, pick = EXTREMES[name]
                self.extremes[column] = pick(self.extremes.get(column, value), value)
        if values.get("wait_memory", 0) > 0:
            self.sums["mem_wait_seconds"] += weight
            self.sums["@idle_cores_mem_wait"] += max(values["cpu_target"] - values["cpu_busy"], 0.0) * weight
            self.sums["@free_mem_mem_wait"] += max(values["mem_available"] - values["mem_floor"], 0) * weight
            self.sums["@growth_mem_wait"] += values["growth"] * weight

    def emit(self, builds_cgroup: str | None, builds_fallback: tuple[int, float] | None = None) -> None:
        if self.seconds <= 0 or self.minute is None:
            return
        types = {name: kind for name, kind, _ in METRIC_COLUMNS}
        row: dict = {"_kind": "minute", "t": self.minute, "sampled": round(self.seconds, 1),
                     "_builds": builds_cgroup, "_builds_fallback": builds_fallback}
        waited = self.sums.get("mem_wait_seconds", 0.0)
        for name, total in self.sums.items():
            if name.startswith("@"):
                column = name[1:]
                value = total / waited if waited else None
            elif name == "mem_wait_seconds":
                column, value = name, total
            else:
                column, value = name, total / self.seconds
            if value is not None:
                row[column] = int(value) if types.get(column) == "INTEGER" else round(value, 3)
        for column, value in self.extremes.items():
            row[column] = int(value) if types.get(column) == "INTEGER" else round(float(value), 3)
        self.put(row)

    def build(self, row: dict) -> None:
        self.put({"_kind": "build", **row})

    def put(self, item) -> None:
        if self.failed or self.stopping:
            return
        with self.lock:
            if self.thread is not None and not self.thread.is_alive():
                self.trouble("the analytics writer had stopped; starting it again")
                self.thread = None
            if self.thread is None:
                self.thread = threading.Thread(target=self.run, name="metrics", daemon=True)
                self.thread.start()
        try:
            self.items.put_nowait(item)
        except queue.Full:
            if not self.dropped:
                self.dropped = True
                self.trouble("the analytics writer is behind; dropping rows until it catches up")

    def flush(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while self.items.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        return not self.items.unfinished_tasks

    def stop(self, timeout: float = 2.0) -> None:
        if not self.stopping and self.minute is not None and self.seconds > 0:
            self.emit(self.builds_cgroup, self._current_fallback())  # the minute so far
            self.minute = None
        with self.lock:
            self.stopping = True
            thread = self.thread
        if thread is not None and thread.is_alive():
            try:
                self.items.put(None, timeout=timeout)
            except queue.Full:
                pass
            thread.join(timeout)

    # ---- on its own thread

    def open(self):
        import sqlite3
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        new = not os.path.exists(self.path)
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        if new:
            db.execute("PRAGMA auto_vacuum = INCREMENTAL")  # freed pages go back to the disk
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("PRAGMA synchronous = NORMAL")
        db.execute(f"PRAGMA journal_size_limit = {self.WAL_LIMIT}")
        for table in ("minute", "hour"):
            db.execute(f"CREATE TABLE IF NOT EXISTS {table} (t INTEGER PRIMARY KEY)")
            have = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            for name, kind, _ in METRIC_COLUMNS:
                if name not in have:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
        db.execute("CREATE TABLE IF NOT EXISTS build (id TEXT PRIMARY KEY)")
        have = {row[1] for row in db.execute("PRAGMA table_info(build)")}
        for name, kind in BUILD_COLUMNS:
            if name not in have:
                db.execute(f"ALTER TABLE build ADD COLUMN {name} {kind.replace(' PRIMARY KEY', '')}")
        db.execute("CREATE INDEX IF NOT EXISTS build_ended ON build (ended)")
        db.execute("CREATE INDEX IF NOT EXISTS build_key ON build (key)")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return db

    def wal_too_big(self, db) -> bool:
        """Checkpoint a write-ahead log that has grown, and say if it is still too big (a reader
        holds it open): rows are dropped until it shrinks, rather than the disk filling."""
        try:
            size = os.path.getsize(self.path + "-wal")
        except OSError:
            return False
        if size <= self.WAL_LIMIT:
            return False
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        try:
            size = os.path.getsize(self.path + "-wal")
        except OSError:
            return False
        if size > self.WAL_MAX:
            self.trouble(f"the analytics file's write-ahead log is {common.fmt_size(size)} and can't be cut back "
                         "(a reader holds it open); dropping rows until it shrinks")
            return True
        return False

    def run(self) -> None:
        import sqlite3
        try:
            db = self.open()
        except (sqlite3.Error, OSError) as e:
            self.trouble(f"can't open the analytics file {self.path}: {e}; the queue carries on without it")
            self.failed = True
            while True:  # let flush() and stop() see an empty queue
                item = self.items.get()
                self.items.task_done()
                if item is None:
                    return
        consumers = Consumers()
        cleaned = -1e18  # monotonic: when the rows were last rolled up and trimmed
        since: float | None = None  # the earliest minute written since then
        written = 0
        full = False
        while True:
            item = self.items.get()
            try:
                if item is None:
                    db.close()
                    return
                kind = item.pop("_kind")
                if written % 100 == 0:
                    full = self.wal_too_big(db)
                written += 1
                if kind == "minute":
                    builds = item.pop("_builds")
                    builds_fallback = item.pop("_builds_fallback", None)
                    item.update(consumers.read(builds, float(item.get("cpu_busy") or 0.0), builds_fallback))
                    if not full:
                        names = [n for n in item if n == "t" or n in {c for c, _, _ in METRIC_COLUMNS}]
                        db.execute(f"INSERT OR REPLACE INTO minute ({', '.join(names)}) VALUES "  # noqa: S608 (schema constants)
                                   f"({', '.join('?' * len(names))})", [item[n] for n in names])
                        since = min(since, float(item["t"])) if since is not None else float(item["t"])
                elif kind == "build" and not full:
                    names = [n for n, _ in BUILD_COLUMNS if n in item]
                    db.execute(f"INSERT OR REPLACE INTO build ({', '.join(names)}) VALUES "  # noqa: S608 (schema constants)
                               f"({', '.join('?' * len(names))})", [item[n] for n in names])
                if time.monotonic() - cleaned >= 3600:
                    cleaned = time.monotonic()  # (a failure is tried again in an hour)
                    self.clean(db, time.time(), since)
                    since = None
                    full = self.wal_too_big(db)
                self.dropped = False
            except Exception as e:  # never let the writer die on a row
                self.trouble(f"can't write to the analytics file {self.path}: {e!r}")
            finally:
                self.items.task_done()

    def clean(self, db, now: float, since: float | None = None) -> None:
        """Roll the minutes up into hours (from the newest hour on, or the earliest minute written
        since the last time, so the hour still going and late minutes are rolled again), and delete
        rows past their age, keeping at most BUILD_ROWS builds. On an error, nothing is kept."""
        names = [n for n, _, _ in METRIC_COLUMNS]
        rolled = ", ".join(rollup_sql(n, how) for n, _, how in METRIC_COLUMNS)
        newest = db.execute("SELECT MAX(t) FROM hour").fetchone()[0]
        start = int(newest) if newest is not None else 0
        if since is not None:
            start = min(start, int(since // 3600) * 3600)
        db.execute("BEGIN")
        try:
            db.execute(f"INSERT OR REPLACE INTO hour (t, {', '.join(names)}) SELECT (t / 3600) * 3600, {rolled} "  # noqa: S608 (schema constants)
                       "FROM minute WHERE t >= ? GROUP BY t / 3600", (start,))
            db.execute("DELETE FROM minute WHERE t < ?", (now - self.MINUTE_DAYS * 86400,))
            db.execute("DELETE FROM hour WHERE t < ?", (now - self.HOUR_DAYS * 86400,))
            db.execute("DELETE FROM build WHERE ended < ?", (now - self.BUILD_DAYS * 86400,))
            db.execute("DELETE FROM build WHERE id IN (SELECT id FROM build ORDER BY ended DESC "
                       "LIMIT -1 OFFSET ?)", (self.BUILD_ROWS,))
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        db.execute("PRAGMA incremental_vacuum").fetchall()  # (it frees a page per row it returns)

