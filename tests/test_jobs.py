"""Unit tests for cores, jobservers, recovery, history, command keys, config, supervisor, shim, and client helpers."""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from unittest import mock

if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import BASE_CONFIG, BUILDQD, DaemonCase, FakeMachine, FakeRunner, GIB, MIB, ROOT, load_shim  # noqa: E402
from buildq import client, commands, common, config, daemon, history, machine, runners, ticket  # noqa: E402


class Cores(DaemonCase):
    def history(self, ticket, **record):
        self.daemon.history.add({"key": ticket.key, "t": self.now, "peak_mem": GIB, **record})

    def test_a_saturated_build_wants_more_next_time(self):
        t = self.submit()
        self.history(t, peak_cores=7.8, granted=8)
        self.daemon.estimate(t, self.now)
        self.assertEqual(t.want, 12)

    def test_a_build_that_left_cores_idle_wants_fewer(self):
        t = self.submit()
        self.history(t, peak_cores=2.5, granted=8)
        self.daemon.estimate(t, self.now)
        self.assertEqual((t.want, t.need), (3, 2))

    def test_given_cores_dont_shrink_a_build_that_ignores_them(self):
        t = self.submit(cores=1)
        self.history(t, peak_cores=14.0, avg_cores=12.0, granted=4)
        self.daemon.estimate(t, self.now)
        self.assertGreaterEqual(t.need, 14)

    def test_a_build_that_ignores_the_job_count_needs_what_it_uses(self):
        t = self.submit()
        self.history(t, peak_cores=14.0, granted=4)  # a burst: not enough to call it rigid
        self.daemon.estimate(t, self.now)
        self.assertFalse(t.rigid)
        self.history(t, peak_cores=14.0, avg_cores=12.0, granted=4)  # sustained
        self.daemon.estimate(t, self.now)
        self.assertTrue(t.rigid)
        self.assertEqual(t.need, t.want)
        self.assertGreaterEqual(t.need, 14)

    def test_a_cold_cargo_build_counts_as_a_full_one(self):
        project = self.workdir("project")
        open(os.path.join(project, "Cargo.lock"), "w").close()
        t = self.submit(cwd=project)
        self.history(t, peak_cores=1.0, granted=8, peak_mem=GIB // 2)
        self.daemon.estimate(t, self.now)
        self.assertTrue(t.cold)
        self.assertEqual(t.want, 16)
        self.assertGreaterEqual(t.est_mem, 20 * GIB)
        self.finish(t)
        os.makedirs(os.path.join(project, "target", "debug"))
        warm = self.submit(cwd=project)
        self.assertFalse(warm.cold)

    def test_the_build_environment_carries_the_cores(self):
        t = self.submit(env={"CMAKE_BUILD_PARALLEL_LEVEL": "3"})
        with open(os.path.join(t.dir, "parallel.json")) as f:
            parallel = json.load(f)
        self.assertNotIn("CMAKE_BUILD_PARALLEL_LEVEL", parallel)  # the caller's own choice wins
        self.assertEqual(parallel["BUILDQ_CORES"], str(t.granted))
        self.assertIn(f"--jobserver-auth=fifo:{t.jobserver}", parallel["CARGO_MAKEFLAGS"])
        self.assertNotIn("MAKEFLAGS", parallel)  # cargo hands its jobserver to build scripts itself
        self.assertNotIn("RUST_TEST_THREADS", parallel)  # projects pin these; only a lane asks
        self.assertEqual(t.js.free(), t.granted - 1)

    def test_make_runs_in_parallel_only_when_asked(self):
        serial = self.submit(("make", "all"))
        with open(os.path.join(serial.dir, "parallel.json")) as f:
            self.assertNotIn("MAKEFLAGS", json.load(f))
        parallel = self.submit(("make", "-j8", "all"))
        with open(os.path.join(parallel.dir, "parallel.json")) as f:
            self.assertIn("--jobserver-auth", json.load(f)["MAKEFLAGS"])
        self.assertEqual(parallel.exec_argv, ["make", "all"])  # its own -j would override the jobserver

    def test_requeue_clears_exec_argv(self):
        t = self.submit(("make", "-j8", "all"))
        self.assertEqual(t.exec_argv, ["make", "all"])
        self.daemon.requeue(t, self.now + 10)
        self.assertIsNone(t.exec_argv)
        self.daemon.cfg.lanes[t.lane].jobserver = "off"
        self.daemon.schedule(self.now + 11)
        self.assertEqual((t.state, t.exec_argv), ("running", None))
        # Also cleared on the check_starting() recovery path and defensively in start(), along with elastic.
        t2 = self.submit(("make", "-j8", "all"), cwd=self.workdir("two"))
        t2.state, t2.launcher, t2.exec_argv, t2.elastic = "starting", None, ["make", "all"], True
        self.runner.running.discard(t2.id)
        self.runner.unit_state = lambda ticket: "gone"
        self.daemon.check_starting(t2, self.now + 20)
        self.assertEqual((t2.state, t2.exec_argv, t2.elastic), ("queued", None, False))
        t2.exec_argv, t2.elastic = ["make", "all"], True  # stale on disk from an older daemon version
        self.daemon.schedule(self.now + 21)
        self.assertEqual((t2.state, t2.exec_argv, t2.elastic), ("running", None, False))

    def test_test_threads_are_set_only_where_the_lane_asks(self):
        self.daemon.cfg.lanes["heavy"].test_threads = True
        t = self.submit(("cargo", "test"))
        with open(os.path.join(t.dir, "parallel.json")) as f:
            self.assertEqual(json.load(f)["RUST_TEST_THREADS"], str(t.granted))

    def test_no_jobserver_when_the_caller_brings_makeflags(self):
        t = self.submit(env={"MAKEFLAGS": "-j3"})
        self.assertIsNone(t.jobserver)

    def test_an_inherited_fd_jobserver_is_replaced_by_buildqs(self):
        outer = {"MAKEFLAGS": "w -j8 --jobserver-auth=3,4 --no-print-directory",
                 "CARGO_MAKEFLAGS": "-j8 --jobserver-fds=3,4 --jobserver-auth=3,4"}
        t = self.submit(("make", "-j4", "all"), env=outer)
        self.assertIsNotNone(t.jobserver)  # its descriptors don't reach the build: buildq's instead
        with open(os.path.join(t.dir, "parallel.json")) as f:
            parallel = json.load(f)
        self.assertEqual(parallel["MAKEFLAGS"], f"w --no-print-directory -j{t.want} --jobserver-auth=fifo:{t.jobserver}")
        self.assertEqual(parallel["CARGO_MAKEFLAGS"], f"-j{t.want} --jobserver-auth=fifo:{t.jobserver}")
        self.assertNotIn("3,4", json.dumps(parallel))
        cargo = self.submit(("cargo", "build"), env={"MAKEFLAGS": " -j8 --jobserver-auth=3,4"})
        self.assertIsNotNone(cargo.jobserver)
        with open(os.path.join(cargo.dir, "parallel.json")) as f:
            self.assertEqual(json.load(f)["MAKEFLAGS"], "")  # cleared: cargo's build scripts mustn't use it

    def test_options_go_before_a_double_dash_and_an_inherited_parallel_make_stays_parallel(self):
        t = self.submit(("make", "all"), env={"MAKEFLAGS": "rRw -j --jobserver-auth=3,4 -- FOO=bar"})
        self.assertIsNotNone(t.jobserver)
        with open(os.path.join(t.dir, "parallel.json")) as f:
            flags = json.load(f)["MAKEFLAGS"]
        self.assertEqual(flags, f"rRw -j{t.want} --jobserver-auth=fifo:{t.jobserver} -- FOO=bar")
        self.assertEqual(commands.with_make_flags("a -- X=1", "-j2"), "a -j2 -- X=1")
        self.assertEqual(commands.with_make_flags("", "-j2"), "-j2")

    def test_an_empty_inherited_variable_is_no_choice_of_the_callers(self):
        t = self.submit(env={"MAKEFLAGS": "-j8 --jobserver-auth=3,4", "CARGO_MAKEFLAGS": ""})
        self.assertIsNotNone(t.jobserver)
        with open(os.path.join(t.dir, "parallel.json")) as f:
            self.assertIn(f"--jobserver-auth=fifo:{t.jobserver}", json.load(f)["CARGO_MAKEFLAGS"])  # it reaches cargo
        # a make recipe exports MAKEFLAGS= and MFLAGS=: a make -j8 it runs still gets buildq's jobserver
        m = self.submit(("make", "-j8", "all"), env={"MAKEFLAGS": "", "MFLAGS": ""})
        self.assertIsNotNone(m.jobserver)
        with open(os.path.join(m.dir, "parallel.json")) as f:
            self.assertIn(f"--jobserver-auth=fifo:{m.jobserver}", json.load(f)["MAKEFLAGS"])
        self.assertEqual(m.exec_argv, ["make", "all"])

    def test_assignments_after_a_double_dash_are_kept_as_written(self):
        flags = "s -j4 --jobserver-auth=3,4 -- SUBMAKE=make\\ -j4 TAB=a\\\tb"
        self.assertEqual(commands.without_fd_jobserver(flags), "s -- SUBMAKE=make\\ -j4 TAB=a\\\tb")
        self.assertEqual(commands.with_make_flags("s -- X=make\\ -j4", "-j2"), "s -j2 -- X=make\\ -j4")
        self.assertEqual(commands.with_make_flags("-- X=1", "-j2"), "-j2 -- X=1")
        self.assertIsNone(commands.without_fd_jobserver("-- X=--jobserver-auth=3,4"))  # a value, not an option
        self.assertEqual(commands.split_make_flags("-I a\\ -- -- X=1"), ("-I a\\ --", "X=1"))  # an escaped space
        self.assertEqual(commands.split_make_flags("-I a\\\\ -- X=1"), ("-I a\\\\", "X=1"))  # an escaped backslash
        self.assertEqual(commands.split_make_flags("s --"), ("s", ""))
        self.assertEqual(commands.split_make_flags("--foo X=a--"), ("--foo X=a--", None))

    def test_a_j_in_a_kept_assignment_doesnt_stop_buildqs_jobserver(self):
        for value in ("SUBMAKE=make\\ -j4", "X=--jobserver-auth=fifo:/x"):
            t = self.submit(("make", "all"), env={"MAKEFLAGS": f"s -j4 --jobserver-auth=3,4 -- {value}"})
            self.assertIsNotNone(t.jobserver)  # the caller's make ran in parallel; the -j is a value's
            with open(os.path.join(t.dir, "parallel.json")) as f:
                flags = json.load(f)["MAKEFLAGS"]
            self.assertEqual(flags, f"s -j{t.want} --jobserver-auth=fifo:{t.jobserver} -- {value}")

    def test_escaped_whitespace_in_an_option_is_kept_as_written(self):
        # make writes a directory ending in a space as `-Ia\ `, one with a tab in it as `-Ia\<tab>b` (#37)
        self.assertEqual(commands.without_fd_jobserver("s -j4 --jobserver-auth=3,4 -Ia\\  -- X=1"), "s -Ia\\  -- X=1")
        self.assertEqual(commands.without_fd_jobserver("s --jobserver-auth=3,4 -Ia\\\tb"), "s -Ia\\\tb")
        self.assertEqual(commands.with_make_flags("s -Ia\\ ", "-j2"), "s -Ia\\  -j2")
        self.assertEqual(commands.make_words("a\\\\ b"), ["a\\\\", "b"])  # an escaped backslash, then a split

    def test_make_s_own_word_rules_in_the_corners(self):
        self.assertEqual(commands.make_words("s -Ia\\"), ["s", "-Ia\\"])  # a lone backslash at the end stays
        self.assertEqual(commands.with_make_flags("s -Ia\\", "-j2"), "s -Ia\\\\ -j2")  # and can't escape what follows
        self.assertEqual(commands.make_words("s -Ia\nb"), ["s", "-Ia\nb"])  # only spaces and tabs split
        self.assertEqual(commands.without_fd_jobserver("s -j 8 --jobserver-auth=3,4"), "s")  # -j's count goes with it
        self.assertFalse(commands.callers_jobs("s -I -jx"))  # a folder called -jx
        self.assertTrue(commands.callers_jobs("s --jobs=3"))
        self.assertTrue(commands.callers_jobs("s \\--jobserver-auth=fifo:/x"))  # make takes the backslash out

    def test_a_j_inside_an_option_s_path_doesnt_stop_buildqs_jobserver(self):
        t = self.submit(("make", "all"), env={"MAKEFLAGS": "s -j4 --jobserver-auth=3,4 -I a\\ -jx"})
        self.assertIsNotNone(t.jobserver)  # `a\ -jx` is a directory, not the caller's -j
        with open(os.path.join(t.dir, "parallel.json")) as f:
            flags = json.load(f)["MAKEFLAGS"]
        self.assertEqual(flags, f"s -I a\\ -jx -j{t.want} --jobserver-auth=fifo:{t.jobserver}")

    def test_a_wrapper_s_own_words_are_never_rewritten(self):
        t = self.submit(("env", "-u", "-j", "make", "-j8"))  # unset a variable called -j, then run make -j8
        self.assertIsNotNone(t.jobserver)
        self.assertEqual(t.exec_argv, ["env", "-u", "-j", "make"])  # only make's own -j8 goes

    def test_a_fifo_jobserver_from_the_caller_is_kept(self):
        t = self.submit(env={"MAKEFLAGS": "-j8 --jobserver-auth=fifo:/tmp/outer-js"})
        self.assertIsNone(t.jobserver)
        with open(os.path.join(t.dir, "parallel.json")) as f:
            self.assertNotIn("MAKEFLAGS", json.load(f))

    def test_given_cores_are_used_as_they_are(self):
        t = self.submit(cores=3)
        self.assertEqual((t.want, t.need, t.granted), (3, 3, 3))

    def test_given_memory_can_raise_but_not_lower_the_estimate(self):
        self.assertEqual(self.submit(mem="1G").est_mem, 20 * GIB)
        self.assertEqual(self.submit(mem="24G").est_mem, 24 * GIB)



class Jobservers(DaemonCase):
    def test_tokens(self):
        js = runners.Jobserver(os.path.join(self.tmp.name, "fifo"), create=True)
        js.resize(3)
        self.assertEqual(js.free(), 3)
        os.read(js.fd, 1)  # a client takes a token
        js.observe()
        self.assertTrue(js.used)
        js.resize(1)  # only the two free tokens can be taken back now
        self.assertEqual((js.out, js.free()), (1, 0))
        os.write(js.fd, b"+")  # the client gives its token back
        js.resize(1)
        self.assertEqual((js.out, js.free()), (1, 1))
        js.close()

    def test_rebalance_grows_busy_builds_and_shrinks_them_for_others(self):
        project = self.workdir("project")
        open(os.path.join(project, "Cargo.lock"), "w").close()  # a cold build: it wants 16
        self.machine.busy = self.machine.busy_now = 20.0
        t = self.submit(cwd=project)
        self.assertEqual((t.want, t.granted), (16, 12))  # 12 cores were free
        t.elastic, t.cores_now = True, 12.0
        self.machine.busy = self.machine.busy_now = 14.0  # others finished
        self.daemon.rebalance(self.now + 120)
        self.assertEqual(t.granted, 16)  # it used all 12, so it gets the rest it wants
        self.assertEqual(t.js.out, 15)
        self.machine.busy = self.machine.busy_now = 34.0  # the machine filled up
        self.daemon.rebalance(self.now + 125)
        self.assertEqual(t.granted, 14)
        self.assertEqual(t.js.out, 13)


    def test_a_ci_build_short_of_cores_takes_them_from_agents_builds(self):
        project = self.workdir("project")
        open(os.path.join(project, "Cargo.lock"), "w").close()  # a cold build: it wants 16
        self.machine.busy = self.machine.busy_now = 20.0
        agent = self.submit(cwd=project)
        self.assertEqual(agent.granted, 12)  # 12 cores were free
        agent.elastic, agent.cores_now = True, 12.0
        self.machine.busy = self.machine.busy_now = 28.0  # the machine is at its target
        ci = self.submit(("cargo", "test"), cwd=self.workdir("ci"), env={"GITHUB_ACTIONS": "true", "RUNNER_NAME": "r1"})
        self.assertTrue(ci.from_ci)
        self.assertEqual(ci.state, "running")
        start = ci.granted
        self.assertLess(start, 8)
        ci.elastic, ci.cores_now = True, float(start)  # using all it has
        target = self.daemon.cfg.cpu_target * self.machine.ncpu
        self.machine.busy = self.machine.busy_now = target  # no idle cores: they come from the agent's build
        self.daemon.rebalance(self.now + 120)
        self.assertEqual(ci.granted, 8)  # up to its lane's usual cores, not the 16 a cold build may want
        self.assertEqual(agent.granted, 12 - (8 - start))  # from the agent's build, the machine still at its target
        self.assertEqual((ci.js.out, agent.js.out), (7, agent.granted - 1))
        ci.cores_now = 8.0
        self.daemon.rebalance(self.now + 125)
        self.assertEqual(ci.granted, 8)  # no more from agents: beyond its usual cores, only idle ones
        self.machine.busy = self.machine.busy_now = target + 2  # over its target: agents' builds are cut first
        self.daemon.rebalance(self.now + 130)
        self.assertEqual(ci.granted, 8)
        self.assertLess(agent.granted, 12 - (8 - start))


    def test_idle_cores_dont_wait_for_a_cut_build_s_jobs(self):
        project = self.workdir("project")
        open(os.path.join(project, "Cargo.lock"), "w").close()
        self.machine.busy = self.machine.busy_now = 20.0
        agent = self.submit(cwd=project)
        agent.elastic, agent.cores_now = True, 12.0
        self.machine.busy = self.machine.busy_now = 28.0
        ci = self.submit(("cargo", "test"), cwd=self.workdir("ci"), env={"GITHUB_ACTIONS": "true", "RUNNER_NAME": "r1"})
        ci.elastic, ci.cores_now = True, float(ci.granted)
        start = ci.js.out
        held = os.read(agent.js.fd, agent.js.out)  # the agent's jobs hold every token it has
        agent.granted, agent.cores_now = agent.need, 1.0  # cut (for another reason), its tokens not back yet
        self.machine.busy = self.machine.busy_now = 12.0  # cores went idle elsewhere
        self.daemon.rebalance(self.now + 120)
        self.assertGreater(ci.granted - 1, start)
        self.assertEqual(ci.js.out, ci.granted - 1)  # idle cores paid for it: its tokens went out at once
        os.write(agent.js.fd, held)

    def test_idle_cores_pay_for_a_token_once(self):
        # One idle core and a transfer in the same pass: the CI build gets one token for the idle
        # core, not one more for the grant that core already raised (#52 review round 3).
        project = self.workdir("project")
        open(os.path.join(project, "Cargo.lock"), "w").close()
        self.machine.busy = self.machine.busy_now = 20.0
        agent = self.submit(cwd=project)
        agent.elastic, agent.cores_now = True, 12.0
        self.machine.busy = self.machine.busy_now = 28.0
        ci = self.submit(("cargo", "test"), cwd=self.workdir("ci"), env={"GITHUB_ACTIONS": "true", "RUNNER_NAME": "r1"})
        ci.elastic, ci.cores_now = True, float(ci.granted)
        start = ci.js.out
        held = os.read(agent.js.fd, agent.js.out)  # the agent's jobs hold every token it has
        target = self.daemon.cfg.cpu_target * self.machine.ncpu
        self.machine.busy = self.machine.busy_now = target - 1.5  # one idle core
        self.daemon.rebalance(self.now + 120)
        self.assertEqual(ci.granted, 8)
        self.assertEqual(ci.js.out, start + 1)  # one token: the idle core's
        # The next pass: idle cores raise the agent's grant while the CI build is still owed tokens.
        # The CI build gets no more than those idle cores paid for (Opus, #52 review round 3).
        agent.cores_now = 12.0
        self.machine.busy = self.machine.busy_now = target - 2.5  # two idle cores
        was = ci.js.out
        self.daemon.rebalance(self.now + 122)
        self.assertLessEqual(ci.js.out - was, 2)
        self.assertLessEqual(sum(t.js.out for t in (agent, ci)), 11 + was + 2)
        # A token whose count couldn't be saved doesn't go out.
        was = ci.js.out
        with unittest.mock.patch.object(self.daemon, "save", return_value=False):
            os.write(agent.js.fd, held)  # the agent's jobs end
            self.daemon.rebalance(self.now + 125)
        self.assertEqual(ci.js.out, was)
        self.daemon.rebalance(self.now + 130)
        self.assertEqual(ci.js.out, 7)

    def test_tokens_moved_to_a_ci_build_are_issued_as_they_come_back(self):
        project = self.workdir("project")
        open(os.path.join(project, "Cargo.lock"), "w").close()
        self.machine.busy = self.machine.busy_now = 20.0
        agent = self.submit(cwd=project)
        agent.elastic, agent.cores_now = True, 12.0
        self.machine.busy = self.machine.busy_now = 28.0
        ci = self.submit(("cargo", "test"), cwd=self.workdir("ci"), env={"GITHUB_ACTIONS": "true", "RUNNER_NAME": "r1"})
        start = ci.granted
        ci.elastic, ci.cores_now = True, float(start)
        held = os.read(agent.js.fd, agent.js.out)  # the agent's jobs hold every token it has
        self.machine.busy = self.machine.busy_now = self.daemon.cfg.cpu_target * self.machine.ncpu
        self.daemon.rebalance(self.now + 120)
        self.assertEqual((ci.granted, agent.granted), (8, 12 - (8 - start)))  # the grants move at once
        self.assertEqual((ci.js.out, agent.js.out), (start - 1, 11))  # but no token is issued before one comes back
        os.write(agent.js.fd, held)  # its jobs end and give the tokens back
        # A crash once the grower's tokens are out, before its save after them: what it saved is
        # never below what is out, so a restart can't issue them twice.
        def saved(t):
            with open(os.path.join(t.dir, "ticket.json")) as f:
                return json.load(f)["tokens_out"]
        save = self.daemon.save
        def crashing(t):
            if t is ci and ci.js.out > start - 1:
                raise SystemExit("crash")
            return save(t)
        with unittest.mock.patch.object(self.daemon, "save", side_effect=crashing):
            try:
                self.daemon.rebalance(self.now + 124)
            except SystemExit:
                pass  # (a save after its tokens went out: none is needed, as the count went first)
        self.assertEqual(ci.js.out, 7)
        self.assertGreaterEqual(saved(ci), ci.js.out)
        self.daemon.rebalance(self.now + 125)
        self.assertEqual((ci.js.out, agent.js.out), (7, agent.granted - 1))
        self.assertEqual(saved(ci), 7)
        # While a build waits for memory, none is taken for more cores.
        ci.granted, agent.granted = ci.need, 12
        big = self.submit(("cargo", "build"), cwd=self.workdir("big"), mem="90G")
        self.assertEqual(big.reason_cat, "memory")  # it waits for memory
        with unittest.mock.patch.object(self.daemon, "mem_for", lambda t, cores: t.est_mem + (cores - t.need) * GIB):
            self.daemon.rebalance(self.now + 127)
        self.assertEqual(ci.granted, ci.need)
        # A CI build whose hold was let go past its peak grows no further this way, as growth doesn't.
        ci.granted = ci.need
        ci.release_at, ci.release_revoked, ci.release_hold = 60.0, False, 64 * GIB  # (kept: it stays under it)
        with unittest.mock.patch.object(self.daemon, "past_peak_hold", return_value=64 * GIB):  # (still released)
            self.daemon.rebalance(self.now + 130)
        self.assertEqual(ci.granted, ci.need)

    def test_rebalance_ignores_builds_held_for_memory_but_makes_room_for_cores(self):
        project = self.workdir("project")
        open(os.path.join(project, "Cargo.lock"), "w").close()
        self.machine.busy = self.machine.busy_now = 20.0
        running = self.submit(cwd=project)
        self.assertEqual(running.granted, 12)
        running.elastic, running.cores_now = True, 12.0
        self.machine.mem_available = 12 * GIB
        queued = self.submit(cwd=self.workdir("other"), cores=8)
        self.assertEqual(queued.state, "queued")
        queued.reason_cat, queued.held_for = "held", "memory"
        self.machine.mem_available = 100 * GIB
        target = self.daemon.cfg.cpu_target * self.machine.ncpu
        self.machine.busy = self.machine.busy_now = target
        self.daemon.rebalance(self.now + 120)
        self.assertEqual(running.granted, 12)  # waiting on memory must not throttle running builds' cores
        queued.reason_cat, queued.held_for = "held", "cores"
        self.daemon.rebalance(self.now + 140)
        self.assertLess(running.granted, 12)  # waiting on cores does shrink it



class Recovery(DaemonCase):
    def test_a_resent_submission_gets_its_ticket_back(self):
        cwd = self.workdir()
        first = self.submit(cwd=cwd, submission="abc")
        again = self.submit(cwd=cwd, submission="abc")
        self.assertIs(first, again)
        restarted = daemon.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                                     queries=common.InlineExecutor())
        restarted.load_tickets(self.now)
        t, _ = restarted.submit({"argv": ["cargo", "build"], "cwd": cwd, "env": {}, "submission": "abc"}, self.now)
        self.assertEqual(t.id, first.id)
        for ticket in restarted.tickets.values():
            restarted.close_jobserver(ticket)

    def test_a_ticket_keeps_the_runner_it_started_under(self):
        t = self.submit()
        t.runner = "direct"
        self.daemon.save(t)
        again = daemon.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                                     queries=common.InlineExecutor())
        again.load_tickets(self.now)
        self.assertEqual(again.tickets[t.id].state, "running")  # not given up for a runner change
        for ticket in again.tickets.values():
            again.close_jobserver(ticket)

    def test_a_cancel_goes_on_after_a_restart(self):
        t = self.submit()
        self.daemon.cancel(t, self.now)
        self.runner.stopped.clear()
        again = daemon.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                                     queries=common.InlineExecutor())
        again.load_tickets(self.now)
        self.assertEqual(self.runner.stopped, [t.id])
        for ticket in again.tickets.values():
            again.close_jobserver(ticket)

    def test_a_build_lost_before_it_started_is_queued_again(self):
        t = self.submit()
        t.state = "starting"
        self.daemon.save(t)
        self.runner.running.discard(t.id)
        self.runner.unit_state = lambda ticket: "gone"
        again = daemon.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                                     queries=common.InlineExecutor())
        again.load_tickets(self.now)
        again.check(self.now + 11)
        self.assertEqual(again.tickets[t.id].state, "queued")
        for ticket in again.tickets.values():
            again.close_jobserver(ticket)

    def test_a_typo_in_the_runner_keeps_the_last_good_configuration(self):
        with open(self.config_path, "w") as f:
            f.write(BASE_CONFIG.replace('default_lane = "heavy"', 'default_lane = "heavy"\nrunner = "systmed"'))
        again = daemon.Daemon(self.config_path, self.state, machine=self.machine)
        self.assertEqual(again.cfg.max_jobs, 8)  # the last good file, not the defaults
        self.assertEqual(again.cfg.runner, "auto")



