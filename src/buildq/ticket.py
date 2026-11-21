# SPDX-License-Identifier: MIT
"""Ticket state, persistence, cgroup measurement, and client connection wrapper."""

from __future__ import annotations

import collections
import itertools
import json
import os
import time

from . import commands, common, machine
from .runners import Jobserver

_seq = itertools.count()


class Conn:
    """One client connection; sends newline-delimited JSON."""

    def __init__(self, writer):
        self.writer = writer
        self.closed = False
        self.submission = None  # the submission a `buildq run` on this connection made or joined
        self.peer_pid = None  # the client's process, from the socket

    def send(self, message: dict) -> bool:
        if self.closed or self.writer is None or self.writer.is_closing():
            self.closed = True
            return False
        try:
            self.writer.write((json.dumps(message) + "\n").encode())
            return True
        except Exception:
            self.closed = True
            return False


class Ticket:
    PERSIST = ("id", "name", "argv", "cwd", "lane", "key", "label", "notify", "submitted", "priority", "timeout",
               "workspace", "disk_paths", "submission", "attached", "grant_area", "rlimits", "umask", "exec_argv", "cold", "cores_given", "mem_given", "state", "started", "ended", "exit", "oom",
               "timed_out", "error", "unit", "cg", "runner", "pid", "pid_start", "nice", "cancel_requested", "want",
               "need", "rigid", "est_mem", "start_plan", "start_cores", "outgrown_at", "breached_at", "breaches", "start_rule", "start_base", "start_base_cores", "start_rigid", "started_max_cores", "ended_rule", "peak_swap", "tail_used", "mem_base", "mem_cores", "est_src", "peak_mem_cores", "granted", "max_granted", "jobserver", "tokens_out",
               "elastic", "max_log", "mem_peak", "peak_cores", "avg_cores", "measured", "mem_wait_some",
               "mem_wait_full", "mem_high", "source", "dedupe", "caller", "frozen", "frozen_at", "requeues",
               "requeue_pending", "shed_error", "restartable", "joiners", "answered", "interests", "followed",
               "extra_notify", "agents",
               "frozen_total", "frozen_attempt", "attempts_wall", "attempts_mem_wait", "attempts_peak", "attempts_cpu",
               "attempts_mem_high", "git_env", "freeze_failed", "freeze_asked", "thaw_failed",
               "source_verified", "source_changed", "attempt_offsets", "queued_at", "wait_total", "source_checks", "from_ci", "workspace_group", "group_lane",
               "waited_by", "wait_mark", "peak_at", "tail", "released", "release_at", "release_hold",
               "after_release_peak", "release_revoked", "observed_gap", "release_end", "peak_cpu",
               "past_peak_attempts", "past_peak_over", "past_peak_most_over", "past_peak_freed",
               "past_peak_unknown", "past_peak_released", "release_est", "release_oom_base", "release_breach",
               "release_open_at_gap", "write_dirs", "ci_stops", "yields_to", "yield_until")

    def __init__(self, tid: str, argv: list[str], cwd: str, lane: str, key: str, submitted: float,
                 directory: str | None):
        self.id = tid
        self.name = None
        self.argv = argv
        self.cwd = cwd
        self.lane = lane
        self.key = key
        self.label = commands.redacted_label(argv)
        self.notify = None
        self.submitted = submitted
        self.priority = 0
        self.timeout = None
        self.workspace = None
        self.disk_paths: list[str] = []
        self.submission = None
        self.attached = False  # submitted by `buildq run`: cancelled once nobody follows it
        self.rlimits: dict = {}  # the submitter's soft limits, applied to the build
        self.umask = 0o022  # the submitter's, so its build writes files as the command run directly would
        self.exec_argv = None  # what actually runs, when it differs from argv
        self.grant_area = 0.0  # core-seconds given, for the time-weighted grant
        self.cold = False
        self.start_plan = 0  # its memory plan when this attempt started, and the cores it started with
        self.start_cores = 0
        self.outgrown_at = None  # seconds into this attempt when it first used more than start_plan
        self.breached_at = None  # ... and more than its running plan (est_mem then)
        self.breaches = 0  # attempts in which it used more than its running plan
        self.start_rule = None  # scale_mem_with_cores when this attempt started
        self.start_base = 0  # and the peak and cores its plan was made of then (a waiting build is estimated again)
        self.start_base_cores = 0
        self.start_rigid = False  # ... and whether it ignored its core count then
        self.started_max_cores = 0  # the most cores the last started attempt was given (requeue zeroes max_granted)
        self.ended_rule = None  # scale_mem_with_cores when that attempt was stopped and queued again
        self.peak_swap = 0  # swap at its memory peak (not kept across attempts)
        self.tail_used = 0  # the most memory and swap together from half a minute after its peak
        self.behind = 0  # (not kept) builds in line whose room binds this one, while it is held behind them
        self.seq = next(_seq)
        self.dir = directory
        self.cores_given = None
        self.mem_given = None
        self.state = "queued"
        self.started = None
        self.ended = None
        self.exit = None
        self.oom = False
        self.timed_out = False
        self.error = None
        self.unit = None
        self.cg = None
        self.runner = None
        self.pid = None
        self.pid_start = None
        self.nice = None
        self._pids: list[int] = []
        self._own_stopped: bool | None = None
        self._all_stopped: bool | None = None
        self.cap_killed = False
        self.cancel_requested = False
        self.want = 1
        self.need = 1
        self.rigid = False
        self.expected = None  # seconds it usually takes, from history
        self.est_mem = 0
        self.mem_base = 0   # the memory estimate at mem_cores cores
        self.mem_cores = 1
        self.est_src = ""
        self.granted = 0
        self.max_granted = 0
        self.jobserver = None  # its FIFO's path
        self.tokens_out = 0
        self.elastic = False
        self.max_log = 0
        self.mem_peak = 0
        self.peak_mem_cores = 0
        self.peak_cores = 0.0
        self.avg_cores = 0.0
        self.measured = False
        self.mem_wait_some = 0.0  # seconds some of its tasks waited on memory (memory.pressure)
        self.mem_wait_full = 0.0  # seconds all of them did
        self.mem_high = 0  # times it went over its own MemoryHigh, where the kernel slows it
        self.source = None  # the commit it is of, and whether its tree had changes ({repo, commit, dirty, ...})
        self.dedupe = None  # same source, command, directory, lane and environment: the same build
        self.caller = None  # the unit its client ran in: a session, a CI runner
        self.from_ci = False  # submitted from a GitHub Actions job, which reports its own result
        # Its lane's workspace_group when it started, kept across reloads: "" for a non-exclusive
        # lane (no group); None when unknown (saved before groups were recorded).
        self.workspace_group: str | None = None
        self.group_lane: str | None = None  # the lane it started in: it also holds that lane's group as it is now
        self.waited_by: dict[str, float] = {}  # seconds queued, by the reason it waited
        self.wait_mark: float | None = None  # when that was last added up
        self.peak_at = 0.0  # seconds into this attempt when it reached mem_peak (time paused not counted)
        self.tail = 0  # the most it has used from half a minute after that peak on
        self.trace: list[list[float]] = []  # [seconds in, most memory] every TRACE_SECONDS, for its profile
        self.trace_merges = 1  # each point now covers this many TRACE_SECONDS
        self.released = False  # its reservation was let go after its peak (release_after_peak = true)
        # Past the peak, worked out in "shadow" mode too: when (seconds in) and what it would hold,
        # the most it used from then on, and whether it went past that and was taken back.
        self.release_at: float | None = None
        self.release_hold = 0
        self.after_release_peak = 0
        self.release_revoked = False
        self.release_end: float | None = None  # when it went past that (seconds in)
        self.peak_cpu = 0.0  # CPU-seconds this attempt had used when it reached mem_peak
        # The same over all attempts, kept across requeues: see close_release_audit.
        self.past_peak_attempts = self.past_peak_over = self.past_peak_most_over = self.past_peak_unknown = 0
        self.past_peak_freed = 0.0  # byte-seconds of held room it would have freed
        self.past_peak_released = 0  # attempts really released (mode true)
        self.release_est = 0  # its expected peak when the release was decided
        self.release_oom_base = 0  # its OOM kills then
        self.release_breach = False  # taken back because it went over its hold or was killed for memory
        self.kills_seen = 0  # OOM kills in its supervisor's result, wherever the limit was (not saved)
        self.kills_own_cap = False  # and whether one was at its own MemoryMax (not saved)
        self.release_open_at_gap = False  # its decision still stood when a daemon restart left it unwatched
        self.release_stood_at_load = False  # ... when this daemon loaded it (not saved)
        self.release_dirty = False  # measure() took its decision back: the daemon saves the ticket
        self.write_dirs: list[str] = []  # its CARGO_TARGET_DIR and TMPDIR, absolute: what it fills on disk
        self.settled_since: float | None = None  # it has looked past its peak since then
        self.swap_now = 0  # bytes of it in swap
        self.observed_gap = False  # picked up after a daemon restart: its peak may have been missed
        self.frozen = False  # paused to give the machine its memory back
        self.frozen_at = None
        self.requeues = 0  # times it was stopped to free memory and queued again
        self.ci_stops = 0  # of those, the times it was stopped to make room for a CI job's build
        self.yields_to: str | None = None  # the CI job's build it was last stopped for: it doesn't go before it
        self.yield_until: float | None = None  # ...for at most this long
        self.short_hm = 0.0  # (not kept) the room held ahead of it then, which it couldn't use
        self.short_agents = 0.0  # (not kept) of that, the room held for agents' builds (not CI jobs')
        self.short_by = 0.0  # (not kept) the memory it lacked when the queue was last looked at
        self.growth_now = 0  # (not kept) what is held for it to grow into, as the queue last worked it out
        self.held_for = None  # (not kept) "memory" or "cores": what a build in line waits to be free of others' holds
        self.requeue_pending = False  # being stopped, to be queued again
        self.shed_error = None  # why it can't be queued again once stopped
        self.restartable = False  # its caller says it can start over from the beginning safely
        self.joiners = 0  # other submissions that joined it
        self.answered: dict[str, str] = {}  # other submissions it answered: id -> "joined" or "reused"
        self.interests: dict[str, str] = {}  # submissions waiting for it: id -> "run" (followed) or "submit"
        self.followed: dict[str, float] = {}  # a run submission -> when a client of it last followed
        # A dropped-off submission -> the agent session it came from (#71):
        # {"pids": [[pid, start ticks], ...], "thread": its T3 thread or None}
        self.agents: dict[str, dict] = {}
        self.frozen_total = 0.0  # seconds spent paused, over all its attempts
        self.frozen_attempt = 0.0  # seconds this attempt spent paused, the current pause not included
        self.attempts_cpu = 0.0  # CPU seconds of attempts stopped for memory
        self.attempts_mem_high = 0
        self.attempts_wall = 0.0  # run time, memory waits and peak of attempts stopped for memory
        self.attempts_mem_wait = 0.0
        self.attempts_peak = 0
        self.git_env: dict[str, str] = {}  # what git needs to see the tree as its submitter did
        self.extra_notify: list[str] = []  # joiners' --notify commands
        self.source_verified = None  # its source was the same at its start and end (None: not known yet)
        self.source_checks: dict = {}  # "start"/"end" -> whether the tree matched the record then
        self.source_changed = False  # it wasn't: it built something else than its record says
        self.attempt_offsets: dict = {}  # where the last attempt's output begins in stdout/stderr.log
        self.queued_at = None  # when it last joined the queue
        self.wait_total = 0.0  # time spent queued, over all its attempts
        self.freeze_asked = None  # (frozen?, when) of the last freezer request not yet seen to take
        self.force_sent = False  # SIGKILL sent: its memory is on its way back
        self.freeze_failed = False  # this attempt's freeze never took: don't try again (kept on disk)
        self.thaw_failed = False  # its thaw never took and it couldn't be stopped: don't ask again
        # Not kept on disk:
        self.env: dict | None = None  # its environment, to queue it again (gone after a restart)
        self.js: Jobserver | None = None
        self.launcher = None
        self.running_since = None
        self.result_seen = None
        self.last_watched = time.time()
        self.wanted_at = time.time()  # when someone last waited on it, or its agent session last ran (#71)
        self.agent_now = None  # "alive" or "gone", while it is queued and up to its agent session to want it
        self.last_grant_t = None
        self.recheck_at = None
        self.stop_sent = None
        self.samples: collections.deque = collections.deque()
        self.cores_now = 0.0
        self.mem_now = 0
        self.cpu_usec = 0
        self.cpu_usec_base = None
        self.oom_base = 0
        self.oom_kills = 0
        self.flock = None  # (lock path, holder pid) for a build found holding a flock slot
        self.reason = ""
        self.reason_cat = ""
        self.held = False
        self.sent_cat = None
        self.sent_at = 0.0

    @property
    def log(self) -> str | None:
        return os.path.join(self.dir, "output.log") if self.dir else None

    @property
    def est_cores(self) -> float:
        return self.granted if self.state in ("starting", "running") and self.granted else self.need

    def record(self) -> dict:
        return {k: getattr(self, k) for k in self.PERSIST}

    @classmethod
    def from_record(cls, rec: dict, directory: str) -> "Ticket":
        t = cls(rec["id"], list(rec["argv"]), rec["cwd"], rec["lane"], rec["key"], float(rec["submitted"]), directory)
        for k in cls.PERSIST:
            if k in rec:
                setattr(t, k, rec[k])
        t.take_waits(0, 0, 0)  # numbers only, whatever the file said
        if "start_rigid" not in rec and (t.started or t.start_rule):  # (saved by an older daemon: #63)
            t.start_rigid = bool(t.rigid)
        for name in ("peak_swap", "tail_used", "start_plan", "start_cores", "started_max_cores", "breaches",
                     "start_base", "start_base_cores"):
            setattr(t, name, int(common.sane_count(getattr(t, name)) or 0))
        t.outgrown_at, t.breached_at = common.sane_count(t.outgrown_at), common.sane_count(t.breached_at)
        waited = t.waited_by if isinstance(t.waited_by, dict) else {}
        t.waited_by = {str(k): v for k, v in ((k, common.sane_count(v)) for k, v in waited.items()) if v is not None}
        t.peak_at = common.sane_count(t.peak_at) or 0.0
        t.tail = int(common.sane_count(t.tail) or 0)
        t.ci_stops = int(common.sane_count(t.ci_stops) or 0)
        t.yields_to = t.yields_to if isinstance(t.yields_to, str) else None
        t.yield_until = common.sane_count(t.yield_until)
        t.released = t.released is True
        t.release_at = common.sane_count(t.release_at)
        t.release_hold = int(common.sane_count(t.release_hold) or 0)
        t.after_release_peak = int(common.sane_count(t.after_release_peak) or 0)
        t.release_revoked = t.release_revoked is True
        t.release_breach = t.release_breach is True
        t.release_open_at_gap = t.release_open_at_gap is True
        t.group_lane = t.group_lane if isinstance(t.group_lane, str) else None
        t.workspace_group = t.workspace_group if isinstance(t.workspace_group, str) else None
        t.observed_gap = t.observed_gap is True
        t.write_dirs = [r for r in (commands.within_home(d) for d in t.write_dirs if isinstance(d, str))
                        if r][:4] if isinstance(t.write_dirs, list) else []
        t.release_end = common.sane_count(t.release_end)
        t.peak_cpu = common.sane_count(t.peak_cpu) or 0.0
        for name in ("past_peak_attempts", "past_peak_over", "past_peak_most_over", "past_peak_unknown",
                     "past_peak_released", "release_est", "release_oom_base"):
            setattr(t, name, int(common.sane_count(getattr(t, name)) or 0))
        t.past_peak_freed = common.sane_count(t.past_peak_freed, 1e18) or 0.0
        t.wait_mark = common.sane_count(t.wait_mark)
        return t

    def measure(self, now: float, proc_sample: dict | None = None) -> None:
        if self.js is not None:
            self.js.observe()
            if self.js.used and not self.elastic:
                self.elastic = True
        if self.cg:
            stats = machine.cgroup_stats(self.cg)
            if stats is None:
                return
            usage, need, oom = stats
            swap = machine.cgroup_swap(self.cg)
            waits = machine.cgroup_memory_waits(self.cg)
        elif proc_sample is not None:
            usage = proc_sample["cpu_usec"]
            if self.measured and usage < self.cpu_usec:
                usage = self.cpu_usec  # keep monotonic across brief process-reap gaps
            need = proc_sample["mem"]
            swap = proc_sample["swap"]
            oom = (self.oom_base or 0) + self.oom_kills
            self._pids = proc_sample.get("pids") or []
            self._own_stopped = proc_sample.get("own_stopped")
            self._all_stopped = proc_sample.get("all_stopped")
            waits = None
        else:
            return
        if self.cpu_usec_base is None:
            self.cpu_usec_base = usage if self.measured else 0  # picked up after a restart: count from here
        self.measured = True
        self.samples.append((now, usage))
        # Keep about five seconds of samples, so cores_now is a five-second average.
        while len(self.samples) > 2 and now - self.samples[1][0] >= 5.0:
            self.samples.popleft()
        t0, u0 = self.samples[0]
        if now - t0 >= 1.0:
            self.cores_now = max(0.0, (usage - u0) / 1e6 / (now - t0))
            if now - t0 >= 3.0:
                self.peak_cores = max(self.peak_cores, self.cores_now)
        self.cpu_usec = usage
        self.mem_now = need
        self.swap_now = swap
        pausing = max(0.0, now - self.frozen_at) if self.frozen and self.frozen_at else 0.0
        elapsed = max(0.0, now - (self.started or now) - self.frozen_attempt - pausing)
        if need > self.mem_peak:
            self.mem_peak = need
            self.peak_mem_cores = self.granted
            self.peak_at, self.tail, self.tail_used = elapsed, 0, 0
            self.peak_swap = self.swap_now
            self.peak_cpu = max(0, usage - (self.cpu_usec_base or 0)) / 1e6
        elif elapsed - self.peak_at >= 30 and not self.frozen:
            self.tail = max(self.tail, need)
            self.tail_used = max(self.tail_used, need + self.swap_now)  # (what it held, swap included: #56)
        if not self.frozen:
            self.note_trace(elapsed, need)
        if self.release_at is not None and (not self.release_revoked or self.release_breach):
            # Past its peak: did it need more, and how much more? After a breach it is still
            # followed: the room let go may already be another build's, so all of it counts.
            used = need + self.swap_now
            self.after_release_peak = max(self.after_release_peak, used)
            if used > self.release_hold and not self.release_revoked:
                self.release_revoked = self.release_breach = self.release_dirty = True
                self.release_end = round(elapsed, 1)
                if self.released:
                    common.log(f"{self.id} grew past what was kept for it after its peak ({common.fmt_size(used)} > "
                        f"{common.fmt_size(self.release_hold)}); holding its full {common.fmt_size(self.est_mem)} again", level=4)
        self.oom_kills = max(0, oom - (self.oom_base or 0))
        if self.release_at is not None and not self.release_revoked and self.oom_kills > self.release_oom_base:
            self.release_revoked = self.release_breach = self.release_dirty = True  # something in it was killed for memory
            self.release_end = round(elapsed, 1)
            if self.released:
                common.log(f"{self.id} had a process killed for memory after its peak; holding its full "
                    f"{common.fmt_size(self.est_mem)} again", level=4)
        if waits is not None:
            self.take_waits(*waits)

    TRACE_SECONDS = 10.0
    TRACE_POINTS = 720

    def note_trace(self, elapsed: float, need: int) -> None:
        """The most memory in each TRACE_SECONDS of the run; past TRACE_POINTS, neighbours are
        merged, so a long run keeps its shape in a bounded list."""
        step = self.TRACE_SECONDS * self.trace_merges
        if self.trace and elapsed - self.trace[-1][0] < step:
            self.trace[-1][1] = max(self.trace[-1][1], need)
            return
        self.trace.append([elapsed, need])
        if len(self.trace) > self.TRACE_POINTS:
            pairs = [[a[0], max(a[1], b[1])] for a, b in zip(self.trace[::2], self.trace[1::2])]
            self.trace = pairs + ([self.trace[-1]] if len(self.trace) % 2 else [])
            self.trace_merges *= 2

    def take_waits(self, some, full, high) -> None:
        """Keep the largest memory waits seen: the cgroup's counters only grow while it lives. A
        value that isn't a sensible number (a damaged result.json or ticket.json) is ignored."""
        for name, value in (("mem_wait_some", some), ("mem_wait_full", full), ("mem_high", high)):
            value = common.sane_count(value)
            best = max(common.sane_count(getattr(self, name)) or 0.0, value or 0.0)
            setattr(self, name, int(best) if name == "mem_high" else best)

    def row(self, now: float, ahead: int | None = None) -> dict:
        elapsed = round((self.ended or now) - self.started, 1) if self.started else None
        return {
            "id": self.id, "short": self.id if self.flock else self.id[:8], "name": self.name,
            "lane": self.lane, "label": self.label, "key": self.key, "cwd": self.cwd, "state": self.state,
            "priority": self.priority, "submitted": self.submitted, "started": self.started, "ended": self.ended,
            "waited": round((self.started or now) - self.submitted, 1), "elapsed": elapsed,
            "est_cores": self.est_cores, "est_mem": self.est_mem, "est_src": self.est_src,
            "want": self.want, "need": self.need, "granted": self.granted, "elastic": self.elastic,
            "rigid": self.rigid, "cold": self.cold, "expected": self.expected,
            "cores_now": round(self.cores_now, 2), "mem_now": self.mem_now, "mem_peak": self.mem_peak,
            "peak_cores": round(self.peak_cores, 2), "avg_cores": round(self.avg_cores, 2),
            "measured": self.measured, "exit": self.exit, "oom": self.oom, "timed_out": self.timed_out,
            "mem_wait_some": round(self.mem_wait_some, 1), "mem_wait_full": round(self.mem_wait_full, 1),
            "mem_high": self.mem_high, "source": self.source, "caller": self.caller, "from_ci": bool(self.from_ci),
            "agent": self.agent_now if self.state == "queued" else None,
            "frozen": self.frozen,
            "requeues": self.requeues, "ci_stops": self.ci_stops, "yields_to": self.yields_to,
            "source_changed": self.source_changed, "attempt_offsets": self.attempt_offsets,
            "source_verified": self.source_verified,
            "joiners": self.joiners,
            "error": self.error, "reason": self.reason if self.state == "queued" else None,
            "waiting_for": self.reason_cat if self.state == "queued" else None,
            "held": self.held if self.state == "queued" else False, "ahead": ahead,
            "log": self.log, "dir": self.dir, "outside": self.flock[0] if self.flock else None,
            "cancel_requested": self.cancel_requested, "runner": self.runner, "timeout": self.timeout,
            # for `buildq top`: where to read it, and what it fills
            "cg": self.cg, "pid": self.pid, "write_dirs": self.write_dirs, "disk_paths": self.disk_paths, "swap_now": self.swap_now, "waited_by": self.waited_by,
            "peak_at": round(self.peak_at, 1), "release_at": self.release_at, "release_hold": self.release_hold,
            "released": bool(self.released and not self.release_revoked),
            "growth": int(self.growth_now) if self.state in ("starting", "running") else 0,
            "held_for": self.held_for if self.state == "queued" else None,
            "behind": self.behind if self.state == "queued" else 0,
        }

