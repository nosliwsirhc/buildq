"""Unit tests for buildqd: admission, cores, order, turns, history, keys, configuration, the supervisor."""

import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.dont_write_bytecode = True
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILDQD = os.path.join(ROOT, "bin", "buildqd")


def load(name):
    loader = importlib.machinery.SourceFileLoader(name, os.path.join(ROOT, "bin", name))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


buildqd = load("buildqd")
buildqd.log = lambda message: None  # keep test output readable
GIB = buildqd.GIB


class FakeMachine:
    def __init__(self, ncpu=32, mem_total=128 * GIB, mem_available=100 * GIB, busy=0.0):
        self.ncpu = ncpu
        self.mem_total = mem_total
        self.mem_available = mem_available
        self.busy = self.busy_now = busy
        self.swap_used = 0
        self.psi_cpu = self.psi_mem = self.psi_mem_some = 0.0

    def sample(self, now):
        pass

    def summary(self):
        return {}


class FakeRunner:
    name = "systemd"  # so lane caps count as enforced, as they are in production

    def __init__(self):
        self.running = set()
        self.stopped = []

    def launch(self, ticket, lane, slice_name):
        self.running.add(ticket.id)
        return None, True

    def prepare(self, ticket, slice_name):
        ticket.unit = f"buildq-{ticket.id}.service"

    def unit_state(self, ticket):
        return "pending" if ticket.id in self.running else "gone"

    def result(self, ticket):
        return ""

    def cleanup(self, ticket):
        return None

    def alive(self, ticket):
        return ticket.id in self.running

    def stop(self, ticket, force=False):
        self.stopped.append(ticket.id)
        return None

    @staticmethod
    def check_lane(lane):
        return None


BASE_CONFIG = """
max_jobs = 8
cpu_target = 1.0
mem_floor = "10G"
disk_floor = "0"
ramp_seconds = 60
starve_seconds = 300
default_lane = "heavy"
[lanes.heavy]
cores = 8
min_cores = 2
max_cores = 16
mem = "20G"
properties = { MemoryMax = "26G" }
[lanes.web]
cores = 2
min_cores = 1
max_cores = 4
mem = "4G"
may_pass_held = true
exclusive = false
"""


class DaemonCase(unittest.TestCase):
    config = BASE_CONFIG

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.tmp.name, "config.toml")
        with open(self.config_path, "w") as f:
            f.write(self.config)
        self.machine = FakeMachine()
        self.runner = FakeRunner()
        self.state = os.path.join(self.tmp.name, "state")
        self.daemon = buildqd.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner)
        self.now = time.time()
        self.dirs = 0

    def tearDown(self):
        self.daemon.stop_memory_watch()
        for t in self.daemon.tickets.values():
            self.daemon.close_jobserver(t)
        self.tmp.cleanup()

    def workdir(self, name=None):
        self.dirs += 1
        path = os.path.join(self.tmp.name, "work", name or f"w{self.dirs}")
        os.makedirs(os.path.join(path, ".git"), exist_ok=True)  # its own worktree
        return path

    def submit(self, argv=("cargo", "build"), lane=None, cwd=None, env=None, **extra):
        msg = {"argv": list(argv), "cwd": cwd or self.workdir(), "lane": lane, "env": env or {}, **extra}
        t, error = self.daemon.submit(msg, self.now)
        self.assertIsNone(error)
        return t

    def finish(self, t, exit_code=0, after=30, **result):
        with open(os.path.join(t.dir, "result.json"), "w") as f:
            json.dump({"exit": exit_code, **result}, f)
        self.runner.running.discard(t.id)
        self.daemon.check(self.now + after)

    def states(self, *tickets):
        return [t.state for t in tickets]