class Turns(DaemonCase):
    def test_a_finished_build_ends_its_turn_and_teaches_history(self):
        t = self.submit()
        t.measured, t.mem_peak, t.peak_cores, t.cpu_usec = True, 6 * GIB, 7.5, 60_000_000
        self.finish(t, 0)
        self.assertEqual(t.state, "succeeded")
        records = list(self.daemon.history.by_key[t.key])
        self.assertEqual((records[0]["peak_mem"], records[0]["granted"]), (6 * GIB, 8))

    def test_memory_waits_are_kept_shown_and_learned(self):
        t = self.submit()
        t.measured, t.mem_peak = True, 22 * GIB
        t.take_waits(10.0, 2.0, 3)  # a reading while it ran
        self.finish(t, 0, mem_wait_some=340.5, mem_wait_full=120.0, mem_high=104492)
        self.assertEqual((t.mem_wait_some, t.mem_wait_full, t.mem_high), (340.5, 120.0, 104492))
        row = t.row(self.now)
        self.assertEqual((row["mem_wait_some"], row["mem_high"]), (340.5, 104492))
        record = list(self.daemon.history.by_key[t.key])[0]
        self.assertEqual((record["mem_wait_some"], record["mem_wait_full"], record["mem_high"]),
                         (340.5, 120.0, 104492))
        restarted = daemon.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                                     queries=common.InlineExecutor())
        restarted.load_tickets(self.now + 60)
        self.assertEqual(restarted.tickets[t.id].mem_high, 104492)

    def test_history_records_the_wait_and_the_caller(self):
        t = self.submit(caller="agent.scope")
        t.measured, t.mem_peak = True, GIB
        self.finish(t, 0)
        record = list(self.daemon.history.by_key[t.key])[0]
        self.assertEqual((record["caller"], record["requeues"]), ("agent.scope", 0))
        self.assertGreaterEqual(record["waited"], 0)

    def test_bad_memory_waits_are_ignored(self):
        t = self.submit()
        self.finish(t, 0, mem_wait_some="lots", mem_wait_full=None, mem_high=10 ** 400)
        self.assertEqual(t.state, "succeeded")  # a number too big for a float doesn't stop the daemon
        self.assertEqual((t.mem_wait_some, t.mem_wait_full, t.mem_high), (0.0, 0.0, 0))
        t.take_waits(float("nan"), -5, True)
        self.assertEqual((t.mem_wait_some, t.mem_wait_full, t.mem_high), (0.0, 0.0, 0))
        with open(os.path.join(t.dir, "ticket.json")) as f:
            record = json.load(f)
        record.update(mem_wait_some="x", mem_high=[1])  # a damaged ticket file
        loaded = ticket.Ticket.from_record(record, t.dir)
        self.assertEqual((loaded.mem_wait_some, loaded.mem_high), (0.0, 0))
        loaded.row(self.now)

    def test_a_signalled_or_timed_out_build_teaches_nothing(self):
        killed = self.submit()
        killed.measured = True
        self.finish(killed, 143)
        timed = self.submit()
        timed.measured = True
        self.finish(timed, 124, timed_out=True)
        self.assertEqual(timed.state, "failed")
        self.assertIn("timed out", timed.error)
        self.assertNotIn(killed.key, self.daemon.history.by_key)

    def test_an_oom_kill_too_quick_to_measure_still_teaches(self):
        t = self.submit()
        self.finish(t, 137, oom_kills=1)  # never sampled
        self.assertTrue(t.oom)
        self.assertEqual(self.submit(cwd=t.cwd).est_mem, 26 * GIB)

    def test_an_oom_kill_teaches_the_cap(self):
        t = self.submit()
        t.measured, t.mem_peak = True, 3 * GIB
        self.finish(t, 101, oom_kills=1)
        self.assertTrue(t.oom)
        again = self.submit(cwd=t.cwd)  # the same command in the same repository
        self.assertEqual(again.est_mem, 26 * GIB)

    def test_the_turn_lasts_until_leftovers_are_gone(self):
        t = self.submit()
        with open(os.path.join(t.dir, "result.json"), "w") as f:
            json.dump({"exit": 0}, f)
        self.daemon.check(self.now + 10)  # the service still has processes
        self.assertEqual(t.state, "running")
        self.daemon.check(self.now + 26)  # past 15 s they are killed, and the turn waits for that
        self.assertEqual(t.state, "running")
        self.assertIn(t.id, self.runner.stopped)
        self.runner.running.discard(t.id)
        self.daemon.check(self.now + 27)
        self.assertEqual(t.state, "succeeded")

    def test_a_build_that_vanishes_is_lost(self):
        t = self.submit()
        self.runner.running.discard(t.id)
        self.daemon.check(self.now + 10)
        self.assertEqual(t.state, "lost")

    def test_cancel(self):
        self.daemon.cfg.max_jobs = 1
        running = self.submit(cores=1)
        queued = self.submit(cores=1)
        self.daemon.cancel(queued, self.now)
        self.assertEqual(queued.state, "cancelled")
        self.daemon.cancel(running, self.now)
        self.assertEqual(self.runner.stopped, [running.id])
        self.finish(running, 143)
        self.assertEqual(running.state, "cancelled")

    def test_refusals(self):
        for msg, words in [({"argv": ["make"], "cwd": "/", "env": {}, "lane": "nope"}, "unknown lane"),
                           ({"argv": ["make"], "cwd": "relative", "env": {}}, "absolute"),
                           ({"argv": [], "cwd": "/", "env": {}}, "no command"),
                           ({"argv": ["make"], "cwd": "/", "env": {"A": 1}}, "environment"),
                           ({"argv": ["make"], "cwd": "/", "env": {}, "cores": float("nan")}, "bad option"),
                           ({"argv": ["make"], "cwd": "/", "env": {}, "mem": 1e400}, "bad option")]:
            t, error = self.daemon.submit(msg, self.now)
            self.assertIsNone(t, msg)
            self.assertIn(words, error)

    def test_nothing_starts_while_stopping(self):
        self.daemon.cfg.max_jobs = 1
        self.submit(cores=1)
        queued = self.submit(cores=1)
        self.daemon.stopping = True
        self.daemon.schedule(self.now)
        self.assertEqual(queued.state, "queued")
        self.assertIsNone(self.daemon.submit({"argv": ["make"], "cwd": "/", "env": {}}, self.now)[0])

    def test_tickets_survive_a_daemon_restart(self):
        self.daemon.cfg.max_jobs = 1
        running = self.submit(cores=2)
        queued = self.submit(cores=1)
        again = daemon.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                                     queries=common.InlineExecutor())
        again.load_tickets(self.now)
        self.assertEqual(again.tickets[running.id].state, "running")
        self.assertEqual(again.tickets[queued.id].state, "queued")
        self.assertIsNotNone(again.tickets[running.id].js)  # its jobserver is open again
        for t in again.tickets.values():
            again.close_jobserver(t)

    def test_finish_hooks_run(self):
        marker = os.path.join(self.tmp.name, "hook")
        t = self.submit(notify=f'echo "$BUILDQ_STATE $BUILDQ_EXIT" > {marker}')
        self.finish(t, 3)
        for _ in range(100):
            if os.path.exists(marker) and os.path.getsize(marker):
                break
            time.sleep(0.05)
        with open(marker) as f:
            self.assertEqual(f.read().strip(), "failed 3")



