# SPDX-License-Identifier: MIT
"""Scheduling, admission, jobserver rebalancing, memory shedding, slice guard, and memory watch."""

from __future__ import annotations

import collections
import math
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import traceback

from . import commands, common, machine, runners, tools
from .commands import MAKE_FLAG_VARS
from .common import FINISHED, GIB, MIB, PENDING
from .config import CI_LONG_WAITS
from .runners import Jobserver
from .ticket import Ticket

_MAKE_FIFO_CACHE: dict[tuple, bool] = {}


def _make_supports_fifo(argv0: str, env_path: str | None) -> bool:
    """GNU make 4.4+ supports `--jobserver-auth=fifo:PATH`; GNU make <= 4.3 aborts on it."""
    exe = shutil.which(argv0, path=env_path) if env_path or os.path.isabs(argv0) else shutil.which(argv0)
    if not exe:
        return True
    try:
        st = os.stat(exe)
    except OSError:
        return True
    key = (exe, st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)
    cached = _MAKE_FIFO_CACHE.get(key)
    if cached is not None:
        return cached
    supported = True
    try:
        res = subprocess.run([exe, "--version"], stdin=common.DEVNULL, capture_output=True, text=True, timeout=2)
        m = re.search(r"GNU Make\s+(\d+)\.(\d+)", res.stdout or "")
        if m:
            supported = (int(m.group(1)), int(m.group(2))) >= (4, 4)
    except (OSError, subprocess.SubprocessError):
        pass
    _MAKE_FIFO_CACHE[key] = supported
    return supported

