"""Unit tests for memory shedding, outgrowing estimates, slice guard, and memory watch."""

import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import DaemonCase, GIB, MIB  # noqa: E402
from buildq import commands, common, config, daemon, history, machine, runners, scheduler, supervisor  # noqa: E402


class SheddingMemory(DaemonCase):
    """When other work takes the machine's memory, buildq pauses its newest build, then stops it
    and queues it again (GNU parallel's --memsuspend and --memfree)."""

    def falling_to(self, avail, at):
        """Memory falls to `avail` at `at`, from plenty a few seconds before."""
        self.machine.mem_available = 9 * GIB
        self.daemon.shed_memory(at - 5)
        self.machine.mem_available = avail

    def running(self, n):
        builds = []
        for i in range(n):
            t = self.submit(cores=2)
            t.started = self.now + i  # started one after another
            t.mem_now = 6 * GIB
            builds.append(t)
        return builds

    def test_the_newest_build_is_paused_then_stopped_and_queued_again_at_the_front(self):
        old, new = self.running(2)
        self.falling_to(4 * GIB, self.now + 100)  # under half the 10G floor, and falling
        self.daemon.shed_memory(self.now + 100)
        self.assertEqual(self.runner.frozen, [(new.id, True)])
        self.assertTrue(new.frozen)
        self.daemon.shed_memory(self.now + 105)
        self.assertEqual(len(self.runner.frozen), 1)  # one step every ten seconds
        self.machine.mem_available = 2 * GIB  # under a quarter
        self.daemon.shed_memory(self.now + 111)
        self.assertIn(new.id, self.runner.forced)
        self.assertTrue(new.requeue_pending)
        self.runner.running.discard(new.id)  # it is gone
        self.daemon.check(self.now + 112)
        self.assertEqual((new.state, new.requeues, new.frozen, new.unit), ("queued", 1, False, None))
        with open(os.path.join(new.dir, "env.json")) as f:
            self.assertEqual(json.load(f)["BUILDQ_JOB"], new.id)  # ready to start again
        with open(new.log) as f:
            self.assertIn("ran short of memory", f.read())
        waiting = self.submit(cores=1)
        self.assertEqual(self.daemon.queue(self.now + 113)[0].id, new.id)  # it goes first
        self.assertEqual(old.state, "running")
        del waiting

    def test_a_stop_isnt_held_up_by_a_pause_and_frees_enough(self):
        old, new = self.running(2)
        new.mem_now = 1 * GIB  # the newest is small; the old one holds what is needed
        self.falling_to(4 * GIB, self.now + 100)
        self.daemon.shed_memory(self.now + 100)
        self.assertEqual(self.runner.frozen, [(new.id, True)])
        self.machine.mem_available = 1 * GIB  # a second later, far worse
        self.daemon.shed_memory(self.now + 101)
        self.assertEqual(self.runner.forced, [old.id])  # at once, and the one whose memory covers the shortfall

    def test_a_steady_shortage_pauses_nothing(self):
        self.running(2)
        self.machine.mem_available = 4 * GIB  # low, but not falling: pausing would free nothing
        for i in range(6):
            self.daemon.shed_memory(self.now + 100 + 10 * i)
        self.assertEqual(self.runner.frozen, [])

    def test_a_pause_lasts_five_minutes_then_the_build_is_stopped_if_memory_is_still_short(self):
        (t,) = self.running(1)
        self.falling_to(4 * GIB, self.now + 100)
        self.daemon.shed_memory(self.now + 100)
        self.assertTrue(t.frozen)
        self.daemon.shed_memory(self.now + 100 + 301)
        self.assertTrue(t.requeue_pending)  # stopped instead: that frees its memory

    def test_a_pause_lasts_five_minutes_then_the_build_goes_on_if_memory_allows(self):
        (t,) = self.running(1)
        t.est_mem = 20 * GIB  # resuming before then needs the floor plus 14G of growth free
        self.falling_to(4 * GIB, self.now + 100)
        self.daemon.shed_memory(self.now + 100)
        self.assertTrue(t.frozen)
        self.machine.mem_available = 6 * GIB  # above half the floor
        self.daemon.shed_memory(self.now + 200)
        self.assertTrue(t.frozen)
        self.daemon.shed_memory(self.now + 100 + 301)
        self.assertFalse(t.frozen)

    def test_a_build_paused_for_five_minutes_isnt_paused_again(self):
        (t,) = self.running(1)
        self.falling_to(4 * GIB, self.now + 100)
        self.daemon.shed_memory(self.now + 100)
        self.machine.mem_available = 6 * GIB
        self.daemon.shed_memory(self.now + 401)  # at the cap, with half the floor free: it goes on
        self.assertFalse(t.frozen)
        self.falling_to(4 * GIB, self.now + 420)  # memory falls again
        self.daemon.shed_memory(self.now + 420)
        self.assertFalse(t.frozen)  # its five minutes are used up

    def test_a_pause_adopted_from_the_cgroup_still_ends(self):
        (t,) = self.running(1)
        t.frozen, t.frozen_at = True, None  # found paused, start unknown
        self.machine.mem_available = 6 * GIB
        self.daemon.shed_memory(self.now + 100)
        self.assertEqual(t.frozen_at, self.now + 100)
        self.daemon.shed_memory(self.now + 100 + 301)
        self.assertFalse(t.frozen)

    def test_paused_time_is_counted_per_attempt(self):
        (t,) = self.running(1)
        t.started = self.now
        self.falling_to(4 * GIB, self.now + 100)
        self.daemon.shed_memory(self.now + 100)  # paused at 100
        self.machine.mem_available = 1 * GIB
        self.daemon.shed_memory(self.now + 130)  # stopped at 130, paused for 30 of its 130 s
        self.runner.running.discard(t.id)
        self.daemon.check(self.now + 131)
        self.assertEqual((t.state, round(t.attempts_wall), round(t.frozen_total), t.frozen_attempt),
                         ("queued", 101, 30, 0.0))

    def cgroup(self, t, frozen, own=None):
        """The build's cgroup: frozen in effect, and its own freeze setting (as frozen, unless a
        parent holds it)."""
        t.cg = os.path.join(self.tmp.name, "cg", t.id)
        os.makedirs(t.cg, exist_ok=True)
        with open(os.path.join(t.cg, "cgroup.events"), "w") as f:
            f.write(f"populated 1\nfrozen {1 if frozen else 0}\n")
        with open(os.path.join(t.cg, "cgroup.freeze"), "w") as f:
            f.write("1\n" if (frozen if own is None else own) else "0\n")

    def test_a_thaw_that_cant_even_be_sent_still_ends_in_a_stop(self):
        t = self.submit(("sh", "tests.sh"), cores=2)  # a script: stopped, it fails with the reason
        t.mem_now = t.est_mem = 6 * GIB
        self.cgroup(t, frozen=True)
        self.daemon.shed_memory(self.now + 100)  # found paused
        self.runner.freeze = lambda ticket, frozen: (_ for _ in ()).throw(OSError("no way to thaw"))
        self.machine.mem_available = 100 * GIB
        self.daemon.shed_memory(self.now + 120)  # asked to go on, but the request fails
        self.daemon.shed_memory(self.now + 181)
        self.assertTrue(t.requeue_pending)
        self.assertIn("couldn't be resumed", t.shed_error or "")  # not "to free memory" with plenty free

    def test_a_pause_that_ended_while_the_daemon_was_away_ends_on_load(self):
        (t,) = self.running(1)
        self.machine.mem_available = 8 * GIB  # short: buildq keeps it paused
        self.cgroup(t, frozen=True)
        self.daemon.shed_memory(self.now + 100)
        self.assertTrue(t.frozen)
        self.daemon.save(t)
        self.cgroup(t, frozen=False)  # resumed meanwhile
        again = daemon.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                                     queries=common.InlineExecutor())
        again.load_tickets(self.now + 130)
        loaded = again.tickets[t.id]
        self.assertEqual((loaded.frozen, round(loaded.frozen_attempt)), (False, 30))
        again.stop_memory_watch()
        for x in again.tickets.values():
            again.close_jobserver(x)

    def test_a_slice_set_frozen_isnt_taken_for_buildqs_pause_on_older_systemd_either(self):
        (t,) = self.running(1)
        self.cgroup(t, frozen=True, own=True)  # older systemd sets each member's own setting too
        with open(os.path.join(os.path.dirname(t.cg), "cgroup.freeze"), "w") as f:
            f.write("1\n")  # ... and its slice's
        self.machine.mem_available = 8 * GIB
        for second in range(100, 500, 5):
            self.daemon.shed_memory(self.now + second)
        self.assertEqual((t.frozen, t.requeue_pending), (False, False))

    def test_an_unconfirmed_thaw_keeps_its_minute_across_a_clean_restart(self):
        t = self.submit(("sh", "tests.sh"), cores=2)
        t.mem_now = t.est_mem = 6 * GIB
        self.cgroup(t, frozen=True)
        self.daemon.shed_memory(self.now + 100)  # found paused
        self.machine.mem_available = 100 * GIB
        self.daemon.shed_memory(self.now + 120)  # asked to go on; it doesn't (its own setting stays 1)
        sent = len(self.runner.frozen)
        self.daemon.thaw_all(self.now + 130)  # the daemon stops
        self.assertEqual(self.runner.frozen[sent:], [(t.id, False)])  # one last thaw on the way out
        again = daemon.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                                     queries=common.InlineExecutor())
        again.load_tickets(self.now + 140)
        loaded = again.tickets[t.id]
        self.assertEqual(loaded.freeze_asked[0], False)  # still asked for, sent again
        again.shed_memory(self.now + 201)
        self.assertTrue(loaded.requeue_pending)  # its minute ran out: stopped, not paused again
        again.stop_memory_watch()
        for x in again.tickets.values():
            again.close_jobserver(x)

    def test_the_undo_of_a_freeze_that_never_took_is_tracked_like_any_thaw(self):
        t = self.submit(("sh", "tests.sh"), cores=2)
        t.mem_now = 6 * GIB
        self.cgroup(t, frozen=False)
        self.falling_to(4 * GIB, self.now + 100)
        self.daemon.shed_memory(self.now + 100)  # asked to pause
        self.cgroup(t, frozen=False, own=True)  # set, but never in effect (a task stuck in the kernel)
        self.daemon.shed_memory(self.now + 161)  # gives up, undoes it; the undo doesn't take either
        self.assertEqual(t.freeze_asked[0], False)
        self.daemon.shed_memory(self.now + 222)
        self.assertTrue(t.requeue_pending)

    def test_with_shedding_off_a_thaw_that_never_takes_isnt_asked_again(self):
        (t,) = self.running(1)
        self.cgroup(t, frozen=True)
        self.daemon.cfg.shed_memory = False
        self.daemon.shed_memory(self.now + 100)  # adopted, then asked to go on: shedding is off
        self.daemon.shed_memory(self.now + 161)  # it didn't: left paused
        asks = len(self.runner.frozen)
        for second in range(162, 400, 10):
            self.daemon.shed_memory(self.now + second)
        self.assertEqual(len(self.runner.frozen), asks)
        self.assertTrue(t.thaw_failed)
        self.cgroup(t, frozen=False)  # someone thawed it after all
        self.daemon.shed_memory(self.now + 400)
        self.assertEqual((t.frozen, t.thaw_failed), (False, False))
        self.cgroup(t, frozen=True)  # paused again, by someone
        self.daemon.shed_memory(self.now + 410)
        self.assertEqual(self.runner.frozen[asks:], [(t.id, False)])  # this pause is asked to end

    def test_a_thaw_that_failed_is_forgotten_once_a_restart_finds_the_build_running(self):
        (t,) = self.running(1)
        self.cgroup(t, frozen=True)
        self.daemon.cfg.shed_memory = False
        self.daemon.shed_memory(self.now + 100)
        self.daemon.shed_memory(self.now + 161)
        self.assertTrue(t.thaw_failed)
        self.daemon.save(t)
        self.cgroup(t, frozen=False)  # someone thawed it while the daemon was away
        again = daemon.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                                     queries=common.InlineExecutor())
        again.load_tickets(self.now + 200)
        loaded = again.tickets[t.id]
        self.assertEqual((loaded.frozen, loaded.thaw_failed), (False, False))
        again.stop_memory_watch()
        for x in again.tickets.values():
            again.close_jobserver(x)

    def test_a_build_frozen_by_a_parent_isnt_buildqs_pause_and_isnt_stopped(self):
        builds = self.running(2)
        for t in builds:
            self.cgroup(t, frozen=True, own=False)  # someone froze buildq.slice
        self.machine.mem_available = 100 * GIB
        for second in range(100, 400, 5):
            self.daemon.shed_memory(self.now + second)
        self.assertEqual([(t.frozen, t.requeue_pending) for t in builds], [(False, False), (False, False)])
        self.assertEqual(self.runner.forced, [])

    def test_a_pause_counts_only_once_the_cgroup_says_it_took(self):
        (t,) = self.running(1)
        self.cgroup(t, frozen=False)
        self.falling_to(4 * GIB, self.now + 100)
        self.daemon.shed_memory(self.now + 100)
        self.assertTrue(t.frozen)
        self.assertIsNone(t.frozen_at)  # asked, not yet seen
        self.cgroup(t, frozen=True)
        self.daemon.shed_memory(self.now + 102)
        self.assertEqual(t.frozen_at, self.now + 102)

    def test_a_freeze_that_never_takes_is_undone_and_not_tried_again(self):
        (t,) = self.running(1)
        self.cgroup(t, frozen=False)
        self.falling_to(4 * GIB, self.now + 100)
        self.daemon.shed_memory(self.now + 100)
        for second in range(101, 159):  # every tick for most of a minute: one retry, not one a tick
            self.daemon.shed_memory(self.now + second)
        self.assertEqual(self.runner.frozen, [(t.id, True), (t.id, True)])
        self.daemon.shed_memory(self.now + 161)  # a minute on, still running
        self.assertEqual((t.frozen, t.freeze_failed, t.frozen_attempt), (False, True, 0.0))
        self.assertEqual(self.runner.frozen[-1], (t.id, False))  # undone, so it can't land later
        tries = len(self.runner.frozen)
        self.falling_to(4 * GIB, self.now + 200)
        self.daemon.shed_memory(self.now + 200)
        self.assertEqual(len(self.runner.frozen), tries)

    def test_a_build_frozen_or_running_against_its_ticket_is_taken_as_it_is(self):
        (t,) = self.running(1)
        self.machine.mem_available = 8 * GIB  # short (under the floor), so nothing resumes it
        self.cgroup(t, frozen=True)  # a freeze that landed late, or one from before a restart
        self.daemon.shed_memory(self.now + 100)
        self.assertEqual((t.frozen, t.frozen_at), (True, self.now + 100))
        self.cgroup(t, frozen=False)  # a thaw that landed after the restart
        self.daemon.shed_memory(self.now + 130)
        self.assertEqual((t.frozen, round(t.frozen_attempt)), (False, 30))

    def test_a_thaw_ends_the_pause_when_it_takes_and_one_that_never_does_stops_the_build(self):
        (t,) = self.running(1)
        t.est_mem = t.mem_now
        self.cgroup(t, frozen=True)
        self.daemon.shed_memory(self.now + 100)  # found paused: from now
        self.machine.mem_available = 100 * GIB
        self.daemon.shed_memory(self.now + 120)  # asked to go on
        self.assertEqual(self.runner.frozen[-1], (t.id, False))
        self.assertTrue(t.frozen)  # not until the cgroup says so
        self.cgroup(t, frozen=False)
        self.daemon.shed_memory(self.now + 125)
        self.assertEqual((t.frozen, round(t.frozen_attempt)), (False, 25))
        (u,) = self.running(1)
        u.est_mem = u.mem_now
        self.cgroup(u, frozen=True)
        self.machine.mem_available = 8 * GIB
        self.daemon.shed_memory(self.now + 200)
        self.machine.mem_available = 100 * GIB
        self.daemon.last_freeze = -1e9
        self.daemon.shed_memory(self.now + 220)  # asked to go on; it never does
        self.daemon.shed_memory(self.now + 281)
        self.assertTrue(u.requeue_pending)  # it can't go on, so it is stopped

    def test_a_cancelled_build_is_killed_outright_when_memory_is_short(self):
        (t,) = self.running(1)
        self.daemon.cancel(t, self.now)  # SIGTERM: it has a minute to go
        self.assertNotIn(t.id, self.runner.forced)
        self.machine.mem_available = 1 * GIB
        self.daemon.shed_memory(self.now + 5)
        self.assertIn(t.id, self.runner.forced)

    def test_memory_being_freed_counts_so_one_shortfall_stops_one_build(self):
        builds = self.running(3)
        self.machine.mem_available = 1 * GIB
        self.daemon.shed_memory(self.now + 100)
        self.daemon.shed_memory(self.now + 103)  # the first one hasn't gone yet, but its 6G is on the way
        self.assertEqual(len(self.runner.forced), 1)
        del builds

    def test_small_builds_are_left_alone(self):
        (t,) = self.running(1)
        t.mem_now = 100 * common.MIB  # what holds the memory isn't buildq's
        self.machine.mem_available = 1 * GIB
        self.daemon.shed_memory(self.now + 100)
        self.assertEqual(self.runner.forced, [])

    def test_a_script_is_stopped_and_failed_not_run_again(self):
        script = self.submit(("sh", "deploy-then-test.sh"), cores=2)
        script.mem_now = 6 * GIB
        self.machine.mem_available = 1 * GIB
        self.daemon.shed_memory(self.now + 100)
        self.runner.running.discard(script.id)
        self.daemon.check(self.now + 101)
        self.assertEqual(script.state, "failed")
        self.assertIn("might repeat what it had done", script.error)
        self.machine.mem_available = 100 * GIB
        safe = self.submit(("sh", "only-tests.sh"), cores=2, restartable=True)
        self.assertEqual(safe.state, "running")
        safe.mem_now = 6 * GIB
        self.machine.mem_available = 1 * GIB
        self.daemon.shed_memory(self.now + 110)
        self.runner.running.discard(safe.id)
        self.daemon.check(self.now + 111)
        self.assertEqual((safe.state, safe.requeues), ("queued", 1))

    def test_a_script_named_in_restartable_scripts_is_queued_again(self):
        self.daemon.cfg.restartable_scripts = frozenset({"wrap.sh"})
        script = self.submit(("bash", "ci/wrap.sh", "rust-tests"), cores=2)
        script.mem_now = 6 * GIB
        self.machine.mem_available = 1 * GIB
        self.daemon.shed_memory(self.now + 100)
        self.runner.running.discard(script.id)
        self.daemon.check(self.now + 101)
        self.assertEqual((script.state, script.requeues), ("queued", 1))

    def test_which_scripts_restartable_scripts_covers(self):
        scripts = frozenset({"wrap.sh", "tests.py"})
        safe = commands.restart_safe
        self.assertTrue(safe(["bash", "ci/wrap.sh", "label"], scripts))
        self.assertTrue(safe(["./wrap.sh"], scripts))
        self.assertTrue(safe(["/home/x/wrap.sh", "a"], scripts))
        self.assertTrue(safe(["python3", "tests.py"], scripts))
        self.assertFalse(safe(["bash", "ci/wrap.sh"]))  # not listed
        self.assertFalse(safe(["bash", "-c", "wrap.sh"], scripts))  # a command string: anything could be in it
        self.assertFalse(safe(["bash", "deploy.sh"], scripts))
        self.assertFalse(safe(["bash", "-e", "wrap.sh"], scripts))  # an option before it: not straight after
        self.assertFalse(safe(["python3", "-u", "tests.py"], scripts))
        self.assertTrue(safe(["env", "FOO=1", "bash", "wrap.sh"], scripts))  # wrappers are seen through
        self.assertTrue(safe(["nice", "./wrap.sh"], scripts))
        self.assertFalse(safe(["nice", "-n", "5", "./wrap.sh"], scripts))  # a wrapper given options isn't read through
        self.assertTrue(safe(["timeout", "1h", "bash", "wrap.sh"], scripts))
        self.assertFalse(safe(["env", "-u", "wrap.sh", "bash", "deploy.sh"], scripts))  # -u's operand isn't the command
        self.assertFalse(safe(["env", "-S", "bash wrap.sh"], scripts))  # a command string: unread
        for argv in (["env", "-iu", "wrap.sh", "bash", "deploy.sh"], ["env", "-a", "wrap.sh", "bash", "deploy.sh"],
                     ["env", "--argv0", "wrap.sh", "bash", "deploy.sh"], ["env", "-iC", "wrap.sh", "bash", "deploy.sh"],
                     ["env", "--un", "wrap.sh", "bash", "deploy.sh"], ["env", "--", "-u", "wrap.sh", "cargo", "check"],
                     ["env", "X=1", "-u", "wrap.sh", "cargo", "check"], ["sudo", "-D", "wrap.sh", "bash", "deploy.sh"]):
            self.assertFalse(safe(argv, scripts), argv)  # an option's value is never taken for the script
        self.assertEqual(commands.strip_wrappers(["env", "-i", "A=1", "cargo", "test"]), ["cargo", "test"])
        self.assertFalse(safe(["env", "X-Y=./wrap.sh", "bash", "deploy.sh"], scripts))  # GNU env: any word with "="
        self.assertFalse(safe(["/usr/bin/time", "-f", "eslint", "bash", "deploy.sh"], scripts))  # -f's value isn't the tool
        self.assertFalse(commands.reusable_check(["/usr/bin/time", "-f", "eslint", "bash", "deploy.sh"]))
        self.assertFalse(safe(["nice", "-n", "5", "cargo", "test"]))  # (a wrapper given options: not read for certain)
        for argv in (["env", "--split-string=bash deploy.sh", "cargo", "test"], ["env", "-Sbash deploy.sh", "cargo", "test"],
                     ["env", "--split-string=bash deploy.sh", "bash", "wrap.sh"], ["sudo", "X-Y=./wrap.sh", "bash", "deploy.sh"]):
            self.assertFalse(safe(argv, scripts), argv)  # an option with "=" in it isn't an assignment
            self.assertFalse(commands.reusable_check(argv), argv)
        self.assertTrue(safe(["env", "-u", "RUSTFLAGS", "cargo", "test"]))  # env's exact forms are read for certain
        self.assertTrue(safe(["env", "-i", "PATH=/usr/bin", "cargo", "check"]))
        self.assertTrue(safe(["timeout", "1h", "cargo", "test"]))
        for argv in (["env", "--chdir=", "-C", ".", "cargo", "test"], ["env", "--unset=", "-i", "cargo", "test"]):
            self.assertTrue(safe(argv), argv)  # an empty value fails or is overridden: the command is the same
        # A command is run, not read by a shell: a word with "=" is an assignment only where sudo or
        # env takes one. Before the command or after another wrapper, it is a path (the folder "X=.").
        for argv in (["X=./deploy.sh", "cargo", "test"], ["nice", "X=./deploy.sh", "cargo", "test"],
                     ["timeout", "1h", "X=./deploy.sh", "cargo", "test"]):
            self.assertFalse(safe(argv), argv)
            self.assertFalse(commands.reusable_check(argv), argv)
        for argv in (["sudo", "X=1", "cargo", "test"], ["env", "RUST_LOG=debug", "cargo", "test"]):
            self.assertTrue(safe(argv), argv)
            self.assertTrue(commands.reusable_check(argv), argv)
        # `pnpm exec BIN` runs a binary, never a package script (pnpm.io/cli/exec); with any of
        # pnpm's own options (a filter, -r, -c's shell), before or after exec, it isn't read.
        for argv in (["pnpm", "exec", "vitest", "run"], ["pnpm", "exec", "tsc", "--noEmit"]):
            self.assertTrue(commands.reusable_check(argv), argv)
            self.assertTrue(safe(argv), argv)
        for argv in (["pnpm", "exec", "-c", "vitest run"], ["pnpm", "-r", "exec", "vitest", "run"],
                     ["pnpm", "exec", "--filter", "x", "vitest", "run"], ["pnpm", "vitest", "run"], ["pnpm", "exec"]):
            self.assertFalse(commands.reusable_check(argv), argv)
            self.assertFalse(safe(argv), argv)
        # history keys and lanes still read env's exact forms (a decision to restart or reuse never does)
        words = commands.command_words
        self.assertEqual(words(["env", "-u", "CI", "pnpm", "test"]), words(["pnpm", "test"]))
        self.assertEqual(words(["env", "--unset=X", "-C", "api", "dotnet", "test"]), words(["dotnet", "test"]))
        self.assertEqual(words(["env", "-u", "RUSTFLAGS", "cargo", "test", "-p", "api"]), words(["cargo", "test", "-p", "api"]))
        # a listed script that runs the command after its `--` is only as safe as that command
        self.assertTrue(safe(["bash", "wrap.sh", "label", "12G", "--", "cargo", "check"], scripts))
        self.assertFalse(safe(["bash", "wrap.sh", "label", "--", "git", "push"], scripts))
        self.assertFalse(safe(["bash", "wrap.sh", "--"], scripts))
        self.assertFalse(safe(["./wrap.sh", "--", "git", "push"], scripts))  # run directly, too
        self.assertTrue(safe(["bash", "wrap.sh", "--", "--nocapture"], scripts))  # its own arguments, not a command
        self.assertFalse(safe(["bash", "wrap.sh", "--", "--", "git", "push"], scripts))  # another `--`: a command after it
        self.assertFalse(safe(["bash", "wrap.sh", "--", "-a", "x", "git", "push"], scripts))  # a short option: fails safe
        self.assertFalse(safe(["wrap.sh", "--"] * 1100 + ["cargo", "check"], scripts))  # deep, but no RecursionError
        for tool in ("bash", "git", "cargo", "env", "python3.12", "python2", "node22", "nice", "sudo", "xargs", "mvn",
                     "heavy-run", "buildq", "bash5", "nodejs", "systemd-run"):
            with self.assertRaises(ValueError, msg=tool) as refused:
                config.Config({"restartable_scripts": [tool]}, 64 * GIB, 16)
            self.assertIn("names scripts, not tools", str(refused.exception))
        cfg = config.Config({"restartable_scripts": ["wrap.sh"]}, 64 * GIB, 16)
        self.assertEqual((cfg.restartable_scripts, cfg.summary()["restartable_scripts"]), ({"wrap.sh"}, ["wrap.sh"]))
        self.assertEqual(config.Config({}, 64 * GIB, 16).restartable_scripts, frozenset())
        with self.assertRaises(ValueError):
            config.Config({"restartable_scripts": ["../x.sh"]}, 64 * GIB, 16)

    def test_a_cancel_while_it_is_being_stopped_wins(self):
        (t,) = self.running(1)
        self.machine.mem_available = 1 * GIB
        self.daemon.shed_memory(self.now + 100)
        self.daemon.cancel(t, self.now + 100)
        self.runner.running.discard(t.id)
        self.daemon.check(self.now + 101)
        self.assertEqual(t.state, "cancelled")

    def test_each_attempt_has_a_service_name_of_its_own(self):
        (t,) = self.running(1)
        self.machine.mem_available = 1 * GIB
        self.daemon.shed_memory(self.now + 100)
        self.runner.running.discard(t.id)
        self.daemon.check(self.now + 101)
        self.assertEqual(t.requeues, 1)
        runners.SystemdRunner(self.tmp.name).prepare(t, "buildq.slice")
        self.assertTrue(t.unit.endswith(f"{t.id}-r1.service"))

    def test_systemd_result_detects_oom_kills_with_oom_policy_continue(self):
        runner = runners.SystemdRunner(self.tmp.name)
        t = self.submit()
        t.unit = None
        with mock.patch.object(runners.subprocess, "run", side_effect=AssertionError("must not call systemctl")):
            self.assertEqual(runner.result(t), "")
        t.unit = "buildq-test.service"
        cases = [
            ("LoadState=not-found\nResult=success\nOOMKills=18446744073709551615\nManagedOOMKills=18446744073709551615\n", ""),
            ("LoadState=loaded\nResult=signal\nOOMKills=18446744073709551615\nManagedOOMKills=18446744073709551615\n", "signal"),
            ("LoadState=loaded\nResult=signal\nOOMKills=18446744073709551615\nManagedOOMKills=2\n", "managed-oom-kill"),
            ("LoadState=loaded\nResult=signal\nOOMKills=1\nManagedOOMKills=18446744073709551615\n", "oom-kill"),
            ("LoadState=loaded\nResult=signal\nOOMKills=1\nManagedOOMKills=5\n", "oom-kill"),
            ("LoadState=loaded\nResult=signal\nOOMKills=0\nManagedOOMKills=38\n", "managed-oom-kill"),
            ("LoadState=loaded\nResult=signal\nOOMKills=1\nManagedOOMKills=0\n", "oom-kill"),
            ("LoadState=loaded\nResult=signal\nOOMKills=0\nManagedOOMKills=0\n", "signal"),
            ("LoadState=loaded\nResult=oom-kill\n", "oom-kill"),
            ("LoadState=loaded\nResult=exit-code\nOOMKills=[not set]\nManagedOOMKills=\n", "exit-code"),
        ]
        for stdout, expected in cases:
            with mock.patch.object(runners.subprocess, "run",
                                   return_value=subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")):
                self.assertEqual(runner.result(t), expected, stdout)

    def test_managed_oom_kill_records_oom_without_inflating_future_estimates(self):
        cwd = self.workdir("oomd")
        base = self.submit(("cargo", "test"), cwd=cwd)
        base.measured, base.mem_peak = True, 2 * GIB
        self.finish(base, 0)
        self.now += 40
        victim = self.submit(("cargo", "test"), cwd=cwd, again=True)
        self.assertLess(victim.est_mem, 5 * GIB)
        victim.measured, victim.mem_peak = True, 2 * GIB
        self.runner.running.discard(victim.id)
        self.runner.result = lambda ticket: "managed-oom-kill"
        self.daemon.check(self.now + 40)
        self.assertEqual((victim.state, victim.oom), ("lost", True))
        self.assertIn("systemd-oomd", victim.error)
        # Slice/ancestor managed-oom kill must not inflate this command's next reservation to MemoryMax.
        self.now += 50
        after = self.submit(("cargo", "test"), cwd=cwd, again=True)
        self.assertLess(after.est_mem, 5 * GIB)
        # A kernel OOM kill on the unit's own cgroup still teaches history to reserve more.
        after.measured, after.mem_peak = True, 2 * GIB
        self.runner.running.discard(after.id)
        self.runner.result = lambda ticket: "oom-kill"
        self.daemon.check(self.now + 40)
        self.assertEqual((after.state, after.oom), ("lost", True))
        self.now += 50
        inflated = self.submit(("cargo", "test"), cwd=cwd, again=True)
        self.assertGreaterEqual(inflated.est_mem, 20 * GIB)

    def test_systemd_slice_oom_kill_does_not_inflate_future_estimates(self):
        # Part 2 of #79: distinguish shared-slice OOM kills from own-cap OOM kills via memory.events.local.
        cg = os.path.join(self.tmp.name, "cg-slice-oom")
        os.makedirs(cg, exist_ok=True)
        with open(os.path.join(cg, "memory.events.local"), "w") as f:
            f.write("low 0\nhigh 0\nmax 0\noom 0\noom_kill 1\n")
        runner = runners.SystemdRunner(self.tmp.name)
        probe = self.submit(("cargo", "check"), cwd=self.workdir("probe"))
        probe.unit, probe.cg = "buildq-probe.service", cg
        out = "LoadState=loaded\nResult=signal\nOOMKills=1\nManagedOOMKills=0\n"
        with mock.patch.object(runners.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, stdout=out, stderr="")):
            self.assertEqual(runner.result(probe), "slice-oom-kill")
        with open(os.path.join(cg, "memory.events.local"), "w") as f:
            f.write("low 0\nhigh 0\nmax 0\noom 1\noom_kill 1\n")
        with mock.patch.object(runners.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, stdout=out, stderr="")):
            self.assertEqual(runner.result(probe), "oom-kill")

        # Even if the cgroup is already gone when systemd reports OOMKills=1, a prior 1 Hz sample
        # that saw memory.events.local `oom 0` prevents inflating future estimates.
        cwd = self.workdir("slice-sampled")
        base = self.submit(("cargo", "test"), cwd=cwd)
        base.measured, base.mem_peak = True, 2 * GIB
        self.finish(base, 0)
        self.now += 40
        victim = self.submit(("cargo", "test"), cwd=cwd, again=True)
        self.assertLess(victim.est_mem, 5 * GIB)
        victim.cg = cg
        with open(os.path.join(cg, "memory.events.local"), "w") as f:
            f.write("low 0\nhigh 0\nmax 0\noom 0\noom_kill 1\n")
        with mock.patch.object(machine, "cgroup_stats", return_value=(1_000_000, 2 * GIB, 1)), \
                mock.patch.object(machine, "cgroup_swap", return_value=0), \
                mock.patch.object(machine, "cgroup_memory_waits", return_value=None):
            victim.measure(self.now + 10)
        self.assertIs(victim.sampled_own_oom, False)
        self.runner.running.discard(victim.id)
        self.runner.result = lambda ticket: "oom-kill"
        self.daemon.check(self.now + 40)
        self.assertEqual((victim.state, victim.oom), ("lost", True))
        self.assertIn("slice memory limit", victim.error)
        self.now += 50
        after = self.submit(("cargo", "test"), cwd=cwd, again=True)
        self.assertLess(after.est_mem, 5 * GIB)

    def test_turning_it_off_lets_paused_builds_go_on(self):
        (t,) = self.running(1)
        self.falling_to(4 * GIB, self.now + 100)
        self.daemon.shed_memory(self.now + 100)
        self.assertTrue(t.frozen)
        self.daemon.cfg.shed_memory = False
        self.daemon.shed_memory(self.now + 101)
        self.assertFalse(t.frozen)

    def test_a_paused_build_goes_on_once_there_is_room_for_it(self):
        (t,) = self.running(1)
        t.est_mem = 10 * GIB
        self.falling_to(4 * GIB, self.now + 100)
        self.daemon.shed_memory(self.now + 100)
        self.assertTrue(t.frozen)
        self.machine.mem_available = 12 * GIB  # above the floor, but not with its growth (4G more)
        self.daemon.shed_memory(self.now + 120)
        self.assertTrue(t.frozen)
        self.machine.mem_available = 16 * GIB
        self.daemon.shed_memory(self.now + 140)
        self.assertFalse(t.frozen)
        self.assertEqual(self.runner.frozen[-1], (t.id, False))

    def test_a_build_without_its_environment_is_stopped_and_fails_clearly(self):
        (t,) = self.running(1)
        t.env = None  # the daemon restarted since it was submitted
        self.machine.mem_available = 1 * GIB
        self.daemon.shed_memory(self.now + 100)
        self.runner.running.discard(t.id)
        self.daemon.check(self.now + 101)
        self.assertEqual(t.state, "failed")
        self.assertIn("stopped to free memory", t.error)
        self.assertNotIn(t.key, self.daemon.history.by_key)  # a stopped run teaches nothing

    def test_a_build_that_finished_first_is_kept_as_it_ended(self):
        (t,) = self.running(1)
        self.machine.mem_available = 1 * GIB
        self.daemon.shed_memory(self.now + 100)
        with open(os.path.join(t.dir, "result.json"), "w") as f:
            json.dump({"exit": 0}, f)
        self.runner.running.discard(t.id)
        self.daemon.check(self.now + 101)
        self.assertEqual(t.state, "succeeded")

    def test_nothing_happens_with_memory_to_spare_or_when_turned_off(self):
        self.running(2)
        self.daemon.shed_memory(self.now + 100)
        self.machine.mem_available = 1 * GIB
        self.daemon.cfg.shed_memory = False
        self.daemon.shed_memory(self.now + 200)
        self.assertEqual((self.runner.frozen, self.runner.stopped), ([], []))