class FlockSlots(DaemonCase):
    def slots(self, pattern):
        self.daemon.cfg = config.Config(
            {"default_lane": "heavy", "lanes": {"heavy": {"cores": 8, "mem": "20G"}},
             "flock_slots": [{"glob": pattern, "lane": "heavy"}]},
            self.machine.mem_total, self.machine.ncpu)

    @unittest.skipUnless(shutil.which("flock") and shutil.which("bash"), "needs flock(1) and bash")
    def test_a_held_slot_counts_as_a_running_build(self):
        # The usual semaphore script: open the lock on fd 9, lock it with flock(1), which exits at
        # once, then run the job while the shell keeps fd 9 open.
        self.slots(os.path.join(self.tmp.name, "slot-*.lock"))
        lock = os.path.join(self.tmp.name, "slot-1.lock")
        holder = subprocess.Popen(["bash", "-c", f'exec 9>"{lock}"; flock -n 9; echo locked; sleep 30 9>&-'],
                                  stdout=subprocess.PIPE)
        waiter = None
        try:
            self.assertEqual(holder.stdout.readline().strip(), b"locked")
            # A waiter has the same file open but doesn't hold the lock; it mustn't be taken for the holder.
            waiter = subprocess.Popen(["bash", "-c", f'exec 8<"{lock}"; sleep 30'])
            time.sleep(0.2)
            self.daemon.scan_flock_slots(self.now)
            t = self.daemon.tickets["flock:" + os.path.realpath(lock)]
            self.assertEqual((t.state, t.flock[1], t.label), ("running", holder.pid, "sleep 30"))
        finally:
            for p in (holder, waiter):
                if p:
                    p.kill()
                    p.wait()
            holder.stdout.close()
        self.daemon.scan_flock_slots(self.now + 5)
        self.assertFalse([t for t in self.daemon.tickets if t.startswith("flock:")])

    def test_slots_with_the_same_name_in_different_places_are_different(self):
        self.slots(os.path.join(self.tmp.name, "*", "slot.lock"))
        holders = []
        try:
            for name in ("a", "b"):
                os.makedirs(os.path.join(self.tmp.name, name))
                path = os.path.join(self.tmp.name, name, "slot.lock")
                p = subprocess.Popen([sys.executable, "-c", "import fcntl,sys,time; f=open(sys.argv[1],'w'); "
                                      "fcntl.flock(f, fcntl.LOCK_EX); print('locked', flush=True); time.sleep(30)", path],
                                     stdout=subprocess.PIPE)
                self.assertEqual(p.stdout.readline().strip(), b"locked")
                holders.append(p)
            self.daemon.scan_flock_slots(self.now)
            self.assertEqual(len([t for t in self.daemon.tickets if t.startswith("flock:")]), 2)
        finally:
            for p in holders:
                p.kill()
                p.wait()
                p.stdout.close()