class Admission(DaemonCase):
    def test_memory_decides_how_many_start(self):
        self.machine.mem_available = 50 * GIB  # 40G above the 10G floor: two 20G builds
        tickets = [self.submit() for _ in range(3)]
        self.assertEqual(self.states(*tickets), ["running", "running", "queued"])
        self.assertEqual(tickets[2].reason_cat, "memory")

    def test_running_builds_growth_is_held(self):
        self.machine.mem_available = 50 * GIB
        first = self.submit()
        first.mem_now = 15 * GIB
        self.machine.mem_available = 35 * GIB
        second = self.submit()  # 35 - (20 - 15) growth - 10 floor = 20: fits exactly
        self.assertEqual(second.state, "running")
        third = self.submit(mem="1G")
        self.assertEqual(third.state, "queued")

    def test_builds_get_the_cores_that_are_free(self):
        self.machine.busy = 20.0
        script = ("bash", "build.sh")  # not resizable: no cores to lend
        first = self.submit(script)   # alone: 12 free, wants 8
        second = self.submit(script)  # 32 - 20 busy - 8 ramping = 4 free
        third = self.submit(script)   # nothing left for its minimum of 2
        self.assertEqual(self.states(first, second, third), ["running", "running", "queued"])
        self.assertEqual((first.granted, second.granted), (8, 4))
        self.assertEqual(third.reason_cat, "cpu")

    def test_resizable_builds_lend_cores(self):
        self.machine.busy = 20.0
        first = self.submit()   # cargo: speaks the jobserver protocol, so it can give cores back
        second = self.submit()
        third = self.submit()   # fits by taking cores the running ones can hand back
        self.assertEqual(self.states(first, second, third), ["running", "running", "running"])
        self.assertTrue(first.elastic)

    def test_ramp_reservation_expires(self):
        self.machine.busy = 28.0
        first = self.submit(("bash", "build.sh"))
        second = self.submit(("bash", "build.sh"))
        self.assertEqual(second.state, "queued")
        first.cores_now = 1.0
        self.machine.busy = 20.0
        self.daemon.schedule(self.now + 61)
        self.assertEqual(second.state, "running")

    def test_a_lone_build_ignores_cpu_load_and_takes_its_minimum(self):
        self.machine.busy = 32.0
        t = self.submit()
        self.assertEqual(t.state, "running")
        self.assertEqual(t.granted, 2)

    def test_memory_is_never_ignored(self):
        self.machine.mem_available = 12 * GIB
        t = self.submit()
        self.assertEqual((t.state, t.reason_cat), ("queued", "memory"))

    def test_max_jobs(self):
        self.daemon.cfg.max_jobs = 2
        tickets = [self.submit(cores=1) for _ in range(3)]
        self.assertEqual(self.states(*tickets), ["running", "running", "queued"])
        self.assertEqual(tickets[2].reason_cat, "max-jobs")

    def test_pressure(self):
        self.machine.psi_mem = 25.0
        first = self.submit()
        self.assertEqual(first.reason_cat, "memory-pressure")
        self.machine.psi_mem = 0.0
        self.daemon.schedule(self.now)
        self.assertEqual(first.state, "running")
        self.machine.psi_cpu = 80.0
        second = self.submit()
        self.assertEqual(second.reason_cat, "cpu-pressure")

    def test_too_big_for_the_machine(self):
        t = self.submit(mem="125G")
        self.assertEqual((t.state, t.reason_cat), ("queued", "too-big"))

    def test_disk_floor(self):
        self.daemon.cfg.disk_floor = 1 << 60
        self.assertEqual(self.submit().reason_cat, "disk")

    def test_one_build_per_workspace_in_an_exclusive_lane(self):
        tree = self.workdir("tree")
        os.makedirs(os.path.join(tree, "api"))
        first = self.submit(cwd=tree)
        second = self.submit(cwd=os.path.join(tree, "api"))  # same worktree
        self.assertEqual(self.states(first, second), ["running", "queued"])
        self.assertEqual(second.reason_cat, "workspace")
        web1 = self.submit(("pnpm", "build"), lane="web", cwd=tree)
        web2 = self.submit(("pnpm", "test"), lane="web", cwd=tree)
        self.assertEqual(self.states(web1, web2), ["running", "running"])
        self.finish(first)
        self.daemon.schedule(self.now + 31)
        self.assertEqual(second.state, "running")

    def test_a_shared_target_dir_is_one_workspace(self):
        target = os.path.join(self.tmp.name, "target")
        self.submit(env={"CARGO_TARGET_DIR": target})
        second = self.submit(env={"CARGO_TARGET_DIR": target})  # another worktree, same target dir
        self.assertEqual(second.reason_cat, "workspace")

    def test_a_build_whose_directory_is_gone_fails_without_a_turn(self):
        self.daemon.cfg.max_jobs = 1
        self.submit(cores=1)
        gone = self.workdir("gone")
        t = self.submit(cwd=gone)
        shutil.rmtree(gone)
        self.daemon.schedule(self.now)
        self.assertEqual(t.state, "failed")
        self.assertIn("is gone", t.error)


