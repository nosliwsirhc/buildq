"""Unit tests for SQLite analytics (metrics.db) and GitHub commit status posting."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import BUILDQD, DaemonCase, GIB, MIB, ROOT, SOURCE  # noqa: E402
from buildq import common, config, daemon, github, machine, metrics, ticket  # noqa: E402


class Analytics(DaemonCase):
    """metrics.db: a row a minute, an hour rolled up, a row per build; and the numbers behind them."""

    def db(self):
        import sqlite3
        self.assertTrue(self.daemon.metrics.flush())
        db = sqlite3.connect(os.path.join(self.state, "metrics.db"))
        db.row_factory = sqlite3.Row
        return db

    def test_a_minute_row_says_what_builds_waited_for(self):
        minute = int(self.now // 60) * 60
        self.machine.mem_available = 30 * GIB  # 20G above the 10G floor: one 20G build fits
        builds = [self.submit() for _ in range(3)]  # 20G each: one runs, two wait on memory
        for second in range(0, 60, 2):
            self.daemon.schedule(minute + second)
            for t in builds:
                if t.state == "running":
                    t.mem_now = 6 * GIB if second < 30 else 0  # (as cgroup_stats would read it)
            self.daemon.observe_metrics(minute + second)
        self.daemon.observe_metrics(minute + 61)  # the next minute: the last one is written
        row = self.db().execute("SELECT * FROM minute WHERE t = ?", (minute,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(self.states(*builds).count("queued"), 2)
        self.assertAlmostEqual(row["wait_memory"], 2.0, places=1)  # two builds waited the whole minute
        self.assertEqual(row["running_max"], 1)
        self.assertEqual(row["mem_available_min"], 30 * GIB)
        self.assertGreater(row["reserved"], 0)
        self.assertAlmostEqual(row["builds_mem_avg"] / GIB, 3.0, delta=0.25)  # 6G for half the minute
        self.assertGreater(row["sampled"], 55)
        self.assertGreater(row["mem_wait_seconds"], 55)
        self.assertEqual(row["free_mem_mem_wait"], 20 * GIB)  # 30G available, 10G floor
        self.assertAlmostEqual(row["idle_cores_mem_wait"], 32.0, places=1)

    def test_time_waited_is_counted_by_reason_and_lands_in_the_build_row(self):
        self.machine.mem_available = 25 * GIB
        t = self.submit()
        self.daemon.schedule(self.now)
        self.daemon.schedule(self.now + 40)  # 40 seconds waiting on memory
        self.assertEqual(t.state, "queued")
        self.machine.mem_available = 100 * GIB
        self.daemon.schedule(self.now + 50)  # 10 more, then it starts
        self.assertNotEqual(t.state, "queued")
        self.assertAlmostEqual(t.waited_by.get("memory", 0), 50, delta=0.5)
        t.mem_now = t.mem_peak = 3 * GIB
        t.peak_at, t.tail, t.measured = 20.0, GIB, True
        t.trace = [[0, GIB], [20, 3 * GIB], [80, GIB]]
        self.finish(t, 0, after=150)
        row = self.db().execute("SELECT * FROM build WHERE id = ?", (t.id,)).fetchone()
        self.assertEqual(json.loads(row["waited_by"]), {"memory": 50.0})
        self.assertEqual((row["mem_peak"], row["peak_at"], row["tail"]), (3 * GIB, 20.0, GIB))
        self.assertEqual(row["est_mem"], t.est_mem)
        self.assertEqual(len(json.loads(row["profile"])), 10)
        self.assertEqual(row["state"], "succeeded")

    def test_the_plan_a_build_started_with_and_when_it_passed_it(self):
        t = self.submit()
        self.assertEqual(t.state, "running")
        plan, cores = t.est_mem, t.granted
        t.est_mem = plan + 4 * GIB  # raised since it started (it grew its cores, say)
        t.measured, t.mem_now = True, plan + GIB  # past its start plan, within the raised one
        self.daemon.outgrown(t, self.now + 90)
        self.assertEqual(t.est_mem, plan + 4 * GIB)  # nothing to re-plan
        t.mem_now = plan + 5 * GIB
        self.daemon.outgrown(t, self.now + 200)  # past the raised plan too: re-planned; the first time counts
        self.assertGreater(t.est_mem, plan + 4 * GIB)
        self.finish(t)
        row = self.db().execute("SELECT * FROM build WHERE id = ?", (t.id,)).fetchone()
        self.assertEqual((row["start_plan"], row["start_cores"], row["outgrown_at"]), (plan, cores, 90.0))
        self.assertEqual((row["breached_at"], row["breaches"]), (200.0, 1))  # past the plan it held: at 200 s, not 90
        self.assertGreater(row["est_mem"], plan)
        self.assertEqual((row["plan_base"], row["plan_cores"], row["cold"]), (t.mem_base, t.mem_cores, 0))
        self.assertEqual((row["start_rule"], row["max_cores"], row["rigid"]), ("always", cores, 0))

    def test_a_requeued_build_cancelled_before_it_runs_again_keeps_its_last_attempt(self):
        t = self.submit()
        cores, plan = t.granted, t.est_mem
        t.measured, t.mem_now = True, plan + GIB
        self.daemon.outgrown(t, self.now + 60)
        with open(os.path.join(t.dir, "ticket.json")) as f:
            self.assertEqual(json.load(f)["breached_at"], 60.0)  # saved at once (#63)
        self.daemon.requeue(t, self.now + 90)
        self.assertEqual((t.state, t.max_granted), ("queued", 0))
        t.rigid = True  # estimated again while it waits
        self.daemon.cfg.scale_mem_with_cores = "never"  # and the setting changed meanwhile
        self.machine.mem_available = GIB  # (so it doesn't start again)
        self.daemon.cancel(t, self.now + 100)
        row = self.db().execute("SELECT * FROM build WHERE id = ?", (t.id,)).fetchone()
        self.assertEqual((row["start_plan"], row["max_cores"], row["rigid"]), (plan, cores, 0))  # the attempt that ran
        self.assertEqual((row["start_rule"], row["end_rule"]), ("always", "always"))  # it ended before the change
        self.daemon.cfg.scale_mem_with_cores = "always"
        self.machine.mem_available = 200 * GIB
        u = self.submit()
        self.assertEqual(u.state, "running")
        self.daemon.cfg.scale_mem_with_cores = "never"  # changed while it runs
        self.finish(u, after=110)
        row = self.db().execute("SELECT start_rule, end_rule FROM build WHERE id = ?", (u.id,)).fetchone()
        self.assertEqual(tuple(row), ("always", "never"))  # it straddled the change

    def test_a_running_build_saved_by_an_older_daemon_keeps_its_rigidity(self):
        t = self.submit()
        rec = t.record()
        rec.pop("start_rigid")
        rec.update(rigid=True, state="running", started=self.now)
        loaded = ticket.Ticket.from_record(rec, t.dir)
        self.assertTrue(loaded.start_rigid)
        rec.update(state="queued", started=None, start_rule="always")  # queued again before the upgrade
        self.assertTrue(ticket.Ticket.from_record(rec, t.dir).start_rigid)
        rec.update(tail_used="lots", peak_swap=None, breached_at="x")  # a damaged file: numbers only
        loaded = ticket.Ticket.from_record(rec, t.dir)
        self.assertEqual((loaded.tail_used, loaded.peak_swap, loaded.breached_at), (0, 0, None))

    def test_the_start_fields_are_one_attempt_on_one_clock(self):
        t = self.submit()
        plan, base, base_cores = t.est_mem, t.mem_base, t.mem_cores
        t.frozen_attempt = 200.0  # paused 200 s of it
        t.measured, t.mem_now = True, plan + GIB
        self.daemon.outgrown(t, self.now + 300)
        self.assertEqual((t.outgrown_at, t.breached_at), (100.0, 100.0))  # 100 s of running, as attempt_wall counts
        t.mem_base, t.mem_cores = base // 2, base_cores * 2  # estimated again later (while queued again, say)
        self.finish(t)
        row = self.db().execute("SELECT * FROM build WHERE id = ?", (t.id,)).fetchone()
        self.assertEqual((row["plan_base"], row["plan_cores"]), (base, base_cores))  # the plan it started with
        u = self.submit()
        self.finish(u)
        self.assertIsNone(self.db().execute("SELECT outgrown_at FROM build WHERE id = ?", (u.id,)).fetchone()[0])

    def history_of(self, key, runs, peak_at=100.0, tail=2 * GIB, peak=18 * GIB, wall=600.0, exit=0, **more):
        for i in range(runs):
            self.daemon.history.add({"key": key, "lane": "heavy", "t": self.now - 1000 + i, "wall": wall,
                                     "exit": exit, "peak_mem": peak, "peak_cores": 4.0, "peak_at": peak_at,
                                     "tail": tail, "mem_cores": 8, "peak_cpu": 50.0, **more})

    def past_its_peak(self, mode=True):
        self.daemon.cfg.release_after_peak = mode
        t = self.submit()
        t.state, t.started, t.est_mem, t.granted = "running", self.now, 20 * GIB, 8
        t.mem_peak, t.peak_at, t.mem_now = 18 * GIB, 90.0, 2 * GIB
        t.cpu_usec, t.cpu_usec_base = 200_000_000, 0  # 200 CPU-seconds in: past the 50 its runs peaked at
        return t

    def sample(self, t, at, gib, kills=0):
        """One measurement of t at `at` seconds in, using `gib` GiB."""
        with mock.patch.object(machine, "cgroup_stats", return_value=(t.cpu_usec, int(gib * GIB), kills)), \
                mock.patch.object(machine, "cgroup_memory_waits", return_value=None), \
                mock.patch.object(machine, "cgroup_swap", return_value=0):
            t.cg = t.cg or "/nonexistent"
            t.measure(self.now + at)

    def settle_release(self, t, at):
        first = self.daemon.still_to_grow(t, self.now + at)
        return first, self.daemon.still_to_grow(t, self.now + at + 30)  # settled for half a minute

    def test_a_build_past_its_peak_holds_only_what_its_command_used_after_peaks(self):
        t = self.past_its_peak()
        self.history_of(t.key, 3)
        self.assertEqual(self.daemon.still_to_grow(t, self.now + 150), 18 * GIB)  # too early: 1.25 x 100 + 60
        first, settled = self.settle_release(t, 190)
        self.assertEqual(first, 18 * GIB)  # not yet settled for long enough
        self.assertEqual(settled, int(2 * GIB * 1.1) - 2 * GIB)
        self.assertTrue(t.released)
        self.assertEqual((t.release_at, t.release_hold), (220.0, int(2 * GIB * 1.1)))

    def test_shadow_mode_records_the_release_and_holds_everything(self):
        t = self.past_its_peak(mode="shadow")
        self.history_of(t.key, 3)
        self.assertEqual(self.settle_release(t, 190), (18 * GIB, 18 * GIB))
        self.assertEqual(t.release_hold, int(2 * GIB * 1.1))
        self.assertFalse(t.released)
        self.sample(t, 300, 5)  # it grows past what would have been kept: the record says so
        self.assertEqual((t.after_release_peak, t.release_revoked, t.release_end), (5 * GIB, True, 300.0))

    def test_a_released_build_that_grows_again_is_taken_back(self):
        t = self.past_its_peak()
        self.history_of(t.key, 3)
        self.settle_release(t, 190)
        self.assertTrue(t.released)
        self.sample(t, 300, 3)  # over its hold of 2.2G
        self.assertEqual(self.daemon.still_to_grow(t, self.now + 300), 17 * GIB)
        self.sample(t, 400, 2)  # and it stays taken back for the attempt
        self.assertEqual(self.daemon.still_to_grow(t, self.now + 400), 18 * GIB)

    def test_a_release_is_taken_back_by_a_pause_a_restart_or_an_opt_out(self):
        for change in (dict(frozen_attempt=5.0), dict(observed_gap=True), dict(mem_high=1)):
            t = self.past_its_peak()
            self.history_of(t.key, 3)
            self.settle_release(t, 190)
            self.assertTrue(t.released)
            for name, value in change.items():
                setattr(t, name, value)
            self.assertEqual(self.daemon.still_to_grow(t, self.now + 260), 18 * GIB, change)
            self.assertTrue(t.release_revoked)
        t = self.past_its_peak()
        self.settle_release(t, 190)
        self.daemon.lane("heavy").release_after_peak = False
        self.assertEqual(self.daemon.still_to_grow(t, self.now + 260), 18 * GIB)

    def test_a_shadow_decision_never_takes_effect_when_the_mode_is_switched_on(self):
        t = self.past_its_peak(mode="shadow")
        self.history_of(t.key, 3)
        self.settle_release(t, 190)
        self.assertIsNotNone(t.release_at)
        self.daemon.cfg.release_after_peak = True  # a reload mid-run
        self.assertEqual(self.daemon.still_to_grow(t, self.now + 260), 18 * GIB)
        self.assertFalse(t.released)

    def test_the_record_survives_a_requeue_and_a_breach_on_the_last_sample(self):
        t = self.past_its_peak(mode="shadow")
        self.history_of(t.key, 3)
        self.settle_release(t, 190)
        self.sample(t, 400, 4)  # over by 1.8G, then stopped to free memory and queued again
        t.unit = None
        self.daemon.requeue(t, self.now + 410)
        self.assertEqual((t.past_peak_attempts, t.past_peak_over), (1, 1))
        self.assertEqual(t.past_peak_most_over, 4 * GIB - int(2 * GIB * 1.1))
        self.assertAlmostEqual(t.past_peak_freed, (20 * GIB - int(2 * GIB * 1.1)) * (400 - 220), delta=GIB)
        self.assertIsNone(t.release_at)  # the next attempt starts afresh

    def test_a_kill_for_memory_after_the_decision_is_a_breach_even_unsampled(self):
        t = self.past_its_peak(mode="shadow")
        self.history_of(t.key, 3)
        self.settle_release(t, 190)
        t.oom_kills = 1  # the link jumped past its hold and was killed between two samples
        self.daemon.close_release_audit(t, 400.0)
        self.assertEqual((t.past_peak_attempts, t.past_peak_over), (1, 1))
        self.assertGreater(t.past_peak_most_over, 0)
        self.assertEqual(t.release_end, 400.0)

    def test_a_kill_seen_while_it_runs_takes_the_release_back(self):
        t = self.past_its_peak()
        self.history_of(t.key, 3)
        self.settle_release(t, 190)
        self.assertTrue(t.released)
        self.sample(t, 300, 2, kills=1)  # a test binary spiked past everything and was killed
        self.assertEqual((t.release_revoked, t.release_breach, t.release_end), (True, True, 300.0))
        self.assertEqual(self.daemon.still_to_grow(t, self.now + 301), 18 * GIB)

    def test_a_kill_in_the_supervisors_result_counts_even_at_the_slices_limit(self):
        t = self.past_its_peak(mode="shadow")
        self.history_of(t.key, 3)
        self.settle_release(t, 190)
        t.ended = None
        self.daemon.finalize(t, self.now + 400, 137, oom=False,
                             result={"exit": 137, "oom_kills": 1, "hit_own_cap": False})
        self.assertFalse(t.oom)  # the history isn't told it needs more: the slice's limit killed it
        self.assertEqual((t.past_peak_attempts, t.past_peak_over), (1, 1))
        self.assertEqual(t.past_peak_most_over, 20 * GIB - int(2 * GIB * 1.1))  # killed from outside: what was let go

    def test_how_far_it_went_over_is_followed_after_the_breach(self):
        t = self.past_its_peak(mode="shadow")
        self.history_of(t.key, 3)
        self.settle_release(t, 190)
        self.sample(t, 300, 3)  # just over its hold
        self.sample(t, 310, 10)  # and then the link step
        self.daemon.close_release_audit(t, 400.0)
        self.assertEqual((t.past_peak_over, t.past_peak_most_over), (1, 10 * GIB - int(2 * GIB * 1.1)))
        self.assertEqual(t.release_end, 300.0)

    def test_a_breach_known_before_a_restart_still_counts(self):
        t = self.past_its_peak(mode="shadow")
        self.history_of(t.key, 3)
        self.settle_release(t, 190)
        self.sample(t, 300, 6)
        self.daemon.save_queued(self.now + 310)  # the daemon stops: running tickets are saved too
        again = daemon.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                                     queries=common.InlineExecutor())
        again.cfg.release_after_peak = "shadow"
        again.history = self.daemon.history
        again.load_tickets(self.now + 320)
        loaded = again.tickets[t.id]
        self.assertEqual((loaded.observed_gap, loaded.release_open_at_gap, loaded.release_breach), (True, False, True))
        again.close_release_audit(loaded, 600.0)
        self.assertEqual((loaded.past_peak_attempts, loaded.past_peak_over, loaded.past_peak_unknown), (1, 1, 0))
        again.stop_memory_watch()
        for x in again.tickets.values():
            again.close_jobserver(x)

    def reload(self):
        again = daemon.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                                     queries=common.InlineExecutor())
        again.cfg.release_after_peak = "shadow"
        again.history = self.daemon.history
        self.addCleanup(lambda: (again.stop_memory_watch(), [again.close_jobserver(x) for x in again.tickets.values()]))
        return again

    def test_two_restarts_dont_turn_an_unwatched_decision_into_a_clean_one(self):
        t = self.past_its_peak(mode="shadow")
        self.history_of(t.key, 3)
        self.settle_release(t, 190)
        self.daemon.save_queued(self.now + 300)
        first = self.reload()
        first.load_tickets(self.now + 310)
        loaded = first.tickets[t.id]
        self.sample(loaded, 311, 2)  # watched again: nothing wrong
        first.still_to_grow(loaded, self.now + 312)  # taken back: it was unwatched
        self.assertTrue(loaded.release_revoked)
        first.save_queued(self.now + 320)
        second = self.reload()
        second.load_tickets(self.now + 330)
        again = second.tickets[t.id]
        self.assertTrue(again.release_open_at_gap)
        second.close_release_audit(again, 600.0)
        self.assertEqual((again.past_peak_attempts, again.past_peak_unknown), (0, 1))

    def test_a_kill_found_only_in_the_result_after_a_restart_is_a_breach(self):
        t = self.past_its_peak(mode="shadow")
        self.history_of(t.key, 3)
        self.settle_release(t, 190)
        self.daemon.save_queued(self.now + 300)
        later = self.reload()
        later.load_tickets(self.now + 310)
        loaded = later.tickets[t.id]
        loaded.kills_seen = 1  # its cgroup went before a sample; the supervisor's result says
        later.close_release_audit(loaded, 600.0)
        self.assertEqual((loaded.past_peak_attempts, loaded.past_peak_over, loaded.past_peak_unknown), (1, 1, 0))

    def test_a_whole_service_oom_kill_during_a_restart_is_a_breach(self):
        t = self.past_its_peak(mode="shadow")
        self.history_of(t.key, 3)
        self.settle_release(t, 190)
        self.daemon.save_queued(self.now + 300)
        later = self.reload()
        later.load_tickets(self.now + 310)
        loaded = later.tickets[t.id]
        loaded.ended = None
        later.finalize(loaded, self.now + 320, None, oom=True, lost=True)  # oomd took it all: no result, no cgroup
        self.assertEqual((loaded.past_peak_attempts, loaded.past_peak_over, loaded.past_peak_unknown), (1, 1, 0))

    def test_a_kill_after_the_restarts_take_back_is_unknown(self):
        t = self.past_its_peak(mode="shadow")
        self.history_of(t.key, 3)
        self.settle_release(t, 190)
        self.daemon.save_queued(self.now + 300)
        later = self.reload()
        later.load_tickets(self.now + 310)
        loaded = later.tickets[t.id]
        self.sample(loaded, 320, 2)  # watched again: nothing wrong
        later.still_to_grow(loaded, self.now + 321)  # taken back: it had been unwatched
        self.sample(loaded, 400, 2, kills=1)  # then the slice's limit killed something
        later.close_release_audit(loaded, 600.0)
        self.assertEqual((loaded.past_peak_attempts, loaded.past_peak_over, loaded.past_peak_unknown), (0, 0, 1))

    def test_after_a_restart_the_first_sample_comes_before_any_take_back(self):
        t = self.past_its_peak(mode="shadow")
        self.history_of(t.key, 3)
        self.settle_release(t, 190)
        self.daemon.save_queued(self.now + 300)
        later = self.reload()
        later.load_tickets(self.now + 310)
        loaded = later.tickets[t.id]
        later.still_to_grow(loaded, self.now + 311)  # a request ran a pass before the first tick
        self.assertFalse(loaded.release_revoked)
        self.sample(loaded, 312, 2, kills=1)  # the first sample finds the kill from the gap
        self.assertEqual((loaded.release_revoked, loaded.release_breach), (True, True))

    def test_unknown_needs_a_command_that_could_have_been_released_then(self):
        for change in (dict(est_mem=2 * GIB), dict(granted=12)):  # a hold not below its plan; more cores
            t = self.past_its_peak(mode="shadow")
            self.history_of(t.key, 3)
            t.measured, t.observed_gap = True, True
            for name, value in change.items():
                setattr(t, name, value)
            self.daemon.close_release_audit(t, 600.0)
            self.assertEqual(t.past_peak_unknown, 0, change)
        t = self.past_its_peak(mode="shadow")
        self.history_of(t.key, 3)
        t.measured, t.observed_gap = True, True
        self.daemon.close_release_audit(t, 200.0)  # too short to have had a decision
        self.assertEqual(t.past_peak_unknown, 0)

    def test_a_sampled_kill_from_outside_is_sized_by_what_was_let_go(self):
        t = self.past_its_peak(mode="shadow")
        self.history_of(t.key, 3)
        self.settle_release(t, 190)
        self.sample(t, 300, 2, kills=1)  # the slice's limit killed the linker; a sample saw it
        t.ended = None
        self.daemon.finalize(t, self.now + 310, 1, oom=False, result={"exit": 1, "oom_kills": 1, "hit_own_cap": False})
        self.assertFalse(t.oom)  # sampled outside kill must not override hit_own_cap=False (#79)
        self.assertEqual(t.past_peak_most_over, 20 * GIB - int(2 * GIB * 1.1))
        # Next run must keep its learned estimate instead of inflating to the lane's MemoryMax (26G).
        nxt = self.submit(t.argv, cwd=t.cwd, again=True)
        self.assertLess(nxt.est_mem, 24 * GIB)

    def test_switching_it_off_ends_a_standing_decision_without_a_breach(self):
        t = self.past_its_peak()
        self.history_of(t.key, 3)
        self.settle_release(t, 190)
        self.daemon.cfg.release_after_peak = False
        self.assertEqual(self.daemon.still_to_grow(t, self.now + 250), 18 * GIB)
        self.assertEqual((t.release_revoked, t.release_breach, t.release_end), (True, False, 250.0))
        self.sample(t, 400, 6)
        self.daemon.cfg.release_after_peak = True  # and on again: the old decision stays taken back
        self.assertEqual(self.daemon.still_to_grow(t, self.now + 410), 14 * GIB)
        self.daemon.close_release_audit(t, 500.0)
        self.assertEqual(t.past_peak_over, 0)

    def test_a_peak_reached_on_more_cores_than_the_historys_isnt_comparable(self):
        t = self.past_its_peak()
        self.history_of(t.key, 3)
        t.peak_mem_cores = 16  # peaked on 16 cores, cut back to 8 since
        self.assertEqual(self.settle_release(t, 900), (18 * GIB, 18 * GIB))

    def test_growth_after_a_take_back_for_another_reason_isnt_a_breach(self):
        t = self.past_its_peak(mode="shadow")
        self.history_of(t.key, 3)
        self.settle_release(t, 190)
        t.frozen_attempt = 10.0  # paused by the give-back: taken back, with everything held from then
        self.daemon.still_to_grow(t, self.now + 250)
        self.assertEqual((t.release_revoked, t.release_breach), (True, False))
        self.sample(t, 400, 6, kills=1)  # it grew, and was even killed: not the rule's doing any more
        self.daemon.close_release_audit(t, 500.0)
        self.assertEqual((t.past_peak_attempts, t.past_peak_over), (1, 0))
        self.assertAlmostEqual(t.past_peak_freed, (20 * GIB - int(2 * GIB * 1.1)) * (250 - 220), delta=GIB)

    def test_a_build_that_could_never_be_released_isnt_unknown_after_a_restart(self):
        t = self.past_its_peak(mode="shadow")
        self.history_of(t.key, 3)
        t.measured, t.observed_gap = True, True
        self.daemon.lane("heavy").release_after_peak = False
        self.daemon.close_release_audit(t, 600.0)
        self.assertEqual(t.past_peak_unknown, 0)

    def test_a_restart_makes_an_attempt_unknown_with_or_without_a_saved_decision(self):
        t = self.past_its_peak(mode="shadow")
        self.history_of(t.key, 3)
        self.settle_release(t, 190)  # decided, and saved
        again = daemon.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                                     queries=common.InlineExecutor())
        again.cfg.release_after_peak = "shadow"
        again.history = self.daemon.history
        again.load_tickets(self.now + 300)
        loaded = again.tickets[t.id]
        self.assertTrue(loaded.observed_gap)
        self.assertIsNotNone(loaded.release_at)
        again.close_release_audit(loaded, 600.0)
        self.assertEqual((loaded.past_peak_attempts, loaded.past_peak_unknown), (0, 1))
        other = self.past_its_peak(mode="shadow")  # no decision saved
        other.measured, other.observed_gap = True, True
        self.daemon.close_release_audit(other, 600.0)
        self.assertEqual(other.past_peak_unknown, 0)  # and its command has no history: it couldn't have had one
        self.history_of(other.key, 3)  # it could have
        self.daemon.close_release_audit(other, 600.0)
        self.assertEqual(other.past_peak_unknown, 1)
        again.stop_memory_watch()
        for x in again.tickets.values():
            again.close_jobserver(x)

    def test_savings_use_the_reservation_at_the_decision(self):
        t = self.past_its_peak(mode="shadow")
        self.history_of(t.key, 3)
        self.settle_release(t, 190)
        t.est_mem = 30 * GIB  # grown since (it can't be in shadow mode now, but say it was)
        self.daemon.close_release_audit(t, 400.0)
        self.assertAlmostEqual(t.past_peak_freed, (20 * GIB - int(2 * GIB * 1.1)) * (400 - 220), delta=GIB)

    def test_a_run_killed_for_memory_blocks_any_release(self):
        t = self.past_its_peak()
        self.history_of(t.key, 3)
        self.daemon.history.add({"key": t.key, "lane": "heavy", "t": self.now - 900, "wall": 500.0, "exit": 137,
                                 "peak_mem": 16 * GIB, "peak_cores": 4.0, "oom": True})
        self.assertEqual(self.settle_release(t, 900), (18 * GIB, 18 * GIB))

    def test_a_requeued_build_s_row_keeps_its_last_attempt_apart(self):
        t = self.submit()
        t.attempts_wall, t.requeues = 100.0, 1  # as if it had been stopped once, 100 s in
        self.finish(t, after=30)
        row = self.db().execute("SELECT wall, attempt_wall, workspace FROM build WHERE id = ?", (t.id,)).fetchone()
        self.assertEqual((row["wall"], row["attempt_wall"]), (130.0, 30.0))  # its profile is the 30 s one
        self.assertTrue(t.workspace)
        self.assertEqual(row["workspace"], t.workspace)  # what it wrote to: replays of it take turns by this

    def test_a_ticket_too_big_to_ever_fit_isnt_waiting_on_memory(self):
        minute = int(self.now // 60) * 60
        self.submit(mem=200 * GIB)
        for second in range(0, 60, 5):
            self.daemon.schedule(minute + second)
            self.daemon.observe_metrics(minute + second)
        self.daemon.metrics.stop()  # the minute so far is written at a stop
        self.daemon.metrics.thread.join(5)
        import sqlite3
        row = sqlite3.connect(os.path.join(self.state, "metrics.db")).execute(
            "SELECT wait_memory, wait_other, mem_wait_seconds FROM minute WHERE t = ?", (minute,)).fetchone()
        self.assertEqual((row[0], row[1], row[2]), (0.0, 1.0, None))

    def test_a_command_whose_peak_comes_at_its_end_is_never_released(self):
        t = self.past_its_peak()
        self.history_of(t.key, 3)
        self.history_of(t.key, 1, peak_at=570.0, tail=0)  # one run peaked in its last half minute
        self.assertEqual(self.settle_release(t, 900), (18 * GIB, 18 * GIB))

    def test_failed_runs_and_runs_unlike_this_one_teach_no_release(self):
        t = self.past_its_peak()
        self.history_of(t.key, 3, exit=1)  # failures may have skipped a later phase
        self.assertEqual(self.settle_release(t, 900), (18 * GIB, 18 * GIB))
        self.history_of(t.key, 3)
        self.daemon.history.add({"key": t.key, "lane": "heavy", "t": self.now - 2000, "wall": 600.0, "exit": 0,
                                 "peak_mem": 30 * GIB, "peak_cores": 4.0, "mem_cores": 8})  # a big run, unshaped
        self.assertEqual(self.settle_release(t, 900), (18 * GIB, 18 * GIB))  # the estimate's run must say

    def test_nothing_is_released_without_the_signs_of_a_past_peak(self):
        t = self.past_its_peak()
        self.history_of(t.key, 2)  # two runs aren't enough
        self.assertEqual(self.settle_release(t, 900), (18 * GIB, 18 * GIB))
        self.history_of(t.key, 1)
        for change, undo in (
                (dict(mem_now=15 * GIB), dict(mem_now=2 * GIB)),  # still near its peak
                (dict(mem_now=3 * GIB), dict(mem_now=2 * GIB)),  # above what its command used after peaks
                (dict(mem_peak=10 * GIB), dict(mem_peak=18 * GIB)),  # hasn't reached the peak behind the estimate
                (dict(peak_at=880.0), dict(peak_at=90.0)),  # its own peak was moments ago
                (dict(granted=12), dict(granted=8)),  # more cores than that peak had
                (dict(swap_now=3 * GIB), dict(swap_now=0)),  # the rest is in swap, not gone
                (dict(cold=True), dict(cold=False)),  # a full build: its peak may be later and bigger
                (dict(observed_gap=True), dict(observed_gap=False)),  # picked up after a restart
                (dict(frozen_attempt=20.0), dict(frozen_attempt=0.0)),  # paused this attempt
                (dict(mem_high=1), dict(mem_high=0)),  # over its MemoryHigh: reclaimed, not done
                (dict(mem_given=20 * GIB), dict(mem_given=None)),  # sized by its caller
                (dict(cpu_usec=40_000_000), dict(cpu_usec=200_000_000))):  # slower: hasn't done the work yet
            for name, value in change.items():
                setattr(t, name, value)
            self.assertEqual(self.settle_release(t, 900)[1], 20 * GIB - t.mem_now, change)  # all held
            for name, value in undo.items():
                setattr(t, name, value)
        self.daemon.cfg.release_after_peak = False
        self.assertEqual(self.settle_release(t, 900), (18 * GIB, 18 * GIB))
        self.daemon.cfg.release_after_peak = True
        self.daemon.lane("heavy").release_after_peak = False  # (docker build: dockerd holds the memory)
        self.assertEqual(self.settle_release(t, 900), (18 * GIB, 18 * GIB))
        self.daemon.lane("heavy").release_after_peak = True
        self.assertFalse(t.released)
        self.assertEqual(self.settle_release(t, 900)[1], int(2 * GIB * 1.1) - 2 * GIB)  # all well: released

    def test_measuring_finds_the_peak_what_came_after_and_a_bounded_trace(self):
        t = self.submit()
        t.started, t.cg = self.now, "/nonexistent"
        for second, gib in ((0, 1), (10, 8), (20, 3), (40, 2), (70, 4)):
            with mock.patch.object(machine, "cgroup_stats", return_value=(0, gib * GIB, 0)), \
                    mock.patch.object(machine, "cgroup_memory_waits", return_value=None):
                t.measure(self.now + second)
        self.assertEqual((t.mem_peak, t.peak_at), (8 * GIB, 10.0))
        self.assertEqual(t.tail, 4 * GIB)  # 3G at 20s is within half a minute of the peak
        u = self.submit()
        u.started, u.cg = self.now, "/nonexistent"
        for second, gib, swap in ((0, 1, 0), (10, 8, GIB), (50, 2, 5 * GIB), (70, 3, 1 * GIB)):
            with mock.patch.object(machine, "cgroup_stats", return_value=(0, gib * GIB, 0)), \
                    mock.patch.object(machine, "cgroup_memory_waits", return_value=None), \
                    mock.patch.object(machine, "cgroup_swap", return_value=swap):
                u.measure(self.now + second)
        self.assertEqual((u.peak_swap, u.tail, u.tail_used), (GIB, 3 * GIB, 7 * GIB))  # 2G resident + 5G swapped (#56)
        for second in range(0, 200000, 7):
            t.note_trace(float(second), GIB)
        self.assertLessEqual(len(t.trace), t.TRACE_POINTS + 1)
        self.assertEqual(metrics.memory_profile([[0, GIB], [50, 2 * GIB], [99, 3 * GIB]], 100), [1024, 0, 0, 0, 0, 2048,
                                                                                                0, 0, 0, 3072])

    def test_hours_are_rolled_up_and_old_rows_deleted(self):
        import sqlite3
        metrics = self.daemon.metrics
        hour = int(self.now // 3600) * 3600
        old = hour - 20 * 86400
        metrics.build({"id": "old", "ended": hour - 200 * 86400, "key": "k"})  # builds are kept 180 days
        metrics.build({"id": "kept", "ended": old, "key": "k"})
        for t in (old, hour, hour + 60):  # minutes 14 days
            metrics.put({"_kind": "minute", "t": t, "_builds": "/nonexistent", "sampled": 60.0, "cpu_busy": 4.0,
                         "wait_memory": 1.0})
        metrics.build({"id": "new", "ended": hour + 90, "key": "k"})
        db = self.db()
        metrics.clean(db, hour + 120)  # (the writer does this once an hour of its rows' time)
        self.assertEqual([r["t"] for r in db.execute("SELECT t FROM minute ORDER BY t")], [hour, hour + 60])
        self.assertEqual([r["id"] for r in db.execute("SELECT id FROM build ORDER BY id")], ["kept", "new"])
        rolled = db.execute("SELECT * FROM hour WHERE t = ?", (hour,)).fetchone()
        self.assertEqual((rolled["sampled"], rolled["cpu_busy"], rolled["wait_memory"]), (120.0, 4.0, 1.0))

    def test_a_ticket_file_with_nonsense_in_the_new_fields_still_loads(self):
        t = self.submit()
        record = dict(t.record(), waited_by=["x"], peak_at="soon", tail=-5, released="yes")
        loaded = ticket.Ticket.from_record(record, t.dir)
        self.assertEqual((loaded.waited_by, loaded.peak_at, loaded.tail, loaded.released), ({}, 0.0, 0, False))
        record["waited_by"] = {"memory": 12.5, "cpu": "nan", "held": float("inf")}
        self.assertEqual(ticket.Ticket.from_record(record, t.dir).waited_by, {"memory": 12.5})

    def test_off_means_no_file(self):
        self.daemon.cfg.metrics = False
        for second in range(0, 130, 5):
            self.daemon.observe_metrics(self.now + second)
        t = self.submit()
        self.finish(t, 0)
        self.assertFalse(os.path.exists(os.path.join(self.state, "metrics.db")))

    def test_the_client_queries_and_reports_from_the_file(self):
        minute = int(self.now // 60) * 60
        for t in (minute - 120, minute - 60):
            self.daemon.metrics.put({"_kind": "minute", "t": t, "_builds": "/nonexistent", "sampled": 60.0,
                                     "cpu_busy": 6.0, "cpu_target": 28.8, "mem_available": 60 * GIB,
                                     "mem_floor": 16 * GIB, "growth": 30 * GIB, "wait_memory": 2.0, "wait_cpu": 0.0,
                                     "wait_held": 0.0, "wait_turn": 0.0, "wait_workspace": 0.0, "wait_other": 0.0,
                                     "cores": 32, "mem_wait_seconds": 60.0, "idle_cores_mem_wait": 22.8,
                                     "free_mem_mem_wait": 44 * GIB, "growth_mem_wait": 30 * GIB})
        self.daemon.metrics.build({"id": "b1", "ended": round(self.now), "key": "k", "est_mem": 20 * GIB,
                                   "mem_peak": 5 * GIB, "waited_by": json.dumps({"memory": 120.0}), "released": 0,
                                   "past_peak_attempts": 1, "past_peak_over": 1, "past_peak_most_over": GIB,
                                   "past_peak_freed": 18.0, "past_peak_unknown": 0, "past_peak_released": 0})
        self.assertTrue(self.daemon.metrics.flush())
        env = dict(os.environ, BUILDQ_STATE_DIR=self.state)
        client = os.path.join(ROOT, "bin", "buildq")
        out = subprocess.run([sys.executable, client, "query", "SELECT COUNT(*) AS n FROM minute"], env=env,
                             capture_output=True, text=True)
        self.assertEqual(out.stdout.split(), ["n", "2"], out.stderr)
        tables = subprocess.run([sys.executable, client, "query"], env=env, capture_output=True, text=True).stdout
        self.assertIn("build: id TEXT", tables)
        refused = subprocess.run([sys.executable, client, "query", "DELETE FROM minute"], env=env,
                                 capture_output=True, text=True)
        self.assertNotEqual(refused.returncode, 0)  # read-only
        report = subprocess.run([sys.executable, client, "report", "--capacity", "--json"], env=env,
                                capture_output=True, text=True)
        data = json.loads(report.stdout)
        period = data["periods"][-1]
        self.assertEqual(sum(p["waited_memory_minutes"] for p in data["periods"]), 4.0)
        self.assertAlmostEqual(period["idle_cores_while_waiting_on_memory"], 22.8)
        self.assertEqual(period["free_memory_while_waiting_on_memory"], 44 * GIB)
        self.assertEqual((data["builds"], data["past_peak"], data["went_over_what_was_kept"]), (1, 1, 1))
        self.assertEqual((data["most_over"], data["held_gb_hours_freed"]), (GIB, 18.0))
        self.assertEqual(data["median_peak_of_plan"], 0.25)
        text = subprocess.run([sys.executable, client, "report", "--capacity"], env=env, capture_output=True,
                              text=True).stdout
        self.assertIn("MEMWAIT", text)
        self.assertIn("waited for: memory 2m", text)
        self.assertIn("1 attempts were past their peak; 1 of them later used more than would have been kept "
                      "(by up to 1.0G)", text)

    def test_a_failed_cleanup_leaves_no_transaction_open(self):
        import sqlite3
        db = self.daemon.metrics.open()
        db.execute("DROP TABLE hour")
        with self.assertRaises(sqlite3.Error):
            self.daemon.metrics.clean(db, self.now)
        self.assertFalse(db.in_transaction)
        db.close()

    def test_ci_runners_in_app_slice_are_ci_not_apps(self):
        manager = os.path.join(self.tmp.name, "cg")

        def group(path, usec, anon):
            os.makedirs(path, exist_ok=True)
            with open(os.path.join(path, "cpu.stat"), "w") as f:
                f.write(f"usage_usec {usec}\n")
            with open(os.path.join(path, "memory.stat"), "w") as f:
                f.write(f"anon {anon}\nkernel 0\nshmem 0\nslab_reclaimable 0\n")

        apps, runner = os.path.join(manager, "app.slice"), os.path.join(manager, "app.slice", "gha-x-1.service")
        builds = os.path.join(manager, "buildq.slice")
        for path, usec, anon in ((apps, 0, 10 * GIB), (runner, 0, 4 * GIB), (builds, 0, GIB)):
            group(path, usec, anon)
        consumers = metrics.Consumers()
        consumers.manager = manager
        first = consumers.read(builds, 8.0)
        self.assertEqual((first["ci_mem"], first["apps_mem"]), (4 * GIB, 6 * GIB))
        self.assertNotIn("ci_cores", first)  # nothing to compare with yet
        consumers.last = (consumers.last[0] - 60, consumers.last[1])
        group(apps, 120_000_000, 10 * GIB)  # 2 cores over the minute, 1.5 of them the runner's
        group(runner, 90_000_000, 4 * GIB)
        second = consumers.read(builds, 8.0)
        self.assertAlmostEqual(second["ci_cores"], 1.5, places=1)
        self.assertAlmostEqual(second["apps_cores"], 0.5, places=1)
        self.assertAlmostEqual(second["other_cores"], 6.0, places=1)

    def test_waits_carry_across_a_restart(self):
        self.machine.mem_available = 25 * GIB
        t = self.submit()
        self.daemon.schedule(self.now)
        self.daemon.schedule(self.now + 30)
        self.daemon.save_queued(self.now + 40)  # the daemon stops
        again = daemon.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                                     queries=common.InlineExecutor())
        again.load_tickets(self.now + 100)
        loaded = again.tickets[t.id]
        again.schedule(self.now + 100)  # the time it was away counts as "restart"
        self.assertEqual(loaded.waited_by, {"memory": 40.0, "restart": 60.0})
        again.stop_memory_watch()
        for x in again.tickets.values():
            again.close_jobserver(x)



class GitHubStatuses(DaemonCase):
    def setUp(self):
        super().setUp()
        self.daemon.cfg.github_status = True
        self.calls_file = os.path.join(self.tmp.name, "gh-calls")
        self.daemon.gh = [sys.executable, "-c",
                          f"import sys; open({self.calls_file!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')"]

    def drain(self, now):
        self.daemon.send_status_posts(now)
        for proc, _, _ in list(self.daemon.status_procs):
            proc.wait(10)
        self.daemon.send_status_posts(now)

    def calls(self):
        try:
            with open(self.calls_file) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def test_a_check_that_passed_on_a_clean_commit_is_posted_and_nothing_else(self):
        passed = self.submit(("cargo", "test"), source=SOURCE)
        self.finish(passed, 0)
        dirty = self.submit(("cargo", "check"), source={**SOURCE, "dirty": True})
        self.finish(dirty, 0)
        build = self.submit(("cargo", "build"), source=SOURCE)  # not a check, and not named
        self.finish(build, 0)
        failed = self.submit(("cargo", "clippy"), source=SOURCE)  # a failure with no pass before
        self.finish(failed, 1)
        self.settle()
        self.drain(self.now + 60)
        calls = self.calls()
        self.assertEqual(len(calls), 1, calls)
        self.assertIn(f"repos/owner/project/statuses/{'a' * 40}", calls[0])
        self.assertIn("state=success", calls[0])
        self.assertIn("context=buildq/cargo test", calls[0])
        self.assertEqual(self.daemon.status_posts, [])

    def test_a_later_failure_of_the_same_check_turns_it_red(self):
        first = self.submit(("cargo", "test"), source=SOURCE, name="tests")
        self.finish(first, 0)
        self.settle()
        self.drain(self.now + 60)
        again = self.submit(("cargo", "test"), cwd=first.cwd, source=SOURCE, name="tests", again=True)
        self.finish(again, 101, after=60)  # ends after the first
        self.settle()
        self.drain(self.now + 61)
        self.assertEqual([("state=success" in c, "state=failure" in c) for c in self.calls()],
                         [(True, False), (False, True)])

    def test_an_older_result_never_replaces_a_newer_one(self):
        newer = self.submit(("cargo", "test"), source=SOURCE, name="tests")
        self.finish(newer, 0, after=60)
        older = self.submit(("cargo", "test"), cwd=newer.cwd, source=SOURCE, name="tests", again=True)
        self.finish(older, 101, after=30)  # ended earlier, but its check comes back later
        self.settle()
        self.drain(self.now + 70)
        self.assertEqual([("state=success" in c) for c in self.calls()], [True])

    def test_a_newer_failure_holds_back_an_older_pass_even_when_nothing_is_posted(self):
        first = self.submit(("cargo", "test"), source=SOURCE, name="tests")
        self.finish(first, 101, after=60)  # the newest verdict: red, with nothing green to correct
        self.settle()
        older = self.submit(("cargo", "test"), cwd=first.cwd, source=SOURCE, name="tests", again=True)
        self.finish(older, 0, after=30)  # ended before it; its check comes back after
        self.settle()
        self.drain(self.now + 70)
        self.assertEqual(self.calls(), [])

    def test_a_kill_or_a_timeout_doesnt_turn_it_red(self):
        first = self.submit(("cargo", "test"), source=SOURCE, name="tests")
        self.finish(first, 0)
        self.settle()
        self.drain(self.now + 40)
        killed = self.submit(("cargo", "test"), cwd=first.cwd, source=SOURCE, name="tests", again=True)
        self.finish(killed, 137, after=60, oom_kills=1)
        self.settle()
        self.drain(self.now + 70)
        self.assertEqual(len(self.calls()), 1)

    def test_after_a_restart_only_the_newest_queued_result_is_sent(self):
        posts = [{"repo": "owner/project", "sha": "a" * 40, "state": state, "context": "buildq/tests",
                  "where": f"owner/project@{'a' * 40}@buildq/tests", "description": "x", "first": self.now,
                  "next": self.now, "order": [self.now + i, 0, ""]} for i, state in enumerate(("success", "failure"))]
        with open(os.path.join(self.state, "github-statuses.json"), "w") as f:
            json.dump({"queue": posts, "posted": {}}, f)
        again = daemon.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                                     queries=common.InlineExecutor())
        self.assertEqual([p["state"] for p in again.status_posts], ["failure"])
        again.stop_memory_watch()

    def test_a_commit_github_doesnt_have_yet_is_tried_again_after_a_push(self):
        self.daemon.gh = [sys.executable, "-c",
                          "import sys; sys.stderr.write('No commit found for SHA: aaaa\\n'); sys.exit(1)"]  # not on GitHub yet
        t = self.submit(("cargo", "test"), source=SOURCE)
        self.finish(t, 0)
        self.settle()
        self.drain(self.now + 60)
        (post,) = self.daemon.status_posts
        self.assertGreater(post["next"], self.now + 600)
        push = self.submit(("git", "push"), source=SOURCE)
        self.finish(push, 0)
        self.assertLessEqual(post["next"], self.now + 60)

    def test_gh_is_found_on_path_first_then_where_a_user_install_puts_it(self):
        def place(path, executable=True):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write("#!/bin/sh\n")
            os.chmod(path, 0o755 if executable else 0o644)
            return path

        with tempfile.TemporaryDirectory() as home:
            on_path = os.path.join(home, "path")
            with mock.patch.dict(os.environ, HOME=home, PATH=on_path):
                self.assertEqual(machine.find_gh(), "gh")  # nowhere: left to the error that says so
                place(os.path.join(home, ".local", "bin", "gh"), executable=False)
                self.assertEqual(machine.find_gh(), "gh")  # not runnable: not taken
                home_bin = place(os.path.join(home, "bin", "gh"))
                self.assertEqual(machine.find_gh(), home_bin)
                local = place(os.path.join(home, ".local", "bin", "gh"))
                self.assertEqual(machine.find_gh(), local)  # ~/.local/bin before ~/bin
                first = place(os.path.join(on_path, "gh"))
                self.assertEqual(machine.find_gh(), first)  # PATH before both

    def test_the_gh_setting_is_used_ahead_of_the_search(self):
        script = os.path.join(self.tmp.name, "my-gh")
        with open(script, "w") as f:
            f.write(f"#!/bin/sh\necho \"$*\" >> {self.calls_file}\n")
        os.chmod(script, 0o755)
        self.daemon.gh = None  # nothing stood in: the configuration decides
        self.daemon.cfg.gh = script
        t = self.submit(("cargo", "test"), source=SOURCE)
        self.finish(t, 0)
        self.settle()
        with mock.patch.object(machine, "find_gh", side_effect=AssertionError("searched")):
            self.drain(self.now + 60)
        self.assertEqual(len(self.calls()), 1)
        self.assertIn("state=success", self.calls()[0])

    def test_the_gh_setting_must_be_a_full_path(self):
        script = os.path.join(self.tmp.name, "gh")
        with open(script, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(script, 0o755)
        self.assertEqual(config.Config({"gh": script}, 64 * GIB, 16).gh, script)
        self.assertEqual(config.Config({"gh": script}, 64 * GIB, 16).summary()["gh"], script)
        self.assertEqual(config.Config({}, 64 * GIB, 16).gh, "")
        missing = os.path.join(self.tmp.name, "missing")  # gone later: the configuration still loads
        self.assertEqual(config.Config({"gh": missing}, 64 * GIB, 16).gh, missing)
        self.assertEqual(config.Config({"gh": "~/bin/gh"}, 64 * GIB, 16).gh, os.path.expanduser("~/bin/gh"))
        for bad in ("gh", "bin/gh"):
            with self.assertRaises(ValueError, msg=bad):
                config.Config({"gh": bad}, 64 * GIB, 16)

    def test_check_config_warns_of_a_gh_that_cant_run_and_still_passes(self):
        config = os.path.join(self.tmp.name, "check.toml")
        with open(config, "w") as f:
            f.write(f'gh = "{os.path.join(self.tmp.name, "no-such-gh")}"\n')
        result = subprocess.run([sys.executable, BUILDQD, "--check-config", "--config", config],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("isn't an executable file", result.stderr)
        self.assertEqual(json.loads(result.stdout)["gh"], os.path.join(self.tmp.name, "no-such-gh"))

    def test_gh_that_cant_run_is_logged_once_and_tried_again_later(self):
        self.daemon.gh = [os.path.join(self.tmp.name, "no-such-gh")]
        for argv in (("cargo", "test"), ("cargo", "check")):
            t = self.submit(argv, source=SOURCE)
            self.finish(t, 0)
        self.settle()
        with mock.patch.object(common, "log") as log:
            self.drain(self.now + 60)
            self.drain(self.now + 700)  # ten minutes on, tried again
        said = [c for c in log.call_args_list if "can't run gh" in str(c)]
        self.assertEqual(len(said), 1, log.call_args_list)
        self.assertNotIn("isn't on PATH", str(said[0]))  # a set path: no search to blame
        self.assertEqual(len(self.daemon.status_posts), 2)  # both kept for later
        missing = self.daemon.gh
        self.daemon.gh = None  # found as it is sent: nowhere
        with mock.patch.object(machine, "find_gh", return_value="gh"), \
                mock.patch.dict(os.environ, PATH=os.path.join(self.tmp.name, "empty")), \
                mock.patch.object(common, "log") as log:
            self.drain(self.now + 1400)
        self.assertEqual([c for c in log.call_args_list if "can't run gh" in str(c)], [])  # the same outage
        self.daemon.gh = [sys.executable, "-c", "pass"]  # gh runs again
        self.drain(self.now + 2100)
        self.daemon.gh = missing
        for argv in (("cargo", "clippy"),):
            t = self.submit(argv, source=SOURCE)
            self.finish(t, 0)
        self.settle()
        with mock.patch.object(common, "log") as log:
            self.drain(self.now + 2200)
        said = [c for c in log.call_args_list if "can't run gh" in str(c)]
        self.assertEqual(len(said), 1, log.call_args_list)  # a new outage is said again

    def test_a_gh_found_nowhere_says_where_it_was_looked_for(self):
        self.daemon.gh = None
        t = self.submit(("cargo", "test"), source=SOURCE)
        self.finish(t, 0)
        self.settle()
        with mock.patch.object(machine, "find_gh", return_value="gh"), \
                mock.patch.dict(os.environ, PATH=os.path.join(self.tmp.name, "empty")), \
                mock.patch.object(common, "log") as log:
            self.drain(self.now + 60)
        (said,) = [c for c in log.call_args_list if "can't run gh" in str(c)]
        self.assertIn("isn't on PATH, in ~/.local/bin or in ~/bin", str(said))

    CI = {"GITHUB_ACTIONS": "true", "RUNNER_NAME": "ci-runner-1"}

    def test_a_pass_in_a_github_actions_job_posts_nothing(self):
        t = self.submit(("cargo", "test"), source=SOURCE, env=self.CI)
        self.finish(t, 0)
        self.settle()
        self.drain(self.now + 60)
        self.assertTrue(t.source_verified)
        self.assertEqual((self.calls(), self.daemon.status_posts), ([], []))
        self.assertTrue(ticket.Ticket.from_record(t.record(), t.dir).from_ci)  # kept across a restart

    def test_github_actions_set_by_hand_isnt_a_ci_job(self):
        t = self.submit(("cargo", "test"), source=SOURCE, env={"GITHUB_ACTIONS": "true"})  # (for annotations)
        self.finish(t, 0)
        self.settle()
        self.drain(self.now + 60)
        self.assertEqual(len(self.calls()), 1)

    def test_a_failure_in_a_github_actions_job_turns_a_buildq_green_red(self):
        first = self.submit(("cargo", "test"), source=SOURCE)
        self.finish(first, 0)
        self.settle()
        self.drain(self.now + 60)
        ci = self.submit(("cargo", "test"), source=SOURCE, env=self.CI)
        self.finish(ci, 101, after=60)  # ends after the first
        self.settle()
        self.drain(self.now + 61)
        self.assertEqual([("state=success" in c, "state=failure" in c) for c in self.calls()],
                         [(True, False), (False, True)])

    def test_off_means_nothing_is_sent(self):
        t = self.submit(("cargo", "test"), source=SOURCE)
        self.finish(t, 0)
        self.settle()
        self.daemon.cfg.github_status = False  # turned off with a post still queued
        self.drain(self.now + 60)
        self.assertEqual(self.calls(), [])

    def test_missing_commit_backs_off_and_gives_up_after_six_hours(self):
        self.daemon.gh = [sys.executable, "-c",
                          "import sys; sys.stderr.write('gh: Validation Failed (HTTP 422)\\n"
                          "No commit found for SHA: aaaa\\n'); sys.exit(1)"]
        t = self.submit(("cargo", "test"), source=SOURCE)
        self.finish(t, 0)
        self.settle()
        (post,) = self.daemon.status_posts
        first = post["first"]
        t1 = first + 60
        self.drain(t1)
        self.assertEqual((post["tries"], post.get("missing"), post["next"]), (1, True, t1 + 600))
        t2 = t1 + 600
        self.drain(t2)
        self.assertEqual((post["tries"], post["next"]), (2, t2 + 1200))
        t3 = t2 + 1200
        self.drain(t3)
        self.assertEqual((post["tries"], post["next"]), (3, t3 + 2400))
        # A newer verdict for the same commit/check during backoff preserves backoff and first timestamp.
        self.now = t3 + 100
        newer = self.submit(("cargo", "test"), cwd=t.cwd, source=SOURCE, again=True)
        self.finish(newer, 1, after=10)
        self.settle()
        (replaced,) = self.daemon.status_posts
        self.assertEqual((replaced["state"], replaced["first"], replaced["next"],
                          replaced["tries"], replaced.get("missing")),
                         ("failure", first, t3 + 2400, 3, True))
        # Pushing resets backoff and persists the reset across a daemon restart.
        self.now = t3 + 200
        push = self.submit(("git", "push"), source=SOURCE)
        self.finish(push, 0)
        self.assertEqual(replaced["tries"], 0)
        self.assertLessEqual(replaced["next"], push.ended)
        saved = common.read_json(os.path.join(self.state, "github-statuses.json"))
        self.assertEqual(saved["queue"][0]["tries"], 0)
        self.assertLessEqual(saved["queue"][0]["next"], push.ended)
        with mock.patch.object(common, "log") as log:
            self.daemon.send_status_posts(first + daemon.Daemon.STATUS_MISSING_MAX_AGE + 1)
        self.assertEqual(self.daemon.status_posts, [])
        self.assertTrue(any("6 hours" in str(c) for c in log.call_args_list), log.call_args_list)

    def test_permanent_http_errors_drop_immediately_while_rate_limits_retry(self):
        for msg in ("gh: Bad credentials (HTTP 401)",
                    "gh: Not Found (HTTP 404)",
                    "gh: Resource not accessible by integration (HTTP 403)",
                    "gh: Validation Failed (HTTP 422)\nState is invalid",
                    "gh: Validation Failed (HTTP 422)\nThis commit has reached the maximum number of statuses"):
            self.now += 60  # strictly increasing order so note_status() never suppresses a later case
            self.daemon.gh = [sys.executable, "-c", f"import sys; sys.stderr.write({msg!r}); sys.exit(1)"]
            t = self.submit(("cargo", "test"), source=SOURCE, again=True)
            self.finish(t, 0)
            self.settle()
            with mock.patch.object(common, "log") as log:
                self.drain(self.now + 60)
            self.assertEqual(self.daemon.status_posts, [], msg)
            self.assertTrue(any("not retrying" in str(c) for c in log.call_args_list), (msg, log.call_args_list))
        self.now += 60
        self.daemon.gh = [sys.executable, "-c",
                          "import sys; sys.stderr.write('gh: API rate limit exceeded (HTTP 403)'); sys.exit(1)"]
        t = self.submit(("cargo", "test"), source=SOURCE, again=True)
        self.finish(t, 0)
        self.settle()
        (post,) = self.daemon.status_posts
        first = post["first"]
        self.drain(first + 60)
        self.assertEqual((len(self.daemon.status_posts), post.get("missing")), (1, None))
        # An unrelated git push does not reset rate-limit backoff.
        rate_next = post["next"]
        push = self.submit(("git", "push"), source=SOURCE)
        self.finish(push, 0)
        self.assertEqual((post["next"], post["tries"]), (rate_next, 1))
        # Rate limits keep the full 24h retry window, not the 6h missing-commit window.
        post["next"] = first + daemon.Daemon.STATUS_MAX_AGE + 100
        self.daemon.send_status_posts(first + daemon.Daemon.STATUS_MISSING_MAX_AGE + 60)
        self.assertEqual(len(self.daemon.status_posts), 1)
        self.daemon.send_status_posts(first + daemon.Daemon.STATUS_MAX_AGE + 1)
        self.assertEqual(self.daemon.status_posts, [])

    def test_in_flight_failure_applies_backoff_to_queued_replacement(self):
        import io
        t1 = self.submit(("cargo", "test"), source=SOURCE)
        self.finish(t1, 0)
        self.settle()
        (p1,) = self.daemon.status_posts

        class FakeProc:
            def __init__(self):
                self.returncode = None
                self.stderr = io.BytesIO(b"gh: Validation Failed (HTTP 422)\nNo commit found for SHA: aaaa\n")

            def poll(self):
                return self.returncode

        proc = FakeProc()
        launched = []

        def fake_popen(*a, **kw):
            launched.append(a[0])
            return proc

        t_start = t1.ended + 10
        with mock.patch.object(github.subprocess, "Popen", side_effect=fake_popen):
            self.daemon.send_status_posts(t_start)
        self.assertEqual(len(launched), 1)
        # Newer verdict arrives while p1 is still in flight (outside Popen mock so source check runs).
        self.now = t_start + 5
        t2 = self.submit(("cargo", "test"), cwd=t1.cwd, source=SOURCE, again=True)
        self.finish(t2, 1, after=5)
        self.settle()
        self.assertEqual(len(self.daemon.status_posts), 2)
        # p1 finishes with missing-commit 422; replacement p2 inherits backoff and is NOT launched this tick.
        proc.returncode = 1
        t_done = t_start + 15
        with mock.patch.object(github.subprocess, "Popen", side_effect=fake_popen):
            self.daemon.send_status_posts(t_done)
        self.assertEqual(len(launched), 1)
        self.assertEqual(self.daemon.status_procs, [])
        (p2,) = self.daemon.status_posts
        self.assertEqual((p2["state"], p2["tries"], p2.get("missing"), p2["next"]),
                         ("failure", 1, True, t_done + 600))
        # When an in-flight retry succeeds past the 6h missing-commit window, a queued replacement gets
        # a fresh delivery window instead of expiring before its first send.
        t_retry = t_done + 600
        proc2 = FakeProc()
        proc2.stderr = io.BytesIO(b"")
        with mock.patch.object(github.subprocess, "Popen", return_value=proc2):
            self.daemon.send_status_posts(t_retry)
        self.now = t_retry + 5
        t3 = self.submit(("cargo", "test"), cwd=t1.cwd, source=SOURCE, again=True)
        self.finish(t3, 0, after=5)
        self.settle()
        proc2.returncode = 0
        t_ok = p2["first"] + daemon.Daemon.STATUS_MISSING_MAX_AGE + 10
        proc3 = FakeProc()
        with mock.patch.object(github.subprocess, "Popen", return_value=proc3):
            self.daemon.send_status_posts(t_ok)
        self.assertEqual(len(self.daemon.status_posts), 1)
        (p3,) = self.daemon.status_posts
        self.assertEqual((p3["state"], p3.get("missing"), p3.get("tries"), p3["first"]),
                         ("success", None, None, t_ok))
        self.assertEqual(len(self.daemon.status_procs), 1)
        self.daemon.status_procs.clear()

    def test_in_flight_status_post_is_not_dropped_before_it_finishes(self):
        t = self.submit(("cargo", "test"), source=SOURCE)
        self.finish(t, 0)
        self.settle()
        (post,) = self.daemon.status_posts

        class Running:
            stderr = None
            returncode = None

            def poll(self):
                return None

        with mock.patch.object(github.subprocess, "Popen", return_value=Running()):
            self.daemon.send_status_posts(post["first"] + 10)
        self.assertEqual(len(self.daemon.status_procs), 1)
        post["first"] -= daemon.Daemon.STATUS_MAX_AGE + 100  # past max age while still in flight
        self.daemon.send_status_posts(post["first"] + daemon.Daemon.STATUS_MAX_AGE + 120)
        self.assertIn(post, self.daemon.status_posts)
        self.daemon.status_procs.clear()



if __name__ == "__main__":
    unittest.main()