class Estimates(DaemonCase):
    def estimate(self, key="k", cold=False):
        return self.daemon.history.estimate(key, self.daemon.cfg.lanes["heavy"], self.daemon.cfg, 32, True, cold,
                                            self.now)

    def test_default_then_learned(self):
        e = self.estimate()
        self.assertEqual((e.want, e.need, e.mem), (8, 2, 20 * GIB))
        for peak in (2 * GIB, 5 * GIB, 3 * GIB):
            self.daemon.history.add({"key": "k", "t": self.now, "peak_mem": peak, "peak_cores": 3.0, "granted": 8})
        e = self.estimate()
        self.assertEqual(e.mem, int(5 * GIB * 1.15) + 256 * common.MIB)
        self.assertEqual(e.want, 4)

    def test_small_runs_dont_hide_a_big_one(self):
        self.daemon.history.add({"key": "k", "t": self.now, "peak_mem": 18 * GIB, "peak_cores": 8})
        for _ in range(20):  # twenty quick incremental runs
            self.daemon.history.add({"key": "k", "t": self.now, "peak_mem": GIB, "peak_cores": 1})
        self.assertGreater(self.estimate().mem, 18 * GIB)

    def test_old_runs_are_forgotten(self):
        self.daemon.history.add({"key": "k", "t": self.now - 40 * 86400, "peak_mem": 18 * GIB, "peak_cores": 8})
        self.assertEqual(self.estimate().mem, 20 * GIB)  # back to the lane default

    def test_history_survives_a_restart_and_skips_bad_records(self):
        self.daemon.history.add({"key": "k", "t": self.now, "peak_mem": GIB, "peak_cores": 1.0})
        with open(self.daemon.history.path, "a") as f:
            f.write('{"key": "k"}\nnot json\n')
        again = history.History(self.daemon.history.path)
        self.assertEqual(len(again.by_key["k"]), 1)

    def test_history_keeps_a_bounded_number_of_commands(self):
        history = history.History(os.path.join(self.tmp.name, "h.jsonl"), max_keys=10)
        for i in range(50):
            history.add({"key": f"k{i}", "t": self.now, "peak_mem": 1, "peak_cores": 1.0})
        self.assertEqual(len(history.by_key), 10)
        self.assertIn("k49", history.by_key)