class QuickLanesDontWaitForCpu(DaemonCase):
    config = BASE_CONFIG.replace("may_pass_held = true", "may_pass_held = true\nwaits_for_cpu = false")

    def test_a_lane_that_doesnt_wait_for_cpu_starts_on_a_busy_machine(self):
        self.machine.busy = self.machine.busy_now = 32.0
        first = self.submit()
        self.assertEqual(first.state, "running")  # alone: it ignores the load anyway
        second = self.submit()
        self.assertEqual((second.state, second.reason_cat), ("queued", "cpu"))
        web = self.submit(("pnpm", "build"), lane="web")
        self.assertEqual((web.state, web.granted), ("running", 1))  # its minimum, over the target
        self.machine.psi_cpu = 80.0
        again = self.submit(("pnpm", "test"), lane="web")
        self.assertEqual(again.state, "running")  # CPU pressure doesn't hold it either
        self.daemon.schedule(self.now)
        self.assertEqual(second.state, "queued")  # the cores it took aren't handed to waiting builds

    def test_memory_still_holds_it(self):
        self.submit()
        self.machine.mem_available = 12 * GIB
        web = self.submit(("pnpm", "build"), lane="web")
        self.assertEqual((web.state, web.reason_cat), ("queued", "memory"))

    def test_a_rigid_build_needs_what_it_uses_even_above_the_cpu_target(self):
        self.daemon.cfg.cpu_target = 0.05  # 1.6 of 32 cores
        t = self.submit(("pnpm", "build"), lane="web")
        self.daemon.history.add({"key": t.key, "t": self.now, "peak_mem": GIB, "peak_cores": 4.0,
                                 "avg_cores": 4.0, "granted": 1})
        self.daemon.estimate(t, self.now)
        self.assertEqual((t.rigid, t.need), (True, 4))  # the lane's max_cores, not the target

    def test_a_starving_build_of_such_a_lane_holds_no_cores(self):
        self.daemon.cfg.lanes["web"].may_pass_held = False
        self.machine.mem_available = 40 * GIB
        self.submit()  # a heavy build: 20G of the 30G above the floor
        web = self.submit(("pnpm", "build"), lane="web", mem="20G")
        self.daemon.schedule(self.now + 400)  # past starve_seconds
        self.assertTrue(web.held)
        self.assertEqual((self.daemon.budget["held_mem"], self.daemon.budget["held_cores"]), (web.est_mem, 0))

    def test_the_rebalancer_takes_its_cores_back_from_resizable_builds(self):
        project = self.workdir("project")
        open(os.path.join(project, "Cargo.lock"), "w").close()  # a cold build: it wants 16
        heavy = self.submit(cwd=project)
        self.assertEqual(heavy.granted, 16)
        heavy.elastic, heavy.cores_now = True, 16.0
        self.machine.busy = self.machine.busy_now = 32.0  # the machine is full
        web = self.submit(("pnpm", "build"), lane="web")
        self.assertEqual((web.state, web.granted), ("running", 1))
        self.daemon.rebalance(self.now + 1)
        self.assertEqual(heavy.granted, 15)