class Memory(DaemonCase):
    def test_more_cores_need_more_memory_unless_told_never(self):
        t = self.submit()
        self.daemon.history.add({"key": t.key, "t": self.now, "peak_mem": 8 * GIB, "peak_cores": 2.0, "granted": 2})
        self.daemon.estimate(t, self.now)
        base = t.mem_base
        self.assertEqual(self.daemon.mem_for(t, 2), base)
        self.assertEqual(self.daemon.mem_for(t, 3), int(base * 3 / 2))  # "always", the default
        self.assertEqual(self.daemon.mem_for(t, 16), 26 * GIB)  # no more than the enforced cap
        self.daemon.cfg.scale_mem_with_cores = "never"
        self.assertEqual((self.daemon.mem_for(t, 3), self.daemon.mem_for(t, 16)), (base, base))
        self.daemon.cfg.scale_mem_with_cores = "always"
        self.assertEqual(config.Config({}, 64 * GIB, 16).scale_mem_with_cores, "always")
        self.assertEqual(config.Config({"scale_mem_with_cores": True}, 64 * GIB, 16).scale_mem_with_cores, "always")
        self.assertEqual(config.Config({"scale_mem_with_cores": False}, 64 * GIB, 16).scale_mem_with_cores, "never")
        for wrong in ("cold", "sometimes"):
            with self.assertRaises(ValueError):
                config.Config({"scale_mem_with_cores": wrong}, 64 * GIB, 16)

    def test_the_grant_is_cut_to_what_memory_allows(self):
        tree = self.workdir("tree")
        first = self.submit(cwd=tree)
        self.daemon.history.add({"key": first.key, "t": self.now, "peak_mem": 4 * GIB, "peak_cores": 8.0, "granted": 8})
        self.finish(first)
        self.machine.mem_available = 26 * GIB  # 16G above the floor
        t = self.submit(cwd=tree)  # wants 12 cores; at 4.85G per 8 cores, 12 would need 7.3G
        self.assertEqual(t.state, "running")
        self.assertEqual(t.granted, 12)
        self.machine.mem_available = 15 * GIB  # 5G above the floor: room for 8 cores (4.85G), not 12
        self.finish(t)
        u = self.submit(cwd=tree)
        self.assertEqual(u.granted, 8)
        self.assertLessEqual(u.est_mem, 5 * GIB)

    def test_growing_a_build_charges_its_memory(self):
        project = self.workdir("project")
        open(os.path.join(project, "Cargo.lock"), "w").close()
        self.machine.busy = self.machine.busy_now = 24.0
        t = self.submit(cwd=project)  # cold: 20G at 8 cores, given 8 of its 16
        self.assertEqual(t.granted, 8)
        t.elastic, t.cores_now = True, 8.0
        self.machine.busy = self.machine.busy_now = 8.0
        self.machine.mem_available = 33 * GIB  # 20G of growth held, 10G floor: 3G free
        self.daemon.rebalance(self.now + 120)
        self.assertLess(t.granted, 16)  # 16 cores would need 26G, 6G more than it holds
        self.assertLessEqual(t.est_mem - 20 * GIB, 3 * GIB)


    def seen_at_8_cores(self):
        tree = self.workdir("tree")
        first = self.submit(cwd=tree)
        self.daemon.history.add({"key": first.key, "t": self.now, "peak_mem": 4 * GIB, "peak_cores": 8.0, "granted": 8})
        self.finish(first)
        return tree

    def test_with_never_a_build_starts_with_all_its_cores_when_memory_is_tight(self):
        tree = self.seen_at_8_cores()
        self.daemon.cfg.scale_mem_with_cores = "never"
        self.machine.mem_available = 15 * GIB  # 5G above the floor: scaled to 12 cores it would need 7.3G
        t = self.submit(cwd=tree)
        self.assertEqual((t.state, t.granted), ("running", 12))
        self.assertLessEqual(t.est_mem, 5 * GIB)

    def test_with_never_growing_a_build_costs_no_memory(self):
        tree = self.seen_at_8_cores()
        self.daemon.cfg.scale_mem_with_cores = "never"
        self.machine.busy = self.machine.busy_now = 24.0
        t = self.submit(cwd=tree)
        start_cores, plan = t.granted, t.est_mem
        self.assertLess(start_cores, 12)
        t.elastic, t.cores_now = True, float(start_cores)
        self.machine.busy = self.machine.busy_now = 8.0
        self.machine.mem_available = 10 * GIB + plan + GIB // 2  # half a gigabyte free past its plan and the floor
        self.daemon.rebalance(self.now + 120)
        self.assertGreater(t.granted, start_cores)
        self.assertEqual(t.est_mem, plan)