class Keys(unittest.TestCase):
    def words(self, *argv):
        return " ".join(commands.command_words(list(argv)))

    def test_cargo(self):
        self.assertEqual(self.words("cargo", "test", "--locked", "-p", "api", "--lib", "--", "--test-threads=4"),
                         "cargo test -p api --lib")
        self.assertEqual(self.words("cargo", "+nightly", "build", "--release", "--package=api"),
                         "cargo build -p api --release")
        self.assertEqual(self.words("cargo", "test", "-p", "api", "--test", "a", "--test", "b"), "cargo test -p api")

    def test_wrappers_and_assignments_are_ignored(self):
        self.assertEqual(self.words("env", "SQLX_OFFLINE=true", "nice", "-n", "10", "cargo", "check"), "cargo check")
        self.assertEqual(self.words("FOO=1", "timeout", "-s", "KILL", "600", "make", "-j8", "all"), "make all")

    def test_js_git_and_scripts(self):
        self.assertEqual(self.words("pnpm", "--filter", "web", "run", "build"), "pnpm --filter web build")
        self.assertEqual(self.words("npx", "vitest", "run"), "npx vitest")
        self.assertEqual(self.words("git", "-C", "/x", "push", "--force-with-lease", "origin", "b"), "git push")
        self.assertEqual(self.words("bash", "./scripts/ci.sh", "--fast"), "bash ci.sh")
        self.assertEqual(self.words("python3", "-m", "pytest", "-x"), "python3 -m pytest")  # (#49)
        self.assertEqual(self.words("python3", "script.py", "-m", "slow"), "python3 script.py")  # the script's own -m
        self.assertEqual(self.words("python3", "-X", "dev", "-W", "error", "-m", "pytest"), "python3 -m pytest")

    def test_a_labelled_scripts_label_tells_its_builds_apart(self):
        labels = frozenset({"persistent-cargo-target.sh", "gen.py"})
        wrapper = ("bash", "scripts/ci/persistent-cargo-target.sh")

        def words(*argv):
            return " ".join(commands.command_words(list(argv), labels))
        self.assertEqual(words(*wrapper, "architecture", "2G", "--", "cargo", "run", "--release"),
                         "bash persistent-cargo-target.sh architecture")
        self.assertEqual(words(*wrapper, "rust-tests", "60G", "--", "bash", "scripts/ci/rust-unit-tests.sh"),
                         "bash persistent-cargo-target.sh rust-tests")
        self.assertEqual(self.words(*wrapper, "architecture", "2G"), "bash persistent-cargo-target.sh")  # not listed
        self.assertEqual(words("python3", "tools/gen.py", "wire_types"), "python3 gen.py wire_types")
        # only the argument right after the script, and only a plain lowercase word
        self.assertEqual(words("python3", "tools/gen.py", "--password", "hunter"), "python3 gen.py")
        for arg in ("/home/u/.t3/worktrees/x", "./tree", "60G", "Ab3dE9fGh2", "ghp_0123456789abcdefABCDEF",
                    "a-very-long-argument-of-thirty-chars"):
            self.assertEqual(words("bash", "persistent-cargo-target.sh", arg), "bash persistent-cargo-target.sh", arg)
        # an unlisted script, -c, -lc and modules are keyed as before
        self.assertEqual(words("bash", "run.sh", "build"), "bash run.sh")
        self.assertEqual(words("bash", "-lc", "persistent-cargo-target.sh build"), self.words("bash", "-lc", "persistent-cargo-target.sh build"))
        self.assertEqual(words("python3", "-m", "unittest", "discover", "tests"), "python3 -m unittest discover")
        self.assertEqual(words("python3", "-m", "unittest", "discover"), "python3 -m unittest discover")  # keys back to itself (#49)
        # only a script that comes straight after the interpreter: not a command string or an option's value
        self.assertEqual(words("bash", "-lc", "persistent-cargo-target.sh", "hunter"),
                         self.words("bash", "-lc", "persistent-cargo-target.sh", "hunter"))
        self.assertEqual(" ".join(commands.command_words(["node", "--require", "preload.js", "hunter"],
                                                        frozenset({"preload.js"}))),
                         self.words("node", "--require", "preload.js", "hunter"))
        # a script's own -m is neither a module nor a label
        self.assertEqual(words(*wrapper, "architecture", "-m", "fast"), "bash persistent-cargo-target.sh architecture")
        # its own -c is its argument, not a command string
        self.assertEqual(words(*wrapper, "architecture", "-c", "debug"), "bash persistent-cargo-target.sh architecture")
        # an option before the script turns the label off
        self.assertEqual(words("bash", "-e", *wrapper[1:], "architecture"), "bash persistent-cargo-target.sh")

    def test_labelled_scripts_are_configured(self):
        cfg = config.Config({"script_labels": ["persistent-cargo-target.sh"]}, 64 * GIB, 16)
        self.assertEqual(cfg.script_labels, frozenset({"persistent-cargo-target.sh"}))
        self.assertEqual(cfg.summary()["script_labels"], ["persistent-cargo-target.sh"])
        self.assertEqual(config.Config({}, 64 * GIB, 16).script_labels, frozenset())
        ruled = config.Config(config.tomllib.loads('script_labels = ["wrap.sh"]\n[[lane_rules]]\n'
                                                     'match = "^bash wrap.sh architecture$"\nlane = "small"\n'
                                                     '[lanes.default]\n[lanes.small]\n'), 64 * GIB, 16)
        self.assertEqual(ruled.lane_for(["bash", "wrap.sh", "architecture"]), "small")  # rules see the label
        self.assertEqual(ruled.lane_for(["bash", "wrap.sh", "rust-tests"]), "default")
        for bad in ("x.sh", ["a/b.sh"], [3]):
            with self.assertRaises(ValueError):
                config.Config({"script_labels": bad}, 64 * GIB, 16)

    def test_a_listed_package_script_keeps_its_label(self):
        cfg = config.Config(config.tomllib.loads(
            'script_labels = ["verify:push"]\n'
            '[[lane_rules]]\nmatch = "^(pnpm|npm|yarn) .*(verify:push|verify-push) docs\\\\b"\nlane = "light"\n'
            '[[lane_rules]]\nmatch = "^(pnpm|npm|yarn) .*(verify:push|verify-push)"\nlane = "heavy"\n'
            '[lanes.default]\n[lanes.light]\n[lanes.heavy]\n'), 64 * GIB, 16)
        self.assertEqual(cfg.lane_for(["pnpm", "run", "verify:push", "docs", "origin/main", "HEAD"]), "light")
        self.assertEqual(cfg.lane_for(["/usr/bin/pnpm", "verify:push", "docs"]), "light")
        self.assertEqual(cfg.lane_for(["pnpm", "run", "verify:push", "api", "a", "b"]), "heavy")
        self.assertEqual(cfg.lane_for(["pnpm", "run", "verify:push", "documentation"]), "heavy")
        self.assertEqual(commands.command_words(["pnpm", "run", "verify:push", "full", "a"], cfg.script_labels),
                         ["pnpm", "verify:push", "full"])  # its own history, too
        self.assertEqual(commands.command_words(["pnpm", "run", "verify:push", "docs"], frozenset()),
                         ["pnpm", "verify:push"])  # unlisted: as before
        self.assertEqual(commands.command_words(["pnpm", "run", "verify:push", "abc123"], cfg.script_labels),
                         ["pnpm", "verify:push"])  # only a plain lowercase word
        self.assertEqual(commands.command_words(["pnpm", "run", "verify:push", "--reporter", "dot", "docs"], cfg.script_labels),
                         ["pnpm", "verify:push"])  # not an option's value
        self.assertEqual(cfg.lane_for(["pnpm", "--filter", "web", "verify:push", "docs"]), "light")
        listed = frozenset({"build"})
        self.assertEqual(commands.command_words(["pnpm", "--filter", "build", "build", "docs"], listed),
                         ["pnpm", "--filter build", "build", "docs"])  # the script, not the filter's value
        for verb in ("exec", "dlx", "x"):  # a binary, not a package script: as before
            self.assertEqual(commands.command_words(["pnpm", verb, "build", "docs"], listed), ["pnpm", "build"], verb)

    def test_keys_hold_no_urls_but_keep_npm_scopes(self):
        self.assertNotIn("token", self.words("wget", "https://files.example/private-access-token"))
        self.assertEqual(self.words("pnpm", "--filter", "@scope/web", "build"), "pnpm --filter @scope/web build")

    def test_keys_hold_no_secrets(self):
        for argv in (["bash", "-c", "PGPASSWORD=s3cret psql -h db"], ["psql", "postgres://u:hunter2@db/x"],
                     ["docker", "run", "-e", "TOKEN=abc", "img"]):
            key = self.words(*argv)
            for secret in ("s3cret", "hunter2", "abc"):
                self.assertNotIn(secret, key)
        label = commands.redacted_label(["env", "TOKEN=abc", "sh", "-c", "X=1 make", "postgres://u:hunter2@db/x"])
        self.assertNotIn("abc", label)
        self.assertNotIn("hunter2", label)

    def test_repo_name_is_shared_by_worktrees(self):
        with tempfile.TemporaryDirectory() as tmp:
            main = os.path.join(tmp, "project")
            os.makedirs(os.path.join(main, ".git", "worktrees", "feature"))
            with open(os.path.join(main, ".git", "worktrees", "feature", "commondir"), "w") as f:
                f.write("../..\n")
            tree = os.path.join(tmp, "elsewhere", "feature")
            os.makedirs(os.path.join(tree, "sub"))
            with open(os.path.join(tree, ".git"), "w") as f:
                f.write(f"gitdir: {os.path.join(main, '.git', 'worktrees', 'feature')}\n")
            self.assertEqual(commands.repo_name(main), "project")
            self.assertEqual(commands.repo_name(os.path.join(tree, "sub")), "project")