class Order(DaemonCase):
    def test_a_starving_build_goes_before_quicker_ones(self):
        self.daemon.cfg.max_jobs = 1
        blocker = self.submit(cores=1)
        long = self.submit(("cargo", "test", "-p", "long"), cores=1)
        self.daemon.history.add({"key": long.key, "t": self.now, "peak_mem": GIB, "peak_cores": 1.0,
                                 "wall": 3000, "exit": 0})
        self.now += 301
        short = self.submit(("cargo", "test", "-p", "short"), cores=1)
        self.daemon.history.add({"key": short.key, "t": self.now, "peak_mem": GIB, "peak_cores": 1.0,
                                 "wall": 10, "exit": 0})
        self.daemon.schedule(self.now)
        self.assertEqual([t.id for t in self.daemon.queue(self.now)], [long.id, short.id])
        self.assertIsNotNone(blocker)

    def test_backfill_lets_a_small_build_pass(self):
        self.machine.mem_available = 40 * GIB  # 30G free: 6G left once the 24G build runs
        self.submit(mem="24G")
        waiting = self.submit()
        small = self.submit(mem="512M", cores=1, lane="web")
        self.assertEqual(self.states(waiting, small), ["queued", "running"])

    def test_a_starving_build_gets_room_held(self):
        self.daemon.cfg.lanes["heavy"].mem = GIB  # builds with no history count as 1G
        self.daemon.cfg.lanes["web"].mem = GIB
        self.machine.mem_available = 35 * GIB  # 25G free
        self.submit(mem="24G")
        waiting = self.submit(mem="20G")
        self.now += 301
        small = self.submit(cores=1)  # 1G would fit, but the starving build's room is held
        self.assertEqual(self.states(waiting, small), ["queued", "queued"])
        self.assertTrue(waiting.held)
        self.assertEqual(small.reason_cat, "held")
        web = self.submit(("pnpm", "build"), lane="web", cores=1)
        self.assertEqual(web.state, "running")  # its lane may pass held room

    def test_no_room_is_held_for_a_build_that_could_never_fit(self):
        self.machine.mem_available = 30 * GIB
        huge = self.submit(mem="100G")  # more than could ever be free here
        self.now += 301
        small = self.submit(cores=1, lane="web")
        self.assertFalse(huge.held)
        self.assertEqual(small.state, "running")

    def test_priority_goes_first(self):
        self.daemon.cfg.max_jobs = 1
        blocker = self.submit(cores=1)
        low = self.submit(cores=1)
        high = self.submit(cores=1, priority=5)
        self.finish(blocker)
        self.daemon.schedule(self.now + 31)
        self.assertEqual(self.states(low, high), ["queued", "running"])


class Priorities(DaemonCase):
    config = BASE_CONFIG.replace('may_pass_held = true', 'may_pass_held = true\npriority = 100\nextra_jobs = 2') + """
[lanes.dotnet]
cores = 4
mem = "6G"
priority = 50
[[lane_rules]]
match = "^(pnpm|npm|npx|yarn|vite|vitest|tsc|eslint|node) "
lane = "web"
[[lane_rules]]
match = "^dotnet "
lane = "dotnet"
"""

    def test_the_command_picks_the_lane(self):
        self.assertEqual(self.submit(("pnpm", "--filter", "web", "build")).lane, "web")
        self.assertEqual(self.submit(("dotnet", "build")).lane, "dotnet")
        self.assertEqual(self.submit(("cargo", "test")).lane, "heavy")
        self.assertEqual(self.submit(("pnpm", "build"), lane="heavy").lane, "heavy")  # a named lane wins

    def test_quick_lanes_go_first(self):
        self.daemon.cfg.max_jobs = 1
        blocker = self.submit(("cargo", "build"), cores=1)
        cargo = self.submit(("cargo", "test"), cores=1)
        dotnet = self.submit(("dotnet", "build"), cores=1)
        self.assertEqual([t.id for t in self.daemon.queue()], [dotnet.id, cargo.id])
        web = self.submit(("pnpm", "build"), cores=1)
        self.assertEqual(web.state, "running")  # beside the blocker: the web lane has extra jobs
        self.finish(blocker)
        self.daemon.schedule(self.now + 31)
        self.assertEqual((dotnet.state, cargo.state), ("running", "queued"))

    def test_extra_jobs_are_limited(self):
        self.daemon.cfg.max_jobs = 1
        self.submit(("cargo", "build"), cores=1)
        webs = [self.submit(("pnpm", "build"), cores=1) for _ in range(3)]
        self.assertEqual(self.states(*webs), ["running", "running", "queued"])

    def test_within_a_lane_the_quickest_goes_first(self):
        self.daemon.cfg.max_jobs = 1
        blocker = self.submit(("cargo", "build"), cores=1)
        slow = self.submit(("cargo", "test", "-p", "slow"), cores=1)
        quick = self.submit(("cargo", "test", "-p", "quick"), cores=1)
        for t, wall in ((slow, 900), (quick, 40)):
            self.daemon.history.add({"key": t.key, "t": self.now, "peak_mem": GIB, "peak_cores": 1.0,
                                     "wall": wall, "exit": 0})
        self.daemon.schedule(self.now)
        self.assertEqual([t.id for t in self.daemon.queue()], [quick.id, slow.id])

    def test_dotnet_builds_are_told_their_cores(self):
        t = self.submit(("dotnet", "build"))
        with open(os.path.join(t.dir, "parallel.json")) as f:
            parallel = json.load(f)
        self.assertEqual(parallel["DOTNET_PROCESSOR_COUNT"], str(t.granted))
        self.assertEqual(parallel["MSBUILDDISABLENODEREUSE"], "1")


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

    def test_test_threads_are_set_only_where_the_lane_asks(self):
        self.daemon.cfg.lanes["heavy"].test_threads = True
        t = self.submit(("cargo", "test"))
        with open(os.path.join(t.dir, "parallel.json")) as f:
            self.assertEqual(json.load(f)["RUST_TEST_THREADS"], str(t.granted))

    def test_no_jobserver_when_the_caller_brings_makeflags(self):
        t = self.submit(env={"MAKEFLAGS": "-j3"})
        self.assertIsNone(t.jobserver)

    def test_given_cores_are_used_as_they_are(self):
        t = self.submit(cores=3)
        self.assertEqual((t.want, t.need, t.granted), (3, 3, 3))

    def test_given_memory_can_raise_but_not_lower_the_estimate(self):
        self.assertEqual(self.submit(mem="1G").est_mem, 20 * GIB)
        self.assertEqual(self.submit(mem="24G").est_mem, 24 * GIB)