class Outgrowing(DaemonCase):
    def test_a_build_past_its_estimate_is_planned_at_its_cap(self):
        t = self.submit()
        t.measured, t.mem_now = True, t.est_mem + GIB
        self.daemon.outgrown(t)
        self.assertEqual(t.est_mem, 26 * GIB)

    def test_a_full_rebuild_isnt_forgotten_after_many_small_runs(self):
        t = self.submit()
        self.daemon.history.add({"key": t.key, "t": self.now - 86400, "peak_mem": 18 * GIB, "peak_cores": 8,
                                 "granted": 8})
        for _ in range(150):  # more than the history keeps run by run
            self.daemon.history.add({"key": t.key, "t": self.now, "peak_mem": GIB, "peak_cores": 1, "granted": 8})
        self.daemon.estimate(t, self.now)
        self.assertGreater(t.mem_base, 18 * GIB)
        again = history.History(self.daemon.history.path)  # and it survives a restart
        e = again.estimate(t.key, self.daemon.cfg.lanes["heavy"], self.daemon.cfg, 32, True, False, self.now)
        self.assertGreater(e.mem, 18 * GIB)

    def test_a_kill_from_outside_doesnt_teach_the_cap(self):
        self.assertFalse(supervisor.oom_of({"oom_kills": 1, "hit_own_cap": False}))
        self.assertTrue(supervisor.oom_of({"oom_kills": 1, "hit_own_cap": True}))