class ScriptLabels(DaemonCase):
    def test_a_submitted_ticket_carries_its_label_and_keeps_its_key(self):
        self.daemon.cfg.script_labels = frozenset({"persistent-cargo-target.sh"})
        t = self.submit(("bash", "scripts/ci/persistent-cargo-target.sh", "architecture", "2G", "--", "cargo", "run"))
        self.assertTrue(t.key.endswith("|bash persistent-cargo-target.sh architecture"), t.key)
        self.daemon.cfg.script_labels = frozenset()  # a reload that drops it keeps the ticket's key
        self.assertEqual(ticket.Ticket.from_record(t.record(), t.dir).key, t.key)



class QueriesOffTheLoop(DaemonCase):
    def test_a_slow_systemctl_doesnt_hold_up_the_daemon(self):
        gate = threading.Event()
        self.runner.result = lambda ticket: "oom-kill" if gate.wait(10) else ""
        self.daemon.queries = daemon.ThreadPoolExecutor(max_workers=1)  # the real thing
        self.addCleanup(self.daemon.queries.shutdown, wait=False)
        t = self.submit()
        self.assertEqual(t.state, "running")
        self.runner.running.discard(t.id)  # gone without a result: systemd's verdict is asked for
        began = time.monotonic()
        self.daemon.check(self.now + 10)
        self.assertLess(time.monotonic() - began, 1.0)  # not the seconds systemctl takes
        self.assertEqual(t.state, "running")  # decided once systemd answers
        gate.set()
        for _ in range(500):
            self.daemon.check(self.now + 11)
            if t.state != "running":
                break
            time.sleep(0.01)  # (a second look doesn't wait: give the worker its moment)
        self.assertTrue(t.oom)  # with systemd's verdict
        self.assertEqual(self.daemon.pending_queries, {})



class QueriesBelongToTheirAttempt(DaemonCase):
    def test_a_ticket_s_queries_go_when_its_attempt_ends(self):
        gate = threading.Event()
        self.runner.result = lambda ticket: "oom-kill" if gate.wait(10) else ""
        self.daemon.queries = daemon.ThreadPoolExecutor(max_workers=1)
        self.addCleanup(self.daemon.queries.shutdown, wait=False)
        t = self.submit()
        self.runner.running.discard(t.id)
        self.daemon.check(self.now + 10)  # asked; not in yet
        self.assertEqual(len(self.daemon.pending_queries), 1)
        self.daemon.requeue(t, self.now + 11)  # (stopped for memory meanwhile: a new attempt)
        self.assertEqual(self.daemon.pending_queries, {})  # the old attempt's question is dropped
        gate.set()

    def test_the_direct_runners_state_is_read_inline_on_the_ticket(self):
        t = self.submit()

        class Direct:
            name = "direct"

            def unit_state(self, ticket):
                ticket.pid = 4242  # (as the direct runner records the supervisor it found)
                return "pending"

            def result(self, ticket):
                return ""
        self.daemon.queries = mock.Mock(submit=mock.Mock(side_effect=AssertionError("not through the pool")))
        with mock.patch.object(self.daemon, "runner_of", return_value=Direct()):
            self.assertEqual(self.daemon.unit_state_of(t), "pending")
            self.assertEqual(self.daemon.result_of(t), "")
        self.assertEqual(t.pid, 4242)  # on the ticket itself, not a copy

    def test_unreadable_container_storage_is_said_once(self):
        self.daemon.cfg.container_storage = ["/root/no-access"]
        with mock.patch.object(machine.os, "stat", side_effect=PermissionError("denied")) as _, \
                mock.patch.object(common, "log") as log:
            t = ticket.Ticket("x" * 36, ["docker", "build", "."], self.workdir(), "image", "k", self.now, self.tmp.name)
            t.disk_paths = []
            with mock.patch.object(machine, "disk_space", return_value=None), \
                    mock.patch.object(machine, "fs_device", return_value=None):
                self.daemon.disk_short(t)
                self.daemon.disk_short(t)
        said = [c for c in log.call_args_list if "can't read container storage" in str(c)]
        self.assertEqual(len(said), 1)