class SchedulerMixin:
    """Queue ordering, admission, jobserver rebalancing, slice guard, memory shedding, and watch."""

    REBALANCE_SECONDS = 5.0

    # ---- runner capability helpers (stay compatible with minimal test/sim runners)

    @staticmethod
    def _runner_has_cgroup(r) -> bool:
        return bool(getattr(r, "has_cgroup", r.name == "systemd"))

    @staticmethod
    def _runner_enforces_mem_cap(r) -> bool:
        return bool(getattr(r, "enforces_mem_cap", r.name == "systemd"))

    def _runner_builds_cgroup(self, r) -> str | None:
        fn = getattr(r, "builds_cgroup", None)
        if fn is not None:
            return fn(self.cfg.slice)
        return os.path.join(self.cg_base, machine.slice_path(self.cfg.slice)) if r.name == "systemd" else None

    @staticmethod
    def _runner_freeze_state(r, t: Ticket) -> tuple[bool | None, bool | None, bool]:
        fn = getattr(r, "freeze_state", None)
        if fn is not None:
            return fn(t)
        if not t.cg:
            return None, None, False
        return (machine.cgroup_freeze_set(t.cg), machine.cgroup_frozen(t.cg),
                machine.parent_freeze_set(t.cg))

    # ---- the ticket index

    def queue(self, now: float | None = None) -> list[Ticket]:
        """Queued tickets in the order they get a turn: any that has waited starve_seconds (or, with
        ci_first, comes from a CI job) first, then the higher priority (the lane's plus the ticket's
        own), CI jobs first at the same priority with ci_first (one that has waited starve_seconds
        before newer ones), then the one expected to finish soonest, then the oldest.

        With ci_first, any build that has waited three times starve_seconds, or was stopped to free
        memory, goes before all of those, so CI jobs (in a higher lane, for a CI job's build) can't
        keep it waiting for ever. With ci_deadline, a CI job's build that has waited that long goes
        before even those, the oldest first; a build that has waited CI_LONG_WAITS times starve_seconds
        goes before even that, so CI jobs can't keep it waiting for ever either."""
        now = time.time() if now is None else now

        # Fair shares: at the same priority, a caller (a session, a CI runner) with fewer builds
        # running goes first, so one agent's burst of builds doesn't keep everyone else waiting.
        running = collections.Counter(t.caller for t in self.active())

        def tier(t: Ticket):
            lane = self.cfg.lanes.get(t.lane)
            passes = bool(lane and lane.may_pass_held)
            # A build that has waited starve_seconds goes ahead of everything else, or a stream of
            # quicker or higher builds could keep it waiting for ever. With ci_first a CI job's build
            # counts so at once, and any build that has waited much longer goes before it: a
            # session's, or a CI job's that higher-lane CI builds keep passing.
            starving = self.starving(t, now) and not passes
            overdue = (self.cfg.ci_first and not passes and not self.yielding(t, now)
                       and (now - t.submitted >= 3 * self.cfg.starve_seconds or t.requeues > 0))
            # A CI job's build goes before the others at its priority, and one that has waited out its
            # turn before newer ones, or a stream of quicker CI builds could keep it waiting past its
            # job's time limit. Not among overdue builds (they go in the plain order, or a backlog of
            # CI builds could keep an overdue session's build waiting), nor in a lane that passes held
            # room (it gets no head start there).
            first = self.cfg.ci_first and t.from_ci and not passes and not overdue
            # A CI job's build near its job's time limit goes before all of them, the oldest first (the
            # nearest to its limit, whatever its lane); a build that has waited CI_LONG_WAITS times
            # starve_seconds (two hours here) goes before even that, so a stream of CI jobs can't keep
            # it waiting for ever, while a release's steps still go first in any likely queue.
            due = self.ci_due(t, now)
            queued = t.wait_total + max(0.0, now - (t.queued_at or t.submitted))  # (its queued time, not its runs)
            long = overdue and not t.from_ci and queued >= CI_LONG_WAITS * self.cfg.starve_seconds
            return (not long, not due, t.submitted if due else 0.0, not overdue, not starving, -self.weight(t),
                    not first, not (first and self.waited_out(t, now)))

        queued = [t for t in self.tickets.values() if t.state == "queued"]
        tiers = {t.id: tier(t) for t in queued}

        def tail(t: Ticket):
            return (t.expected if t.expected is not None else math.inf, t.submitted, t.seq)

        queued.sort(key=lambda t: (*tiers[t.id], *tail(t)))
        # Within a tier, a caller's k-th waiting build counts as if k more of its builds ran, so
        # callers take turns: one session's burst of builds is interleaved with everyone else's.
        share, seen = {}, collections.Counter()
        for t in queued:
            share[t.id] = running[t.caller] + seen[(tiers[t.id], t.caller)]
            seen[(tiers[t.id], t.caller)] += 1
        return sorted(queued, key=lambda t: (*tiers[t.id], share[t.id], *tail(t)))

    def weight(self, t: Ticket) -> int:
        """Its priority: its lane's plus its own."""
        lane = self.cfg.lanes.get(t.lane)
        return t.priority + (lane.priority if lane else 0)

    def waited_out(self, t: Ticket, now: float) -> bool:
        """It has waited starve_seconds, or it was stopped to free memory once already (and doesn't
        yield to the CI job's build it was stopped for)."""
        if self.yielding(t, now):
            return False
        return now - t.submitted >= self.cfg.starve_seconds or t.requeues > 0

    YIELD_MAX = 1800.0  # seconds a build stopped for a CI job's build waits for it at most

    def yielding(self, t: Ticket, now: float) -> bool:
        """It was stopped to make room for a CI job's build that is still waiting, less than
        YIELD_MAX ago: it doesn't start, go before that build, hold room or count as waited out, or
        it would take back the room it gave. (A CI build that can't start for long doesn't keep it
        waiting for ever: after YIELD_MAX it goes first, as any stopped build does.)"""
        if not t.yields_to or now >= (t.yield_until or 0.0):
            return False
        ci = self.tickets.get(t.yields_to)
        return ci is not None and ci.state == "queued"

    def ci_due(self, t: Ticket, now: float) -> bool:
        """A CI job's build that has waited ci_deadline: its job has a time limit that counts the wait
        (GitHub cancels a step still queued), so from here it goes before everything, builds that have
        waited three times starve_seconds included, and its room binds every lane. A release's step
        waited 47 minutes of 50, and a scan 30 of 30, behind such builds (2026-11-15/16; #60)."""
        if not (self.cfg.ci_deadline and self.cfg.ci_first and t.from_ci) or self.yielding(t, now):
            return False
        lane = self.cfg.lanes.get(t.lane)
        return not (lane and lane.may_pass_held) and now - t.submitted >= self.cfg.ci_deadline

    def starving(self, t: Ticket, now: float) -> bool:
        """It has waited out its turn, or (with ci_first) it comes from a CI job, which has a time
        limit of its own and holds up a merge or a release: it goes first, and the room it needs is
        held for it. (A CI job's room binds only builds of its priority or lower until it has
        waited out its turn too; see schedule.)"""
        return self.waited_out(t, now) or (self.cfg.ci_first and t.from_ci)

    def estimate(self, t: Ticket, now: float) -> None:
        e = self.history.estimate(t.key, self.lane(t.lane), self.cfg, self.machine.ncpu,
                                  self._runner_enforces_mem_cap(self.runner), t.cold, now)
        t.want, t.need, t.est_src, t.rigid, t.expected = e.want, e.need, e.source, e.rigid, e.wall
        t.mem_base, t.mem_cores = e.mem, e.mem_cores
        if t.cores_given is not None:
            if t.rigid:  # it was seen to use more than it is told; telling it less changes nothing
                t.est_src = f"{t.est_src}; cores given, but it doesn't follow them"
            else:  # the caller knows how many cores the build should use
                t.want = t.need = int(t.cores_given)
                t.est_src = f"{t.est_src}; cores given"
        if t.mem_given is not None and t.mem_given > t.mem_base:  # a given size may raise, never lower
            t.mem_base, t.mem_cores = t.mem_given, max(t.mem_cores, t.want)
            t.est_src = f"{t.est_src}; memory given"
        t.est_mem = self.mem_for(t, t.granted if t.state in ("starting", "running") and t.granted else t.need)

    def mem_for(self, t: Ticket, cores: int) -> int:
        """The memory a build needs with `cores` cores. Builds that run more compilers at once need
        more memory, so past the cores its history was measured at, the estimate grows in
        proportion, up to the lane's MemoryMax where that is enforced. With scale_mem_with_cores =
        "never" it doesn't: on this workstation's history, a warm build's peak rarely grew with its
        cores, and far less than in proportion when it did (a median x1.0; a few commands x1.2 to
        x1.6 at 4 to 8 times the cores). A build that outgrows its plan is planned at its lane's
        MemoryMax from then on (outgrown)."""
        base = t.mem_base
        if t.rigid or cores <= t.mem_cores:
            return base
        if self.cfg.scale_mem_with_cores == "never":
            return base
        scaled = int(base * cores / t.mem_cores)
        lane = self.lane(t.lane)
        if lane.mem_cap and self._runner_enforces_mem_cap(self.runner_of(t)):
            scaled = min(scaled, max(lane.mem_cap, base))
        return scaled

    # ---- giving memory back when the machine runs short

    FREEZE_SECONDS = 10  # between pauses, so the machine's memory can settle

    KILL_SECONDS = 2  # between stops: a stop frees memory at once, and a pause must never hold one up

    SHED_MIN = 512 * MIB  # a build holding less frees too little to be worth stopping

    FREEZE_MAX = 300  # a pause lasts this long at most: then the build goes on, or is stopped

    def shed_memory(self, now: float) -> None:
        """When the machine runs short of memory although no build may grow past its estimate (other
        work grew: CI, containers, sessions), give memory back before the machine stalls, as GNU
        parallel's --memsuspend and --memfree do, and as Slurm requeues the jobs it preempts.

        Memory still held by builds being stopped counts as free already, so one shortfall stops
        one build. Below a quarter of mem_floor available, stop a build at once: the newest of those
        whose memory covers the shortfall (back to half the floor), else the largest; never one
        holding less than 512 MiB. A build that can start over safely (a compile or test run, or
        --restartable) is queued again at the front; anything else fails, saying why, as running it
        again might repeat what it did. Below half of mem_floor, while memory is still falling, pause
        (freeze) the newest build holding at least 1 GiB: it stops growing and using CPU. A paused
        build goes on once the machine has mem_floor free plus the room it may still grow into, at
        once if shed_memory is off, and after FREEZE_MAX in any case: if memory is still below half
        the floor then, it is stopped instead."""
        cfg, m = self.cfg, self.machine
        builds = [t for t in self.active() if not t.flock and t.state == "running" and not t.cancel_requested
                  and not t.requeue_pending]
        self.reconcile_freezes(builds, now)
        builds = [t for t in builds if not t.requeue_pending]  # (reconciling may have stopped one)
        frozen = [t for t in builds if t.frozen]
        floor = cfg.mem_floor
        if m.mem_available < floor / 4 and cfg.shed_memory:
            for t in self.active():  # a cancelled build in its grace period is going anyway: now, then
                if t.cancel_requested and not t.flock and not t.force_sent:
                    self.stop(t, now, force=True)
                    t.force_sent = True
        stopping = sum(t.mem_now for t in self.active() if t.requeue_pending or t.force_sent)
        avail = m.mem_available + stopping
        self.avail_seen.append((now, avail))
        while self.avail_seen and now - self.avail_seen[0][0] > 10:
            self.avail_seen.popleft()
        if not cfg.shed_memory or not floor:
            for t in frozen:
                if not (t.freeze_asked and not t.freeze_asked[0]) and not t.thaw_failed:
                    self.freeze(t, False, now, "shed_memory is off")
            return
        thawing = {t.id for t in frozen if t.freeze_asked and not t.freeze_asked[0]}
        for t in frozen:
            if t.id in thawing:
                continue  # asked to go on already
            if self.paused_for(t, now) >= self.FREEZE_MAX:
                if avail >= floor / 2:
                    self.freeze(t, False, now, f"paused for {self.FREEZE_MAX // 60} minutes in all, the longest it "
                                               "may be (it won't be paused again)")
                    self.last_freeze = now
                elif now - self.last_kill >= self.KILL_SECONDS:
                    self.shed(t, now)  # memory stays short: its memory is what frees it
                    self.last_kill = now
                return
        if avail < floor / 4:
            if now - self.last_kill < self.KILL_SECONDS:
                return
            pool = sorted((t for t in builds if t.mem_now >= self.SHED_MIN), key=lambda t: t.started or 0,
                          reverse=True)
            if not pool:
                return  # what holds the memory isn't ours to stop
            shortfall = floor / 2 - avail
            enough = [t for t in pool if t.mem_now >= shortfall]
            self.shed(enough[0] if enough else max(pool, key=lambda t: t.mem_now), now)
            self.last_kill = now
        elif avail < floor / 2:
            falling = self.avail_seen[0][1] - avail >= 256 * MIB  # pausing only helps while memory still goes
            if now - self.last_freeze < self.FREEZE_SECONDS or not falling:
                return
            pool = [t for t in sorted(builds, key=lambda t: t.started or 0, reverse=True)
                    if not t.frozen and not t.freeze_failed and t.mem_now >= GIB and self.runner_of(t).can_freeze
                    and self.paused_for(t, now) < self.FREEZE_MAX]  # each attempt is paused for so long at most
            if pool:
                self.freeze(pool[0], True, now, f"only {common.fmt_size(m.mem_available)} of memory is free (half the floor "
                                                f"is {common.fmt_size(floor / 2)}) and falling")
                self.last_freeze = now
        elif [t for t in frozen if t.id not in thawing] and now - self.last_freeze >= self.FREEZE_SECONDS:
            t = min((t for t in frozen if t.id not in thawing), key=lambda t: t.frozen_at or 0)
            if avail > floor + max(0, t.est_mem - t.mem_now):
                self.freeze(t, False, now, f"{common.fmt_size(m.mem_available)} of memory is free again")
                self.last_freeze = now

    def paused_for(self, t: Ticket, now: float) -> float:
        """How long this attempt has been paused, in all."""
        return t.frozen_attempt + (max(0.0, now - t.frozen_at) if t.frozen and t.frozen_at else 0.0)

    def freeze(self, t: Ticket, frozen: bool, now: float, why: str) -> None:
        try:
            proc = self.runner_of(t).freeze(t, frozen)
            if proc is not None:
                self.helpers.append((proc, now + 30))
        except OSError as e:
            common.log(f"can't {'pause' if frozen else 'resume'} {t.id}: {e}", level=4)
            if frozen:
                return
            # A thaw that couldn't even be sent still has its minute: then the build is stopped.
        _, took, _ = self._runner_freeze_state(self.runner_of(t), t)
        observable = took is not None
        # A pause begins, and ends, when the cgroup (or process state) says so (reconcile_freezes);
        # where that can't be seen, at once.
        if frozen:
            t.frozen, t.frozen_at, t.thaw_failed = True, (None if observable else now), False
        elif not observable:
            if t.frozen_at:
                t.frozen_attempt += max(0.0, now - t.frozen_at)
            t.frozen, t.frozen_at = False, None
        t.freeze_asked = (frozen, now)
        self.save(t)
        self.broadcast(t, now)
        common.log(f"asked {t.id} to {'pause' if frozen else 'go on'}: {why}", level=4)

    def reconcile_freezes(self, builds: list[Ticket], now: float) -> None:
        """The build's own cgroup.freeze says whether buildq's pause is set; cgroup.events whether
        it took (a frozen parent, buildq.slice say, freezes a build too, but that isn't buildq's
        pause, and isn't taken for one). A pause counts from when it is seen to take. A request that
        hasn't taken after 15 s is sent once more; after a minute the daemon gives up: a freeze that
        never took is undone (so it can't land later), and this attempt isn't paused again; a thaw
        that never took (its own setting still frozen) means the build can't go on, so it is
        stopped, if shed_memory is on. With no request pending, the ticket follows the build's own
        setting (a freeze that landed late; a thaw from before a restart)."""
        for t in builds:
            own, took, parent_frozen = self._runner_freeze_state(self.runner_of(t), t)
            if t.freeze_asked is None:
                if own is None:
                    if t.frozen and t.frozen_at is None:
                        t.frozen_at = now  # can't be seen: paused, as far as is known, from now
                elif own and not t.frozen and parent_frozen:
                    pass  # its slice is frozen (older systemd sets each member's own setting too): not buildq's
                elif own and not t.frozen:
                    t.frozen, t.frozen_at = True, (now if took else None)
                    if not t.freeze_failed:  # (else it is buildq's own freeze, whose undo didn't take)
                        common.log(f"{t.id} is set paused, though buildq didn't mean it to be; taking it so", level=4)
                    self.save(t)
                    self.broadcast(t, now)
                elif not own and t.frozen:
                    if t.frozen_at:
                        t.frozen_attempt += max(0.0, now - t.frozen_at)
                    t.frozen, t.frozen_at, t.thaw_failed = False, None, False  # (thawed by someone: it may be again)
                    self.save(t)
                    self.broadcast(t, now)
                elif own and took and t.frozen_at is None:
                    t.frozen_at = now
                    self.save(t)
                continue
            wanted, asked, retried = (list(t.freeze_asked) + [False, False])[:3]
            if own is None:
                continue
            done = (own and took) if wanted else not own  # a thaw is done once its own setting is clear
            if done:
                if wanted and t.frozen_at is None:
                    t.frozen_at = now  # it took: the pause counts from here
                elif not wanted:  # thawed (a parent may still hold it, but that isn't buildq's pause)
                    if t.frozen_at:
                        t.frozen_attempt += max(0.0, now - t.frozen_at)
                    t.frozen, t.frozen_at, t.thaw_failed = False, None, False
                    self.broadcast(t, now)
                t.freeze_asked = None
                self.save(t)
                continue
            if now - asked >= 60:
                if wanted:  # a freeze that never took: undo it, so it can't land later; and no more tries
                    common.log(f"{t.id} hadn't paused a minute after it was asked to; undoing it, and not pausing it "
                        "again", level=4)
                    t.frozen, t.frozen_at, t.freeze_failed = False, None, True
                    try:
                        proc = self.runner_of(t).freeze(t, False)
                        if proc is not None:
                            self.helpers.append((proc, now + 30))
                    except OSError:
                        pass
                    t.freeze_asked = (False, now)  # tracked: if even the undo doesn't take, the build is stopped
                elif self.cfg.shed_memory:  # a thaw that never took: it can't go on, so it is stopped
                    undo = t.freeze_failed  # (the undo of a freeze that never took, rather than a pause)
                    common.log(f"{t.id} hadn't {'had its pause undone' if undo else 'resumed'} a minute after it was asked "
                        "to; stopping it", level=4)
                    t.freeze_asked = None
                    self.shed(t, now, why="its pause couldn't be undone" if undo
                              else "it couldn't be resumed after a pause")
                    continue
                else:
                    common.log(f"{t.id} hadn't resumed a minute after it was asked to, and shed_memory is off: "
                        "leaving it paused", level=4)
                    t.freeze_asked, t.thaw_failed = None, True  # not asked again: it would only fail again
                self.save(t)
                self.broadcast(t, now)
            elif now - asked >= 15 and not retried:
                t.freeze_asked = (wanted, asked, True)  # once more, not on every tick
                try:
                    proc = self.runner_of(t).freeze(t, wanted)
                    if proc is not None:
                        self.helpers.append((proc, now + 30))
                except OSError:
                    pass

    def thaw_all(self, now: float) -> None:
        """Before the daemon stops: nothing would resume a paused build while it is gone. Nothing
        will see these thaws take either, so the pauses end, for the record, as they are sent."""
        for t in self.active():
            if t.frozen and not t.flock:
                if not (t.freeze_asked and not t.freeze_asked[0]):
                    self.freeze(t, False, now, "the daemon is stopping")
                else:  # a thaw pending: send it once more, the last chance before the daemon goes; its minute stays
                    try:
                        proc = self.runner_of(t).freeze(t, False)
                        if proc is not None:
                            self.helpers.append((proc, now + 30))
                    except OSError:
                        pass
                if t.frozen_at:
                    t.frozen_attempt += max(0.0, now - t.frozen_at)
                t.frozen_at = None  # the pause ends here for the record; the thaw stays asked for until seen
                self.save(t)

    def shed(self, t: Ticket, now: float, why: str | None = None) -> None:
        """Stop a build (to free its memory, or `why`); check() queues it again once it is gone, if it may be."""
        avail = common.fmt_size(self.machine.mem_available)
        stopped = f"stopped ({why})" if why else f"stopped to free memory (only {avail} was free)"
        if not (t.restartable or commands.restart_safe(t.argv, self.cfg.restartable_scripts)):
            t.shed_error = (f"{stopped}. It isn't started again by itself, as it might repeat what it had done (a "
                            "script, a push): run it again once the machine has room, or submit it with "
                            "--restartable if starting over is safe (or list the script in the machine's "
                            "restartable_scripts)")
        elif t.env is None:
            t.shed_error = (f"{stopped}. Its environment is gone since the daemon restarted, so it isn't queued "
                            "again: run it again")
        elif t.requeues - t.ci_stops >= 3:  # (stops for a CI job's build don't count: those are limited apart)
            t.shed_error = f"{stopped} for the fourth time; run it again later"
        if t.frozen and t.frozen_at:  # its pause ends here: it is being killed
            t.frozen_attempt += max(0.0, now - t.frozen_at)
        t.frozen, t.frozen_at, t.freeze_asked = False, None, None
        t.requeue_pending = True
        self.stop(t, now, force=True)  # SIGKILL reaches frozen processes too
        self.save(t)
        self.broadcast(t, now)
        cause = why or (f"to free memory: only {avail} is free (a quarter of the floor is "
                        f"{common.fmt_size(self.cfg.mem_floor / 4)})")
        common.log(f"stopping {t.id} ({common.fmt_size(t.mem_now)}) {cause}{'' if t.shed_error else '; it will be queued again'}",
            level=4)

    CI_STOPS_MAX = 2  # times one build may be stopped to make room for CI jobs' builds

    ROOM_PAUSE = 120.0  # seconds after making room for a CI job's build, while it is queued, before more is made

    def was_overdue(self, t: Ticket) -> bool:
        """It went before CI jobs' builds when it started: it had waited three times starve_seconds,
        or it had been stopped once, and its lane doesn't pass held room (see queue)."""
        if self.lane(t.lane).may_pass_held:
            return False  # never overdue there: it gets no head start, and none to protect
        return t.requeues > 0 or (t.started is not None and t.started - t.submitted >= 3 * self.cfg.starve_seconds)

    def make_room_for_ci(self, now: float) -> None:
        """A CI job's build that has waited make_room_for_ci for memory that builds of ours hold: stop
        the newest of the builds that can start over safely (a compile or test run, or --restartable),
        of its priority or lower and not a CI job's, whose memory covers what it lacks, else the newest
        few that do together, and queue them again (see requeue). Each yields to it (doesn't start,
        whatever its lane) until it has started, or for YIELD_MAX, then goes first, as a build stopped
        to free memory does. What it lacks is worked out again after the pass, with the builds the pass
        started. None is stopped if all of them together wouldn't make it fit, nor one stopped
        CI_STOPS_MAX times already, nor any for a CI job's build in a lane that passes held room (it
        holds none, so what is freed could go elsewhere). One CI job's build at a time: not while
        builds stopped for the last are still going, nor within ROOM_PAUSE of making room for one
        that is still queued."""
        wait = self.cfg.make_room_for_ci
        self.partly_made = {k for k in self.partly_made if k in self.tickets and self.tickets[k].state == "queued"}
        self.room_made = {k: v for k, v in self.room_made.items()
                          if k in self.tickets and self.tickets[k].state == "queued"}
        if not wait or self.stopping or self.paused is not None:
            return
        if any(t.requeue_pending and t.yields_to for t in self.active()):
            return  # builds stopped for a CI job's build are still going: their memory isn't free yet
        if any(now - made < self.ROOM_PAUSE for made in self.room_made.values()):
            return  # room was just made for one that is still queued: it starts first (what it frees may lag)
        if any(t.cancel_requested and not t.flock for t in self.active()):
            return  # a cancelled build (a CI job past its time limit, say) is giving its memory back: wait for it
        running = self.active()
        free_now = None
        for ci in self.queue(now):
            if not (ci.from_ci and ci.short_by > 0 and now - ci.submitted >= wait
                    and not self.lane(ci.lane).may_pass_held  # it holds no room: what is freed could go elsewhere
                    ):
                continue
            if ci.short_agents > 0 and not self.cfg.make_room_behind_agents:
                continue  # it waits partly behind agents' builds that have waited longer: they go first (#38)
            # What it lacks now, after the builds this pass started (one in a lane that passes held
            # room may have taken what was free), with the room still held ahead of it.
            if free_now is None:
                # Read again rather than this tick's sample (only once a CI build is due): a build that
                # ended in check() since has given its memory back, and stopping one for it would be
                # for nothing.
                read = getattr(self.machine, "read_available", None)
                available = read() if read is not None else self.machine.mem_available
                free_now = available - sum(self.still_to_grow(t, now) for t in running) - self.cfg.mem_floor
            short = ci.est_mem - (free_now - ci.short_hm)
            if short <= 0:
                continue  # it fits: it starts on the next pass
            weight = self.weight(ci)
            spare = self.cfg.make_room_spare_after
            protect = not self.cfg.make_room_behind_agents
            builds = [t for t in running if not t.flock and t.state == "running" and not t.from_ci
                      and not t.cancel_requested and not t.requeue_pending and t.started is not None
                      and self.weight(t) <= weight and t.ci_stops < self.CI_STOPS_MAX
                      and (t.restartable or commands.restart_safe(t.argv, self.cfg.restartable_scripts)) and t.env is not None
                      and t.requeues - t.ci_stops < 3
                      and not (spare and now - t.started - self.paused_for(t, now) >= spare)  # far along: spared
                      and not (protect and self.was_overdue(t))]  # its turn came first (#38)
            gives = {t.id: t.mem_now + self.still_to_grow(t, now) for t in builds}  # what admission sets aside
            newest = sorted(builds, key=lambda t: t.started, reverse=True)
            chosen = next(([t] for t in newest if gives[t.id] >= short), None)
            if chosen is None:
                chosen, total = [], 0.0
                for t in newest:
                    chosen.append(t)
                    total += gives[t.id]
                    if total >= short:
                        break
                else:
                    if not (chosen and self.ci_due(ci, now)) or ci.id in self.partly_made or total < short / 2:
                        continue  # stopping every one of them wouldn't make it fit (or, past its deadline,
                        # wouldn't free half of it: the rest is held by builds that can't be stopped)
                    # Past its deadline: stop them all the same. What they free is held for it (its
                    # room binds every lane, and they yield to it), so it starts as soon as the rest
                    # frees, rather than when its job is cancelled. Once only: if what they freed went
                    # elsewhere (a lane that passes held room), stopping more would lose work for nothing.
                    self.partly_made.add(ci.id)
            self.room_made[ci.id] = now
            waited = f"{(now - ci.submitted) / 60:.0f} min"
            for t in chosen:  # (counted in ci_stops once it is gone: it may yet finish by itself)
                t.yields_to, t.yield_until = ci.id, now + self.YIELD_MAX
                self.shed(t, now, why=f"to make room for a CI job's build that has waited {waited} for "
                                      f"{common.fmt_size(short)} more memory: {ci.label[:60]} ({ci.id[:8]})")
            return

    # ---- admission

    def disk_short(self, t: Ticket) -> tuple[int, int, int, str] | None:
        """(free, floor, size, path) of the filesystem where a build would write that is furthest
        under its floor, or None if every one has its floor free. Those are its own (directory,
        CARGO_TARGET_DIR, TMPDIR), buildq's state directory (its log and ticket go there), and, for
        an image build (docker, podman, buildah), the container storage that exists. The floor is
        disk_floor (a size) or, given as a percentage, disk_floor_share of that filesystem's own
        size. A path whose filesystem can't be read is skipped. The floor gates starts only: builds
        already running can still write below it."""
        paths = [*(t.disk_paths or [t.cwd]), self.state_dir]
        remote = str((t.env or {}).get("DOCKER_HOST") or (t.env or {}).get("CONTAINER_HOST") or "")
        if tools.uses_container_storage(commands._tool_and_args(t.argv)[0]) and not remote.startswith(("tcp:", "ssh:")):
            for path in self.cfg.container_storage:  # (a build on a remote engine writes there, not here)
                try:
                    os.stat(path)
                except FileNotFoundError:
                    continue  # (not on this machine)
                except OSError as e:
                    if path not in self.storage_unreadable:  # once
                        self.storage_unreadable.add(path)
                        common.log(f"can't read container storage at {path} ({e}); name a readable directory on "
                            "that filesystem in container_storage, or image builds aren't held to disk_floor there")
                    continue
                paths.append(path)
        worst, devices = None, set()
        for path in paths:
            device = machine.fs_device(path)  # None: unreadable (disk_space says what it can)
            if device is not None:
                if device in devices:
                    continue  # that filesystem is counted already
                devices.add(device)
            space = machine.disk_space(path)
            if space is None:
                continue
            free, size = space
            floor = max(self.cfg.disk_floor, int(self.cfg.disk_floor_share * size))
            if free < floor and (worst is None or free - floor < worst[0] - worst[1]):
                worst = (free, floor, size, path)
        return worst

    def add_wait(self, t: Ticket, now: float) -> None:
        """Count the time since the last pass as waited for the reason the ticket had then."""
        if t.wait_mark is not None and now > t.wait_mark:
            reason = t.reason_cat or "new"
            t.waited_by[reason] = round(t.waited_by.get(reason, 0.0) + (now - t.wait_mark), 1)
        t.wait_mark = now

    def save_queued(self, now: float) -> None:
        """At shutdown: queued tickets' waits so far, by reason, so the next daemon goes on from them,
        and every running build's latest state (its peak, its past-the-peak record)."""
        for t in list(self.tickets.values()):
            if t.state == "queued":
                self.add_wait(t, now)
            if t.state not in FINISHED and not t.flock:
                self.save(t)

    def still_to_grow(self, t: Ticket, now: float) -> int:
        """What a running build may still grow into: its expected peak less what it holds now. With
        release_after_peak = true, a build released past its peak (past_peak_hold) is held to what its
        command's runs used after their peaks instead. In "shadow" mode the same is worked out and
        recorded, but the full room stays held; a decision made in shadow mode never takes effect,
        even if the mode is switched on later."""
        full = max(0, t.est_mem - t.mem_now)
        if not self.cfg.release_after_peak and t.release_at is not None and not t.release_revoked:
            t.release_revoked, t.release_end = True, round(max(0.0, now - (t.started or now)), 1)  # switched off
            self.save(t)
        if not self.cfg.release_after_peak or t.flock or t.state != "running":
            return full
        hold = self.past_peak_hold(t, now)
        if hold is None or self.cfg.release_after_peak is not True or not t.released:  # ("shadow")
            return full
        return max(0, min(t.est_mem, hold) - t.mem_now)

    def past_peak_hold(self, t: Ticket, now: float) -> int | None:
        """The memory to keep held for a build that is past its peak, or None. It is past its peak
        once all of these have held for SETTLE_SECONDS:
        - its lane allows it; it isn't a cold build or one given a size (--mem); it hasn't been
          paused or asked to pause, or gone over its MemoryHigh, this attempt; and it wasn't picked
          up after a daemon restart (any of these later takes a release back);
        - History.after_peak has its command's runs (the runs behind the estimate, all successful);
        - it has run 1.25 times the latest of their peaks and a minute more, and used 1.25 times
          the most CPU-seconds any of them had used by its peak (so a build on fewer cores, slower
          to its peak, waits for it); and its own peak was a minute or more ago;
        - its peak reached 0.8 of the peak behind the estimate, on no more cores than that had;
        - what it uses now, swap included, is at most 0.7 of its peak and at most what those runs
          used after their peaks, times 1.1 (the hold).
        Once released, a build that uses more than its hold is taken back for the rest of the
        attempt (measure() notices on every sample)."""
        if t.observed_gap and t.cpu_usec_base is None and t.release_at is not None and not t.release_revoked:
            return None  # picked up after a restart: the first sample says first whether it went over meanwhile
        if (not self.could_release(t) or t.observed_gap or t.frozen or t.freeze_asked is not None
                or t.frozen_attempt > 0 or t.mem_high > 0 or t.mem_peak <= 0 or not t.started
                or t.oom_kills > t.release_oom_base or (t.release_at is None and t.oom_kills > 0)):
            t.settled_since = None
            if t.release_at is not None and not t.release_revoked:
                t.release_revoked, t.release_end = True, round(max(0.0, now - t.started), 1)
                t.release_breach = t.oom_kills > t.release_oom_base
                self.save(t)
                if t.released:
                    common.log(f"{t.id} was paused, restarted, slowed at its MemoryHigh or opted out after its peak; "
                        f"holding its full {common.fmt_size(t.est_mem)} again", level=4)
            return None
        if t.release_at is not None:
            return None if t.release_revoked else t.release_hold
        after = self.history.after_peak(t.key, self.cfg, now)
        if after is None:
            t.settled_since = None
            return None
        latest, tail, top_peak, top_cores, peak_cpu = after
        hold = int(tail * 1.1)
        used = t.mem_now + t.swap_now
        elapsed = now - t.started
        cpu = max(0, t.cpu_usec - (t.cpu_usec_base or 0)) / 1e6
        if not (elapsed >= latest * 1.25 + 60 and cpu >= peak_cpu * 1.25 and elapsed - t.peak_at >= 60
                and t.mem_peak >= 0.8 * top_peak
                and (not top_cores or (t.granted <= top_cores and (t.peak_mem_cores or t.granted) <= top_cores))
                and used <= 0.7 * t.mem_peak and used <= hold and hold < t.est_mem):
            t.settled_since = None
            return None
        if t.settled_since is None:
            t.settled_since = now
        if now - t.settled_since < self.SETTLE_SECONDS:
            return None
        t.release_at, t.release_hold, t.after_release_peak = round(elapsed, 1), hold, used
        t.release_est, t.release_oom_base = t.est_mem, t.oom_kills
        if self.cfg.release_after_peak is True:
            t.released = True
            common.log(f"{t.id} is past its peak ({common.fmt_size(t.mem_peak)}, {elapsed:.0f}s in); holding "
                f"{common.fmt_size(hold)} for it instead of {common.fmt_size(t.est_mem)}")
        self.save(t)  # so a restart knows a decision was made
        return hold

    def could_release(self, t: Ticket) -> bool:
        """What rules a build out of "past the peak" from the start: its lane opts out, it is a cold
        build, or its caller gave its size."""
        return self.lane(t.lane).release_after_peak and not t.cold and not t.mem_given

    def close_release_audit(self, t: Ticket, elapsed: float) -> None:
        """At the end of an attempt (it finished, or was stopped to be queued again), add up what
        "past the peak" said about it, so a requeue doesn't lose it:
        - picked up after a daemon restart: unknown, if it had a decision or its command could have
          had one (part of it ran unwatched);
        - past its peak: went over what would be kept (a sample over it, or killed for memory after
          the decision, which a sample may have missed), and by how much at most; and the held room
          it would have freed, from the decision until it went over or ended, against the
          reservation it had at the decision."""
        kills = max(t.oom_kills, t.kills_seen)
        if t.observed_gap:
            # A daemon restart left it unwatched for a while. A decision taken back before that is
            # counted as usual, and so is a breach seen after it; a decision that stood across the
            # gap, or none where its command could have had one, says nothing.
            if t.release_at is None:
                after = self.history.after_peak(t.key, self.cfg, time.time()) if self.cfg.release_after_peak else None
                if (after is not None and t.measured and self.could_release(t) and t.mem_high == 0
                        and t.frozen_attempt == 0 and int(after[1] * 1.1) < t.est_mem
                        and (not after[3] or t.granted <= after[3])
                        and elapsed >= after[0] * 1.25 + 60 + self.SETTLE_SECONDS):
                    t.past_peak_unknown += 1  # it could have had a decision while nobody watched
                return
            if t.release_open_at_gap and not t.release_breach:
                # Killed for memory, the decision standing when this daemon loaded it, and no sample
                # since (its cgroup was gone first: a kill in the result, or systemd's verdict): the
                # kill came while the decision stood. Anything else, nobody can say.
                killed = kills > t.release_oom_base or (t.oom and not t.timed_out and not t.cancel_requested)
                if killed and t.release_stood_at_load and t.cpu_usec_base is None:
                    t.release_revoked = t.release_breach = True
                else:
                    t.past_peak_unknown += 1
                    return
        if t.release_at is None:
            return
        t.past_peak_attempts += 1
        t.past_peak_released += int(t.released)
        over = t.after_release_peak - t.release_hold  # (followed while the decision stood, and after a breach)
        if not (t.release_revoked and not t.release_breach):  # taken back for another reason: what came after isn't the rule's
            killed = kills > t.release_oom_base or (t.oom and not t.timed_out and not t.cancel_requested)
            if killed:
                lane = self.lane(t.lane)
                own = t.kills_own_cap and lane.mem_cap and self._runner_enforces_mem_cap(self.runner_of(t))
                # At its own cap it used that much; killed from outside (the slice's limit, oomd), it
                # may not have grown at all, so count what was let go.
                size = (lane.mem_cap if own else (t.release_est or t.est_mem)) - t.release_hold
                over = max(over, size, 1)
                t.release_breach = True
                if t.release_end is None:
                    t.release_end = round(elapsed, 1)
        if over > 0:
            t.past_peak_over += 1
            t.past_peak_most_over = max(t.past_peak_most_over, over)
        until = t.release_end if t.release_end is not None else elapsed
        t.past_peak_freed += max(0, (t.release_est or t.est_mem) - t.release_hold) * max(0.0, until - t.release_at)

    SETTLE_SECONDS = 30

    def schedule(self, now: float) -> None:
        if self.stopping:
            return
        cfg, m = self.cfg, self.machine
        for t in self.tickets.values():
            if t.state == "queued":
                self.add_wait(t, now)
        running = self.active()
        for t in running:
            t.growth_now = self.still_to_grow(t, now)
        growth = sum(t.growth_now for t in running)
        ramping = sum(max(0.0, t.est_cores - t.cores_now) for t in running
                      if t.started and now - t.started < cfg.ramp_seconds)
        # Cores that resizable builds could hand back, down to their minimum.
        shrinkable = sum(max(0, t.granted - t.need) for t in running if t.elastic and not t.rigid)
        ours_now = sum(t.mem_now for t in running if not t.flock)
        free_mem = m.mem_available - growth - cfg.mem_floor
        cpu_cap = cfg.cpu_target * m.ncpu
        free_cpu = cpu_cap - m.busy - ramping
        self.budget = {
            "free_mem": free_mem, "free_cores": round(free_cpu, 2), "cpu_cap": round(cpu_cap, 2),
            "shrinkable_cores": shrinkable, "growth_reserved": growth, "ramp_reserved": round(ramping, 2),
            "running": len(running),
        }
        count = len(running)
        per_lane = collections.Counter(t.lane for t in running)
        held_mem = held_cpu = 0.0
        held_n_mem = held_n_cpu = 0  # how many builds in line those are (#62)
        held_agents = 0.0  # of held_mem, the room held for builds that aren't CI jobs' (#38)
        # Room held for CI jobs' builds by ci_first, as (priority, memory, cores): it binds only
        # builds of the same priority or lower, so a higher lane can still start in room that is free.
        ci_holds: list[tuple[int, float, float]] = []
        busy_workspaces: dict[tuple[str, str], Ticket] = {}
        for t in running:
            if not t.workspace:
                continue
            # Under the group it started with, and the group the lane it started in has now (a reload
            # may have regrouped it); a lane removed since holds only the group it started with.
            groups = {t.workspace_group} if isinstance(t.workspace_group, str) and t.workspace_group else set()
            if t.group_lane in cfg.lanes and cfg.lanes[t.group_lane].exclusive:
                groups.add(cfg.lanes[t.group_lane].workspace_group)
            for group in groups:
                busy_workspaces[(group, t.workspace)] = t
        if self.paused is not None:
            why = self.paused.get("reason") or "no reason given"
            for t in self.queue(now):
                t.held, t.short_by, t.held_for, t.behind = False, 0.0, None, 0
                t.reason_cat, t.reason = "paused", f"the queue is paused ({why}); `buildq resume` starts it again"
            self.budget.update(held_mem=0, held_cores=0)
            return
        for t in self.queue(now):
            self.estimate(t, now)  # history or the configuration may have changed since it was queued
            lane = self.lane(t.lane)
            passes = lane.may_pass_held
            weight = self.weight(t)
            hm = 0.0 if passes else held_mem + sum(mem for w, mem, _ in ci_holds if w >= weight)
            hc = 0.0 if passes else held_cpu + sum(cores for w, _, cores in ci_holds if w >= weight)
            t.held = False
            t.short_by = 0.0
            t.held_for = None
            t.behind = 0
            reason, holds = None, True
            if not os.path.isdir(t.cwd):
                self.finalize(t, now, None, error=f"its working directory {t.cwd} is gone")
                continue
            short = self.disk_short(t)
            if t.est_mem > m.mem_total - cfg.mem_floor:
                reason, holds = ("too-big", f"needs {common.fmt_size(t.est_mem)} of memory, more than this machine has "
                                            f"({common.fmt_size(m.mem_total - cfg.mem_floor)} above the floor); cancel it "
                                            "or give its lane a smaller MemoryMax"), False
            elif short is not None:
                free, floor, size, where = short
                said = (f"{cfg.disk_floor_share * 100:g}% of its {common.fmt_size(size)}, {common.fmt_size(floor)}"
                        if floor > cfg.disk_floor else common.fmt_size(floor))
                reason, holds = ("disk", f"only {common.fmt_size(free)} of disk is free at {where} "
                                         f"(disk_floor is {said})"), False
            elif self.yielding(t, now):  # whatever its lane: it would take back the room it gave
                reason, holds = ("held", f"it was stopped to make room for a CI job's build ({t.yields_to[:8]}); it "
                                         "starts once that has started"), False
            elif lane.exclusive and (lane.workspace_group, t.workspace) in busy_workspaces and not (
                    # a CI job's build held by ci_first binds only builds of its priority or lower
                    (holder := busy_workspaces[(lane.workspace_group, t.workspace)]).state == "queued"
                    and not self.waited_out(holder, now) and weight > self.weight(holder)):
                what = ("is running" if holder.state != "queued" else
                        "is a CI job's, which goes first, and is held" if not self.waited_out(holder, now)
                        else "has waited longest and is held")
                reason, holds = ("workspace", f"another build in the same workspace {what} ({holder.label[:60]}, "
                                              f"{(holder.group_lane or holder.lane) if holder.state != 'queued' else self.lane(holder.lane).name} "
                                              f"lane, {holder.id[:8]}); they would "
                                              "fight over its build directory"), False
                # Kept out only by a CI job's weak hold, a build that has waited out its turn still
                # holds its own room and workspace, so builds behind it can't pass it there.
                # (Not one that couldn't fit even with every build of ours gone: that would stop
                # everything else for nothing.)
                holds = (holder.state == "queued" and not self.waited_out(holder, now) and self.waited_out(t, now)
                         and t.est_mem <= m.mem_available + ours_now - cfg.mem_floor)
            elif m.psi_mem > cfg.mem_psi_block and self.memory_scarce():
                reason = ("memory-pressure", f"memory pressure is {m.psi_mem:.0f}% (no new build above "
                                             f"{cfg.mem_psi_block:g}%)")
            elif per_lane[t.lane] >= lane.extra_jobs and count - sum(
                    min(n, self.lane(name).extra_jobs) for name, n in per_lane.items()) >= cfg.max_jobs:
                # Up to extra_jobs builds of a lane run on top of max_jobs, so a lane of quick builds
                # is never kept waiting for a turn by slow ones; past those it shares the rest.
                reason = ("max-jobs", f"{count} build{'s are' if count != 1 else ' is'} running, the most "
                                      f"allowed (max_jobs = {cfg.max_jobs})")
            elif t.est_mem > free_mem - hm:
                t.short_by = t.est_mem - (free_mem - hm)  # (unless no stopped build could free it, below)
                t.short_hm = hm
                t.short_agents = 0.0 if passes else held_agents
                if t.est_mem <= free_mem:
                    t.held_for = "memory"
                    reason = ("held", "the room it needs is held for a build that has waited longer"
                              if passes or t.est_mem > free_mem - held_mem else
                              "the room it needs is held for a CI job's build, which goes first")
                elif t.est_mem > m.mem_available + ours_now - cfg.mem_floor:
                    # It wouldn't fit even with every build of ours gone: holding room for it would
                    # stop everything else for nothing.
                    t.short_by = 0.0
                    reason, holds = ("memory", f"needs {common.fmt_size(t.est_mem)} of memory; even with no builds "
                                               f"running only {common.fmt_size(m.mem_available + ours_now - cfg.mem_floor)} "
                                               "would be free: other work holds the rest, and it starts "
                                               "when that ends"), False
                else:
                    reason = ("memory", f"needs {common.fmt_size(t.est_mem)} of memory; {common.fmt_size(max(free_mem, 0))} is "
                                        f"free after the {common.fmt_size(cfg.mem_floor)} floor and what running builds may "
                                        "still grow into")
            elif count > 0 and lane.waits_for_cpu and m.psi_cpu > cfg.cpu_psi_block:
                reason = ("cpu-pressure", f"CPU pressure is {m.psi_cpu:.0f}% (no new build above "
                                          f"{cfg.cpu_psi_block:g}% while one runs)")
            elif count > 0 and lane.waits_for_cpu and t.need > free_cpu + shrinkable - hc:
                if t.need <= free_cpu + shrinkable:
                    t.held_for = "cores"
                    reason = ("held", "the cores it needs are held for a build that has waited longer"
                              if passes or t.need > free_cpu + shrinkable - held_cpu else
                              "the cores it needs are held for a CI job's build, which goes first")
                else:
                    reason = ("cpu", f"needs {t.need} core{'s' if t.need != 1 else ''}; "
                                     f"{max(free_cpu + shrinkable, 0):.1f} of {cpu_cap:.1f} are free "
                                     f"({m.busy:.1f} busy, {ramping:.1f} expected from builds just started)")
            if reason is None:
                room = math.floor(max(free_cpu if count else cpu_cap - m.busy, 0))
                granted = int(max(t.need, min(t.want, room)))
                while granted > t.need and self.mem_for(t, granted) > free_mem - hm:
                    granted -= 1  # more cores would need more memory than is free
                t.est_mem = self.mem_for(t, granted)
                self.start(t, now, granted)
                if t.state in FINISHED:
                    continue
                free_mem -= t.est_mem
                taken_from_others = max(0.0, granted - max(free_cpu, 0))
                free_cpu -= granted
                shrinkable = max(0, shrinkable - taken_from_others)
                count += 1
                per_lane[t.lane] += 1
                if lane.exclusive and t.workspace:
                    busy_workspaces[(lane.workspace_group, t.workspace)] = t
                continue
            t.reason_cat, t.reason = reason
            if t.held_for == "memory":  # (the builds in line whose room binds it, for buildq top)
                t.behind = held_n_mem + sum(1 for w, _, _ in ci_holds if w >= weight)
            elif t.held_for == "cores":
                t.behind = held_n_cpu + sum(1 for w, _, c in ci_holds if w >= weight and c > 0)
            if holds and not passes and self.starving(t, now):
                t.held = True
                cores = t.need if lane.waits_for_cpu else 0  # it would start without that room
                if self.waited_out(t, now) or self.ci_due(t, now):
                    held_mem += t.est_mem
                    held_cpu += cores
                    held_n_mem += 1
                    held_n_cpu += 1 if cores else 0
                    if not t.from_ci:
                        held_agents += t.est_mem
                else:  # a CI job's build, first by ci_first: a higher lane may still use the room
                    ci_holds.append((weight, t.est_mem, cores))
                if lane.exclusive and t.workspace:  # and its workspace: a quicker lane mustn't keep taking it
                    key = (lane.workspace_group, t.workspace)
                    current = busy_workspaces.get(key)
                    if current is None or (current.state == "queued" and not self.waited_out(current, now)
                                           and self.waited_out(t, now)):  # a full hold over a CI job's weak one
                        busy_workspaces[key] = t
        self.budget.update(held_mem=held_mem + sum(mem for _, mem, _ in ci_holds),
                           held_cores=held_cpu + sum(cores for _, _, cores in ci_holds))

    def build_env(self, t: Ticket, caller_env: dict | None = None) -> dict:
        """Parallelism for the build's tools, unless the caller chose its own."""
        args = commands.strip_wrappers(t.argv)
        tool = os.path.basename(args[0]) if args else ""
        return tools.env_for(tool, args[1:], t.granted, t.want, t.jobserver,
                             self.lane(t.lane).test_threads, caller_env)

    def start(self, t: Ticket, now: float, granted: int) -> None:
        lane = self.lane(t.lane)
        self.add_wait(t, now)
        t.wait_mark = None
        t.state = "starting"
        t.started = now
        t.wait_total += max(0.0, now - (t.queued_at or t.submitted))
        t.runner = self.runner.name
        t.oom_base = 0
        t.reason = t.reason_cat = ""
        t.yields_to = t.yield_until = None  # running again: a later stop for memory isn't one for that CI job's build
        t.granted = t.max_granted = granted
        t.growth_now = t.est_mem  # (all of its plan, until the queue next works out what it uses)
        t.start_plan, t.start_cores, t.outgrown_at, t.breached_at = t.est_mem, granted, None, None
        t.start_rule, t.ended_rule = self.cfg.scale_mem_with_cores, None
        t.start_base, t.start_base_cores, t.start_rigid = t.mem_base, t.mem_cores, t.rigid
        t.workspace_group = lane.workspace_group if lane.exclusive else ""
        t.group_lane = lane.name  # (the lane it really starts under: a reload may have removed t.lane)
        env_path = os.path.join(t.dir, "env.json")
        try:
            env = common.read_json(env_path)
            if not isinstance(env, dict):
                raise OSError("its environment file is missing")
            stripped = commands.strip_wrappers(t.argv)
            tool = os.path.basename((stripped or ["?"])[0])
            known = tools.uses_jobserver(tool)
            wanted = lane.jobserver == "all" or (lane.jobserver == "tools" and known)
            if wanted and tool in ("make", "gmake") and self.runner.name in ("systemd", "cgroup", "direct"):
                if not _make_supports_fifo(stripped[0], env.get("PATH")):
                    wanted = False
            # An inherited jobserver passed by descriptor is dead in the build's service: drop it,
            # and give the build buildq's, unless the caller still brings make flags of its own.
            cleaned = {k: c for k in MAKE_FLAG_VARS if k in env
                       for c in [commands.without_fd_jobserver(str(env[k]))] if c is not None}
            if cleaned:
                common.log(f"{t.id}: dropped the caller's jobserver ({', '.join(sorted(cleaned))}): its descriptors "
                    "don't reach the build")
            # The caller's own choice still wins: flags it set, or, where its dead jobserver was
            # dropped, a -j or jobserver of its own left in their options (an assignment after the
            # `--`, such as SUBMAKE=make\ -j4, is a variable's value, not make's own setting).
            callers_own = any(k in env and str(env[k]).strip()
                              and (k not in cleaned or commands.callers_jobs(commands.split_make_flags(cleaned[k])[0]))
                              for k in MAKE_FLAG_VARS)  # (an empty one is no choice at all)
            t.elastic = False
            if wanted and not t.rigid and not callers_own:
                t.elastic = known  # it speaks the protocol even before it takes a token
                t.jobserver = os.path.join(t.dir, "jobserver")
                t.js = Jobserver(t.jobserver, create=True)
                t.js.resize(granted - 1)  # every client of a jobserver has one implicit token
                t.tokens_out = t.js.out
            tool_at = len(t.argv) - len(stripped)  # (a wrapper's own words stay: env -u -j make)
            t.exec_argv = tools.rewrite_argv_for(tool, t.argv, tool_at, granted, t.jobserver,
                                                 lane.test_threads, env)
            # buildq's settings go in a file of their own, so a build that is queued again after a
            # restart gets fresh ones. Pass cleaned make flags plus any inline wrapper assignments
            # (`nice -n 10 env GRADLE_OPTS=-Xmx2g gradle test`), unsets (`env -u GRADLE_OPTS ...`),
            # or env resets (`env -i ...` / `env -- - ...`) so merged defaults survive wrappers.
            inline_idx, reset_at, unset_at = commands.wrapper_env_sites(t.argv, tool_at)
            if reset_at is not None:
                cleaned_env = {k: t.argv[idx].split("=", 1)[1] for k, idx in inline_idx.items()}
            else:
                cleaned_env = {**env, **cleaned} if cleaned else dict(env)
                for k in unset_at:
                    cleaned_env.pop(k, None)
                for k, idx in inline_idx.items():
                    cleaned_env[k] = t.argv[idx].split("=", 1)[1]
            ours, merged = tools.env_with_merged(tool, stripped[1:], t.granted, t.want, t.jobserver,
                                                 lane.test_threads, cleaned_env)
            direct_spec = tools.lookup(tool)
            direct_merged = set(direct_spec.merge_env_vars) if direct_spec else set()
            insertions: dict[int, list[str]] = {}
            for k in sorted(merged):
                if k not in ours:
                    continue
                if k in inline_idx:
                    argv_out = list(t.exec_argv) if t.exec_argv is not None else list(t.argv)
                    argv_out[inline_idx[k]] = f"{k}={ours[k]}"
                    t.exec_argv = argv_out
                elif k in direct_merged:
                    site = unset_at.get(k, reset_at)
                    if site is not None:
                        insertions.setdefault(site, []).append(f"{k}={ours[k]}")
            if insertions:
                argv_out = list(t.exec_argv) if t.exec_argv is not None else list(t.argv)
                for site in sorted(insertions, reverse=True):
                    argv_out[site:site] = insertions[site]
                t.exec_argv = argv_out
            # The caller's own settings win, except keys explicitly merged by the emitting tool
            # callback and empty make variables (a make recipe exports MAKEFLAGS= and MFLAGS=).
            given = {k for k in env
                     if k not in merged and (k not in MAKE_FLAG_VARS or str(env[k]).strip())}
            parallel = {k: v for k, v in ours.items() if k not in given}
            for k, rest in cleaned.items():
                if k in merged:
                    parallel[k] = ours[k]  # merger already folded `rest` via cleaned_env
                    continue
                # The caller's make ran in parallel (it had a jobserver): a make or ninja build does
                # too, on buildq's. (cargo hands its own to build scripts through CARGO_MAKEFLAGS.)
                mine = ours.get(k) or (tools.jobserver_flags(t.want, t.jobserver)
                                       if t.jobserver and k != "CARGO_MAKEFLAGS"
                                       and tools.uses_makeflags_jobserver(tool) else "")
                parallel[k] = commands.with_make_flags(rest, mine)
            common.write_json(os.path.join(t.dir, "parallel.json"), parallel)
            t.nice = None
            if self.runner.name == "direct":
                t.nice = runners.lane_nice(lane)
            elif self.runner.name == "cgroup":
                _, hints = runners.translate_cgroup_properties(lane, self.machine.mem_total or 1)
                t.nice = hints.get("nice")
            self.runner.prepare(t, self.cfg.slice)
            self.save(t)  # with its service's name, before it exists
            t.launcher, running = self.runner.launch(t, lane, self.cfg.slice)
        except OSError as e:
            self.finalize(t, now, None, error=f"couldn't start the build: {e}")
            return
        if running:
            t.state = "running"
            t.running_since = now
        self.save(t)
        self.broadcast(t, now)
        if t.dedupe:
            self.check_source(t, "start", now)
        common.log(f"started {t.id} [{t.lane}] with {granted} core{'s' if granted != 1 else ''} (wants {t.want}"
            f"{', jobserver' if t.jobserver else ''}) after {now - t.submitted:.0f}s ({self.runner.name}): {t.label}")

    # ---- resizing builds that speak the jobserver protocol

    def rebalance(self, now: float) -> None:
        """Every few seconds, hand idle cores to builds that would use them, and take cores back
        when the machine is over its target or a queued build is waiting for cores. A CI job's
        build comes first both ways (#45): it has a time limit and holds up a release, and agents'
        builds don't. It gets idle cores first, it is cut last, and one running below its lane's
        usual cores, using all it has, takes cores from agents' builds above their minimum."""
        m, cfg = self.machine, self.cfg
        for t in self.active():
            if t.js is not None and not t.elastic:
                if (t.cg and machine.fifo_in_use(t.cg, t.jobserver)) or (
                    not t.cg and t._pids and machine.fifo_in_use_pids(t._pids, t.jobserver)
                ):
                    t.elastic = t.js.used = True
        elastic = [t for t in self.active() if t.js is not None and t.elastic and t.state == "running"
                   and not t.cancel_requested]
        if not elastic:
            return
        ramping = sum(max(0.0, t.est_cores - t.cores_now) for t in self.active()
                      if t.started and now - t.started < cfg.ramp_seconds and t not in elastic)
        cpu_cap = cfg.cpu_target * m.ncpu
        queued = self.queue(now)
        waiting = next((t for t in queued if t.reason_cat == "cpu" or t.held_for == "cores"), None)
        spare = cpu_cap - max(m.busy, m.busy_now) - ramping - (waiting.need if waiting else 0)
        free_mem = m.mem_available - sum(self.still_to_grow(t, now) for t in self.active()) - cfg.mem_floor
        changed = []
        idle_grown = 0  # cores handed out from measured idle ones this pass
        waiting_for_memory = any(t.reason_cat == "memory" for t in queued)
        if spare >= 1 and free_mem > 0 and not waiting_for_memory:
            growers = sorted((t for t in elastic if t.granted < t.want and t.cores_now >= 0.75 * t.granted
                              and (t.release_at is None or t.release_revoked)),
                             key=lambda t: (not t.from_ci, t.granted / max(t.want, 1)))
            for t in growers:
                add = int(min(t.want - t.granted, math.floor(spare)))
                while add >= 1 and self.mem_for(t, t.granted + add) - t.est_mem > free_mem:
                    add -= 1  # the memory more cores would need isn't free
                if add >= 1:
                    new_mem = max(t.est_mem, self.mem_for(t, t.granted + add))
                    free_mem -= new_mem - t.est_mem
                    t.est_mem = new_mem
                    t.granted += add
                    t.max_granted = max(t.max_granted, t.granted)
                    spare -= add
                    idle_grown += add
                    changed.append(t)
        elif spare <= -1:
            deficit = math.ceil(-spare)
            for t in sorted(elastic, key=lambda t: (t.from_ci, -(t.granted - t.need))):
                cut = min(t.granted - t.need, deficit)
                if cut >= 1:
                    t.granted -= cut
                    deficit -= cut
                    changed.append(t)
                if deficit <= 0:
                    break
        # A CI job's build below its lane's usual cores, and using all it has: cores move to it from
        # agents' builds above their minimum (the most above first), up to those usual cores (more
        # than that only from idle cores, above), as far as the memory more cores need is free.
        # Nothing changes in all: the machine stays within its target.
        def usual(t):
            return min(t.want, self.lane(t.lane).cores)
        for ci in sorted((t for t in elastic if t.from_ci and t.granted < usual(t)
                          and t.cores_now >= 0.75 * t.granted
                          and (t.release_at is None or t.release_revoked)),  # (its hold let go: as growth)
                         key=lambda t: t.granted / max(usual(t), 1)):
            for donor in sorted((t for t in elastic if not t.from_ci and t.granted > t.need),
                                key=lambda t: t.granted - t.need, reverse=True):
                give = min(donor.granted - donor.need, usual(ci) - ci.granted)
                room = 0 if waiting_for_memory else free_mem  # (none taken from a build waiting for memory)
                while give >= 1 and self.mem_for(ci, ci.granted + give) - ci.est_mem > room:
                    give -= 1  # the memory more cores would need isn't free
                if give < 1:
                    break
                new_mem = max(ci.est_mem, self.mem_for(ci, ci.granted + give))
                free_mem -= new_mem - ci.est_mem
                ci.est_mem = new_mem
                donor.granted -= give
                ci.granted += give
                ci.max_granted = max(ci.max_granted, ci.granted)
                changed += [t for t in (donor, ci) if t not in changed]
                if ci.granted >= usual(ci):
                    break
        # Tokens: shrink first, then grow (a CI job's build first) only within what the shrinking gave
        # back. A build whose grant was cut gives its tokens back only as its jobs end, so tokens
        # moved to another build are issued as they come back, never before: the builds together
        # never hold more tokens than their grants.
        # The count each ticket records (tokens_out, what a restart puts back) is never below what is
        # out: a shrink is recorded after its tokens come back, a grower's before its tokens go out,
        # so a crash between two saves leaves a count high (a token lost to that build), never low
        # (a token issued twice).
        before = {t.id: t.js.out for t in elastic}
        for t in elastic:
            if t.js.out > t.granted - 1:
                t.js.resize(t.granted - 1)
                if t.js.out != before[t.id]:
                    t.tokens_out = t.js.out
                    self.save(t)
        # What may go out: what the grants together allow (grants less what is out, tokens held over
        # a cut grant counted against it), or what idle cores paid for this pass if that is more,
        # not both (the grants already include it). The idle cores were measured with every running
        # job counted, those of a build still over its cut grant included, so those needn't wait.
        slack = max(sum(t.granted - 1 for t in elastic) - sum(t.js.out for t in elastic), idle_grown)
        for t in sorted(elastic, key=lambda t: (not t.from_ci, t.granted / max(t.want, 1))):
            if t.js.out < t.granted - 1 and slack > 0:
                was, target = t.js.out, min(t.granted - 1, t.js.out + slack)
                t.tokens_out = target
                if not self.save(t):  # its count isn't on disk: a restart could issue these twice
                    t.tokens_out = was
                    continue
                t.js.resize(target)
                slack -= t.js.out - was
        for t in elastic:
            if t.tokens_out != t.js.out or t in changed:
                t.tokens_out = t.js.out
                self.save(t)
        for t in changed:
            common.log(f"resized {t.id} to {t.granted} cores")

    # ---- keeping all builds together inside what the machine has free

    def memory_scarce(self) -> bool:
        """Whether the machine's available memory is low enough that system-wide memory pressure
        (PSI full) reflects a real shortage rather than one cgroup slowed at its own MemoryHigh or
        routine zram swap-in while tens of gigabytes are free."""
        m = self.machine
        if not m.mem_total:
            return True
        return m.mem_available < max(2 * self.cfg.mem_floor, m.mem_total // 4)

    def memory_moving(self) -> bool:
        """Memory is short, under real machine-wide pressure, or has fallen a gigabyte since the
        slice was last guarded: other work can outgrow a guard every REBALANCE_SECONDS, so it is
        guarded every tick (a second) until things settle."""
        m = self.machine
        return (m.mem_available < 2 * self.cfg.mem_floor or (m.psi_mem > 1.0 and self.memory_scarce())
                or (self.guard_available is not None and self.guard_available - m.mem_available >= GIB))

    def guard_slice(self, stopping: bool = False) -> None:
        """Lower the slice's MemoryMax to what its builds hold plus what the machine has free above
        half of mem_floor, so all builds together can't take the machine below that: when other work
        grows, builds are stopped inside their slice, not elsewhere. It never goes above the limit
        the slice is configured with (the installer's 85% drop-in, or yours), and never below what
        the builds hold now (so the kernel needn't reclaim anything when it is written); when the
        daemon stops, the configured limit is put back."""
        path = self._runner_builds_cgroup(self.runner)
        if not self.cfg.slice_guard or not path:
            return
        if self.slice_limit is None:
            if self.runner.name == "systemd":
                if stopping:  # (putting the limit back as the daemon stops: no later tick to wait for)
                    self.slice_limit = machine.configured_memory_max(self.cfg.slice) or "max"
                else:
                    limit = self.query(("slice-max", self.cfg.slice), machine.configured_memory_max, self.cfg.slice)
                    if limit is PENDING:
                        return  # guarded from a later tick, once systemd has said
                    self.slice_limit = limit or "max"
            else:
                fn = getattr(self.runner, "slice_memory_max", None)
                self.slice_limit = (fn(self.cfg.slice) if fn is not None else None) or "max"
        m = self.machine
        if stopping:
            value = self.slice_limit
        else:
            try:
                with open(os.path.join(path, "memory.current")) as f:
                    held = int(f.read())
            except (OSError, ValueError):
                return
            value = held + max(2 * GIB, m.mem_available - self.cfg.mem_floor // 2)
            self.guard_available = m.mem_available
            if self.slice_limit != "max":
                value = min(value, int(self.slice_limit))
        try:
            with open(os.path.join(path, "memory.max"), "w") as f:
                f.write(str(value))
            self.guard_failed = False
        except OSError as e:
            if not self.guard_failed:
                common.log(f"can't set {self.cfg.slice}'s memory limit ({e}); builds are held only by their own caps")
            self.guard_failed = True

    MEMORY_LOG_SECONDS = 30

    MEMORY_LOG_BYTES = 1 << 20

    def watch_memory(self, tick: float) -> None:
        """While memory is tight, say every half minute how much is free and which cgroups hold the
        most, builds or not, so that if the machine stalls and has to be reset, there is a record of
        what filled it. Tight is less than twice mem_floor available, or every task on the machine
        stalled on memory at once 5% of the time (memory "full") while memory is scarce (under the
        greater of twice mem_floor or a quarter of RAM; "some" also counts one build slowed at its
        own MemoryHigh, which is normal). It is no longer tight once more than 2.5 times mem_floor
        is available and either "full" is under 1% or memory is no longer scarce, for half a minute.

        Nothing slow happens here, on the daemon's loop: memory.log is written and synced by a
        thread of its own, and the cgroups are walked by another."""
        m, floor = self.machine, self.cfg.mem_floor
        scarce = self.memory_scarce()
        tight = m.mem_available < 2 * floor or (m.psi_mem >= 5 and scarce)
        calm = m.mem_available > 2.5 * floor and (m.psi_mem < 1 or not scarce)
        if not tight and not self.memory_tight:
            return
        if calm:
            self.memory_calm_since = self.memory_calm_since or tick
            if tick - self.memory_calm_since >= self.MEMORY_LOG_SECONDS:
                self.memory_tight, self.memory_calm_since = False, None
                self.memory_note(f"memory is no longer tight: {common.fmt_size(m.mem_available)} available")
            return
        self.memory_calm_since = None
        if self.memory_tight and tick - self.last_memory_log < self.MEMORY_LOG_SECONDS:
            return
        self.memory_tight, self.last_memory_log = True, tick
        self.memory_note(f"memory is tight: {common.fmt_size(m.mem_available)} available (floor {common.fmt_size(floor)}), "
                         f"{common.fmt_size(m.swap_used)} of swap used, every task waiting on memory {m.psi_mem:.0f}% "
                         f"of the time (some task {m.psi_mem_some:.0f}%)")
        self.walk_wanted.set()  # one walk at a time: a request while one runs is the next one
        self.memory_thread("walker", self.memory_walker)

    def memory_note(self, text: str) -> None:
        """A memory-watch line: to the journal at warning priority, and queued for memory.log."""
        if self.memory_stopping:
            return
        common.log(text, level=4)
        self.memory_lines.put((time.strftime("%Y-%m-%dT%H:%M:%S%z"), text))
        self.memory_thread("writer", self.memory_writer)

    def memory_thread(self, name: str, target) -> None:
        with self.memory_thread_lock:  # the walker logs too: only one of each thread, ever at once
            thread = self.memory_threads.get(name)
            if self.memory_stopping or (thread is not None and thread.is_alive()):
                return
            try:
                thread = threading.Thread(target=target, name=f"memory-{name}", daemon=True)
                thread.start()
                self.memory_threads[name] = thread
            except RuntimeError as e:  # no threads to be had (a pids limit, say): the journal still has it
                if not self.memory_thread_failed:
                    common.log(f"can't start the memory watch's {name}: {e}")
                self.memory_thread_failed = True

    def memory_walker(self) -> None:
        while True:
            self.walk_wanted.wait()
            if self.memory_stopping:
                return
            self.walking = True  # before the request is cleared, so nobody sees it idle in between
            self.walk_wanted.clear()
            try:
                top, complete = machine.biggest_cgroups(self.cg_root, deadline=time.monotonic() + 20)
                if not top and complete:
                    # Non-systemd / cgroup-v1 / flat-cgroup fallback: top processes from /proc.
                    top, complete = machine.biggest_processes(deadline=time.monotonic() + 15)
                listed = ", ".join(f"{machine.short_cgroup(rel)}{f' ({name})' if name else ''} {common.fmt_size(held)}"
                                   f"{f' + {common.fmt_size(swap)} swap' if swap >= 256 * MIB else ''}"
                                   for held, swap, rel, name in top)
                self.memory_note(f"holding the most memory: {listed or 'nothing measurable'}"
                                 f"{'' if complete else ' (the walk was cut short; partial)'}")
            except Exception:
                if not self.memory_stopping:
                    common.log("the memory watch failed:\n" + traceback.format_exc(), level=4)
            finally:
                self.walking = False

    def memory_writer(self) -> None:
        """Append queued lines to memory.log in the state directory, syncing each batch: the journal
        can lose its last minutes when a machine is reset, which is when this record matters."""
        path = os.path.join(self.state_dir, "memory.log")
        while True:
            batch = [self.memory_lines.get()]
            while True:
                try:
                    batch.append(self.memory_lines.get_nowait())
                except queue.Empty:
                    break
            lines = [item for item in batch if item is not None]
            try:
                if lines:
                    try:
                        if os.path.getsize(path) > self.MEMORY_LOG_BYTES:
                            os.replace(path, path + ".1")
                    except OSError:
                        pass
                    new = not os.path.exists(path)
                    data = "".join(f"{stamp} {text}\n" for stamp, text in lines).encode(errors="backslashreplace")
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                    try:
                        while data:  # a short write (a full disk) is retried, or raises
                            data = data[os.write(fd, data):]
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                    if new:  # a new file's name, after a rotation too, must survive a reset as well
                        dirfd = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY)
                        try:
                            os.fsync(dirfd)
                        finally:
                            os.close(dirfd)
            except Exception as e:  # never lose the writer to one bad batch
                if not self.memory_stopping:
                    common.log(f"can't write {path}: {e!r}")
            finally:
                for _ in batch:
                    self.memory_lines.task_done()
            if len(lines) < len(batch):  # told to stop
                return

    def memory_idle(self, timeout: float = 10.0) -> bool:
        """Wait until the memory watch has nothing left to do (for tests)."""
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if not self.walk_wanted.is_set() and not self.walking and not self.memory_lines.unfinished_tasks:
                return True
            time.sleep(0.01)
        return False

    def stop_memory_watch(self, timeout: float = 2.0) -> None:
        """Stop the watch's threads before the interpreter goes, so none is caught writing."""
        with self.memory_thread_lock:
            self.memory_stopping = True
            threads = list(self.memory_threads.values())
        self.walk_wanted.set()
        self.memory_lines.put(None)
        for thread in threads:
            thread.join(timeout)
        self.metrics.stop(timeout)