class Memory(DaemonCase):
    def test_more_cores_need_more_memory(self):
        t = self.submit()
        self.daemon.history.add({"key": t.key, "t": self.now, "peak_mem": 8 * GIB, "peak_cores": 2.0, "granted": 2})
        self.daemon.estimate(t, self.now)
        base = t.mem_base
        self.assertEqual(self.daemon.mem_for(t, 2), base)
        self.assertEqual(self.daemon.mem_for(t, 3), int(base * 3 / 2))
        self.assertEqual(self.daemon.mem_for(t, 16), 26 * GIB)  # no more than the enforced cap

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
        again = buildqd.History(self.daemon.history.path)  # and it survives a restart
        e = again.estimate(t.key, self.daemon.cfg.lanes["heavy"], self.daemon.cfg, 32, True, False, self.now)
        self.assertGreater(e.mem, 18 * GIB)

    def test_a_kill_from_outside_doesnt_teach_the_cap(self):
        self.assertFalse(buildqd.oom_of({"oom_kills": 1, "hit_own_cap": False}))
        self.assertTrue(buildqd.oom_of({"oom_kills": 1, "hit_own_cap": True}))


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
        self.assertIsNone(buildqd.cgroup_memory_waits(path))  # no memory.pressure
        with open(os.path.join(path, "memory.pressure"), "w") as f:
            f.write("some avg10=1.00 avg60=0.50 avg300=0.10 total=12500000\n"
                    "full avg10=0.00 avg60=0.00 avg300=0.00 total=3000000\n")
        with open(os.path.join(path, "memory.events.local"), "w") as f:
            f.write("low 0\nhigh 5\nmax 0\noom 0\noom_kill 0\n")
        self.assertEqual(buildqd.cgroup_memory_waits(path), (12.5, 3.0, 5))

    def test_the_biggest_cgroups_are_found_where_the_memory_is(self):
        root = os.path.join(self.tmp.name, "cg")
        mib = buildqd.MIB
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
        found, complete = buildqd.biggest_cgroups(root)
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
        found, complete = buildqd.biggest_cgroups(root, max_dirs=1)
        self.assertFalse(complete)
        self.assertEqual([rel for _, _, rel, _ in found], ["a.slice"])  # listed whole, not looked into

    def test_a_swapped_out_cgroup_is_still_found(self):
        root = os.path.join(self.tmp.name, "cg")
        path = self.cgroup("a.slice", 100 * buildqd.MIB, anon=100 * buildqd.MIB)
        with open(os.path.join(path, "memory.swap.current"), "w") as f:
            f.write(str(8 * GIB))
        found, _ = buildqd.biggest_cgroups(root)
        self.assertEqual(found, [(100 * buildqd.MIB, 8 * GIB, "a.slice", "")])

    def test_looking_for_process_names_keeps_to_its_limits(self):
        root = os.path.join(self.tmp.name, "cg")
        self.cgroup("a.slice", 10 * GIB, anon=9 * GIB)
        for i in range(30):  # many empty cgroups below it, none of them big
            os.makedirs(os.path.join(root, "a.slice", f"e{i}.scope"))
        budget = buildqd.WalkBudget(5, None)
        buildqd.biggest_process(os.path.join(root, "a.slice"), budget=budget)
        self.assertEqual((budget.dirs, budget.complete), (0, False))
        expired = buildqd.WalkBudget(1000, time.monotonic() - 1)
        buildqd.biggest_process(os.path.join(root, "a.slice"), budget=expired)
        self.assertEqual(expired.dirs, 1000)  # a deadline that has passed reads nothing

    def test_container_names_are_shortened(self):
        long = "docker-" + "0123456789ab" + "c" * 52 + ".scope"
        self.assertEqual(buildqd.short_cgroup("system.slice/" + long), "docker-0123456789ab.scope")

    def watch(self, tick):
        self.daemon.watch_memory(tick)
        self.assertTrue(self.daemon.memory_idle())

    def test_tight_memory_is_logged_every_half_minute(self):
        lines = []
        old_log, buildqd.log = buildqd.log, lambda message, level=None: lines.append(message)
        self.addCleanup(setattr, buildqd, "log", old_log)
        self.daemon.cg_root = os.path.join(self.tmp.name, "cg")
        self.cgroup("system.slice", 31 * GIB, anon=30 * GIB)
        self.cgroup("system.slice/docker-0123456789abcdef0123.scope", 30 * GIB, anon=29 * GIB)
        self.watch(1000.0)
        self.assertEqual(lines, [])  # 100G available: not tight
        self.machine.psi_mem_some = 40.0  # one build slowed at its MemoryHigh: not a reason
        self.watch(1000.5)
        self.assertEqual(lines, [])
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
        self.watch(1065.0)
        self.watch(1094.0)
        self.assertEqual(len(lines), 6)
        self.watch(1095.0)  # calm for half a minute
        self.assertEqual(lines[-1], "memory is no longer tight: 100G available")
        with open(os.path.join(self.state, "memory.log")) as f:
            kept = f.read().splitlines()
        self.assertEqual(len(kept), 7)
        self.assertIn("memory is tight", kept[0])