class Configuration(unittest.TestCase):
    def make(self, text):
        return config.Config(config.tomllib.loads(text), 128 * GIB, 32)

    def test_bad_values_are_refused(self):
        for text in ('slice_guard = "false"', '[lanes.a]\nexclusive = "no"',
                     '[[lane_rules]]\nmatch = "x"\nlane = "default"\nmax_jobs = 2',"max_jobs = 0", "max_jobs = 2.5", "lanes = 1", 'env = "x"', 'flock_slots = ["x"]',
                     '[lanes.a]\nproperties = "x"', "[lanes.a]\nmin_cores = 9\nmax_cores = 4",
                     'mem_floor = "200G"', "max_jobz = 3", 'runner = "docker"', "[lanes.a]\ncores = nan",
                     '[lanes.a]\nwaits_for_cpu = "no"'):
            if "lanes.a" in text:
                text = 'default_lane = "a"\n' + text
            with self.assertRaises(ValueError, msg=text):
                self.make(text)

    def test_a_broken_file_falls_back_to_the_last_good_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.toml")
            with open(path, "w") as f:
                f.write("max_jobs = 3\n")
            daemon.Daemon(path, os.path.join(tmp, "state"), machine=FakeMachine(), runner=FakeRunner())
            with open(path, "w") as f:
                f.write("max_jobs = 'lots'\n")
            again = daemon.Daemon(path, os.path.join(tmp, "state"), machine=FakeMachine(), runner=FakeRunner())
            self.assertEqual(again.cfg.max_jobs, 3)



class Supervisor(unittest.TestCase):
    """Runs `buildqd --supervise` for real on a prepared ticket directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def supervise(self, argv, env=None, parallel=None, timeout=None, max_log=0):
        d = os.path.join(self.tmp.name, "t")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "ticket.json"), "w") as f:
            json.dump({"argv": argv, "cwd": self.tmp.name, "timeout": timeout, "max_log": max_log}, f)
        with open(os.path.join(d, "env.json"), "w") as f:
            json.dump(env if env is not None else {"PATH": os.environ["PATH"]}, f)
        if parallel is not None:
            with open(os.path.join(d, "parallel.json"), "w") as f:
                json.dump(parallel, f)
        for name in ("stdout.log", "stderr.log", "supervisor.json"):
            if os.path.exists(os.path.join(d, name)):
                os.remove(os.path.join(d, name))
        started = time.time()
        code = subprocess.run([sys.executable, BUILDQD, "--supervise", d], timeout=120).returncode
        with open(os.path.join(d, "result.json")) as f:
            result = json.load(f)
        with open(os.path.join(d, "output.log"), "rb") as f:
            output = f.read()
        return code, result, output, time.time() - started, d

    def test_exit_code_output_and_environment(self):
        code, result, output, _, d = self.supervise(
            ["sh", "-c", 'echo "out $A $B"; echo err >&2; exit 3'],
            env={"PATH": os.environ["PATH"], "A": "mine"}, parallel={"B": "added"})
        self.assertEqual((code, result["exit"]), (3, 3))
        self.assertEqual(sorted(output.splitlines()), [b"err", b"out mine added"])  # both, in either order
        with open(os.path.join(d, "stdout.log"), "rb") as f:
            self.assertEqual(f.read(), b"out mine added\n")
        with open(os.path.join(d, "stderr.log"), "rb") as f:
            self.assertEqual(f.read(), b"err\n")
        self.assertFalse(os.path.exists(os.path.join(d, "env.json")))  # secrets aren't kept
        # Not in a cgroup of its own here, so its memory waits aren't measured.
        self.assertEqual((result["mem_wait_some"], result["mem_wait_full"], result["mem_high"]), (None, None, None))

    def test_a_second_supervisor_runs_nothing(self):
        d = os.path.join(self.tmp.name, "t")
        self.supervise(["true"])
        os.remove(os.path.join(d, "result.json"))  # env.json was claimed by the first
        code = subprocess.run([sys.executable, BUILDQD, "--supervise", d], timeout=60).returncode
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(os.path.join(d, "result.json")))

    def test_the_submitters_limits_are_applied(self):
        d = os.path.join(self.tmp.name, "t")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "ticket.json"), "w") as f:
            json.dump({"argv": ["sh", "-c", "ulimit -n"], "cwd": self.tmp.name, "rlimits": {"NOFILE": 512}}, f)
        with open(os.path.join(d, "env.json"), "w") as f:
            json.dump({"PATH": os.environ["PATH"]}, f)
        subprocess.run([sys.executable, BUILDQD, "--supervise", d], timeout=60)
        with open(os.path.join(d, "stdout.log")) as f:
            self.assertEqual(f.read().strip(), "512")

    def test_a_missing_command(self):
        code, _, output, _, _ = self.supervise(["no-such-command-buildq"])
        self.assertEqual(code, 127)
        self.assertIn(b"can't run", output)

    def test_time_paused_doesnt_count_toward_the_timeout(self):
        d = os.path.join(self.tmp.name, "paused")
        os.makedirs(d)
        with open(os.path.join(d, "ticket.json"), "w") as f:
            json.dump({"argv": ["sleep", "7"], "cwd": self.tmp.name, "timeout": 5, "max_log": 0}, f)
        with open(os.path.join(d, "env.json"), "w") as f:
            json.dump({"PATH": os.environ["PATH"]}, f)
        proc = subprocess.Popen([sys.executable, BUILDQD, "--supervise", d])
        for _ in range(100):
            if os.path.exists(os.path.join(d, "child.json")):
                break
            time.sleep(0.05)
        time.sleep(0.5)
        os.kill(proc.pid, signal.SIGSTOP)  # as if frozen (the sleep goes on: the build "runs" meanwhile)
        time.sleep(6)
        os.kill(proc.pid, signal.SIGCONT)
        self.assertEqual(proc.wait(30), 0)  # not timed out at the 5 s mark, once it is resumed
        with open(os.path.join(d, "result.json")) as f:
            self.assertFalse(json.load(f)["timed_out"])

    def test_timeout(self):
        code, result, output, took, _ = self.supervise(["sleep", "30"], timeout=1)
        self.assertEqual((code, result["timed_out"]), (124, True))
        self.assertLess(took, 20)
        self.assertIn(b"timed out", output)

    def test_leftover_processes_are_stopped(self):
        pidfile = os.path.join(self.tmp.name, "bg.pid")
        code, *_ = self.supervise(["sh", "-c", f"sleep 300 & echo $! > {pidfile}; echo started"])
        self.assertEqual(code, 0)
        with open(pidfile) as f:
            pid = int(f.read())
        time.sleep(0.3)
        ident = machine.proc_identity(pid)
        self.assertTrue(ident is None or ident[1] == "Z", "the background sleep is still running")

    def test_the_submitters_umask_is_used(self):
        d = os.path.join(self.tmp.name, "t")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "ticket.json"), "w") as f:
            json.dump({"argv": ["sh", "-c", "umask"], "cwd": self.tmp.name, "umask": 0o022}, f)
        with open(os.path.join(d, "env.json"), "w") as f:
            json.dump({"PATH": os.environ["PATH"]}, f)
        subprocess.run([sys.executable, BUILDQD, "--supervise", d], timeout=60)
        with open(os.path.join(d, "stdout.log")) as f:
            self.assertEqual(f.read().strip(), "0022")  # not the daemon's own 077

    def test_output_is_capped_keeping_the_end(self):
        script = "i=0; while [ $i -lt 5000 ]; do echo line-$i; i=$((i+1)); done"
        _, _, output, _, d = self.supervise(["sh", "-c", script], max_log=4096)
        self.assertLess(len(output), 4096 + 2048)
        self.assertIn(b"left out here", output)
        self.assertTrue(output.rstrip().endswith(b"line-4999"))
        with open(os.path.join(d, "stdout.log"), "rb") as f:
            self.assertNotIn(b"left out here", f.read())  # stdout stays parseable



class Sizes(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(common.parse_size("16G"), 16 * GIB)
        self.assertEqual(common.parse_size("512MiB"), 512 * common.MIB)
        self.assertEqual(common.parse_size("10%", 100 * GIB), 10 * GIB)
        self.assertEqual(common.parse_duration("45m"), 2700)
        for bad in ("lots", float("inf"), -1, "150%"):
            with self.assertRaises(ValueError):
                common.parse_size(bad, 100)

    def test_slice_path(self):
        self.assertEqual(machine.slice_path("buildq.slice"), "buildq.slice")
        self.assertEqual(machine.slice_path("a-b.slice"), "a.slice/a-b.slice")

    def test_machine_reads_this_machine(self):
        machine = machine.Machine()
        time.sleep(0.2)
        machine.sample(time.monotonic())
        self.assertGreater(machine.mem_available, 0)
        self.assertLessEqual(machine.busy, machine.ncpu + 0.01)



class ShimRules(unittest.TestCase):
    """#72: what `buildq run` runs at once instead of queueing, by the shim's own rule."""

    shim = load_shim()

    def setUp(self):
        patch = mock.patch.dict(os.environ)  # (whatever aliases the suite's own environment sets)
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("CARGO_ALIAS_FMT", None)

    def never_builds(self, command):
        words = command.split()
        return self.shim.never_builds(words[0], words[1:])

    def test_cargo_commands_that_never_compile_run_at_once(self):
        for command in ("cargo fmt", "cargo fmt --check -p api", "cargo +nightly fmt", "cargo -C sub tree",
                        "/home/u/.cargo/bin/cargo metadata --format-version 1", "cargo update -p x",
                        "cargo remove serde", "cargo add serde", "cargo fmt -- --config max_width=100",
                        "cargo --version", "cargo +stable -V", "cargo --list", "cargo help build"):
            self.assertTrue(self.never_builds(command), command)
            words = command.split()  # and the shim never queues what runs at once
            self.assertFalse(self.shim.heavy("cargo", words[1:]), command)

    def test_an_alias_named_fmt_queues(self):
        with mock.patch.dict(os.environ, {"CARGO_ALIAS_FMT": "build --release"}):
            self.assertFalse(self.never_builds("cargo fmt"))
        self.assertEqual(self.shim.CARGO_LIGHT, self.shim.CARGO_OWN_LIGHT | self.shim.CARGO_TOOLS_LIGHT)

    def test_everything_else_queues_as_asked(self):
        for command in ("cargo build", "cargo test", "cargo clippy", "cargo",
                        "cargo test version", "cargo run help",  # a test's name, a program's argument
                        "cargo build --help", "cargo xtask --help", "cargo sqlx --version",  # an alias may compile
                        "cargo clean",  # deletes a target folder a build may be using: it waits its turn
                        "cargo watch -x test", "cargo fmt --watch",
                        "cargo audit", "cargo deny check", "cargo outdated",  # other programs': an alias may take the name
                        "cargo rm serde", "cargo info serde",  # cargo's alias for remove; its own only since 1.82
                        "cargo --config alias.fmt=\"build\" fmt", "cargo --config=x.toml fmt",
                        "make help", "make --version", "pnpm --version", "npm version", "dotnet --info",
                        "env X=1 cargo fmt", "sh -c cargo-fmt", "./cargo-fmt.sh"):
            self.assertFalse(self.never_builds(command), command)

    def test_duplicate_option_values_do_not_confuse_tool_or_subcommand_position(self):
        self.assertTrue(self.shim.heavy("pnpm", ["--filter", "vite", "vite", "build"]))
        self.assertTrue(self.shim.heavy("pnpm", ["-F", "next", "next", "build"]))
        self.assertTrue(self.shim.heavy("pnpm", ["--filter", "vite", "exec", "vite", "build"]))
        self.assertFalse(self.shim.heavy("npx", ["-p", "vite", "vite", "dev"]))
        self.assertTrue(self.shim.heavy("npx", ["-p", "vite", "vite", "build"]))
        self.assertEqual(self.shim.build_then_run("cargo", ["-C", "run", "run", "--bin", "x"]),
                         ["-C", "run", "build", "--bin", "x"])
        self.assertEqual(self.shim.build_then_run("cargo", ["-C", "r", "r", "--", "--flag"]),
                         ["-C", "r", "build"])
        self.assertEqual(self.shim.build_then_run("dotnet", ["run", "--project", "app", "--", "--no-build"]),
                         ["build", "app"])
        self.assertEqual(self.shim.build_then_run("dotnet", ["run", "-p", "app", "-p:Configuration=Release"]),
                         ["build", "app", "-p:Configuration=Release"])
        self.assertEqual(self.shim.build_then_run("dotnet", ["run", "--project", "App.csproj", "-p", "Configuration=Release"]),
                         ["build", "App.csproj", "-p:Configuration=Release"])
        self.assertEqual(self.shim.build_then_run("dotnet", ["run", "--project=app.csproj", "--no-restore"]),
                         ["build", "app.csproj", "--no-restore"])
        self.assertFalse(self.shim.heavy("dotnet", ["run", "--no-build", "--", "--port", "8080"]))
        self.assertIsNone(self.shim.build_then_run("dotnet", ["run", "--no-build", "--", "--port", "8080"]))
        self.assertFalse(self.shim.heavy("dotnet", ["watch", "run"]))
        self.assertTrue(self.shim.heavy("dotnet", ["run", "--project", "App.csproj", "--", "--watch"]))
        self.assertEqual(self.shim.build_then_run("dotnet", ["run", "--project", "App.csproj", "--", "--watch"]),
                         ["build", "App.csproj"])
        self.assertTrue(self.shim.heavy("dotnet", ["run", "--", "--no-build"]))
        self.assertEqual(self.shim.build_then_run("dotnet", ["run", "--", "--no-build"]), ["build"])