class SliceGuard(DaemonCase):
    def test_it_never_loosens_the_configured_limit(self):
        self.daemon.runner = self.runner  # a systemd runner
        self.daemon.cg_base = os.path.join(self.tmp.name, "cg")
        slice_dir = os.path.join(self.daemon.cg_base, "buildq.slice")
        os.makedirs(slice_dir)
        with open(os.path.join(slice_dir, "memory.current"), "w") as f:
            f.write(str(10 * GIB))
        self.daemon.slice_limit = str(40 * GIB)  # what the drop-in says
        self.machine.mem_available = 100 * GIB
        self.daemon.guard_slice()
        with open(os.path.join(slice_dir, "memory.max")) as f:
            self.assertEqual(int(f.read()), 40 * GIB)  # 10 held + 95 free would be more
        self.machine.mem_available = 8 * GIB  # other work grew
        self.daemon.guard_slice()
        with open(os.path.join(slice_dir, "memory.max")) as f:
            self.assertEqual(int(f.read()), 10 * GIB + 3 * GIB)  # held + free above half the floor
        self.machine.mem_available = 1 * GIB
        self.daemon.guard_slice()
        with open(os.path.join(slice_dir, "memory.max")) as f:
            self.assertEqual(int(f.read()), 12 * GIB)  # never below what is held, plus 2G
        self.daemon.guard_slice(stopping=True)
        with open(os.path.join(slice_dir, "memory.max")) as f:
            self.assertEqual(int(f.read()), 40 * GIB)  # the configured limit back


    def test_it_guards_every_tick_while_memory_is_short_or_falling(self):
        self.daemon.runner = self.runner
        self.daemon.cg_base = os.path.join(self.tmp.name, "cg")
        slice_dir = os.path.join(self.daemon.cg_base, "buildq.slice")
        os.makedirs(slice_dir)
        with open(os.path.join(slice_dir, "memory.current"), "w") as f:
            f.write(str(10 * GIB))
        self.daemon.slice_limit = "max"
        self.machine.mem_available = 100 * GIB
        guarded = []
        real = self.daemon.guard_slice
        self.daemon.guard_slice = lambda stopping=False: (guarded.append(self.machine.mem_available), real(stopping))
        self.machine.sample = lambda tick: None  # the test sets the numbers
        start = time.monotonic()
        with mock.patch.object(daemon.time, "monotonic", side_effect=lambda: start + len(ticks)):
            ticks = []
            for available in (100, 100, 100, 98, 98, 98, 98):  # GiB: a 2G fall, then steady
                self.machine.mem_available = available * GIB
                ticks.append(available)
                self.daemon.tick(self.now + len(ticks))
        # the first tick rebalances (and guards); quiet ticks don't; a 2G fall guards, then steady
        # ticks with plenty free wait for the next rebalance (five seconds on: the sixth tick)
        self.assertEqual([g // GIB for g in guarded], [100, 98, 98])
        guarded.clear()
        self.daemon.cfg.mem_floor = 60 * GIB  # less than twice the floor free: short
        for _ in range(3):
            ticks.append(0)
            with mock.patch.object(daemon.time, "monotonic", return_value=start + 100 + len(ticks) * 0.0):
                self.daemon.tick(self.now + len(ticks))
        self.assertEqual(len(guarded), 3)  # every tick



class MemoryWatch(DaemonCase):
    def cgroup(self, rel, current, anon=0, procs=""):
        path = os.path.join(self.tmp.name, "cg", rel)
        os.makedirs(path, exist_ok=True)
        for name, text in (("memory.current", str(current)), ("memory.stat", f"anon {anon}\nfile 0\n"),
                           ("cpu.stat", "usage_usec 0\n"), ("cgroup.procs", procs)):
            with open(os.path.join(path, name), "w") as f:
                f.write(text)
        return path

    def test_memory_waits_are_read_from_the_cgroup(self):
        path = self.cgroup("x.service", GIB)
        self.assertIsNone(machine.cgroup_memory_waits(path))  # no memory.pressure
        with open(os.path.join(path, "memory.pressure"), "w") as f:
            f.write("some avg10=1.00 avg60=0.50 avg300=0.10 total=12500000\n"
                    "full avg10=0.00 avg60=0.00 avg300=0.00 total=3000000\n")
        with open(os.path.join(path, "memory.events.local"), "w") as f:
            f.write("low 0\nhigh 5\nmax 0\noom 0\noom_kill 0\n")
        self.assertEqual(machine.cgroup_memory_waits(path), (12.5, 3.0, 5))

    def test_the_biggest_cgroups_are_found_where_the_memory_is(self):
        root = os.path.join(self.tmp.name, "cg")
        mib = common.MIB
        self.cgroup("a.slice", 10 * GIB, anon=9 * GIB)
        self.cgroup("a.slice/x.service", 7 * GIB, anon=6 * GIB, procs=f"{os.getpid()}\n")
        for i in range(10):  # many small ones beside the big one: 2G together
            self.cgroup(f"a.slice/s{i}.scope", 200 * mib, anon=200 * mib)
        self.cgroup("a.slice/gone.service", 2 * GIB)  # vanished mid-walk: no memory.stat
        os.remove(os.path.join(root, "a.slice/gone.service/memory.stat"))
        self.cgroup("b.slice", 4 * GIB, anon=3584 * mib)  # its children are all small: listed as one
        self.cgroup("b.slice/small.scope", 10 * mib, anon=10 * mib, procs=f"{os.getpid()}\n")
        self.cgroup("c.slice", 100 * mib, anon=100 * mib)
        with open(os.path.join(root, "a.slice/x.service/memory.swap.current"), "w") as f:
            f.write(str(GIB))
        found, complete = machine.biggest_cgroups(root)
        with open("/proc/self/comm") as f:
            me = f.read().strip()
        self.assertTrue(complete)
        self.assertEqual(found, [(6 * GIB, GIB, "a.slice/x.service", me),
                                 (3584 * mib, 0, "b.slice", me),  # its process, found below it
                                 (3 * GIB, 0, "a.slice (the rest)", "")])  # 9 - 6 held by the small ones and the gone one

    def test_the_walk_stops_at_its_limit_and_says_so(self):
        root = os.path.join(self.tmp.name, "cg")
        self.cgroup("a.slice", 10 * GIB, anon=9 * GIB)
        self.cgroup("a.slice/x.service", 7 * GIB, anon=6 * GIB)
        found, complete = machine.biggest_cgroups(root, max_dirs=1)
        self.assertFalse(complete)
        self.assertEqual([rel for _, _, rel, _ in found], ["a.slice"])  # listed whole, not looked into

    def test_a_swapped_out_cgroup_is_still_found(self):
        root = os.path.join(self.tmp.name, "cg")
        path = self.cgroup("a.slice", 100 * common.MIB, anon=100 * common.MIB)
        with open(os.path.join(path, "memory.swap.current"), "w") as f:
            f.write(str(8 * GIB))
        found, _ = machine.biggest_cgroups(root)
        self.assertEqual(found, [(100 * common.MIB, 8 * GIB, "a.slice", "")])

    def test_looking_for_process_names_keeps_to_its_limits(self):
        root = os.path.join(self.tmp.name, "cg")
        self.cgroup("a.slice", 10 * GIB, anon=9 * GIB)
        for i in range(30):  # many empty cgroups below it, none of them big
            os.makedirs(os.path.join(root, "a.slice", f"e{i}.scope"))
        budget = machine.WalkBudget(5, None)
        machine.biggest_process(os.path.join(root, "a.slice"), budget=budget)
        self.assertEqual((budget.dirs, budget.complete), (0, False))
        expired = machine.WalkBudget(1000, time.monotonic() - 1)
        machine.biggest_process(os.path.join(root, "a.slice"), budget=expired)
        self.assertEqual(expired.dirs, 1000)  # a deadline that has passed reads nothing

    def test_container_names_are_shortened(self):
        long = "docker-" + "0123456789ab" + "c" * 52 + ".scope"
        self.assertEqual(machine.short_cgroup("system.slice/" + long), "docker-0123456789ab.scope")

    def watch(self, tick):
        self.daemon.watch_memory(tick)
        self.assertTrue(self.daemon.memory_idle())

    def test_tight_memory_is_logged_every_half_minute(self):
        lines = []
        old_log, common.log = common.log, lambda message, level=None: lines.append(message)
        self.addCleanup(setattr, common, "log", old_log)
        self.daemon.cg_root = os.path.join(self.tmp.name, "cg")
        self.cgroup("system.slice", 31 * GIB, anon=30 * GIB)
        self.cgroup("system.slice/docker-0123456789abcdef0123.scope", 30 * GIB, anon=29 * GIB)
        self.watch(1000.0)
        self.assertEqual(lines, [])  # 100G available: not tight
        self.machine.psi_mem_some = 40.0  # one build slowed at its MemoryHigh: not a reason
        self.watch(1000.5)
        self.assertEqual(lines, [])
        self.machine.psi_mem = 25.0  # PSI full while 100G is free: still not tight
        self.watch(1000.6)
        self.assertEqual(lines, [])
        self.machine.psi_mem = 0.0
        self.machine.mem_available = 15 * GIB  # under twice the 10G floor
        self.watch(1001.0)
        self.watch(1020.0)
        self.assertEqual(len(lines), 2)
        self.assertIn("memory is tight: 15G available", lines[0])
        self.assertIn("holding the most memory: docker-0123456789abcdef0123.scope 29G", lines[1])
        self.watch(1031.0)
        self.assertEqual(len(lines), 4)
        self.machine.mem_available = 22 * GIB  # above twice the floor, below 2.5 times: still tight
        self.watch(1040.0)
        self.watch(1062.0)
        self.assertEqual(len(lines), 6)
        self.machine.mem_available = 100 * GIB
        self.watch(1063.0)
        self.machine.mem_available = 15 * GIB  # flapping doesn't get round the half minute
        self.watch(1064.0)
        self.assertEqual(len(lines), 6)
        self.machine.mem_available = 100 * GIB
        self.machine.psi_mem = 25.0  # plentiful RAM clears tight state even if PSI full is still elevated
        self.watch(1065.0)
        self.watch(1094.0)
        self.assertEqual(len(lines), 6)
        self.watch(1095.0)  # calm for half a minute
        self.assertEqual(lines[-1], "memory is no longer tight: 100G available")
        with open(os.path.join(self.state, "memory.log")) as f:
            kept = f.read().splitlines()
        self.assertEqual(len(kept), 7)
        self.assertIn("memory is tight", kept[0])



if __name__ == "__main__":
    unittest.main()