class Jobservers(DaemonCase):
    def test_tokens(self):
        js = buildqd.Jobserver(os.path.join(self.tmp.name, "fifo"), create=True)
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


class Recovery(DaemonCase):
    def test_a_resent_submission_gets_its_ticket_back(self):
        cwd = self.workdir()
        first = self.submit(cwd=cwd, submission="abc")
        again = self.submit(cwd=cwd, submission="abc")
        self.assertIs(first, again)
        restarted = buildqd.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner)
        restarted.load_tickets(self.now)
        t, _ = restarted.submit({"argv": ["cargo", "build"], "cwd": cwd, "env": {}, "submission": "abc"}, self.now)
        self.assertEqual(t.id, first.id)
        for ticket in restarted.tickets.values():
            restarted.close_jobserver(ticket)

    def test_a_ticket_keeps_the_runner_it_started_under(self):
        t = self.submit()
        t.runner = "direct"
        self.daemon.save(t)
        again = buildqd.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner)
        again.load_tickets(self.now)
        self.assertEqual(again.tickets[t.id].state, "running")  # not given up for a runner change
        for ticket in again.tickets.values():
            again.close_jobserver(ticket)

    def test_a_cancel_goes_on_after_a_restart(self):
        t = self.submit()
        self.daemon.cancel(t, self.now)
        self.runner.stopped.clear()
        again = buildqd.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner)
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
        again = buildqd.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner)
        again.load_tickets(self.now)
        again.check(self.now + 11)
        self.assertEqual(again.tickets[t.id].state, "queued")
        for ticket in again.tickets.values():
            again.close_jobserver(ticket)

    def test_a_typo_in_the_runner_keeps_the_last_good_configuration(self):
        with open(self.config_path, "w") as f:
            f.write(BASE_CONFIG.replace('default_lane = "heavy"', 'default_lane = "heavy"\nrunner = "systmed"'))
        again = buildqd.Daemon(self.config_path, self.state, machine=self.machine)
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
        restarted = buildqd.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner)
        restarted.load_tickets(self.now + 60)
        self.assertEqual(restarted.tickets[t.id].mem_high, 104492)

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
        loaded = buildqd.Ticket.from_record(record, t.dir)
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
        again = buildqd.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner)
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
        self.daemon.cfg = buildqd.Config(
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
        self.assertEqual(e.mem, int(5 * GIB * 1.15) + 256 * buildqd.MIB)
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
        again = buildqd.History(self.daemon.history.path)
        self.assertEqual(len(again.by_key["k"]), 1)

    def test_history_keeps_a_bounded_number_of_commands(self):
        history = buildqd.History(os.path.join(self.tmp.name, "h.jsonl"), max_keys=10)
        for i in range(50):
            history.add({"key": f"k{i}", "t": self.now, "peak_mem": 1, "peak_cores": 1.0})
        self.assertEqual(len(history.by_key), 10)
        self.assertIn("k49", history.by_key)


class Keys(unittest.TestCase):
    def words(self, *argv):
        return " ".join(buildqd.command_words(list(argv)))

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
        self.assertEqual(self.words("python3", "-m", "pytest", "-x"), "python3 pytest")

    def test_keys_hold_no_urls_but_keep_npm_scopes(self):
        self.assertNotIn("token", self.words("wget", "https://files.example/private-access-token"))
        self.assertEqual(self.words("pnpm", "--filter", "@scope/web", "build"), "pnpm --filter @scope/web build")

    def test_keys_hold_no_secrets(self):
        for argv in (["bash", "-c", "PGPASSWORD=s3cret psql -h db"], ["psql", "postgres://u:hunter2@db/x"],
                     ["docker", "run", "-e", "TOKEN=abc", "img"]):
            key = self.words(*argv)
            for secret in ("s3cret", "hunter2", "abc"):
                self.assertNotIn(secret, key)
        label = buildqd.redacted_label(["env", "TOKEN=abc", "sh", "-c", "X=1 make", "postgres://u:hunter2@db/x"])
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
            self.assertEqual(buildqd.repo_name(main), "project")
            self.assertEqual(buildqd.repo_name(os.path.join(tree, "sub")), "project")


class Configuration(unittest.TestCase):
    def make(self, text):
        return buildqd.Config(buildqd.tomllib.loads(text), 128 * GIB, 32)

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
            buildqd.Daemon(path, os.path.join(tmp, "state"), machine=FakeMachine(), runner=FakeRunner())
            with open(path, "w") as f:
                f.write("max_jobs = 'lots'\n")
            again = buildqd.Daemon(path, os.path.join(tmp, "state"), machine=FakeMachine(), runner=FakeRunner())
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
        ident = buildqd.proc_identity(pid)
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
        self.assertEqual(buildqd.parse_size("16G"), 16 * GIB)
        self.assertEqual(buildqd.parse_size("512MiB"), 512 * buildqd.MIB)
        self.assertEqual(buildqd.parse_size("10%", 100 * GIB), 10 * GIB)
        self.assertEqual(buildqd.parse_duration("45m"), 2700)
        for bad in ("lots", float("inf"), -1, "150%"):
            with self.assertRaises(ValueError):
                buildqd.parse_size(bad, 100)

    def test_slice_path(self):
        self.assertEqual(buildqd.slice_path("buildq.slice"), "buildq.slice")
        self.assertEqual(buildqd.slice_path("a-b.slice"), "a.slice/a-b.slice")

    def test_machine_reads_this_machine(self):
        machine = buildqd.Machine()
        time.sleep(0.2)
        machine.sample(time.monotonic())
        self.assertGreater(machine.mem_available, 0)
        self.assertLessEqual(machine.busy, machine.ncpu + 0.01)


if __name__ == "__main__":
    unittest.main()