class ClientAndGuardHelpers(unittest.TestCase):
    def test_caller_recognises_antigravity_conversation_id(self):
        cleared = {k: "" for k in ("BUILDQ_CALLER", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID",
                                   "T3CODE_THREAD_ID", "ANTIGRAVITY_CONVERSATION_ID")}
        with mock.patch.dict(os.environ, {**cleared, "ANTIGRAVITY_CONVERSATION_ID": "conv-42"}):
            self.assertEqual(client.caller(), "conv-42")
        with mock.patch.dict(os.environ, {**cleared, "T3CODE_THREAD_ID": "thread-9",
                                          "ANTIGRAVITY_CONVERSATION_ID": "conv-42"}):
            self.assertEqual(client.caller(), "thread-9")

    def test_tail_offset_handles_edges_and_multi_chunk_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            def check(data: bytes, lines: int, expected: bytes):
                path = os.path.join(tmp, "log")
                with open(path, "wb") as f:
                    f.write(data)
                off = client.tail_offset(path, lines)
                self.assertEqual(data[off:], expected, (len(data), lines, off))

            check(b"", 5, b"")
            check(b"a\nb\nc\n", 0, b"")
            check(b"a\nb\nc\n", -3, b"")
            check(b"a\nb\nc\nd\n", 2, b"c\nd\n")
            check(b"a\nb\nc\nd\n", 10, b"a\nb\nc\nd\n")
            check(b"a\nb\nc\nd", 2, b"c\nd")
            check(b"a\nb\nc\nd", 1, b"d")
            check(b"a\nb", 2, b"a\nb")
            check(b"only", 1, b"only")
            # Multi-chunk (> 200 KiB): both many short lines and lines wider than one 64 KiB chunk.
            many = b"".join(f"line-{i:06d}-{'x' * 40}\n".encode() for i in range(4500))
            self.assertGreater(len(many), 200_000)
            tail_1500 = b"".join(f"line-{i:06d}-{'x' * 40}\n".encode() for i in range(3000, 4500))
            check(many, 1500, tail_1500)
            wide = (b"A" * 80_000 + b"\n") + (b"B" * 80_000 + b"\n") + (b"C" * 80_000 + b"\n")
            check(wide, 2, (b"B" * 80_000 + b"\n") + (b"C" * 80_000 + b"\n"))

    def test_rustc_guard_stat_parser_handles_spaces_parens_and_newlines(self):
        with open(os.path.join(ROOT, "bin", "buildq-rustc-guard")) as f:
            script = f.read()
        match = re.search(r"pid=\$\(awk '([^']+)' \"/proc/\$pid/stat\"", script)
        self.assertIsNotNone(match)
        awk_prog = match.group(1)
        with tempfile.TemporaryDirectory() as tmp:
            for name, content in (
                ("plain", "1234 (bash) S 5678 1234 1234 0 -1\n"),
                ("spaces", "1234 (Web Content) S 5678 1234 1234 0 -1\n"),
                ("parens", "1234 (a (b) c) S 5678 1234 1234 0 -1\n"),
                ("newline", "1234 (line1\nline2) S 5678 1234 1234 0 -1\n"),
            ):
                path = os.path.join(tmp, f"{name}.stat")
                with open(path, "w") as f:
                    f.write(content)
                res = subprocess.run(["awk", awk_prog, path], capture_output=True, text=True, timeout=10)
                self.assertEqual((res.returncode, res.stdout.strip()), (0, "5678"), (name, res.stderr))



if __name__ == "__main__":
    unittest.main()
