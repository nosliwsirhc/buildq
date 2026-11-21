"""Unit tests for buildqd: admission, cores, order, turns, history, keys, configuration, the supervisor."""

import argparse
import importlib.machinery
import importlib.util
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
buildqd.log = lambda message, level=None: None  # keep test output readable
GIB = buildqd.GIB
MIB = buildqd.MIB


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
    can_freeze = True

    def __init__(self):
        self.running = set()
        self.stopped = []
        self.forced = []
        self.frozen = []

    def freeze(self, ticket, frozen):
        self.frozen.append((ticket.id, frozen))
        return None

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
        if force:
            self.forced.append(ticket.id)
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



SOURCE = {"repo": "/src/project", "commit": "a" * 40, "fingerprint": "b" * 32, "dirty": False, "remote": "owner/project"}


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
        self.daemon = buildqd.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                                     queries=buildqd.InlineExecutor())
        self.seen_source = dict(SOURCE)  # what `buildq source` finds when the daemon checks a build's tree
        self.daemon.source_command = [sys.executable, "-c",
                                      "import sys; print(open(sys.argv[1]).read())", self.source_file(), "--"]
        self.now = time.time()
        self.dirs = 0

    def source_file(self):
        path = os.path.join(self.tmp.name, "seen-source.json")
        with open(path, "w") as f:
            json.dump(self.seen_source, f)
        return path

    def settle(self):
        """Let the daemon's source checks finish and be read."""
        for _ in range(100):
            for proc, *_ in list(self.daemon.source_checks):
                proc.wait(10)
            self.daemon.collect_source_checks(self.now)
            if not self.daemon.source_checks:
                return

    def tearDown(self):
        self.doCleanups()  # daemons a test started (reload()) stop their threads before the files go
        self.daemon.stop_memory_watch()
        for t in self.daemon.tickets.values():
            self.daemon.close_jobserver(t)
        self.tmp.cleanup()

    def workdir(self, name=None):
        self.dirs += 1
        path = os.path.join(self.tmp.name, "work", name or f"w{self.dirs}")
        os.makedirs(os.path.join(path, ".git"), exist_ok=True)  # its own worktree
        return path

    def submit(self, argv=("cargo", "build"), lane=None, cwd=None, env=None, peer=None, **extra):
        msg = {"argv": list(argv), "cwd": cwd or self.workdir(), "lane": lane, "env": env or {}, **extra}
        t, error = self.daemon.submit(msg, self.now, peer=peer)  # (peer: the client's pid, from its socket)
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
    def test_the_status_says_what_each_build_holds_and_lacks(self):
        self.machine.mem_available = 50 * GIB  # 40G above the 10G floor: two 20G builds
        a, b, c = (self.submit() for _ in range(3))
        a.mem_now = 5 * GIB
        self.daemon.schedule(self.now + 1)
        ov = self.daemon.overview(self.now + 1)
        rows = {r["id"]: r for r in ov["running"] + ov["queued"]}
        self.assertEqual((rows[a.id]["growth"], rows[b.id]["growth"]), (15 * GIB, 20 * GIB))
        self.assertFalse(rows[a.id]["released"])
        self.assertIsNone(rows[c.id]["held_for"])  # it waits for memory itself, not behind a build in line
        d = self.submit()
        self.assertEqual(d.row(self.now)["growth"], 0)  # (queued)
        self.machine.mem_available = 200 * GIB
        self.daemon.schedule(self.now + 2)
        self.assertEqual(d.state, "running")
        self.assertEqual(d.row(self.now)["growth"], d.est_mem)  # all of its plan from the start, not a tick later
        self.machine.mem_available = 60 * GIB  # less than running builds may grow into, more than "never fits"
        e = self.submit()
        self.daemon.schedule(self.now + 3)
        self.assertEqual((e.state, e.reason_cat), ("queued", "memory"))
        self.assertGreater(e.short_by, 0)
        self.daemon.pause("maintenance", self.now + 4)
        self.daemon.schedule(self.now + 4)
        self.assertEqual((e.reason_cat, e.short_by, e.held_for), ("paused", 0.0, None))  # nothing stale while paused

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
        self.assertFalse(self.daemon.memory_moving())  # 100G available: PSI alone doesn't mean memory is scarce
        first = self.submit()
        self.assertEqual(first.state, "running")
        self.finish(first)
        self.machine.mem_available = 25 * GIB  # under a quarter of 128G: memory is scarce
        self.assertTrue(self.daemon.memory_moving())
        second = self.submit(lane="web")  # 4G lane: fits in the 15G above the 10G floor once PSI clears
        self.assertEqual(second.reason_cat, "memory-pressure")
        self.machine.psi_mem = 0.0
        self.daemon.schedule(self.now)
        self.assertEqual(second.state, "running")
        self.machine.psi_cpu = 80.0
        third = self.submit(lane="web")
        self.assertEqual(third.reason_cat, "cpu-pressure")

    def test_too_big_for_the_machine(self):
        t = self.submit(mem="125G")
        self.assertEqual((t.state, t.reason_cat), ("queued", "too-big"))

    def test_disk_floor(self):
        self.daemon.cfg.disk_floor = 1 << 60
        self.assertEqual(self.submit().reason_cat, "disk")

    def test_disk_floor_as_a_share_of_each_filesystem(self):
        self.daemon.cfg.disk_floor, self.daemon.cfg.disk_floor_share = 0, 0.2
        with mock.patch.object(buildqd, "disk_space", return_value=(300 * GIB, 1900 * GIB)):
            t = self.submit()
        self.assertEqual(t.reason_cat, "disk")
        self.assertIn("20% of its 1900G, 380G", t.reason)
        with mock.patch.object(buildqd, "disk_space", return_value=(400 * GIB, 1900 * GIB)):
            self.assertNotEqual(self.submit().reason_cat, "disk")
        cfg = buildqd.Config(buildqd.tomllib.loads('disk_floor = "20%"'), 128 * GIB, 32)
        self.assertEqual((cfg.disk_floor, cfg.disk_floor_share, cfg.summary()["disk_floor"]), (0, 0.2, "20%"))
        for bad in ('"100%"', '"x%"', '"-5%"'):
            with self.assertRaises(ValueError, msg=bad):
                buildqd.Config(buildqd.tomllib.loads(f"disk_floor = {bad}"), 128 * GIB, 32)

    def test_the_state_directory_and_container_storage_count_too(self):
        self.daemon.cfg.disk_floor, self.daemon.cfg.disk_floor_share = 10 * GIB, 0.0
        storage = os.path.join(self.tmp.name, "storage")
        os.makedirs(storage)
        self.daemon.cfg.container_storage = [storage, os.path.join(self.tmp.name, "not-there")]
        work = self.workdir()
        roomy, short = (500 * GIB, 1900 * GIB), (GIB, 1900 * GIB)
        # each path on a filesystem of its own here, as the stand-in says
        with mock.patch.object(buildqd, "disk_space",
                               side_effect=lambda path: short if path == self.state else roomy), \
                mock.patch.object(buildqd, "fs_device", side_effect=hash):
            t = self.submit(cwd=work)
        self.assertEqual(t.reason_cat, "disk")
        self.assertIn(f"free at {self.state}", t.reason)  # its log would go there
        with mock.patch.object(buildqd, "disk_space",
                               side_effect=lambda path: short if path == storage else roomy), \
                mock.patch.object(buildqd, "fs_device", side_effect=hash):
            image = self.submit(("docker", "build", "."), cwd=self.workdir())
            cargo = self.submit(("cargo", "build"), cwd=self.workdir())
        self.assertEqual(image.reason_cat, "disk")
        self.assertIn(f"free at {storage}", image.reason)
        self.assertNotEqual(cargo.reason_cat, "disk")  # only an image build writes there
        cfg = buildqd.Config({"container_storage": ["~/x"]}, 64 * GIB, 16)
        self.assertEqual(cfg.container_storage, [os.path.expanduser("~/x")])
        for bad in ("/x", [""], [3]):
            with self.assertRaises(ValueError, msg=bad):
                buildqd.Config({"container_storage": bad}, 64 * GIB, 16)

    def test_each_filesystem_is_held_to_its_own_share(self):
        self.daemon.cfg.disk_floor, self.daemon.cfg.disk_floor_share = 0, 0.125
        spaces = {"/big": (400 * GIB, 1900 * GIB), "/small": (GIB, 16 * GIB), "/gone": None}
        with mock.patch.object(buildqd, "disk_space", side_effect=lambda path: spaces.get(path, (GIB, 16 * GIB))):
            t = self.submit()  # (held at submission too, by its working directory)
        t.disk_paths = ["/big", "/gone", "/small"]
        plenty = (900 * GIB, 1900 * GIB)  # buildq's state directory
        with mock.patch.object(buildqd, "disk_space", side_effect=lambda path: spaces.get(path, plenty)):
            self.daemon.schedule(self.now)
        self.assertEqual(t.reason_cat, "disk")
        self.assertIn("1.0G of disk is free", t.reason)
        self.assertIn("12.5% of its 16G, 2.0G", t.reason)  # the small one, by its own size
        spaces["/small"] = (4 * GIB, 16 * GIB)
        with mock.patch.object(buildqd, "disk_space", side_effect=lambda path: spaces.get(path, plenty)):
            self.daemon.schedule(self.now + 1)
        self.assertNotEqual(t.reason_cat, "disk")
        self.daemon.cfg.disk_floor, self.daemon.cfg.disk_floor_share = 500 * GIB, 0.0
        with mock.patch.object(buildqd, "disk_space", side_effect=lambda path: spaces.get(path, (GIB, 16 * GIB))):
            u = self.submit()
        u.disk_paths = ["/big"]
        with mock.patch.object(buildqd, "disk_space", side_effect=lambda path: spaces.get(path, plenty)):
            self.daemon.schedule(self.now + 2)
        self.assertIn("(disk_floor is 500G)", u.reason)

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

    CHECK_LANE = 'priority = 40\nworkspace_group = "heavy"\ncores = 2\nmem = "4G"'

    def with_check_lane(self, extra=""):
        config = BASE_CONFIG + "[lanes.check]\n" + self.CHECK_LANE + "\n" + extra
        with open(self.config_path, "w") as f:
            f.write(config)
        self.daemon.cfg = self.daemon.load_config()
        self.assertIn("check", self.daemon.cfg.lanes)

    def test_lanes_in_one_workspace_group_run_one_build_per_workspace(self):
        self.with_check_lane()
        tree = self.workdir("tree")
        test = self.submit(("cargo", "test"), cwd=tree)
        check = self.submit(("cargo", "check"), lane="check", cwd=tree)  # the same target folder
        self.assertEqual(self.states(test, check), ["running", "queued"])
        self.assertEqual(check.reason_cat, "workspace")
        self.assertIn(f"heavy lane, {test.id[:8]}", check.reason)  # it says who holds it
        elsewhere = self.submit(("cargo", "check"), lane="check", cwd=self.workdir("other"))
        self.assertEqual(elsewhere.state, "running")  # another workspace: no wait
        self.finish(test)
        self.daemon.schedule(self.now + 31)
        self.assertEqual(check.state, "running")
        again = self.submit(("cargo", "test"), cwd=tree)  # and the other way round
        self.assertEqual((again.state, again.reason_cat), ("queued", "workspace"))

    def test_two_in_one_group_queued_in_the_same_pass_start_one_at_a_time(self):
        self.with_check_lane()
        self.daemon.cfg.max_jobs = 0  # hold both in the queue
        tree = self.workdir("tree")
        test = self.submit(("cargo", "test"), cwd=tree)
        check = self.submit(("cargo", "check"), lane="check", cwd=tree)
        self.daemon.cfg.max_jobs = 8
        self.daemon.schedule(self.now + 1)
        self.assertEqual(sorted(self.states(test, check)), ["queued", "running"])
        self.assertEqual(check.state, "running")  # (the check lane comes first)

    def test_a_build_from_before_groups_were_recorded_gets_its_group_when_picked_up(self):
        self.with_check_lane()
        tree = self.workdir("tree")
        check = self.submit(("cargo", "check"), lane="check", cwd=tree)
        check.workspace_group = check.group_lane = None  # saved by an older daemon
        self.daemon.save(check)
        again = buildqd.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                                     queries=buildqd.InlineExecutor())
        self.addCleanup(lambda: (again.stop_memory_watch(), [again.close_jobserver(x) for x in again.tickets.values()]))
        again.load_tickets(self.now + 10)
        self.assertEqual(again.tickets[check.id].workspace_group, "heavy")
        with open(os.path.join(check.dir, "ticket.json")) as f:
            self.assertEqual(json.load(f)["workspace_group"], "heavy")  # saved at once, not only in memory

    def restart(self, config):
        with open(self.config_path, "w") as f:
            f.write(config)
        again = buildqd.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                                     queries=buildqd.InlineExecutor())
        self.addCleanup(lambda: (again.stop_memory_watch(), [again.close_jobserver(x) for x in again.tickets.values()]))
        again.load_tickets(self.now + 10)
        return again

    def test_across_a_restart_without_its_lane_a_build_holds_only_the_group_it_started_with(self):
        self.with_check_lane()
        tree = self.workdir("tree")
        check = self.submit(("cargo", "check"), lane="check", cwd=tree)
        lone = BASE_CONFIG.replace('default_lane = "heavy"', 'default_lane = "lone"') + '[lanes.lone]\ncores = 2\nmem = "1G"\n'
        again = self.restart(lone)  # the check lane is gone; the default lane is an exclusive one of its own
        self.assertEqual((again.tickets[check.id].lane, again.tickets[check.id].workspace_group), ("lone", "heavy"))
        other, error = again.submit({"argv": ["make"], "cwd": tree, "lane": "lone", "env": {}}, self.now + 11)
        self.assertEqual(other.state, "running")  # not the group it started with
        test, error = again.submit({"argv": ["cargo", "test"], "cwd": tree, "lane": "heavy", "env": {}}, self.now + 12)
        self.assertEqual((test.state, test.reason_cat), ("queued", "workspace"))

    def test_a_build_from_before_groups_whose_lane_is_gone_holds_its_lanes_name(self):
        self.with_check_lane()
        tree = self.workdir("tree")
        check = self.submit(("cargo", "check"), lane="check", cwd=tree)
        check.workspace_group = check.group_lane = None  # saved by an older daemon
        self.daemon.save(check)
        again = self.restart(BASE_CONFIG)  # and the upgrade dropped its lane
        self.assertEqual(again.tickets[check.id].workspace_group, "check")  # what the old daemon held
        twice = self.restart(BASE_CONFIG)  # a second restart doesn't take it for the default lane's
        self.assertEqual((twice.tickets[check.id].workspace_group, twice.tickets[check.id].group_lane), ("check", "check"))

    def test_a_queued_build_moved_to_another_lane_holds_that_lanes_group_once_it_starts(self):
        self.with_check_lane()
        tree = self.workdir("tree")
        self.daemon.cfg.max_jobs = 0
        queued = self.submit(("make",), lane="check", cwd=tree)
        self.daemon.save(queued)
        regrouped = (BASE_CONFIG.replace("[lanes.heavy]\n", '[lanes.heavy]\nworkspace_group = "cargo"\n')
                     + '[lanes.cheq]\npriority = 40\nworkspace_group = "cargo"\ncores = 2\nmem = "4G"\n')
        again = self.restart(regrouped)  # the check lane is gone: the queued build goes to heavy and starts
        started = again.tickets[queued.id]
        again.schedule(self.now + 20)
        self.assertEqual((started.state, started.group_lane), ("running", "heavy"))
        check, error = again.submit({"argv": ["cargo", "check"], "cwd": tree, "lane": "cheq", "env": {}}, self.now + 21)
        self.assertEqual((check.state, check.reason_cat), ("queued", "workspace"))

    def test_a_build_queued_in_a_lane_a_reload_removed_records_the_lane_it_starts_under(self):
        self.with_check_lane()
        tree = self.workdir("tree")
        self.daemon.cfg.max_jobs = 0
        queued = self.submit(("make",), lane="check", cwd=tree)
        regrouped = BASE_CONFIG.replace("[lanes.heavy]\n", '[lanes.heavy]\nworkspace_group = "cargo"\n') + (
            '[lanes.cheq]\npriority = 40\nworkspace_group = "cargo"\ncores = 2\nmem = "4G"\n')
        with open(self.config_path, "w") as f:
            f.write(BASE_CONFIG)
        self.daemon.cfg = self.daemon.load_config()  # the check lane is gone; it starts under heavy
        self.daemon.schedule(self.now + 1)
        self.assertEqual((queued.state, queued.group_lane, queued.workspace_group), ("running", "heavy", "heavy"))
        with open(self.config_path, "w") as f:
            f.write(regrouped)
        self.daemon.cfg = self.daemon.load_config()  # heavy joins "cargo" while it runs
        check = self.submit(("cargo", "check"), lane="cheq", cwd=tree)
        self.assertEqual((check.state, check.reason_cat), ("queued", "workspace"))
        self.assertIn("heavy lane", check.reason)

    def test_a_running_build_keeps_its_workspace_when_its_lane_is_removed(self):
        self.with_check_lane()
        tree = self.workdir("tree")
        check = self.submit(("cargo", "check"), lane="check", cwd=tree)
        self.assertEqual(check.workspace_group, "heavy")
        with open(self.config_path, "w") as f:
            f.write(BASE_CONFIG.replace('default_lane = "heavy"', 'default_lane = "web"'))
        self.daemon.cfg = self.daemon.load_config()  # the check lane is gone; the default lane is web
        test = self.submit(("cargo", "test"), lane="heavy", cwd=tree)
        self.assertEqual((test.state, test.reason_cat), ("queued", "workspace"))

    def test_workspace_groups_are_checked(self):
        def config(extra, base=BASE_CONFIG):
            return buildqd.Config(buildqd.tomllib.loads(base + extra), 128 * GIB, 32)
        cfg = config("[lanes.check]\n" + self.CHECK_LANE + "\n")
        self.assertEqual((cfg.lanes["check"].workspace_group, cfg.summary()["lanes"]["check"]["workspace_group"]),
                         ("heavy", "heavy"))
        for bad, said in (('workspace_group = "carg"', "shared with no other lane"),
                          ('workspace_group = ""', "must be a name"),
                          ('workspace_group = "web"', "must be exclusive")):
            with self.assertRaisesRegex(ValueError, said, msg=bad):
                config(f"[lanes.check]\ncores = 2\n{bad}\n")
        grouped = BASE_CONFIG.replace("[lanes.heavy]\n", '[lanes.heavy]\nworkspace_group = "cargo"\n')
        with self.assertRaisesRegex(ValueError, "lane heavy's group is 'cargo'"):
            config('[lanes.check]\ncores = 2\nworkspace_group = "heavy"\n', base=grouped)
        with self.assertRaisesRegex(ValueError, "lane heavy's group is 'cargo'"):  # even with a third lane beside it
            config('[lanes.check]\ncores = 2\nworkspace_group = "heavy"\n'
                   '[lanes.test]\ncores = 2\nworkspace_group = "heavy"\n', base=grouped)
        self.assertEqual(config('[lanes.check]\ncores = 2\nworkspace_group = "check"\n').lanes["check"].workspace_group,
                         "check")  # naming itself: the default, said out loud

    def test_a_build_held_for_waiting_longest_holds_its_workspace(self):
        self.with_check_lane()
        self.machine.mem_available = 35 * GIB  # 25G above the floor, of which a running build may grow into 20G
        self.submit(cwd=self.workdir("elsewhere"))
        tree = self.workdir("tree")
        test = self.submit(("cargo", "test"), cwd=tree)  # 20G: fits once that build ends, not before
        self.now += 400  # past starve_seconds: it is held
        self.daemon.schedule(self.now)
        self.assertTrue(test.held)
        check = self.submit(("cargo", "check"), lane="check", cwd=tree, mem="2G")  # (memory aside from the hold)
        self.daemon.schedule(self.now + 1)
        self.assertEqual((check.state, check.reason_cat), ("queued", "workspace"))
        self.assertIn("waited longest", check.reason)
        self.assertIn("heavy lane", check.reason)  # the lane it is handled under now
        self.daemon.cfg.lanes["check"].may_pass_held = True  # even a lane that may pass held room
        self.daemon.schedule(self.now + 2)
        self.assertEqual(check.reason_cat, "workspace")

    def test_a_regroup_while_builds_run_keeps_the_running_ones_apart(self):
        self.with_check_lane()
        tree = self.workdir("tree")
        check = self.submit(("cargo", "check"), lane="check", cwd=tree)
        regrouped = (BASE_CONFIG.replace("[lanes.heavy]\n", '[lanes.heavy]\nworkspace_group = "cargo"\n')
                     + "[lanes.check]\n" + self.CHECK_LANE.replace('"heavy"', '"cargo"') + "\n")
        with open(self.config_path, "w") as f:
            f.write(regrouped)
        self.daemon.cfg = self.daemon.load_config()
        test = self.submit(("cargo", "test"), cwd=tree)
        self.assertEqual((test.state, test.reason_cat), ("queued", "workspace"))
        self.finish(check)
        self.daemon.schedule(self.now + 31)
        self.assertEqual(test.state, "running")

    def test_a_non_exclusive_lane_blocks_nothing(self):
        tree = self.workdir("tree")
        web = self.submit(("pnpm", "build"), lane="web", cwd=tree)
        self.assertEqual(web.workspace_group, "")
        test = self.submit(("cargo", "test"), cwd=tree)
        self.assertEqual(self.states(web, test), ["running", "running"])

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


buildq = load("buildq")


class SourceRecords(unittest.TestCase):
    """The client's fingerprint, on real repositories."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.repo)
        self.git("init", "-q")
        with open(os.path.join(self.repo, "a.txt"), "w") as f:
            f.write("one\n")
        self.git("add", "a.txt")
        self.git("commit", "-qm", "one")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args, where=None):
        subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "init.defaultBranch=main",
                        "-C", where or self.repo, *args], check=True, capture_output=True)

    def test_a_record_changes_with_the_tree_and_only_with_it(self):
        clean = buildq.source_of(self.repo)
        self.assertEqual((clean["dirty"], buildq.source_of(self.repo)), (False, clean))
        with open(os.path.join(self.repo, "new.txt"), "w") as f:
            f.write("x")
        dirty = buildq.source_of(self.repo)
        self.assertTrue(dirty["dirty"])
        self.assertNotEqual(dirty["fingerprint"], clean["fingerprint"])
        os.chmod(os.path.join(self.repo, "new.txt"), 0o755)  # same size and time, another mode
        self.assertNotEqual(buildq.source_of(self.repo)["fingerprint"], dirty["fingerprint"])

    def test_no_record_where_git_would_run_a_filter_or_hide_changes(self):
        with open(os.path.join(self.repo, ".gitattributes"), "w") as f:
            f.write("*.bin filter=lfs\n")  # untracked still: git applies it all the same
        self.assertIsNone(buildq.source_of(self.repo))
        os.remove(os.path.join(self.repo, ".gitattributes"))
        self.git("update-index", "--skip-worktree", "a.txt")
        self.assertIsNone(buildq.source_of(self.repo))
        self.git("update-index", "--no-skip-worktree", "a.txt")
        self.assertIsNotNone(buildq.source_of(self.repo))

    def test_no_record_with_a_repository_inside(self):
        nested = os.path.join(self.repo, "nested")
        os.makedirs(nested)
        self.git("init", "-q", where=nested)
        with open(os.path.join(nested, "b.txt"), "w") as f:
            f.write("b")
        self.git("add", "b.txt", where=nested)
        self.git("commit", "-qm", "b", where=nested)
        self.assertIsNone(buildq.source_of(self.repo))
        with open(os.path.join(self.repo, ".git", "info", "exclude"), "a") as f:
            f.write("nested/\n")  # ignored, it is no longer part of the tree's source
        self.assertIsNotNone(buildq.source_of(self.repo))

    def test_a_deleted_tracked_attributes_file_means_no_record(self):
        with open(os.path.join(self.repo, ".gitattributes"), "w") as f:
            f.write("*.txt text\n")
        self.git("add", ".gitattributes")
        self.git("commit", "-qm", "attributes")
        self.assertIsNotNone(buildq.source_of(self.repo))
        os.remove(os.path.join(self.repo, ".gitattributes"))  # git falls back to the index's copy
        self.assertIsNone(buildq.source_of(self.repo))

    def test_a_symlinked_attributes_file_means_no_record(self):
        with open(os.path.join(self.repo, ".gitattributes"), "w") as f:
            f.write("*.txt text\n")
        self.git("add", ".gitattributes")
        self.git("commit", "-qm", "attributes")
        os.remove(os.path.join(self.repo, ".gitattributes"))
        with open(os.path.join(self.tmp.name, "harmless"), "w") as f:
            f.write("")
        os.symlink(os.path.join(self.tmp.name, "harmless"), os.path.join(self.repo, ".gitattributes"))
        self.assertIsNone(buildq.source_of(self.repo))  # git reads the index's copy, not the link

    def test_a_relative_attributes_file_is_read_from_the_top(self):
        os.makedirs(os.path.join(self.repo, "src"))
        with open(os.path.join(self.repo, "src", "x.txt"), "w") as f:
            f.write("x")
        self.git("add", "src/x.txt")
        self.git("commit", "-qm", "src")
        with open(os.path.join(self.repo, ".git", "local-attributes"), "w") as f:
            f.write("*.txt filter=clean-it\n")
        self.git("config", "core.attributesFile", ".git/local-attributes")
        self.assertIsNone(buildq.source_of(os.path.join(self.repo, "src")))

    def test_a_submodule_means_no_record(self):
        other = os.path.join(self.tmp.name, "other")
        os.makedirs(other)
        self.git("init", "-q", where=other)
        with open(os.path.join(other, "b.txt"), "w") as f:
            f.write("b")
        self.git("add", "b.txt", where=other)
        self.git("commit", "-qm", "b", where=other)
        self.git("-c", "protocol.file.allow=always", "submodule", "add", "-q", other, "sub")
        self.git("commit", "-qm", "sub")
        self.assertIsNone(buildq.source_of(self.repo))

    def test_an_ignored_attributes_file_still_counts(self):
        with open(os.path.join(self.repo, ".git", "info", "exclude"), "a") as f:
            f.write(".gitattributes\n")
        with open(os.path.join(self.repo, ".gitattributes"), "w") as f:
            f.write("*.txt filter=clean-it\n")
        self.assertIsNone(buildq.source_of(self.repo))

    def test_an_attributes_file_that_is_a_pipe_doesnt_hang_it(self):
        os.mkfifo(os.path.join(self.repo, ".gitattributes"))
        started = time.monotonic()
        self.assertIsNone(buildq.source_of(self.repo))  # not knowing what it says means no record
        self.assertLess(time.monotonic() - started, 4)

    def test_a_pipe_doesnt_hang_it(self):
        os.mkfifo(os.path.join(self.repo, "pipe"))
        started = time.monotonic()
        self.assertIsNotNone(buildq.source_of(self.repo))
        self.assertLess(time.monotonic() - started, 4)


class Pausing(DaemonCase):
    def test_a_paused_queue_starts_nothing_and_stays_paused_across_a_restart(self):
        running = self.submit(cores=2)
        self.daemon.pause("rebooting at noon", self.now)
        waiting = self.submit(cores=1)
        self.assertEqual((running.state, waiting.state, waiting.reason_cat), ("running", "queued", "paused"))
        self.assertIn("rebooting at noon", waiting.reason)
        again = buildqd.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                                     queries=buildqd.InlineExecutor())
        self.assertEqual(again.paused["reason"], "rebooting at noon")
        self.daemon.resume(self.now + 1)
        self.assertEqual(waiting.state, "running")
        self.assertFalse(os.path.exists(os.path.join(self.state, "paused.json")))


class FairShares(DaemonCase):
    def test_a_caller_with_fewer_builds_running_goes_first(self):
        self.daemon.cfg.max_jobs = 2
        busy = [self.submit(cores=1, caller="agent-a.scope") for _ in range(2)]
        self.assertEqual([t.state for t in busy], ["running", "running"])
        quick = self.submit(cores=1, caller="agent-a.scope")
        slow = self.submit(cores=1, caller="agent-b.scope")
        self.daemon.history.add({"key": quick.key, "t": self.now, "peak_mem": GIB, "peak_cores": 1.0,
                                 "wall": 10, "exit": 0})
        self.daemon.schedule(self.now)
        self.assertEqual(quick.expected, 10)  # quicker, but its caller already has two running
        self.assertEqual([t.id for t in self.daemon.queue()], [slow.id, quick.id])


class FairSharesInterleave(DaemonCase):
    def test_callers_take_turns_within_one_pass(self):
        self.daemon.cfg.max_jobs = 0 + 1
        blocker = self.submit(cores=1, caller="z.scope")
        burst = [self.submit(cores=1, caller="a.scope") for _ in range(3)]
        late = self.submit(cores=1, caller="b.scope")
        order = [t.caller for t in self.daemon.queue(self.now)]
        self.assertEqual(order[:2], ["a.scope", "b.scope"])  # b doesn't wait behind all of a's burst
        del blocker, burst, late


class SameSource(DaemonCase):
    def test_the_same_build_on_the_same_source_joins_the_one_running(self):
        first = self.submit(("cargo", "test"), source=SOURCE)
        cwd = first.cwd
        second = self.submit(("cargo", "test"), cwd=cwd, source=SOURCE, notify="echo joined")
        self.assertIs(second, first)
        self.assertEqual((self.daemon.submit_outcome, first.joiners, first.extra_notify),
                         ({"joined": True}, 1, ["echo joined"]))
        other_env = self.submit(("cargo", "test"), cwd=cwd, source=SOURCE, env={"DATABASE_URL": "elsewhere"})
        other_tree = self.submit(("cargo", "test"), cwd=cwd, source={**SOURCE, "fingerprint": "c" * 32})
        other_timeout = self.submit(("cargo", "test"), cwd=cwd, source=SOURCE, timeout=10)
        forced = self.submit(("cargo", "test"), cwd=cwd, source=SOURCE, again=True)
        self.assertEqual(len({first.id, other_env.id, other_tree.id, other_timeout.id, forced.id}), 5)
        unknown = self.submit(("cargo", "test"), cwd=cwd)  # no source recorded: nothing to compare
        self.assertNotEqual(unknown.id, first.id)

    def follow(self, t, submission):
        """A `buildq run` following the build, for the submission it made or joined."""
        class Following:
            def send(self, message):
                return True
        conn = Following()
        conn.submission = submission
        self.daemon.watchers[t.id].add(conn)

    def test_a_shared_build_stops_only_when_nobody_waits_for_it(self):
        run = self.submit(("cargo", "test"), source=SOURCE, attach=True, submission="first")
        self.submit(("cargo", "test"), cwd=run.cwd, source=SOURCE, attach=True, submission="second")
        self.follow(run, "first")
        self.follow(run, "second")

        class Conn:
            sent = []

            def send(self, message):
                self.sent.append(message)
                return True
        conn = Conn()
        self.daemon.handle_message({"op": "cancel", "ticket": run.id}, conn, self.now)
        self.assertIn("is shared", conn.sent[-1]["error"])  # a plain cancel doesn't stop it for everyone
        self.daemon.handle_message({"op": "cancel", "ticket": run.id, "submission": "second"}, conn, self.now)
        self.assertTrue(conn.sent[-1]["kept"])
        self.daemon.handle_message({"op": "cancel", "ticket": run.id, "submission": "second"}, conn, self.now)
        self.assertTrue(conn.sent[-1]["kept"])  # the same one again: it waits no more, but first still does
        self.daemon.handle_message({"op": "cancel", "ticket": run.id, "submission": "stranger"}, conn, self.now)
        self.assertTrue(conn.sent[-1]["kept"])  # not one of its submissions: nothing happens
        self.assertFalse(run.cancel_requested)
        self.daemon.handle_message({"op": "cancel", "ticket": run.id, "submission": "first"}, conn, self.now)
        self.assertTrue(run.cancel_requested)  # the last one went

    def test_a_run_killed_outright_doesnt_make_a_build_look_shared(self):
        owner = self.submit(("cargo", "test"), source=SOURCE, submission="owner")
        self.submit(("cargo", "test"), cwd=owner.cwd, source=SOURCE, attach=True, submission="gone")  # never follows

        class Conn:
            sent = []

            def send(self, message):
                self.sent.append(message)
                return True
        self.daemon.started_at = self.now - 1000
        self.daemon.handle_message({"op": "cancel", "ticket": owner.id}, Conn(), self.now)
        self.assertFalse(owner.cancel_requested)  # within unwatched_seconds the run may be reconnecting
        self.daemon.handle_message({"op": "cancel", "ticket": owner.id}, Conn(), self.now + 100)
        self.assertTrue(owner.cancel_requested)

    def test_a_run_killed_outright_stops_waiting_after_a_while(self):
        owner = self.submit(("cargo", "test"), source=SOURCE, submission="owner")
        self.submit(("cargo", "test"), cwd=owner.cwd, source=SOURCE, attach=True, submission="killed")
        self.follow(owner, "owner-follower-wait")  # someone else (a `buildq wait`) follows the ticket
        self.daemon.started_at = self.now - 1000
        self.daemon.drop_unwatched(self.now + 100)  # the killed run never followed again
        self.assertEqual(list(owner.interests), ["owner"])

    def test_a_new_joiners_grace_holds_off_the_unwatched_cancel(self):
        run = self.submit(("cargo", "test"), source=SOURCE, attach=True, submission="first")
        run.last_watched = self.now  # its client last followed now, and went
        self.daemon.submit({"argv": ["cargo", "test"], "cwd": run.cwd, "env": {}, "submission": "later",
                            "source": SOURCE, "attach": True}, self.now + 29)  # joins, then its connection drops
        self.daemon.started_at = self.now - 1000
        self.daemon.drop_unwatched(self.now + 31)
        self.assertFalse(run.cancel_requested)  # the joiner's 30 s aren't up
        self.daemon.drop_unwatched(self.now + 61)
        self.assertTrue(run.cancel_requested)

    def test_a_retry_after_a_tool_timeout_keeps_the_place_in_the_queue(self):
        self.machine.mem_available = 15 * GIB  # nothing fits: everything waits
        work = self.workdir()
        first = self.submit(("cargo", "test"), cwd=work, attach=True, submission="first", caller="agent")
        self.now += 10
        later = self.submit(("cargo", "build"), cwd=self.workdir(), submission="later", caller="other")
        self.daemon.drop_unwatched(self.now + 100)  # the run's client was killed: cancelled, 110 s in
        self.assertTrue(first.cancel_requested)
        self.now += 101  # the harness runs it again at once
        retry = self.submit(("cargo", "test"), cwd=work, attach=True, submission="retry", caller="agent")
        self.assertEqual(retry.submitted, self.now - 110)  # as long a wait behind it, not the back
        self.assertEqual([t.id for t in self.daemon.queue(self.now)][:2], [retry.id, later.id])
        again = self.submit(("cargo", "test"), cwd=work, attach=True, submission="again", caller="agent")
        self.assertEqual(again.submitted, self.now)  # a place is taken once

    def test_only_the_same_caller_takes_a_place(self):
        self.machine.mem_available = 15 * GIB
        work = self.workdir()
        self.submit(("cargo", "test"), cwd=work, attach=True, submission="first", caller="agent")
        self.daemon.drop_unwatched(self.now + 100)
        self.now += 101
        stranger = self.submit(("cargo", "test"), cwd=work, attach=True, submission="s", caller="someone-else")
        self.assertEqual(stranger.submitted, self.now)

    def test_a_build_queued_again_after_a_memory_stop_leaves_no_place(self):
        self.machine.mem_available = 15 * GIB
        t = self.submit(("cargo", "test"), cwd=self.workdir(), attach=True, submission="first", caller="agent")
        t.requeues = 1  # it ran, was stopped to free memory, and waits again
        self.daemon.drop_unwatched(self.now + 100)
        self.assertEqual(self.daemon.vacated, {})

    def test_a_build_with_no_caller_leaves_no_place(self):
        self.machine.mem_available = 15 * GIB
        self.submit(("cargo", "test"), cwd=self.workdir(), attach=True, submission="first")  # no caller
        self.daemon.drop_unwatched(self.now + 100)
        self.assertEqual(self.daemon.vacated, {})

    def test_a_build_that_was_running_leaves_no_place(self):
        work = self.workdir()
        ran = self.submit(("cargo", "test"), cwd=work, attach=True, submission="first", caller="agent")
        self.assertEqual(ran.state, "running")
        self.daemon.drop_unwatched(self.now + 100)
        self.assertTrue(ran.cancel_requested)
        self.assertEqual(self.daemon.vacated, {})  # it had its turn

    def test_a_client_stopped_by_sigterm_leaves_its_place(self):
        self.machine.mem_available = 15 * GIB
        work = self.workdir()
        first = self.submit(("cargo", "test"), cwd=work, attach=True, submission="first", caller="agent")

        class Conn:
            sent = []

            def send(self, msg):
                self.sent.append(msg)
        self.daemon.handle_message({"op": "cancel", "ticket": first.id, "submission": "first", "vacate": True},
                                   Conn(), self.now + 50)
        self.assertTrue(first.cancel_requested)
        self.now += 51
        retry = self.submit(("cargo", "test"), cwd=work, attach=True, submission="retry", caller="agent")
        self.assertEqual(retry.submitted, self.now - 50)
        plain = self.submit(("cargo", "test", "-p", "x"), cwd=work, attach=True, submission="p", caller="agent")
        self.daemon.handle_message({"op": "cancel", "ticket": plain.id, "submission": "p"}, Conn(), self.now + 5)
        self.assertEqual(self.daemon.vacated, {})  # Ctrl-C: a cancel meant as one leaves nothing

    def test_the_longest_wait_is_kept_and_a_place_expires(self):
        self.machine.mem_available = 15 * GIB
        work = self.workdir()
        old = self.submit(("cargo", "test"), cwd=work, attach=True, submission="old", caller="agent", again=True)
        self.now += 500
        new = self.submit(("cargo", "test"), cwd=work, attach=True, submission="new", caller="agent", again=True)
        self.daemon.drop_unwatched(self.now + 100)  # both go together
        self.assertEqual(self.daemon.vacated[(work, ("cargo", "test"), new.lane, "agent")][0], 600)
        self.daemon.drop_unwatched(self.now + 100 + buildqd.Daemon.VACATED_SECONDS + 1)
        self.assertEqual(self.daemon.vacated, {})
        self.assertTrue(old.cancel_requested)

    def test_a_ticket_from_before_this_version_doesnt_break_the_tick(self):
        t = self.submit(("cargo", "test"), attach=True, submission="old")
        t.followed = {}  # saved by an earlier version: no follow history
        t.last_watched = self.now
        self.daemon.drop_unwatched(self.now + 5)  # no error
        self.daemon.drop_unwatched(self.now + 100)
        self.assertTrue(t.cancel_requested)  # nobody followed it: cancelled, as before

    def test_a_run_following_again_is_kept_waiting_across_a_restart(self):
        owner = self.submit(("cargo", "test"), source=SOURCE, submission="owner")
        self.submit(("cargo", "test"), cwd=owner.cwd, source=SOURCE, attach=True, submission="joiner")
        del owner.interests["joiner"]  # given up for gone

        class Conn:
            submission = None

            def send(self, message):
                return True
        self.daemon.handle_message({"op": "watch", "ticket": owner.id, "submission": "joiner"}, Conn(), self.now)
        again = buildqd.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                                     queries=buildqd.InlineExecutor())
        again.load_tickets(self.now + 1)
        self.assertIn("joiner", again.tickets[owner.id].interests)
        again.stop_memory_watch()
        for x in again.tickets.values():
            again.close_jobserver(x)

    def test_a_resent_joined_submission_is_told_it_joined(self):
        first = self.submit(("cargo", "test"), source=SOURCE, submission="a")
        self.submit(("cargo", "test"), cwd=first.cwd, source=SOURCE, submission="b")
        self.assertEqual(self.daemon.submit_outcome, {"joined": True})
        self.daemon.submit({"argv": ["cargo", "test"], "cwd": first.cwd, "env": {}, "submission": "b",
                            "source": SOURCE}, self.now)
        self.assertEqual(self.daemon.submit_outcome, {"joined": True})

    def test_the_run_that_made_a_build_others_joined_doesnt_cancel_it(self):
        run = self.submit(("cargo", "test"), source=SOURCE, attach=True, submission="owner-1")
        self.submit(("cargo", "test"), cwd=run.cwd, source=SOURCE, attach=True, submission="joiner")
        self.follow(run, "owner-1")
        self.follow(run, "joiner")

        class Conn:
            sent = []

            def send(self, message):
                self.sent.append(message)
                return True
        conn = Conn()
        self.daemon.handle_message({"op": "cancel", "ticket": run.id, "submission": "owner-1"}, conn, self.now)
        self.assertTrue(conn.sent[-1].get("kept"))
        self.assertEqual((run.state, run.cancel_requested), ("running", False))
        self.daemon.handle_message({"op": "cancel", "ticket": run.id, "all": True}, conn, self.now)
        self.assertTrue(run.cancel_requested)

    def test_a_dropped_off_submission_that_joins_a_run_keeps_it_alive(self):
        run = self.submit(("cargo", "test"), source=SOURCE, attach=True)
        self.assertTrue(run.attached)
        self.submit(("cargo", "test"), cwd=run.cwd, source=SOURCE)
        self.assertFalse(run.attached)  # nobody's client owns it now: it isn't cancelled when the run goes

    def test_a_resent_submission_that_joined_gets_the_same_ticket(self):
        first = self.submit(("sh", "push.sh"), source=SOURCE)
        joined = self.submit(("sh", "push.sh"), cwd=first.cwd, source=SOURCE, submission="resend-me")
        self.assertIs(joined, first)
        self.finish(first, 0)
        self.settle()
        again = self.submit(("sh", "push.sh"), cwd=first.cwd, source=SOURCE, submission="resend-me")
        self.assertIs(again, first)  # not a second push

    def test_a_passed_check_answers_the_same_one_again(self):
        first = self.submit(("cargo", "test"), source=SOURCE)
        self.finish(first, 0)
        self.settle()
        self.assertTrue(first.source_verified)
        again = self.submit(("cargo", "test"), cwd=first.cwd, source=SOURCE)
        self.assertIs(again, first)
        self.assertEqual(self.daemon.submit_outcome, {"reused": True})
        forced = self.submit(("cargo", "test"), cwd=first.cwd, source=SOURCE, again=True)
        self.assertIsNot(forced, first)

    def test_a_check_of_a_stopped_attempt_says_nothing_of_the_next(self):
        t = self.submit(("cargo", "test"), source=SOURCE)
        self.assertEqual(len(self.daemon.source_checks), 1)  # attempt 0's start check, still running
        t.requeues = 1  # it was stopped and queued again meanwhile
        t.source_checks = {}
        self.settle()
        self.assertEqual(t.source_checks, {})

    def test_a_build_whose_tree_changed_while_it_ran_answers_nothing(self):
        first = self.submit(("cargo", "test"), source=SOURCE)
        self.settle()  # the start check matched
        self.seen_source = {**SOURCE, "fingerprint": "d" * 32}  # someone edited the tree meanwhile
        self.daemon.source_command[3] = self.source_file()
        self.finish(first, 0)
        self.settle()
        self.assertEqual((first.source_verified, first.source_changed), (False, True))
        again = self.submit(("cargo", "test"), cwd=first.cwd, source=SOURCE)
        self.assertIsNot(again, first)

    def test_a_build_that_makes_files_or_a_script_runs_again(self):
        for argv in (("cargo", "build"), ("sh", "run-tests.sh"), ("pnpm", "test")):
            first = self.submit(argv, source=SOURCE)
            self.finish(first, 0)
            self.settle()
            again = self.submit(argv, cwd=first.cwd, source=SOURCE)
            self.assertIsNot(again, first, argv)
            self.assertEqual(self.daemon.submit_outcome["passed_before"]["id"], first.id)
            self.finish(again, 0)
            self.settle()
            taken = self.submit(argv, cwd=first.cwd, source=SOURCE, reuse=True)
            self.assertIn(taken.id, (first.id, again.id), argv)

    def test_a_command_that_reaches_outside_the_tree_matches_nothing(self):
        argv = ("cargo", "test", "--manifest-path", "../other/Cargo.toml")
        first = self.submit(argv, source={**SOURCE, "repo": self.workdir("repo")}, cwd=self.workdir("repo"))
        self.assertIsNone(first.dedupe)
        self.assertTrue(buildqd.paths_inside(["cargo", "test", "--manifest-path=sub/Cargo.toml"], "/r", "/r"))
        self.assertFalse(buildqd.paths_inside(["cargo", "test", "--manifest-path=/elsewhere/Cargo.toml"], "/r", "/r"))
        repo = self.workdir("linked")
        os.symlink(self.tmp.name, os.path.join(repo, "external"))  # a one-word path that leads outside
        self.assertFalse(buildqd.paths_inside(["tsc", "--noEmit", "-p", "external"], repo, repo))
        self.assertTrue(buildqd.paths_inside(["cargo", "test", "test_name"], repo, repo))
        os.symlink(self.tmp.name, os.path.join(repo, "ext deps"))  # a name with a space in it, leading outside
        self.assertFalse(buildqd.paths_inside(["cargo", "test", "--manifest-path", "ext deps/Cargo.toml"], repo, repo))
        self.assertFalse(buildqd.paths_inside(["tsc", "--noEmit", "--project=ext deps/tsconfig.json"], repo, repo))
        outside_file = os.path.join(self.tmp.name, "lint-me.js")
        open(outside_file, "w").close()
        os.symlink(outside_file, os.path.join(repo, "--external.js"))
        for argv in (["env", "CFLAGS=-isystemexternal", "cargo", "check"],  # -isystem + external: where it splits can't be told
                     ["eslint", "--", "--external.js"],  # after --, a name, whatever it starts with
                     ["cargo", "clippy", "--", "-Lext deps/lib"],  # a name with a space, run on to its option
                     ["env", "CARGO_ENCODED_RUSTFLAGS=--sysroot\x1fext deps", "cargo", "check"],  # cargo splits at \x1f
                     ["vitest", "run", "-r=..", "--config", "x.ts"], ["vitest", "run", "-sr", ".."],
                     ["../other/check.sh"]):  # the command's own path (with --reuse)
            self.assertFalse(buildqd.paths_inside(argv, repo, repo), argv)
        for argv in (["cargo", "test", "--", "--test-threads=1"], ["cargo", "clippy", "--", "-D", "warnings"],
                     ["/usr/bin/env", "cargo", "test"], ["cargo", "test", "-F", "Cli"]):
            self.assertTrue(buildqd.paths_inside(argv, repo, repo), argv)
        inside = buildqd.paths_inside
        # A command that moves to another folder matches nothing, whatever it names after: what
        # `env -Csub env -Cexternal` or `--manifest-path external/…` finds there can't be told from here.
        for argv in (["env", "-C../other", "cargo", "test"], ["env", "-C", "sub", "cargo", "test"],
                     ["env", "--chdir=sub", "cargo", "test"], ["env", "-Csub", "env", "-Cexternal", "cargo", "test"],
                     ["env", "-iC../other", "cargo", "test"], ["make", "-sC../other", "test"],
                     ["make", "--directory=sub", "test"], ["sudo", "-D", "/r", "cargo", "test"],
                     ["sudo", "-u", "x", "cargo", "test"], ["vitest", "run", "--root", "sub", "--config", "external/v.ts"],
                     ["pnpm", "--dir", "sub", "exec", "vitest", "run"], ["env", "--ch=sub", "cargo", "test"],
                     ["make", "--dir=sub", "test"], ["npm", "--prefix", "sub", "test"], ["yarn", "--cwd", "sub", "test"]):
            self.assertFalse(inside(argv, "/r", "/r"), argv)
        # Any part of a word that might be a path is checked: after "=", run on, in quotes.
        for argv in (["cargo", "test", "-p../other=1"], ["env", "RUSTC_WRAPPER=/x/w.sh", "cargo", "check"],
                     ["cargo", "check", "--config", 'patch.crates-io.foo.path="../../foo"'],
                     ["cargo", "check", "--config", "patch.crates-io.foo = { path = '/x/foo' }"],
                     ["tsc", "--noEmit", "-p../other/tsconfig.json"],
                     # a value with several in it: cut at spaces, commas, quotes, brackets, ":"
                     ["cargo", "check", "--config", 'paths=["vendor", "../other"]'],
                     ["env", "RUSTFLAGS=-L../other", "cargo", "check"], ["env", "RUSTFLAGS=-L /x/lib", "cargo", "test"],
                     ["cargo", "check", "--config", 'build.rustflags=["--sysroot", "/outside"]'],
                     ["cargo", "check", "--config", 'registries.x.index="file:///x/index"'],
                     ["dotnet", "test", "@../other/args.rsp"],
                     ["env", "-C", "..", "cargo", "test"], ["cargo", "-C", "..", "test"],
                     ["env", "--", "make", "-C", "../x"],  # (env's `--`, not cargo's: make still moves)
                     ["env", "CFLAGS=-isystem/outside/include", "cargo", "check"], ["dotnet", "test", "/outside"],
                     ["vitest", "run", "-r", "sub"]):
            self.assertFalse(inside(argv, "/r", "/r"), argv)
        # What a tool interprets rather than reads as it is (quotes, escapes, brackets, $, URLs) can't
        # be read for certain: refused, not guessed at.
        for argv in (["cargo", "check", "--config", 'patch."https://github.com/x/foo".foo.path="../foo"'],
                     ["cargo", "check", "--config",
                      'build.rustflags=["--remap-path-prefix=src=https://example.invalid","--sysroot","/outside"]'],
                     ["cargo", "check", "--config", 'build.rustflags=["--sysroot","\\u002foutside"]'],
                     ["cargo", "check", "--config", 'paths=["vendor"]'], ["cargo", "test", "--config", "registry.x=https://x.example/y"],
                     ["cargo", "check", "--config", "registries.x.index=file://localhost/x"], ["vitest", "run", "-t", "^adds$"]):
            self.assertFalse(inside(argv, "/r", "/r"), argv)
        # Output folders aren't what a check reads; options, URLs and test names aren't paths.
        for argv in (["cargo", "test", "--target-dir", "/cache/t"], ["cargo", "test", "--target-dir=/cache/t"],
                     ["env", "CARGO_TARGET_DIR=/cache/t", "cargo", "test"], ["cargo", "clippy", "--", "-D", "warnings"],
                     ["cargo", "test", "-p", "api", "-j8", "--", "-Dwarnings"], ["vitest", "run", "--shard=1/2"],
                     ["sudo", "X=1", "cargo", "test"],
                     ["env", "RUSTFLAGS=-C target-cpu=native -D warnings", "cargo", "check"],
                     ["dotnet", "test", "/p:Configuration=Release"], ["dotnet", "test", "-c", "Release", "-p:Configuration=Release"],
                     ["eslint", ".", "--cache-location", "/cache/eslint"], ["cargo", "test", "foo::bar"],
                     ["cargo", "nextest", "run", "--profile", "ci"], ["vitest", "run", "-t", "adds 1 + 2"],
                     ["dotnet", "test", "--logger", "console;verbosity=normal"],
                     ["dotnet", "test", "--filter", "FullyQualifiedName~X&Category!=Slow"], ["eslint", ".", "--max-warnings=0"],
                     ["cargo", "clippy", "--", "-W", "clippy::pedantic", "-Dclippy::all"], ["cargo", "test", "-F", "Cli"],
                     ["cargo", "test", "--", "--test-threads=1"],
                     # variables that name tools, not source, and cargo's own value options and `--` (#48)
                     ["env", "PATH=/usr/bin:/bin", "cargo", "test"], ["env", "RUSTC_WRAPPER=/usr/bin/sccache", "cargo", "check"],
                     ["env", "PATH=/home/u/.cargo/bin:/usr/local/bin:/usr/bin", "cargo", "test"],
                     ["sudo", "PATH=/usr/bin", "cargo", "test"], ["env", "-i", "PATH=/usr/bin", "cargo", "test"],
                     ["cargo", "test", "-pCore"], ["cargo", "test", "-FCli"]):
            self.assertTrue(inside(argv, "/r", "/r"), argv)
        # ... but only where a wrapper sets them, and only cargo's own options: a script's argument
        # `PATH=…` is a path, and after cargo's `--` a custom test harness may take -C (#52 review)
        for argv in (["python3", "check.py", "PATH=../outside/input"], ["cargo", "test", "--", "-C..", "../outside/input"],
                     ["cargo", "test", "--", "-Cfoo"], ["env", "RUSTC_WRAPPER=/x/w.sh", "cargo", "check"],
                     ["env", "RUSTC_WRAPPER=/home/u/other/target/release/sccache", "cargo", "check"],
                     # a word with "=" that a wrapper runs, not sets: a program in a folder named `PATH=..`
                     ["nice", "PATH=../../../outside/check.sh", "cargo", "test"], ["PATH=../x/check.sh", "cargo", "test"],
                     ["env", "PATH=../tools/bin:/usr/bin", "cargo", "xyz"], ["env", "PATH=/home/u/Code/xyz/target/release", "cargo", "xyz"]):
            self.assertFalse(inside(argv, "/r", "/r"), argv)

    def test_a_failure_or_an_old_pass_is_never_reused(self):
        failed = self.submit(("cargo", "test"), source=SOURCE)
        self.finish(failed, 101)
        self.settle()
        retry = self.submit(("cargo", "test"), cwd=failed.cwd, source=SOURCE)
        self.assertIsNot(retry, failed)
        self.finish(retry, 0)
        self.settle()
        retry.ended = self.now - 3 * 3600  # longer ago than the 2h reuse_window
        later = self.submit(("cargo", "test"), cwd=failed.cwd, source=SOURCE)
        self.assertIsNot(later, retry)

    def test_the_ticket_says_what_it_built_and_who_asked(self):
        t = self.submit(("cargo", "test"), source={**SOURCE, "dirty": True}, caller="t3code-claude-1.scope")
        row = t.row(self.now)
        self.assertEqual((row["source"]["commit"], row["source"]["dirty"], row["caller"]),
                         ("a" * 40, True, "t3code-claude-1.scope"))
        self.assertIsNone(buildqd.sane_source({**SOURCE, "commit": "not-a-sha"}))

    def test_which_commands_are_checks_and_which_start_over_safely(self):
        checks = [("cargo", "test", "-p", "api"), ("cargo", "+nightly", "check"), ("cargo", "nextest", "run"),
                  ("vitest", "run", "-t", "adds"),
                  ("cargo", "clippy", "--", "-Dwarnings"), ("npx", "tsc", "--noEmit"), ("vitest", "run"),
                  ("eslint", "."), ("dotnet", "test")]
        not_checks = [("cargo", "build"), ("cargo", "doc"), ("cargo", "run"), ("cargo", "clippy", "--fix"),
                      ("cargo", "test", "--no-run"), ("tsc",), ("tsc", "-w", "--noEmit"), ("vitest", "run", "-u"),
                      ("vitest",), ("eslint", "--fix", "."), ("dotnet", "build"), ("pnpm", "test"),
                      ("tsc", "--noEmit", "false"), ("eslint", "-o", "report.json", "."),
                      ("vitest", "run", "--coverage"), ("vitest", "run", "--outputFile=junit.xml"),
                      ("dotnet", "test", "--logger", "trx"), ("dotnet", "test", "-l", "trx"),
                      ("vitest", "run", "--reporter", "html"), ("dotnet", "test", "--logger:trx"),
                      ("vitest", "run", "--coverage.enabled"),
                      ("pnpm", "run", "build:deploy"), ("sh", "run-tests.sh"), ("git", "push"), ("make", "test"),
                      ("vitest", "run", "-ut", "adds"), ("vitest", "run", "-wt", "adds"),  # -u, -w grouped (#50)
                      ("vitest", "run", "-u=true"), ("vitest", "run", "-tu=x")]
        for argv in checks:
            self.assertTrue(buildqd.reusable_check(list(argv)), argv)
        for argv in not_checks:
            self.assertFalse(buildqd.reusable_check(list(argv)), argv)
        for argv in (("cargo", "build"), ("cargo", "test"), ("dotnet", "build"), ("tsc",), ("ninja",),
                     ("ninja", "-C", "build", "test")):
            self.assertTrue(buildqd.restart_safe(list(argv)), argv)
        for argv in (("sh", "run-tests.sh"), ("git", "push"), ("cargo", "run"), ("pnpm", "build"), ("make",),
                     ("ninja", "install"), ("ninja", "deploy")):
            self.assertFalse(buildqd.restart_safe(list(argv)), argv)

    def test_the_environment_that_counts(self):
        digest = buildqd.env_digest
        self.assertEqual(digest({"PATH": "/bin", "PWD": "/a", "SHLVL": "1", "CLAUDE_CODE_SESSION_ID": "x",
                                 "ANTIGRAVITY_AGENT": "1", "ANTIGRAVITY_SOURCE_METADATA": "call-1",
                                 "ANTIGRAVITY_CONVERSATION_ID": "c1", "ANTIGRAVITY_TRAJECTORY_ID": "t1",
                                 "ANTIGRAVITY_CSRF_TOKEN": "tok1", "ANTIGRAVITY_LS_ADDRESS": "localhost:1"}),
                         digest({"PATH": "/bin", "PWD": "/b", "SHLVL": "2", "CLAUDE_CODE_SESSION_ID": "y",
                                 "ANTIGRAVITY_AGENT": "1", "ANTIGRAVITY_SOURCE_METADATA": "call-2",
                                 "ANTIGRAVITY_CONVERSATION_ID": "c2", "ANTIGRAVITY_TRAJECTORY_ID": "t2",
                                 "ANTIGRAVITY_CSRF_TOKEN": "tok2", "ANTIGRAVITY_LS_ADDRESS": "localhost:2"}))
        for name in ("DATABASE_URL", "API_TOKEN", "SESSION_BACKEND", "DB_PASSWORD", "ANTIGRAVITY_CUSTOM_FLAG"):
            self.assertNotEqual(digest({name: "a"}), digest({name: "b"}), name)


class AbandonedSubmits(DaemonCase):
    """#71: a dropped-off build whose agent session has ended, and that nobody waits on, is cancelled
    while it's still queued."""

    def setUp(self):
        super().setUp()
        self.machine.mem_available = 15 * GIB  # nothing fits: everything waits
        me = os.getpid()
        self.alive = [me, buildqd.proc_identity(me)[0]]
        self.gone = [me, self.alive[1] + 1]  # (another start time: the pid was reused)
        self.real_live_threads = buildqd.live_threads
        self.above = {}  # a client's pid -> the agent processes the daemon finds above it
        self.threads = {}  # a T3 thread -> its agent processes now
        for name, fake in (("agents_above", lambda pid: [list(a) for a in self.above.get(pid, ())]),
                           ("live_threads", lambda: dict(self.threads))):
            patch = mock.patch.object(buildqd, name, fake)
            patch.start()
            self.addCleanup(patch.stop)

    def drop_off(self, *agents, argv=("cargo", "test"), thread=None, **extra):
        """A `buildq submit` from a client that runs under these agent processes, nearest first."""
        pid = 1000 + len(self.above)
        self.above[pid] = agents
        t = self.submit(argv, peer=pid, env={"T3CODE_THREAD_ID": thread} if thread else None, **extra)
        t.wanted_at = self.now
        return t

    class Conn:
        submission = None

        def send(self, message):
            return True

    def test_a_dropped_off_build_whose_agent_went_is_cancelled_after_a_while(self):
        t = self.drop_off(self.gone, submission="s")
        self.assertEqual((t.state, t.agents), ("queued", {"s": {"pids": [self.gone], "thread": None}}))
        self.daemon.drop_abandoned(self.now + 599)
        self.assertFalse(t.cancel_requested)  # within abandoned_seconds: its session may be starting again
        self.assertEqual(t.row(self.now)["agent"], "gone")
        self.daemon.drop_abandoned(self.now + 601)
        self.assertEqual(t.state, "cancelled")
        self.assertIn("agent session that dropped it off had ended", t.error)  # what a later wait is told

    def test_the_time_counts_from_when_the_agent_went_not_from_the_last_wait(self):
        t = self.drop_off(self.alive, submission="s")
        self.daemon.drop_abandoned(self.now + 5000)  # nobody has waited for over an hour: its agent runs
        self.assertEqual((t.cancel_requested, t.row(self.now)["agent"]), (False, "alive"))
        t.agents["s"]["pids"] = [self.gone]  # the agent's process ends (a restart, say)
        self.daemon.drop_abandoned(self.now + 5001)
        self.daemon.drop_abandoned(self.now + 5600)
        self.assertFalse(t.cancel_requested)  # it has abandoned_seconds to come back
        self.daemon.drop_abandoned(self.now + 5601)
        self.assertTrue(t.cancel_requested)

    def test_the_tick_does_it(self):
        t = self.drop_off(self.gone, submission="s")
        self.daemon.tick(self.now + 601)
        self.assertEqual(t.state, "cancelled")

    def test_a_build_from_no_known_agent_or_followed_by_a_run_stays(self):
        unknown = self.drop_off(submission="b")  # CI, a terminal: no agent above the client
        self.assertEqual(unknown.agents, {})
        run = self.drop_off(self.gone, argv=("cargo", "check"), attach=True, submission="r")
        self.assertEqual(run.agents, {})  # a `buildq run` has the unwatched rule instead
        self.daemon.drop_abandoned(self.now + 10_000)
        self.assertFalse(unknown.cancel_requested or run.cancel_requested)
        self.assertIsNone(unknown.row(self.now)["agent"])

    def test_an_agent_run_by_a_session_that_still_runs_keeps_it(self):
        t = self.drop_off(self.gone, self.alive, submission="s")  # a `claude -p` the session ran, and the session
        self.daemon.drop_abandoned(self.now + 10_000)
        self.assertEqual((t.cancel_requested, t.agent_now), (False, "alive"))

    def test_a_new_process_of_the_same_t3_thread_stands_for_the_session(self):
        t = self.drop_off(self.gone, submission="s", thread="thread-1")
        self.assertEqual(t.agents["s"]["thread"], "thread-1")  # from the client's environment
        self.daemon.drop_abandoned(self.now + 300)
        self.assertEqual(t.agent_now, "gone")
        self.threads["thread-1"] = [self.gone, self.alive]  # T3 starts the thread's agent again
        self.daemon.drop_abandoned(self.now + 601)
        self.assertFalse(t.cancel_requested)
        self.assertEqual(t.agents["s"]["pids"], [self.alive])  # followed by its process from now on

    def test_a_process_started_since_the_last_look_is_seen_before_a_cancel(self):
        t = self.drop_off(self.gone, submission="s", thread="thread-1")
        self.daemon.drop_abandoned(self.now + 590)  # a look at the processes, kept for 30 s
        self.threads["thread-1"] = [self.alive]  # started just after it
        self.daemon.drop_abandoned(self.now + 601)
        self.assertFalse(t.cancel_requested)  # a cancel takes a look of its own
        self.assertEqual((t.agent_now, t.wanted_at), ("alive", self.now + 601))  # and what it finds counts at once

    def test_a_thread_whose_only_process_has_gone_too_is_cancelled(self):
        t = self.drop_off(self.gone, submission="s", thread="thread-1")
        self.threads["thread-1"] = [self.gone]  # (gone between the look and its use)
        self.daemon.drop_abandoned(self.now + 601)
        self.assertTrue(t.cancel_requested)

    def test_nothing_is_cancelled_when_the_processes_cant_be_listed(self):
        t = self.drop_off(self.gone, submission="s", thread="thread-1")
        with mock.patch.object(buildqd, "live_threads", side_effect=OSError("no /proc")):
            self.daemon.drop_abandoned(self.now + 601)
        self.assertEqual((t.cancel_requested, t.agent_now), (False, "alive"))

    def test_a_wait_on_the_ticket_holds_it_off(self):
        t = self.drop_off(self.gone, submission="s")
        self.daemon.watchers[t.id].add(self.Conn())  # a `buildq wait` on the ticket
        self.daemon.drop_abandoned(self.now + 1000)
        self.assertFalse(t.cancel_requested)
        self.daemon.watchers[t.id].clear()  # its --timeout ran out
        self.daemon.drop_abandoned(self.now + 1500)
        self.assertFalse(t.cancel_requested)  # 500 s since the last wait
        self.daemon.drop_abandoned(self.now + 1601)
        self.assertTrue(t.cancel_requested)

    def test_a_wait_shorter_than_a_tick_counts(self):
        t = self.drop_off(self.gone, submission="s")
        self.daemon.handle_message({"op": "watch", "ticket": t.id}, self.Conn(), self.now + 500)
        self.daemon.watchers[t.id].clear()  # it went before the next tick
        self.daemon.drop_abandoned(self.now + 1000)
        self.assertFalse(t.cancel_requested)
        self.daemon.drop_abandoned(self.now + 1101)
        self.assertTrue(t.cancel_requested)

    def test_another_submission_that_still_wants_it_keeps_it(self):
        first = self.drop_off(self.gone, source=SOURCE, submission="a")
        self.drop_off(self.alive, cwd=first.cwd, source=SOURCE, submission="b")  # joins it
        self.assertEqual(set(first.agents), {"a", "b"})
        self.daemon.drop_abandoned(self.now + 1000)
        self.assertEqual((first.cancel_requested, first.agent_now), (False, "alive"))
        first.agents["b"]["pids"] = [self.gone]
        self.drop_off(cwd=first.cwd, source=SOURCE, submission="c")  # joined from a terminal
        self.daemon.drop_abandoned(self.now + 5000)
        self.assertFalse(first.cancel_requested)
        self.assertIsNone(first.row(self.now)["agent"])  # not up to the agents' sessions any more

    def test_a_run_that_joined_keeps_it_until_it_goes(self):
        t = self.drop_off(self.gone, source=SOURCE, submission="s")
        self.submit(("cargo", "test"), cwd=t.cwd, source=SOURCE, attach=True, submission="r")  # a `buildq run` joins
        self.daemon.started_at = self.now - 1000
        self.daemon.drop_abandoned(self.now + 10)
        self.assertIsNone(t.agent_now)  # the run waits for it too
        self.daemon.drop_abandoned(self.now + 610)
        self.assertFalse(t.cancel_requested)  # 600 s since the run was last seen
        self.daemon.drop_abandoned(self.now + 611)  # the run was killed and never followed again
        self.assertTrue(t.cancel_requested)

    def test_a_running_build_carries_on_and_has_the_whole_time_if_queued_again(self):
        self.machine.mem_available = 200 * GIB
        running = self.drop_off(self.gone, submission="s")
        self.assertEqual(running.state, "running")
        self.daemon.drop_abandoned(self.now + 1000)
        self.assertFalse(running.cancel_requested)
        self.assertIsNone(running.row(self.now)["agent"])
        self.machine.mem_available = 15 * GIB
        running.requeue_pending = True  # stopped to free memory ...
        self.runner.running.discard(running.id)
        self.daemon.check(self.now + 1000)
        self.assertEqual((running.state, running.requeues), ("queued", 1))  # ... and queued again
        self.daemon.drop_abandoned(self.now + 1001)
        self.daemon.drop_abandoned(self.now + 1600)
        self.assertFalse(running.cancel_requested)  # 600 s since it last ran
        self.daemon.drop_abandoned(self.now + 1601)
        self.assertTrue(running.cancel_requested)

    def test_a_look_that_fails_partway_cancels_nothing(self):
        t = self.drop_off(self.gone, submission="s", thread="thread-1")
        with mock.patch.object(buildqd, "live_threads", self.real_live_threads), \
                mock.patch.object(buildqd, "own_pids", return_value=[os.getpid()]), \
                mock.patch.object(buildqd, "is_agent", side_effect=OSError(24, "Too many open files")):
            self.daemon.drop_abandoned(self.now + 601)  # the look can't tell who runs: nothing is called gone
        self.assertEqual((t.cancel_requested, t.agent_now), (False, "alive"))

    def test_a_process_that_cant_be_read_isnt_called_gone(self):
        t = self.drop_off(self.alive, submission="s")
        with mock.patch.object(buildqd, "proc_stat", return_value=None):  # (out of descriptors, say)
            self.assertFalse(buildqd.proc_gone(*self.alive))  # it is there: can't tell
            self.daemon.drop_abandoned(self.now + 10_000)
        self.assertEqual((t.cancel_requested, t.agent_now), (False, "alive"))
        self.assertTrue(buildqd.proc_gone(*self.gone))  # another process has that pid now
        self.assertTrue(buildqd.proc_gone(2 ** 40, 1))  # nothing has
        self.assertTrue(buildqd.proc_gone(float("inf"), 1))

    def test_zero_turns_it_off(self):
        self.daemon.cfg.abandoned_seconds = 0
        t = self.drop_off(self.gone, submission="s")
        self.daemon.drop_abandoned(self.now + 10_000)
        self.assertEqual((t.cancel_requested, t.agent_now), (False, None))

    def test_the_agent_is_kept_across_a_restart_and_the_time_starts_again(self):
        t = self.drop_off(self.gone, submission="s", thread="thread-1")
        t.wanted_at = self.now - 5000
        self.daemon.save(t)
        again = buildqd.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                               queries=buildqd.InlineExecutor())
        again.load_tickets(self.now + 1)
        loaded = again.tickets[t.id]
        self.assertEqual(loaded.agents, {"s": {"pids": [self.gone], "thread": "thread-1"}})
        self.assertGreaterEqual(loaded.wanted_at, self.now)  # not kept: a restart gives it the time again
        again.stop_memory_watch()
        for x in again.tickets.values():
            again.close_jobserver(x)

    def test_bad_saved_agents_are_left_out(self):
        good = {"pids": [[5, 6], [7, 8]], "thread": "thread-1"}
        saved = {"ok": good, "old": [5, 6, None], "inf": {"pids": [[5, float("inf")]]}, "none": {"pids": []},
                 "num": {"pids": 5}, "bool": {"pids": [[True, 1]]}, "huge": {"pids": [[2 ** 70, 1]]},
                 "list": {"pids": [[5, 6]], "thread": ["thread"]}, 7: good}
        self.assertEqual(buildqd.sane_agents(saved),
                         {"ok": good, "list": {"pids": [[5, 6]], "thread": None}})
        self.assertEqual(buildqd.sane_agents(["x"]), {})
        self.assertIsNone(buildqd.proc_identity(float("inf")))  # (no error from a number that isn't a pid)


class AgentProcesses(unittest.TestCase):
    """#71: which processes count as agent sessions, read from the real /proc."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = os.path.join(self.tmp.name, "claude")  # a process named as Claude Code's is
        os.symlink(sys.executable, self.fake)
        self.thread = f"test-thread-{os.getpid()}-{time.time()}"

    def session(self, exe=None, env=None):
        """A stand-in agent session that runs one child, as an agent runs a build client. The child
        reads from the session, so it ends when the session does."""
        code = ("import subprocess; p = subprocess.Popen(['cat'], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL); "
                "print(p.pid, flush=True); p.wait()")
        proc = subprocess.Popen([exe or self.fake, "-I", "-c", code], stdout=subprocess.PIPE, text=True,
                                env=env if env is not None else {**os.environ, "T3CODE_THREAD_ID": self.thread})

        def stop():
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            proc.stdout.close()
        self.addCleanup(stop)
        return proc, int(proc.stdout.readline())

    def test_an_agent_above_a_client_is_found_and_seen_to_go(self):
        proc, child = self.session()
        above = buildqd.agents_above(child)
        self.assertEqual(above[0][0], proc.pid)  # (the suite's own agent session, if any, follows it)
        self.assertTrue(buildqd.proc_alive(*above[0]))
        self.assertEqual(buildqd.live_threads().get(self.thread), [above[0]])
        proc.kill()
        proc.wait()
        self.assertFalse(buildqd.proc_alive(*above[0]))
        self.assertNotIn(self.thread, buildqd.live_threads())

    def test_no_agent_above_init_or_a_pid_that_isnt_one(self):
        self.assertEqual(buildqd.agents_above(1), [])
        self.assertEqual(buildqd.agents_above(None), [])
        self.assertEqual(buildqd.agents_above(2 ** 40), [])

    def test_the_programs_that_count(self):
        for exe in ("/home/u/.local/share/claude/versions/2.1.328",
                    "/home/u/.claude/remote/ccd-cli/2.1.321-3fb3e6595884",  # the desktop app's
                    "/home/u/.local/share/claude/versions/2.1.300 (deleted)"):  # replaced by an update
            self.assertTrue(buildqd.AGENT_EXE.search(exe), exe)
        for exe in ("/usr/bin/python3.13", "/usr/bin/node", "/home/u/.claude/skills/x/run.sh",
                    "/home/u/Code/claude/versions/notes/readme"):
            self.assertFalse(buildqd.AGENT_EXE.search(exe), exe)

    def test_antigravity_process_counts_as_an_agent(self):
        for name in ("antigravity", "agy"):
            fake = os.path.join(self.tmp.name, name)
            os.symlink(sys.executable, fake)
            proc, child = self.session(exe=fake, env=os.environ)
            above = buildqd.agents_above(child)
            self.assertEqual(above[0][0], proc.pid, name)
            self.assertTrue(buildqd.proc_alive(*above[0]), name)
            proc.kill()
            proc.wait()
            self.assertFalse(buildqd.proc_alive(*above[0]), name)


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
        again = buildqd.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                                     queries=buildqd.InlineExecutor())
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
        again = buildqd.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                                     queries=buildqd.InlineExecutor())
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
        again = buildqd.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                                     queries=buildqd.InlineExecutor())
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
        t.mem_now = 100 * buildqd.MIB  # what holds the memory isn't buildq's
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
        safe = buildqd.restart_safe
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
        self.assertEqual(buildqd.strip_wrappers(["env", "-i", "A=1", "cargo", "test"]), ["cargo", "test"])
        self.assertFalse(safe(["env", "X-Y=./wrap.sh", "bash", "deploy.sh"], scripts))  # GNU env: any word with "="
        self.assertFalse(safe(["/usr/bin/time", "-f", "eslint", "bash", "deploy.sh"], scripts))  # -f's value isn't the tool
        self.assertFalse(buildqd.reusable_check(["/usr/bin/time", "-f", "eslint", "bash", "deploy.sh"]))
        self.assertFalse(safe(["nice", "-n", "5", "cargo", "test"]))  # (a wrapper given options: not read for certain)
        for argv in (["env", "--split-string=bash deploy.sh", "cargo", "test"], ["env", "-Sbash deploy.sh", "cargo", "test"],
                     ["env", "--split-string=bash deploy.sh", "bash", "wrap.sh"], ["sudo", "X-Y=./wrap.sh", "bash", "deploy.sh"]):
            self.assertFalse(safe(argv, scripts), argv)  # an option with "=" in it isn't an assignment
            self.assertFalse(buildqd.reusable_check(argv), argv)
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
            self.assertFalse(buildqd.reusable_check(argv), argv)
        for argv in (["sudo", "X=1", "cargo", "test"], ["env", "RUST_LOG=debug", "cargo", "test"]):
            self.assertTrue(safe(argv), argv)
            self.assertTrue(buildqd.reusable_check(argv), argv)
        # `pnpm exec BIN` runs a binary, never a package script (pnpm.io/cli/exec); with any of
        # pnpm's own options (a filter, -r, -c's shell), before or after exec, it isn't read.
        for argv in (["pnpm", "exec", "vitest", "run"], ["pnpm", "exec", "tsc", "--noEmit"]):
            self.assertTrue(buildqd.reusable_check(argv), argv)
            self.assertTrue(safe(argv), argv)
        for argv in (["pnpm", "exec", "-c", "vitest run"], ["pnpm", "-r", "exec", "vitest", "run"],
                     ["pnpm", "exec", "--filter", "x", "vitest", "run"], ["pnpm", "vitest", "run"], ["pnpm", "exec"]):
            self.assertFalse(buildqd.reusable_check(argv), argv)
            self.assertFalse(safe(argv), argv)
        # history keys and lanes still read env's exact forms (a decision to restart or reuse never does)
        words = buildqd.command_words
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
                buildqd.Config({"restartable_scripts": [tool]}, 64 * GIB, 16)
            self.assertIn("names scripts, not tools", str(refused.exception))
        cfg = buildqd.Config({"restartable_scripts": ["wrap.sh"]}, 64 * GIB, 16)
        self.assertEqual((cfg.restartable_scripts, cfg.summary()["restartable_scripts"]), ({"wrap.sh"}, ["wrap.sh"]))
        self.assertEqual(buildqd.Config({}, 64 * GIB, 16).restartable_scripts, frozenset())
        with self.assertRaises(ValueError):
            buildqd.Config({"restartable_scripts": ["../x.sh"]}, 64 * GIB, 16)

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
        buildqd.SystemdRunner(self.tmp.name).prepare(t, "buildq.slice")
        self.assertTrue(t.unit.endswith(f"{t.id}-r1.service"))

    def test_systemd_result_detects_oom_kills_with_oom_policy_continue(self):
        runner = buildqd.SystemdRunner(self.tmp.name)
        t = self.submit()
        t.unit = None
        with mock.patch.object(buildqd.subprocess, "run", side_effect=AssertionError("must not call systemctl")):
            self.assertEqual(runner.result(t), "")
        t.unit = "buildq-test.service"
        cases = [
            ("LoadState=not-found\nResult=success\nOOMKills=18446744073709551615\nManagedOOMKills=18446744073709551615\n", ""),
            ("LoadState=loaded\nResult=signal\nOOMKills=18446744073709551615\nManagedOOMKills=18446744073709551615\n", "signal"),
            ("LoadState=loaded\nResult=signal\nOOMKills=18446744073709551615\nManagedOOMKills=2\n", "oom-kill"),
            ("LoadState=loaded\nResult=signal\nOOMKills=1\nManagedOOMKills=18446744073709551615\n", "oom-kill"),
            ("LoadState=loaded\nResult=signal\nOOMKills=0\nManagedOOMKills=38\n", "oom-kill"),
            ("LoadState=loaded\nResult=signal\nOOMKills=1\nManagedOOMKills=0\n", "oom-kill"),
            ("LoadState=loaded\nResult=signal\nOOMKills=0\nManagedOOMKills=0\n", "signal"),
            ("LoadState=loaded\nResult=oom-kill\n", "oom-kill"),
            ("LoadState=loaded\nResult=exit-code\nOOMKills=[not set]\nManagedOOMKills=\n", "exit-code"),
        ]
        for stdout, expected in cases:
            with mock.patch.object(buildqd.subprocess, "run",
                                   return_value=subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")):
                self.assertEqual(runner.result(t), expected, stdout)

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
        loaded = buildqd.Ticket.from_record(rec, t.dir)
        self.assertTrue(loaded.start_rigid)
        rec.update(state="queued", started=None, start_rule="always")  # queued again before the upgrade
        self.assertTrue(buildqd.Ticket.from_record(rec, t.dir).start_rigid)
        rec.update(tail_used="lots", peak_swap=None, breached_at="x")  # a damaged file: numbers only
        loaded = buildqd.Ticket.from_record(rec, t.dir)
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
        with mock.patch.object(buildqd, "cgroup_stats", return_value=(t.cpu_usec, int(gib * GIB), kills)), \
                mock.patch.object(buildqd, "cgroup_memory_waits", return_value=None), \
                mock.patch.object(buildqd, "cgroup_swap", return_value=0):
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
        again = buildqd.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                                     queries=buildqd.InlineExecutor())
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
        again = buildqd.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                                     queries=buildqd.InlineExecutor())
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
        self.assertEqual(t.past_peak_most_over, 20 * GIB - int(2 * GIB * 1.1))

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
        again = buildqd.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                                     queries=buildqd.InlineExecutor())
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
            with mock.patch.object(buildqd, "cgroup_stats", return_value=(0, gib * GIB, 0)), \
                    mock.patch.object(buildqd, "cgroup_memory_waits", return_value=None):
                t.measure(self.now + second)
        self.assertEqual((t.mem_peak, t.peak_at), (8 * GIB, 10.0))
        self.assertEqual(t.tail, 4 * GIB)  # 3G at 20s is within half a minute of the peak
        u = self.submit()
        u.started, u.cg = self.now, "/nonexistent"
        for second, gib, swap in ((0, 1, 0), (10, 8, GIB), (50, 2, 5 * GIB), (70, 3, 1 * GIB)):
            with mock.patch.object(buildqd, "cgroup_stats", return_value=(0, gib * GIB, 0)), \
                    mock.patch.object(buildqd, "cgroup_memory_waits", return_value=None), \
                    mock.patch.object(buildqd, "cgroup_swap", return_value=swap):
                u.measure(self.now + second)
        self.assertEqual((u.peak_swap, u.tail, u.tail_used), (GIB, 3 * GIB, 7 * GIB))  # 2G resident + 5G swapped (#56)
        for second in range(0, 200000, 7):
            t.note_trace(float(second), GIB)
        self.assertLessEqual(len(t.trace), t.TRACE_POINTS + 1)
        self.assertEqual(buildqd.memory_profile([[0, GIB], [50, 2 * GIB], [99, 3 * GIB]], 100), [1024, 0, 0, 0, 0, 2048,
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
        loaded = buildqd.Ticket.from_record(record, t.dir)
        self.assertEqual((loaded.waited_by, loaded.peak_at, loaded.tail, loaded.released), ({}, 0.0, 0, False))
        record["waited_by"] = {"memory": 12.5, "cpu": "nan", "held": float("inf")}
        self.assertEqual(buildqd.Ticket.from_record(record, t.dir).waited_by, {"memory": 12.5})

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
        consumers = buildqd.Consumers()
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
        again = buildqd.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                                     queries=buildqd.InlineExecutor())
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
        again = buildqd.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                                     queries=buildqd.InlineExecutor())
        self.assertEqual([p["state"] for p in again.status_posts], ["failure"])
        again.stop_memory_watch()

    def test_a_commit_github_doesnt_have_yet_is_tried_again_after_a_push(self):
        self.daemon.gh = [sys.executable, "-c", "import sys; sys.exit(1)"]  # not on GitHub yet
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
                self.assertEqual(buildqd.find_gh(), "gh")  # nowhere: left to the error that says so
                place(os.path.join(home, ".local", "bin", "gh"), executable=False)
                self.assertEqual(buildqd.find_gh(), "gh")  # not runnable: not taken
                home_bin = place(os.path.join(home, "bin", "gh"))
                self.assertEqual(buildqd.find_gh(), home_bin)
                local = place(os.path.join(home, ".local", "bin", "gh"))
                self.assertEqual(buildqd.find_gh(), local)  # ~/.local/bin before ~/bin
                first = place(os.path.join(on_path, "gh"))
                self.assertEqual(buildqd.find_gh(), first)  # PATH before both

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
        with mock.patch.object(buildqd, "find_gh", side_effect=AssertionError("searched")):
            self.drain(self.now + 60)
        self.assertEqual(len(self.calls()), 1)
        self.assertIn("state=success", self.calls()[0])

    def test_the_gh_setting_must_be_a_full_path(self):
        script = os.path.join(self.tmp.name, "gh")
        with open(script, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(script, 0o755)
        self.assertEqual(buildqd.Config({"gh": script}, 64 * GIB, 16).gh, script)
        self.assertEqual(buildqd.Config({"gh": script}, 64 * GIB, 16).summary()["gh"], script)
        self.assertEqual(buildqd.Config({}, 64 * GIB, 16).gh, "")
        missing = os.path.join(self.tmp.name, "missing")  # gone later: the configuration still loads
        self.assertEqual(buildqd.Config({"gh": missing}, 64 * GIB, 16).gh, missing)
        self.assertEqual(buildqd.Config({"gh": "~/bin/gh"}, 64 * GIB, 16).gh, os.path.expanduser("~/bin/gh"))
        for bad in ("gh", "bin/gh"):
            with self.assertRaises(ValueError, msg=bad):
                buildqd.Config({"gh": bad}, 64 * GIB, 16)

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
        with mock.patch.object(buildqd, "log") as log:
            self.drain(self.now + 60)
            self.drain(self.now + 700)  # ten minutes on, tried again
        said = [c for c in log.call_args_list if "can't run gh" in str(c)]
        self.assertEqual(len(said), 1, log.call_args_list)
        self.assertNotIn("isn't on PATH", str(said[0]))  # a set path: no search to blame
        self.assertEqual(len(self.daemon.status_posts), 2)  # both kept for later
        missing = self.daemon.gh
        self.daemon.gh = None  # found as it is sent: nowhere
        with mock.patch.object(buildqd, "find_gh", return_value="gh"), \
                mock.patch.dict(os.environ, PATH=os.path.join(self.tmp.name, "empty")), \
                mock.patch.object(buildqd, "log") as log:
            self.drain(self.now + 1400)
        self.assertEqual([c for c in log.call_args_list if "can't run gh" in str(c)], [])  # the same outage
        self.daemon.gh = [sys.executable, "-c", "pass"]  # gh runs again
        self.drain(self.now + 2100)
        self.daemon.gh = missing
        for argv in (("cargo", "clippy"),):
            t = self.submit(argv, source=SOURCE)
            self.finish(t, 0)
        self.settle()
        with mock.patch.object(buildqd, "log") as log:
            self.drain(self.now + 2200)
        said = [c for c in log.call_args_list if "can't run gh" in str(c)]
        self.assertEqual(len(said), 1, log.call_args_list)  # a new outage is said again

    def test_a_gh_found_nowhere_says_where_it_was_looked_for(self):
        self.daemon.gh = None
        t = self.submit(("cargo", "test"), source=SOURCE)
        self.finish(t, 0)
        self.settle()
        with mock.patch.object(buildqd, "find_gh", return_value="gh"), \
                mock.patch.dict(os.environ, PATH=os.path.join(self.tmp.name, "empty")), \
                mock.patch.object(buildqd, "log") as log:
            self.drain(self.now + 60)
        (said,) = [c for c in log.call_args_list if "can't run gh" in str(c)]
        self.assertIn("isn't on PATH, in ~/.local/bin or in ~/bin", str(said))

    CI = {"GITHUB_ACTIONS": "true", "RUNNER_NAME": "fedora-kinflow-1"}

    def test_a_pass_in_a_github_actions_job_posts_nothing(self):
        t = self.submit(("cargo", "test"), source=SOURCE, env=self.CI)
        self.finish(t, 0)
        self.settle()
        self.drain(self.now + 60)
        self.assertTrue(t.source_verified)
        self.assertEqual((self.calls(), self.daemon.status_posts), ([], []))
        self.assertTrue(buildqd.Ticket.from_record(t.record(), t.dir).from_ci)  # kept across a restart

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
        saved = buildqd.read_json(os.path.join(self.state, "github-statuses.json"))
        self.assertEqual(saved["queue"][0]["tries"], 0)
        self.assertLessEqual(saved["queue"][0]["next"], push.ended)
        with mock.patch.object(buildqd, "log") as log:
            self.daemon.send_status_posts(first + buildqd.Daemon.STATUS_MISSING_MAX_AGE + 1)
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
            with mock.patch.object(buildqd, "log") as log:
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
        # Rate limits keep the full 24h retry window, not the 6h missing-commit window.
        post["next"] = first + buildqd.Daemon.STATUS_MAX_AGE + 100
        self.daemon.send_status_posts(first + buildqd.Daemon.STATUS_MISSING_MAX_AGE + 60)
        self.assertEqual(len(self.daemon.status_posts), 1)
        self.daemon.send_status_posts(first + buildqd.Daemon.STATUS_MAX_AGE + 1)
        self.assertEqual(self.daemon.status_posts, [])

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

        with mock.patch.object(buildqd.subprocess, "Popen", return_value=Running()):
            self.daemon.send_status_posts(post["first"] + 10)
        self.assertEqual(len(self.daemon.status_procs), 1)
        post["first"] -= buildqd.Daemon.STATUS_MAX_AGE + 100  # past max age while still in flight
        self.daemon.send_status_posts(post["first"] + buildqd.Daemon.STATUS_MAX_AGE + 120)
        self.assertIn(post, self.daemon.status_posts)
        self.daemon.status_procs.clear()


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


class CIFirst(DaemonCase):
    CI = {"GITHUB_ACTIONS": "true", "RUNNER_NAME": "filedrop-runner-2"}

    def setUp(self):
        super().setUp()
        self.daemon.cfg.lanes["heavy"].mem = GIB  # builds with no history count as 1G
        self.daemon.cfg.lanes["web"].mem = GIB
        self.machine.mem_available = 35 * GIB  # 25G free
        self.running = self.submit(mem="24G")  # leaves 1G

    def test_off_a_ci_build_waits_its_turn(self):
        waiting = self.submit(mem="20G")
        self.now += 301  # past starve_seconds (300 here): it starves
        ci = self.submit(("cargo", "test"), mem="20G", env=self.CI)
        self.assertEqual([t.id for t in self.daemon.queue(self.now)], [waiting.id, ci.id])
        self.assertFalse(ci.held)

    def test_off_the_order_is_as_before_for_a_lane_that_passes_held_room(self):
        older = self.submit(("pnpm", "lint"), lane="web", mem="20G", caller="one")
        self.daemon.history.add({"key": older.key, "t": self.now, "peak_mem": GIB, "peak_cores": 1.0,
                                 "wall": 600, "exit": 0})
        self.now += 301  # past starve_seconds, but a lane that passes held room never starves
        newer = self.submit(("pnpm", "test"), lane="web", mem="20G", caller="two")
        self.daemon.history.add({"key": newer.key, "t": self.now, "peak_mem": GIB, "peak_cores": 1.0,
                                 "wall": 10, "exit": 0})
        self.daemon.schedule(self.now)
        self.assertNotEqual(older.key, newer.key)
        self.assertEqual([t.id for t in self.daemon.queue(self.now)], [newer.id, older.id])

    def test_a_ci_build_goes_first_and_gets_room_held_at_once(self):
        self.daemon.cfg.ci_first = True
        waiting = self.submit(mem="20G")
        self.now += 301  # it starves, but at the same priority the CI build goes first
        ci = self.submit(("cargo", "test"), mem="20G", env=self.CI)
        self.assertEqual([t.id for t in self.daemon.queue(self.now)], [ci.id, waiting.id])
        self.assertTrue(ci.held)
        small = self.submit(("cargo", "check"), cores=1)  # 1G would fit, but the room is held
        self.assertEqual(self.states(ci, small), ["queued", "queued"])
        self.assertEqual(small.reason_cat, "held")
        web = self.submit(("pnpm", "build"), lane="web", cores=1)
        self.assertEqual(web.state, "running")  # its lane may still pass held room
        self.finish(self.running)
        self.daemon.schedule(self.now + 31)
        self.assertEqual(self.states(ci, waiting), ["running", "queued"])

    def test_a_ci_build_is_held_before_it_has_waited(self):
        self.daemon.cfg.ci_first = True
        ci = self.submit(("cargo", "test"), mem="20G", env=self.CI)
        self.assertEqual(ci.state, "queued")
        self.assertTrue(ci.held)

    def test_github_actions_set_by_hand_doesnt_go_first(self):
        self.daemon.cfg.ci_first = True
        by_hand = self.submit(("cargo", "test"), mem="20G", env={"GITHUB_ACTIONS": "true"})
        self.assertFalse(by_hand.held)

    def higher_lane(self):
        lane = self.daemon.cfg.lanes["web"]  # as a lane like dotnet: above heavy, doesn't pass held room
        lane.priority, lane.may_pass_held = 50, False
        return lane

    def test_a_higher_lane_may_use_a_ci_jobs_held_room(self):
        self.daemon.cfg.ci_first = True
        self.machine.mem_available = 36 * GIB  # 2G free beside the running build
        self.higher_lane()
        ci = self.submit(("cargo", "test"), mem="20G", env=self.CI)
        self.assertTrue(ci.held)
        higher = self.submit(("dotnet", "build"), lane="web", cores=1)
        self.assertEqual(higher.state, "running")  # the CI job's room binds its own priority and lower
        same = self.submit(("cargo", "check"), cores=1)
        self.assertEqual((same.state, same.reason),
                         ("queued", "the room it needs is held for a CI job's build, which goes first"))
        self.assertEqual(same.row(self.now)["behind"], 1)  # the CI job's hold binds its own priority (#62)

    def test_a_ci_build_that_has_waited_its_turn_holds_room_from_every_lane(self):
        self.daemon.cfg.ci_first = True
        self.higher_lane()
        ci = self.submit(("cargo", "test"), mem="20G", env=self.CI)
        self.now += 301  # past starve_seconds (300 here): now it starves like any other build
        higher = self.submit(("dotnet", "build"), lane="web", cores=1)
        self.assertTrue(ci.held)
        self.assertEqual((higher.state, higher.reason),
                         ("queued", "the room it needs is held for a build that has waited longer"))
        self.assertEqual(higher.row(self.now)["behind"], 1)  # the one long wait binds every lane (#62)
        self.assertEqual(higher.row(self.now)["held_for"], "memory")

    def test_a_higher_lane_still_goes_first_when_both_have_waited(self):
        self.daemon.cfg.ci_first = True
        self.higher_lane()
        ci = self.submit(("cargo", "test"), mem="20G", env=self.CI)
        higher = self.submit(("dotnet", "build"), lane="web", mem="20G")
        self.now += 301
        self.assertEqual([t.id for t in self.daemon.queue(self.now)], [higher.id, ci.id])

    def test_a_fresh_ci_build_goes_before_a_fresh_higher_lane_build(self):
        self.daemon.cfg.ci_first = True
        self.higher_lane()
        higher = self.submit(("dotnet", "build"), lane="web", mem="20G")
        ci = self.submit(("cargo", "test"), mem="20G", env=self.CI)
        self.assertEqual([t.id for t in self.daemon.queue(self.now)], [ci.id, higher.id])

    def test_a_higher_lane_build_isnt_kept_out_of_a_ci_jobs_held_workspace(self):
        self.daemon.cfg.ci_first = True
        lane = self.higher_lane()
        lane.exclusive, lane.workspace_group = True, "heavy"  # one build per workspace with heavy
        ci = self.submit(("cargo", "test"), mem="20G", env=self.CI, cwd=self.workdir("runner"))
        self.machine.mem_available = 36 * GIB
        higher = self.submit(("dotnet", "build"), lane="web", cores=1, cwd=self.workdir("runner"))
        self.assertTrue(ci.held)
        self.assertEqual(higher.state, "running")

    def test_a_session_build_that_has_waited_three_turns_goes_before_ci(self):
        self.daemon.cfg.ci_first = True
        session = self.submit(mem="20G")
        self.now += 3 * 300 - 1  # just under three times starve_seconds (300 here)
        ci = self.submit(("cargo", "test"), mem="20G", env=self.CI)
        self.assertEqual([t.id for t in self.daemon.queue(self.now)], [ci.id, session.id])
        self.now += 1
        self.assertEqual([t.id for t in self.daemon.queue(self.now)], [session.id, ci.id])
        self.daemon.schedule(self.now)
        self.assertTrue(session.held)

    def test_a_ci_build_that_has_waited_three_turns_goes_before_newer_higher_lane_ci(self):
        self.daemon.cfg.ci_first = True
        self.higher_lane()
        old = self.submit(("cargo", "test"), mem="20G", env=self.CI)
        self.now += 3 * 300 - 1  # just under three times starve_seconds (300 here)
        fresh = self.submit(("dotnet", "build"), lane="web", mem="20G", env=self.CI)
        self.assertEqual([t.id for t in self.daemon.queue(self.now)], [fresh.id, old.id])
        self.now += 1
        self.assertEqual([t.id for t in self.daemon.queue(self.now)], [old.id, fresh.id])
        self.finish(self.running)
        self.daemon.schedule(self.now + 31)
        self.assertEqual(self.states(old, fresh), ["running", "queued"])  # room for one: the older

    def test_a_ci_build_near_its_deadline_goes_before_overdue_builds(self):
        self.daemon.cfg.ci_first = True
        self.daemon.cfg.ci_deadline = 1200.0
        session = self.submit(mem="20G", caller="session")
        self.now += 1
        ci = self.submit(("cargo", "test"), mem="20G", env=self.CI, caller="runner")
        self.now += 3 * 300  # both overdue: the plain order, the older first
        self.assertEqual([t.id for t in self.daemon.queue(self.now)], [session.id, ci.id])
        self.now += 1200 - 3 * 300  # the CI job's build has waited ci_deadline: its job's limit is near
        self.assertEqual([t.id for t in self.daemon.queue(self.now)], [ci.id, session.id])
        self.daemon.schedule(self.now)
        self.assertTrue(ci.held)
        self.now += 12 * 300 - 1201  # the session's build has now waited twelve times starve_seconds
        self.assertEqual([x.id for x in self.daemon.queue(self.now)], [session.id, ci.id])  # it goes first again
        self.daemon.cfg.ci_deadline = 0.0  # off: as before
        self.assertEqual([t.id for t in self.daemon.queue(self.now)], [session.id, ci.id])
        self.daemon.cfg.ci_deadline = 1200.0
        self.finish(self.running)  # room for one: the long-queued build starts, ahead of the due CI build
        self.daemon.schedule(self.now + 31)
        self.assertEqual(self.states(session, ci), ["running", "queued"])
        self.assertEqual(buildqd.Config({"ci_first": True, "ci_deadline": "20m"}, 64 * GIB, 16).ci_deadline, 1200.0)
        with self.assertRaises(ValueError):
            buildqd.Config({"ci_deadline": "20m"}, 64 * GIB, 16)  # it needs ci_first
        with self.assertRaises(ValueError):
            buildqd.Config({"ci_first": True, "ci_deadline": "5m", "starve_seconds": 600}, 64 * GIB, 16)  # under starve
        with self.assertRaises(ValueError):
            buildqd.Config({"ci_first": True, "ci_deadline": "20m", "starve_seconds": 60}, 64 * GIB, 16)  # undone

    def test_due_ci_builds_go_oldest_first_whatever_their_lane(self):
        self.daemon.cfg.ci_first = True
        self.daemon.cfg.ci_deadline = 1200.0
        self.higher_lane()
        heavy = self.submit(("cargo", "test"), mem="20G", env=self.CI, caller="runner-1")
        self.now += 60
        higher = self.submit(("dotnet", "build"), lane="web", mem="20G", env=self.CI, caller="runner-2")
        self.now += 1200  # both past the deadline; the heavy one nearer its job's limit
        self.assertEqual([t.id for t in self.daemon.queue(self.now)][:2], [heavy.id, higher.id])

    def test_among_overdue_builds_ci_gets_no_preference(self):
        self.daemon.cfg.ci_first = True
        session = self.submit(mem="20G", caller="session")
        self.now += 1
        ci = self.submit(("cargo", "test"), mem="20G", env=self.CI, caller="runner")
        self.now += 3 * 300  # both have waited three turns: the plain order, the older first
        self.assertEqual([t.id for t in self.daemon.queue(self.now)], [session.id, ci.id])

    def test_a_ci_build_in_a_lane_that_passes_held_room_gets_no_head_start(self):
        self.daemon.cfg.ci_first = True
        self.daemon.cfg.lanes["web"].mem = 20 * GIB
        session = self.submit(("pnpm", "build"), lane="web", caller="session")
        self.now += 1
        ci = self.submit(("pnpm", "test"), lane="web", env=self.CI, caller="runner")
        self.assertEqual([t.id for t in self.daemon.queue(self.now)], [session.id, ci.id])

    def test_a_ci_jobs_weak_workspace_hold_doesnt_replace_a_waited_out_builds(self):
        self.daemon.cfg.ci_first = True
        lane = self.higher_lane()  # as a cargo check lane: priority above heavy, in heavy's group
        lane.exclusive, lane.workspace_group = True, "heavy"
        tree = self.workdir("tree")
        session = self.submit(("cargo", "test", "-p", "a"), mem="20G", cwd=tree)
        self.now += 301  # it has waited out its turn
        ci = self.submit(("cargo", "test", "-p", "b"), mem="20G", env=self.CI, cwd=tree)
        self.assertEqual([t.id for t in self.daemon.queue(self.now)], [ci.id, session.id])
        self.assertTrue(session.held)  # kept out of the worktree by the CI job, it still holds
        check = self.submit(("cargo", "check"), lane="web", cores=1, cwd=tree)
        self.assertEqual((check.state, check.reason_cat), ("queued", "workspace"))
        self.assertIn("has waited longest", check.reason)

    def test_a_build_that_could_never_fit_holds_nothing_behind_a_ci_workspace_hold(self):
        self.daemon.cfg.ci_first = True
        tree = self.workdir("tree")
        huge = self.submit(("cargo", "test", "-p", "a"), mem="100G", cwd=tree)
        self.now += 301
        self.submit(("cargo", "test", "-p", "b"), mem="20G", env=self.CI, cwd=tree)
        self.assertFalse(huge.held)

    def test_a_build_stopped_to_free_memory_goes_before_ci(self):
        self.daemon.cfg.ci_first = True
        stopped = self.submit(mem="20G")
        stopped.requeues = 1
        ci = self.submit(("cargo", "test"), mem="20G", env=self.CI)
        self.assertEqual([t.id for t in self.daemon.queue(self.now)], [stopped.id, ci.id])

    def test_a_ci_build_that_has_waited_goes_before_newer_quicker_ci_builds(self):
        self.daemon.cfg.ci_first = True
        old = self.submit(("cargo", "test", "-p", "long"), mem="20G", env=self.CI)
        self.daemon.history.add({"key": old.key, "t": self.now, "peak_mem": GIB, "peak_cores": 1.0,
                                 "wall": 3000, "exit": 0})
        self.now += 601
        quick = self.submit(("cargo", "test", "-p", "short"), mem="20G", env=self.CI)
        self.daemon.history.add({"key": quick.key, "t": self.now, "peak_mem": GIB, "peak_cores": 1.0,
                                 "wall": 10, "exit": 0})
        self.daemon.schedule(self.now)
        self.assertEqual([t.id for t in self.daemon.queue(self.now)], [old.id, quick.id])
        self.finish(self.running)
        self.daemon.schedule(self.now + 31)
        self.assertEqual(self.states(old, quick), ["running", "queued"])  # room for one: the older

    def test_a_ci_build_in_a_lane_that_passes_held_room_isnt_held(self):
        self.daemon.cfg.ci_first = True
        self.daemon.cfg.lanes["web"].mem = 20 * GIB
        ci = self.submit(("pnpm", "test"), lane="web", env=self.CI)
        self.assertEqual(ci.state, "queued")
        self.assertFalse(ci.held)

    def test_a_ci_build_that_could_never_fit_holds_nothing(self):
        self.daemon.cfg.ci_first = True
        huge = self.submit(("cargo", "test"), mem="100G", env=self.CI)
        small = self.submit(("cargo", "check"), cores=1)
        self.assertFalse(huge.held)
        self.assertEqual(small.state, "running")

    def test_the_setting_is_read_and_shown(self):
        cfg = buildqd.Config({"ci_first": True}, 64 * GIB, 16)
        self.assertTrue(cfg.ci_first)
        self.assertTrue(cfg.summary()["ci_first"])
        self.assertFalse(buildqd.Config({}, 64 * GIB, 16).ci_first)
        with self.assertRaises(ValueError):
            buildqd.Config({"ci_first": "sometimes"}, 64 * GIB, 16)


class MakeRoomForCI(DaemonCase):
    """make_room_for_ci: a CI job's build that has waited for memory our builds hold stops some."""
    CI = {"GITHUB_ACTIONS": "true", "RUNNER_NAME": "filedrop-runner-2"}

    def setUp(self):
        super().setUp()
        self.daemon.cfg.ci_first = True
        self.daemon.cfg.make_room_for_ci = 300.0
        self.daemon.cfg.lanes["heavy"].mem = GIB  # builds with no history count as 1G
        self.daemon.cfg.lanes["web"].mem = GIB
        self.machine.mem_available = 45 * GIB  # 35G above the 10G floor

    def agents(self, *sizes, argv=("cargo", "build"), lane=None):
        """Agent builds running, one started after another, each using all it planned."""
        builds = []
        for i, size in enumerate(sizes):
            t = self.submit(argv, lane=lane, mem=f"{size}G", caller=f"agent-{i}", cwd=self.workdir())
            self.assertEqual(t.state, "running")
            t.started = self.now + i
            builds.append(t)
        for t in builds:
            t.mem_now = t.est_mem
        self.machine.mem_available = 45 * GIB - sum(t.mem_now for t in builds)
        return builds

    def ci_waiting(self, mem="16G"):
        ci = self.submit(("cargo", "test"), mem=mem, env=self.CI)
        self.assertEqual((ci.state, ci.reason_cat), ("queued", "memory"))
        return ci

    def tick(self, after):
        self.daemon.schedule(self.now + after)
        self.daemon.make_room_for_ci(self.now + after)

    def test_after_its_wait_the_newest_build_that_covers_the_shortfall_is_stopped_and_yields(self):
        old, new = self.agents(12, 12)  # 11G free
        ci = self.ci_waiting()  # lacks 5G
        self.tick(100)
        self.assertEqual(self.runner.forced, [])  # not before it has waited make_room_for_ci
        self.tick(301)
        self.assertEqual(self.runner.forced, [new.id])  # the newest: the least work lost
        self.assertEqual((new.requeue_pending, new.ci_stops, new.yields_to), (True, 0, ci.id))  # counted once gone
        self.tick(302)
        self.assertEqual(len(self.runner.forced), 1)  # nothing more while it is being stopped
        self.runner.running.discard(new.id)  # it is gone
        self.daemon.check(self.now + 303)
        self.assertEqual((new.state, new.shed_error, new.ci_stops), ("queued", None, 1))  # queued again, not failed
        self.assertIn("CI job's build", new.reason)
        with open(new.log) as f:
            self.assertIn("to make room for a CI job's build", f.read())
        kept = buildqd.Ticket.from_record(json.loads(json.dumps(new.record())), new.dir)
        self.assertEqual((kept.ci_stops, kept.yields_to, kept.yield_until), (1, ci.id, new.yield_until))
        self.assertGreater(kept.yield_until, self.now + 303)
        self.tick(304)  # its memory isn't free yet
        self.assertEqual([t.id for t in self.daemon.queue(self.now + 304)], [ci.id, new.id])  # it yields
        self.assertEqual((ci.held, new.held, new.state), (True, False, "queued"))  # and holds nothing
        self.assertFalse(self.daemon.waited_out(new, self.now + 304))
        self.machine.mem_available += 12 * GIB  # now it is
        self.tick(305)
        self.assertEqual((ci.state, new.state), ("running", "queued"))  # the CI job's build gets the room
        self.assertTrue(self.daemon.waited_out(new, self.now + 305))  # then it goes first, as a stopped build does
        self.assertEqual(len(self.runner.forced), 1)
        self.assertEqual(old.state, "running")

    def test_the_newest_few_are_stopped_when_no_one_covers_it(self):
        a, b, c = self.agents(5, 4, 4)
        self.machine.mem_available = 14 * GIB  # other work holds the rest: 4G free
        self.ci_waiting("10G")  # lacks 6G: no one build gives that; the newest two do
        self.tick(301)
        self.assertEqual(self.runner.forced, [c.id, b.id])
        self.assertEqual(a.state, "running")

    def test_past_its_deadline_builds_are_stopped_even_for_part_of_it(self):
        self.agents(6, argv=("bash", "run.sh"))  # a script: it can't be started over
        self.agents(2, 2)
        self.machine.mem_available = 21 * GIB  # 11G free; ours use 10G, 6G of it the script's
        self.ci_waiting("16G")  # lacks 5G: the two that can start over give only 4G
        self.tick(301)
        self.assertEqual(self.runner.forced, [])  # not yet: it would be work lost for nothing
        self.daemon.cfg.ci_deadline = 1200.0
        self.tick(1201)
        self.assertEqual(len(self.runner.forced), 2)  # near its job's limit: what they free is held for it

    def test_past_its_deadline_builds_are_stopped_for_part_of_it_once(self):
        self.daemon.cfg.ci_deadline = 1200.0
        self.agents(6, argv=("bash", "run.sh"))
        self.agents(2, 2)
        self.machine.mem_available = 21 * GIB
        ci = self.ci_waiting("16G")
        self.daemon.partly_made.add(ci.id)  # builds were stopped for part of it once already, and the room went
        self.tick(1201)
        self.assertEqual(self.runner.forced, [])  # no more work lost for nothing

    def test_past_its_deadline_a_stop_must_free_at_least_half_of_what_it_lacks(self):
        self.daemon.cfg.ci_deadline = 1200.0
        self.agents(6, argv=("bash", "run.sh"))  # a script: it can't be started over
        self.agents(1)
        self.machine.mem_available = 18 * GIB  # 8G free; ours use 7G, 6G of it the script's
        self.ci_waiting("13G")  # lacks 5G: the one that can start over gives 1G, under half
        self.tick(1201)
        self.assertEqual(self.runner.forced, [])

    def test_nothing_is_stopped_when_stopping_all_would_not_make_it_fit(self):
        self.agents(2, 2)
        self.machine.mem_available = 15 * GIB  # others hold the rest: 5G free, 4G more from ours
        self.ci_waiting("12G")  # lacks 7G
        self.tick(301)
        self.assertEqual(self.runner.forced, [])

    def test_scripts_ci_builds_higher_lanes_and_twice_stopped_builds_are_left_alone(self):
        script = self.submit(("bash", "build.sh"), mem="12G", caller="s", cwd=self.workdir())
        other_ci = self.submit(("cargo", "test", "-p", "x"), mem="6G", env=self.CI, cwd=self.workdir())
        self.daemon.cfg.lanes["web"].priority = 100
        web = self.submit(("pnpm", "build"), lane="web", mem="6G", caller="w", cwd=self.workdir())
        twice = self.submit(("cargo", "build"), mem="8G", caller="t", cwd=self.workdir())
        twice.ci_stops = 2
        for i, t in enumerate((script, other_ci, web, twice)):
            self.assertEqual(t.state, "running")
            t.started, t.mem_now = self.now + i, t.est_mem
        self.machine.mem_available = 45 * GIB - 32 * GIB  # 3G free
        self.ci_waiting("10G")
        self.tick(301)
        self.assertEqual(self.runner.forced, [])

    def test_off_by_default_and_only_with_ci_first(self):
        self.daemon.cfg.make_room_for_ci = 0.0
        self.agents(12, 12)
        self.ci_waiting()
        self.tick(3600)
        self.assertEqual(self.runner.forced, [])
        self.assertEqual(buildqd.Config({}, 64 * GIB, 16).make_room_for_ci, 0.0)
        cfg = buildqd.Config({"ci_first": True, "make_room_for_ci": "5m"}, 64 * GIB, 16)
        self.assertEqual((cfg.make_room_for_ci, cfg.summary()["make_room_for_ci"]), (300.0, 300.0))
        with self.assertRaises(ValueError):
            buildqd.Config({"make_room_for_ci": "5m"}, 64 * GIB, 16)  # its CI build would hold no room
        with self.assertRaises(ValueError):
            buildqd.Config({"ci_first": True, "make_room_for_ci": "soon"}, 64 * GIB, 16)

    def test_a_stopped_build_in_a_lane_that_passes_held_room_still_waits_for_its_ci_build(self):
        self.daemon.cfg.lanes["web"].mem = 12 * GIB
        (web,) = self.agents(12, argv=("vitest", "run"), lane="web")  # it may pass held room
        self.daemon.cfg.lanes["web"].priority = 0  # (so the CI build's priority covers it)
        self.machine.mem_available = 45 * GIB - 12 * GIB - 9 * GIB  # others hold 9G: 14G free
        ci = self.ci_waiting()  # lacks 2G
        self.tick(301)
        self.assertEqual(self.runner.forced, [web.id])
        self.runner.running.discard(web.id)
        self.daemon.check(self.now + 302)
        self.machine.mem_available += 12 * GIB - 11 * GIB  # freed, but other work grew by 11G: 15G free
        self.tick(303)
        self.assertEqual((ci.state, web.state), ("queued", "queued"))  # it doesn't take the room back
        self.assertIn("make room for a CI job's build", web.reason)
        self.tick(303 + self.daemon.YIELD_MAX)  # a CI build that can't start doesn't keep it for ever
        self.assertEqual(web.state, "running")

    def test_a_ci_build_in_a_lane_that_passes_held_room_stops_nothing(self):
        self.agents(12, 12)
        ci = self.submit(("vitest", "run"), lane="web", mem="16G", env=self.CI)
        self.assertEqual(ci.state, "queued")
        self.tick(301)
        self.assertEqual(self.runner.forced, [])  # it holds no room: what is freed could go elsewhere

    def test_the_shortfall_is_checked_again_after_the_pass(self):
        self.daemon.cfg.lanes["web"].priority = 100
        self.agents(12)  # 23G free
        self.machine.mem_available = 45 * GIB - 12 * GIB - 10 * GIB  # others hold 10G: 13G free
        ci = self.ci_waiting()  # lacks 3G: stopping the 12G build would do
        self.now += 301
        self.daemon.paused = {"reason": "test"}  # so the next build starts in the same pass as ci is looked at
        web = self.submit(("vitest", "run"), lane="web", mem="12G", caller="w")
        self.daemon.paused = None
        self.tick(0)  # ci first (lacking 3G), then the higher lane passes its hold and takes 12G
        self.assertEqual((ci.state, web.state), ("queued", "running"))
        self.assertEqual(self.runner.forced, [])  # it lacks 15G now: stopping the 12G build wouldn't do

    def test_what_a_build_may_still_grow_into_counts_and_restartable_scripts_may_be_stopped(self):
        (script,) = self.agents(12, argv=("bash", "build.sh"))
        script.restartable = True  # submitted with --restartable
        script.mem_now = 4 * GIB  # it may still grow 8G, which admission holds for it
        self.machine.mem_available = 45 * GIB - 4 * GIB - 21 * GIB  # others hold 21G: 12G free, 8G of it held
        self.ci_waiting("10G")  # lacks 6G; the script gives back its 4G and the 8G held for it
        self.tick(301)
        self.assertEqual(self.runner.forced, [script.id])

    def test_a_build_whose_environment_is_gone_is_left_alone(self):
        (t,) = self.agents(12)
        t.env = None  # picked up after a daemon restart: it couldn't be queued again
        self.machine.mem_available = 45 * GIB - 12 * GIB - 10 * GIB  # others hold 10G: 13G free
        self.ci_waiting("16G")
        self.tick(301)
        self.assertEqual(self.runner.forced, [])

    def test_a_build_that_finishes_before_it_is_stopped_isnt_counted(self):
        old, new = self.agents(12, 12)
        self.ci_waiting()
        self.tick(301)
        self.assertEqual(self.runner.forced, [new.id])
        self.finish(new, after=302)  # it ended by itself first
        self.assertEqual((new.state, new.ci_stops, new.requeues), ("succeeded", 0, 0))

    def test_a_ci_build_that_starts_before_the_stopped_build_is_gone(self):
        old, new = self.agents(12, 12)
        ci = self.ci_waiting()
        self.tick(301)
        self.machine.mem_available += 6 * GIB  # other work ended: it fits without the stop
        self.tick(302)
        self.assertEqual(ci.state, "running")
        self.runner.running.discard(new.id)
        self.daemon.check(self.now + 303)
        self.assertEqual((new.state, new.ci_stops, new.yields_to, new.yield_until), ("queued", 1, None, None))
        self.assertIn("no longer needs it", new.reason)
        with open(new.log) as f:
            self.assertIn("to make room for a CI job's build", f.read())
        self.assertTrue(self.daemon.waited_out(new, self.now + 303))  # it goes first now

    def test_a_build_slow_to_die_isnt_followed_by_more_stops(self):
        old, new = self.agents(12, 12)
        self.ci_waiting()
        self.tick(301)
        self.assertEqual(self.runner.forced, [new.id])
        self.tick(301 + self.daemon.ROOM_PAUSE + 60)  # still alive: what it frees isn't free yet
        self.assertEqual(set(self.runner.forced), {new.id})
        self.assertEqual(old.state, "running")

    def test_a_ci_build_cancelled_before_the_stopped_build_is_gone(self):
        old, new = self.agents(12, 12)
        ci = self.ci_waiting()
        self.tick(301)
        self.daemon.cancel(ci, self.now + 302)
        self.runner.running.discard(new.id)
        self.daemon.check(self.now + 303)
        self.assertEqual((new.state, new.ci_stops, new.yields_to), ("queued", 1, None))
        self.assertIn("no longer needs it", new.reason)
        del old

    def test_one_ci_build_at_a_time(self):
        a, b, c = self.agents(6, 6, 6)  # 17G free
        self.machine.mem_available = 45 * GIB - 18 * GIB - 15 * GIB  # others hold 15G: 2G free
        first = self.ci_waiting("6G")  # lacks 4G
        second = self.submit(("cargo", "test", "-p", "other"), mem="6G", env=self.CI, cwd=self.workdir())
        self.tick(301)
        self.assertEqual(self.runner.forced, [c.id])
        self.runner.running.discard(c.id)
        self.daemon.check(self.now + 302)  # gone, but what it frees isn't seen yet
        self.tick(303)
        self.assertEqual((first.state, second.state), ("queued", "queued"))
        self.assertEqual(self.runner.forced, [c.id])  # the second waits until the first has started
        del a, b

    def test_a_listed_script_may_be_stopped_for_ci(self):
        self.daemon.cfg.restartable_scripts = frozenset({"wrap.sh"})
        (old, new) = self.agents(12, 12, argv=("bash", "wrap.sh", "tests"))
        self.assertFalse(new.restartable)
        self.ci_waiting()
        self.tick(301)
        self.assertEqual(self.runner.forced, [new.id])
        self.daemon.cfg.restartable_scripts = frozenset()

    def behind_an_agent_build_that_waited(self):
        """Two 12G builds running (11G free); a 20G agent build that has waited three times
        starve_seconds (overdue: it goes before CI builds) holds its room; then a CI build arrives
        needing 8G, which lacks it only because of that hold."""
        old, new = self.agents(12, 12)
        held = self.submit(("cargo", "test", "-p", "big"), mem="20G", caller="patient", cwd=self.workdir())
        self.tick(901)  # past three times starve_seconds: overdue, and it holds its room
        self.assertTrue(held.held)
        self.now += 901
        ci = self.submit(("cargo", "test"), mem="8G", env=self.CI)
        self.assertEqual((ci.state, ci.reason_cat), ("queued", "held"))
        return old, new, held, ci

    def test_a_ci_build_behind_agents_builds_that_waited_longer_may_stop_builds(self):
        self.behind_an_agent_build_that_waited()
        self.tick(301)
        self.assertTrue(self.runner.forced)  # by default: room for the held build and the CI one

    def test_make_room_behind_agents_off_leaves_them_their_turn(self):
        self.daemon.cfg.make_room_behind_agents = False
        old, new, held, ci = self.behind_an_agent_build_that_waited()
        self.tick(301)
        self.assertEqual(self.runner.forced, [])  # it waits behind them, as before make_room_for_ci (#38)
        self.assertGreater(ci.short_agents, 0)

    def test_a_build_that_has_run_long_is_spared(self):
        self.daemon.cfg.make_room_spare_after = 200.0
        old, new = self.agents(12, 12)
        self.ci_waiting()
        self.tick(301)  # both have run about 300 s: past 200, so neither is stopped
        self.assertEqual(self.runner.forced, [])
        self.daemon.cfg.make_room_spare_after = 400.0
        self.daemon.room_made.clear()
        self.tick(302)
        self.assertEqual(self.runner.forced, [new.id])  # under 400: the newest is stopped as before

    def test_time_paused_isnt_time_run(self):
        self.daemon.cfg.make_room_spare_after = 200.0
        old, new = self.agents(12, 12)
        for t in (old, new):
            t.frozen, t.frozen_at = True, self.now + 50  # paused since 50 s in: about 50 s of real work
        self.ci_waiting()
        self.tick(301)
        self.assertEqual(self.runner.forced, [new.id])  # not spared: a paused build hasn't run all that time

    def test_with_behind_agents_off_a_build_that_went_first_isnt_stopped_once_it_runs(self):
        self.daemon.cfg.make_room_behind_agents = False
        old, new = self.agents(12, 12)
        new.requeues = 1  # it had been stopped once, so it went before CI jobs' builds when it started
        self.ci_waiting()
        self.tick(301)
        self.assertEqual(self.runner.forced, [old.id])  # the other one, though older

    def test_no_room_is_made_while_a_cancelled_build_gives_its_memory_back(self):
        old, new = self.agents(12, 12)
        self.ci_waiting()
        self.daemon.cancel(old, self.now + 300)  # a CI job past its time limit, say: its build is stopping
        self.tick(301)
        self.assertNotIn(new.id, self.runner.forced)  # what the cancelled build frees may be enough
        self.runner.running.discard(old.id)  # it is gone, but the CI build still doesn't fit
        self.daemon.check(self.now + 302)
        self.assertEqual(old.state, "cancelled")
        self.tick(303)
        self.assertIn(new.id, self.runner.forced)  # the guard opens again

    def test_memory_is_read_again_before_making_room(self):
        old, new = self.agents(12, 12)
        ci = self.ci_waiting()
        self.daemon.schedule(self.now + 301)  # (the tick's own reading: the CI build doesn't fit)
        self.machine.read_available = lambda: 45 * GIB  # a build ended since: its memory is back
        self.daemon.make_room_for_ci(self.now + 301)
        self.assertEqual(self.runner.forced, [])  # so nothing is stopped for it
        self.assertEqual(ci.state, "queued")  # (it starts on the next pass)

    def test_builds_ending_every_second_dont_hold_making_room_back(self):
        old, new = self.agents(12, 12)
        self.ci_waiting()
        quick = [self.submit(("vitest", "run", f"q{i}"), lane="web", mem="1G", cores=1, caller=f"q{i}",
                             cwd=self.workdir()) for i in range(5)]  # (a lane that may pass the CI build's held room)
        self.assertEqual({q.state for q in quick}, {"running"})
        for i, q in enumerate(quick):  # one ends in each tick, just before room would be made
            self.finish(q, after=301 + i)
            self.tick(301 + i)
        self.assertEqual(self.runner.forced, [new.id])  # made all the same, in the first of those ticks

    def test_a_build_that_waited_three_turns_is_protected_once_it_runs(self):
        self.daemon.cfg.make_room_behind_agents = False
        old, new = self.agents(12, 12)
        new.submitted = new.started - 3 * self.daemon.cfg.starve_seconds  # it started after a long wait
        self.ci_waiting()
        self.tick(301)
        self.assertEqual(self.runner.forced, [old.id])

    def test_a_lane_that_passes_held_room_has_no_overdue_builds(self):
        self.daemon.cfg.lanes["web"].mem = 12 * GIB
        (web,) = self.agents(12, argv=("vitest", "run"), lane="web")
        web.requeues = 1
        self.assertFalse(self.daemon.was_overdue(web))  # the queue gives it no head start, so no protection

    def test_the_make_room_options_are_read(self):
        cfg = buildqd.Config({}, 64 * GIB, 16)
        self.assertEqual((cfg.make_room_behind_agents, cfg.make_room_spare_after), (True, 0.0))
        cfg = buildqd.Config({"ci_first": True, "make_room_for_ci": "5m", "make_room_behind_agents": False,
                              "make_room_spare_after": "20m"}, 64 * GIB, 16)
        self.assertEqual((cfg.make_room_behind_agents, cfg.make_room_spare_after), (False, 1200.0))
        self.assertEqual(cfg.summary()["make_room_spare_after"], 1200.0)
        with self.assertRaises(ValueError):
            buildqd.Config({"make_room_spare_after": "soon"}, 64 * GIB, 16)

    def test_stops_for_ci_dont_count_toward_the_limit_of_three(self):
        (t,) = self.agents(12)
        t.requeues, t.ci_stops = 3, 1
        self.daemon.shed(t, self.now)
        self.assertIsNone(t.shed_error)
        (u,) = self.agents(12)
        u.requeues = 3
        self.daemon.shed(u, self.now)
        self.assertIn("fourth time", u.shed_error)


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

    def test_requeue_clears_exec_argv(self):
        t = self.submit(("make", "-j8", "all"))
        self.assertEqual(t.exec_argv, ["make", "all"])
        self.daemon.requeue(t, self.now + 10)
        self.assertIsNone(t.exec_argv)
        self.daemon.cfg.lanes[t.lane].jobserver = "off"
        self.daemon.schedule(self.now + 11)
        self.assertEqual((t.state, t.exec_argv), ("running", None))
        # Also cleared on the check_starting() recovery path and defensively in start().
        t2 = self.submit(("make", "-j8", "all"), cwd=self.workdir("two"))
        t2.state, t2.launcher, t2.exec_argv = "starting", None, ["make", "all"]
        self.runner.running.discard(t2.id)
        self.runner.unit_state = lambda ticket: "gone"
        self.daemon.check_starting(t2, self.now + 20)
        self.assertEqual((t2.state, t2.exec_argv), ("queued", None))
        t2.exec_argv = ["make", "all"]  # stale on disk from an older daemon version
        self.daemon.schedule(self.now + 21)
        self.assertEqual((t2.state, t2.exec_argv), ("running", None))

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
        self.assertEqual(buildqd.with_make_flags("a -- X=1", "-j2"), "a -j2 -- X=1")
        self.assertEqual(buildqd.with_make_flags("", "-j2"), "-j2")

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
        self.assertEqual(buildqd.without_fd_jobserver(flags), "s -- SUBMAKE=make\\ -j4 TAB=a\\\tb")
        self.assertEqual(buildqd.with_make_flags("s -- X=make\\ -j4", "-j2"), "s -j2 -- X=make\\ -j4")
        self.assertEqual(buildqd.with_make_flags("-- X=1", "-j2"), "-j2 -- X=1")
        self.assertIsNone(buildqd.without_fd_jobserver("-- X=--jobserver-auth=3,4"))  # a value, not an option
        self.assertEqual(buildqd.split_make_flags("-I a\\ -- -- X=1"), ("-I a\\ --", "X=1"))  # an escaped space
        self.assertEqual(buildqd.split_make_flags("-I a\\\\ -- X=1"), ("-I a\\\\", "X=1"))  # an escaped backslash
        self.assertEqual(buildqd.split_make_flags("s --"), ("s", ""))
        self.assertEqual(buildqd.split_make_flags("--foo X=a--"), ("--foo X=a--", None))

    def test_a_j_in_a_kept_assignment_doesnt_stop_buildqs_jobserver(self):
        for value in ("SUBMAKE=make\\ -j4", "X=--jobserver-auth=fifo:/x"):
            t = self.submit(("make", "all"), env={"MAKEFLAGS": f"s -j4 --jobserver-auth=3,4 -- {value}"})
            self.assertIsNotNone(t.jobserver)  # the caller's make ran in parallel; the -j is a value's
            with open(os.path.join(t.dir, "parallel.json")) as f:
                flags = json.load(f)["MAKEFLAGS"]
            self.assertEqual(flags, f"s -j{t.want} --jobserver-auth=fifo:{t.jobserver} -- {value}")

    def test_escaped_whitespace_in_an_option_is_kept_as_written(self):
        # make writes a directory ending in a space as `-Ia\ `, one with a tab in it as `-Ia\<tab>b` (#37)
        self.assertEqual(buildqd.without_fd_jobserver("s -j4 --jobserver-auth=3,4 -Ia\\  -- X=1"), "s -Ia\\  -- X=1")
        self.assertEqual(buildqd.without_fd_jobserver("s --jobserver-auth=3,4 -Ia\\\tb"), "s -Ia\\\tb")
        self.assertEqual(buildqd.with_make_flags("s -Ia\\ ", "-j2"), "s -Ia\\  -j2")
        self.assertEqual(buildqd.make_words("a\\\\ b"), ["a\\\\", "b"])  # an escaped backslash, then a split

    def test_make_s_own_word_rules_in_the_corners(self):
        self.assertEqual(buildqd.make_words("s -Ia\\"), ["s", "-Ia\\"])  # a lone backslash at the end stays
        self.assertEqual(buildqd.with_make_flags("s -Ia\\", "-j2"), "s -Ia\\\\ -j2")  # and can't escape what follows
        self.assertEqual(buildqd.make_words("s -Ia\nb"), ["s", "-Ia\nb"])  # only spaces and tabs split
        self.assertEqual(buildqd.without_fd_jobserver("s -j 8 --jobserver-auth=3,4"), "s")  # -j's count goes with it
        self.assertFalse(buildqd.callers_jobs("s -I -jx"))  # a folder called -jx
        self.assertTrue(buildqd.callers_jobs("s --jobs=3"))
        self.assertTrue(buildqd.callers_jobs("s \\--jobserver-auth=fifo:/x"))  # make takes the backslash out

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
        self.assertEqual(buildqd.Config({}, 64 * GIB, 16).scale_mem_with_cores, "always")
        self.assertEqual(buildqd.Config({"scale_mem_with_cores": True}, 64 * GIB, 16).scale_mem_with_cores, "always")
        self.assertEqual(buildqd.Config({"scale_mem_with_cores": False}, 64 * GIB, 16).scale_mem_with_cores, "never")
        for wrong in ("cold", "sometimes"):
            with self.assertRaises(ValueError):
                buildqd.Config({"scale_mem_with_cores": wrong}, 64 * GIB, 16)

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
        with mock.patch.object(buildqd.time, "monotonic", side_effect=lambda: start + len(ticks)):
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
            with mock.patch.object(buildqd.time, "monotonic", return_value=start + 100 + len(ticks) * 0.0):
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
        restarted = buildqd.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                                     queries=buildqd.InlineExecutor())
        restarted.load_tickets(self.now)
        t, _ = restarted.submit({"argv": ["cargo", "build"], "cwd": cwd, "env": {}, "submission": "abc"}, self.now)
        self.assertEqual(t.id, first.id)
        for ticket in restarted.tickets.values():
            restarted.close_jobserver(ticket)

    def test_a_ticket_keeps_the_runner_it_started_under(self):
        t = self.submit()
        t.runner = "direct"
        self.daemon.save(t)
        again = buildqd.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                                     queries=buildqd.InlineExecutor())
        again.load_tickets(self.now)
        self.assertEqual(again.tickets[t.id].state, "running")  # not given up for a runner change
        for ticket in again.tickets.values():
            again.close_jobserver(ticket)

    def test_a_cancel_goes_on_after_a_restart(self):
        t = self.submit()
        self.daemon.cancel(t, self.now)
        self.runner.stopped.clear()
        again = buildqd.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                                     queries=buildqd.InlineExecutor())
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
        again = buildqd.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                                     queries=buildqd.InlineExecutor())
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
        restarted = buildqd.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                                     queries=buildqd.InlineExecutor())
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
        again = buildqd.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                                     queries=buildqd.InlineExecutor())
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


class ShimRules(unittest.TestCase):
    """#72: what `buildq run` runs at once instead of queueing, by the shim's own rule."""

    shim = load("buildq-shim")

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
        self.assertEqual(self.shim.build_then_run("dotnet", ["run", "--project=app.csproj", "--no-restore"]),
                         ["build", "app.csproj"])


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
        self.assertEqual(self.words("python3", "-m", "pytest", "-x"), "python3 -m pytest")  # (#49)
        self.assertEqual(self.words("python3", "script.py", "-m", "slow"), "python3 script.py")  # the script's own -m
        self.assertEqual(self.words("python3", "-X", "dev", "-W", "error", "-m", "pytest"), "python3 -m pytest")

    def test_a_labelled_scripts_label_tells_its_builds_apart(self):
        labels = frozenset({"persistent-cargo-target.sh", "gen.py"})
        wrapper = ("bash", "scripts/ci/persistent-cargo-target.sh")

        def words(*argv):
            return " ".join(buildqd.command_words(list(argv), labels))
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
        self.assertEqual(" ".join(buildqd.command_words(["node", "--require", "preload.js", "hunter"],
                                                        frozenset({"preload.js"}))),
                         self.words("node", "--require", "preload.js", "hunter"))
        # a script's own -m is neither a module nor a label
        self.assertEqual(words(*wrapper, "architecture", "-m", "fast"), "bash persistent-cargo-target.sh architecture")
        # its own -c is its argument, not a command string
        self.assertEqual(words(*wrapper, "architecture", "-c", "debug"), "bash persistent-cargo-target.sh architecture")
        # an option before the script turns the label off
        self.assertEqual(words("bash", "-e", *wrapper[1:], "architecture"), "bash persistent-cargo-target.sh")

    def test_labelled_scripts_are_configured(self):
        cfg = buildqd.Config({"script_labels": ["persistent-cargo-target.sh"]}, 64 * GIB, 16)
        self.assertEqual(cfg.script_labels, frozenset({"persistent-cargo-target.sh"}))
        self.assertEqual(cfg.summary()["script_labels"], ["persistent-cargo-target.sh"])
        self.assertEqual(buildqd.Config({}, 64 * GIB, 16).script_labels, frozenset())
        ruled = buildqd.Config(buildqd.tomllib.loads('script_labels = ["wrap.sh"]\n[[lane_rules]]\n'
                                                     'match = "^bash wrap.sh architecture$"\nlane = "small"\n'
                                                     '[lanes.default]\n[lanes.small]\n'), 64 * GIB, 16)
        self.assertEqual(ruled.lane_for(["bash", "wrap.sh", "architecture"]), "small")  # rules see the label
        self.assertEqual(ruled.lane_for(["bash", "wrap.sh", "rust-tests"]), "default")
        for bad in ("x.sh", ["a/b.sh"], [3]):
            with self.assertRaises(ValueError):
                buildqd.Config({"script_labels": bad}, 64 * GIB, 16)

    def test_a_listed_package_script_keeps_its_label(self):
        cfg = buildqd.Config(buildqd.tomllib.loads(
            'script_labels = ["verify:push"]\n'
            '[[lane_rules]]\nmatch = "^(pnpm|npm|yarn) .*(verify:push|verify-push) docs\\\\b"\nlane = "light"\n'
            '[[lane_rules]]\nmatch = "^(pnpm|npm|yarn) .*(verify:push|verify-push)"\nlane = "heavy"\n'
            '[lanes.default]\n[lanes.light]\n[lanes.heavy]\n'), 64 * GIB, 16)
        self.assertEqual(cfg.lane_for(["pnpm", "run", "verify:push", "docs", "origin/main", "HEAD"]), "light")
        self.assertEqual(cfg.lane_for(["/usr/bin/pnpm", "verify:push", "docs"]), "light")
        self.assertEqual(cfg.lane_for(["pnpm", "run", "verify:push", "api", "a", "b"]), "heavy")
        self.assertEqual(cfg.lane_for(["pnpm", "run", "verify:push", "documentation"]), "heavy")
        self.assertEqual(buildqd.command_words(["pnpm", "run", "verify:push", "full", "a"], cfg.script_labels),
                         ["pnpm", "verify:push", "full"])  # its own history, too
        self.assertEqual(buildqd.command_words(["pnpm", "run", "verify:push", "docs"], frozenset()),
                         ["pnpm", "verify:push"])  # unlisted: as before
        self.assertEqual(buildqd.command_words(["pnpm", "run", "verify:push", "abc123"], cfg.script_labels),
                         ["pnpm", "verify:push"])  # only a plain lowercase word
        self.assertEqual(buildqd.command_words(["pnpm", "run", "verify:push", "--reporter", "dot", "docs"], cfg.script_labels),
                         ["pnpm", "verify:push"])  # not an option's value
        self.assertEqual(cfg.lane_for(["pnpm", "--filter", "web", "verify:push", "docs"]), "light")
        listed = frozenset({"build"})
        self.assertEqual(buildqd.command_words(["pnpm", "--filter", "build", "build", "docs"], listed),
                         ["pnpm", "--filter build", "build", "docs"])  # the script, not the filter's value
        for verb in ("exec", "dlx", "x"):  # a binary, not a package script: as before
            self.assertEqual(buildqd.command_words(["pnpm", verb, "build", "docs"], listed), ["pnpm", "build"], verb)

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


class ScriptLabels(DaemonCase):
    def test_a_submitted_ticket_carries_its_label_and_keeps_its_key(self):
        self.daemon.cfg.script_labels = frozenset({"persistent-cargo-target.sh"})
        t = self.submit(("bash", "scripts/ci/persistent-cargo-target.sh", "architecture", "2G", "--", "cargo", "run"))
        self.assertTrue(t.key.endswith("|bash persistent-cargo-target.sh architecture"), t.key)
        self.daemon.cfg.script_labels = frozenset()  # a reload that drops it keeps the ticket's key
        self.assertEqual(buildqd.Ticket.from_record(t.record(), t.dir).key, t.key)


class QueriesOffTheLoop(DaemonCase):
    def test_a_slow_systemctl_doesnt_hold_up_the_daemon(self):
        gate = threading.Event()
        self.runner.result = lambda ticket: "oom-kill" if gate.wait(10) else ""
        self.daemon.queries = buildqd.ThreadPoolExecutor(max_workers=1)  # the real thing
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
        self.daemon.queries = buildqd.ThreadPoolExecutor(max_workers=1)
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
        with mock.patch.object(buildqd.os, "stat", side_effect=PermissionError("denied")) as _, \
                mock.patch.object(buildqd, "log") as log:
            t = buildqd.Ticket("x" * 36, ["docker", "build", "."], self.workdir(), "image", "k", self.now, self.tmp.name)
            t.disk_paths = []
            with mock.patch.object(buildqd, "disk_space", return_value=None), \
                    mock.patch.object(buildqd, "fs_device", return_value=None):
                self.daemon.disk_short(t)
                self.daemon.disk_short(t)
        said = [c for c in log.call_args_list if "can't read container storage" in str(c)]
        self.assertEqual(len(said), 1)


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



class Top(unittest.TestCase):
    """buildq top: what it reads, and what it draws."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(buildq.GLYPHS.update, dict(buildq.GLYPHS))
        self.addCleanup(buildq.BOX.update, dict(buildq.BOX))
        self.addCleanup(buildq.TOP.update, dict(buildq.TOP))

    def cgroup(self, name, usec, anon, swap=0, rbytes=0, wbytes=0, some=0.0):
        path = os.path.join(self.tmp.name, name)
        os.makedirs(path, exist_ok=True)
        files = {"cpu.stat": f"usage_usec {usec}\n", "memory.stat": f"anon {anon}\nkernel 0\nshmem 0\nslab_reclaimable 0\n",
                 "memory.swap.current": f"{swap}\n",
                 "io.stat": f"253:0 rbytes={rbytes} wbytes={wbytes}\n259:0 rbytes={rbytes} wbytes={wbytes}\n",
                 "memory.pressure": f"some avg10={some} avg60=0 avg300=0 total=0\nfull avg10=0.00 avg60=0 avg300=0 total=0\n",
                 "memory.events": "low 0\nhigh 3\nmax 0\noom 0\noom_kill 1\n"}
        for file, text in files.items():
            with open(os.path.join(path, file), "w") as f:
                f.write(text)
        return path

    def test_a_cgroup_sample_and_its_rates_counting_stacked_devices_once(self):
        path = self.cgroup("b", 0, 2 * GIB, swap=GIB, some=4.5)
        buildq._HOLDERS.clear()
        self.addCleanup(buildq._HOLDERS.clear)
        now = time.monotonic()
        buildq._HOLDERS.update({"253:0": ([], now), "259:0": (["253:0"], now)})  # dm-crypt on a partition of it
        sample = buildq.cgroup_sample(path)
        self.assertEqual((sample["mem"], sample["swap"], sample["psi_mem"]["some"]), (2 * GIB, GIB, 4.5))
        self.assertEqual((sample["events"]["high"], sample["events"]["oom_kill"]), (3, 1))
        sampler = buildq.TopSampler()
        self.assertIsNone(sampler.cgroup(path, 100.0)["cores"])  # nothing to compare with yet
        self.cgroup("b", 4_000_000, 2 * GIB, rbytes=20 * MIB, wbytes=2 * MIB)
        second = sampler.cgroup(path, 102.0)
        self.assertAlmostEqual(second["cores"], 2.0)
        self.assertAlmostEqual(second["read_rate"], 10 * MIB)  # not 20: what went through dm-crypt counts once
        self.assertIsNone(buildq.cgroup_sample(os.path.join(self.tmp.name, "gone")))
        os.remove(os.path.join(path, "memory.stat"))  # going away mid-read
        self.assertIsNone(buildq.cgroup_sample(path))

    def test_stacked_io_is_counted_once_per_interval(self):
        buildq._HOLDERS.clear()
        self.addCleanup(buildq._HOLDERS.clear)
        now = time.monotonic()
        zero = {d: (0, 0) for d in ("253:0", "253:1", "259:0", "8:0", "8:16")}

        def rate(holders, after, before=zero):
            buildq._HOLDERS.clear()
            buildq._HOLDERS.update({d: (h, now) for d, h in holders.items()})
            return buildq.io_rate(before, after, 1.0)[0]
        self.assertEqual(rate({"259:0": ["253:0"]}, {"253:0": (10, 0), "259:0": (30, 0)}), 30)  # 20 to a plain partition
        self.assertEqual(rate({"259:0": ["253:0", "253:0"]}, {"253:0": (10, 0), "259:0": (30, 0)}), 30)  # an LV on 2 parts
        self.assertEqual(rate({"8:0": ["253:1"], "8:16": ["253:1"]},  # striped over two disks, 20 plain on one
                              {"253:1": (100, 0), "8:0": (70, 0), "8:16": (50, 0)}), 120)
        self.assertEqual(rate({"259:0": []}, {"259:0": (8 * GIB + 5, 0)},  # the dm device went away: no burst
                              before={"253:0": (8 * GIB, 0), "259:0": (8 * GIB, 0)}), 5)
        self.assertEqual(rate({"8:0": ["253:1"], "8:16": ["253:1"]},  # a stripe split 100/0 in the interval
                              {"253:1": (100, 0), "8:0": (100, 0), "8:16": (0, 0)}), 100)
        self.assertEqual(rate({"8:0": ["9:0"], "8:16": ["9:0"]},  # RAID1 reads from one mirror
                              {"9:0": (100, 0), "8:0": (100, 0), "8:16": (0, 0)}, before={**zero, "9:0": (0, 0)}), 100)
        self.assertEqual(rate({}, {"259:0": (7, 0)}), 7)  # nothing stacked: itself, once
        self.assertEqual(buildq.text_width("e\u0301\u200dx\ufe0f"), 2)

    def snap(self, running=None):
        running = running or [
            {"id": "a" * 36, "short": "aaaaaaaa", "lane": "heavy", "elapsed": 600, "granted": 8, "want": 8, "need": 2,
             "est_mem": 20 * GIB, "mem_now": 4 * GIB, "mem_peak": 18 * GIB, "label": "cargo test --locked\n\x1b[31mred",
             "write_dirs": ["/h/t/target"], "waited_by": {"memory": 120.0}, "waited": 120, "cwd": "/src",
             "est_src": "largest of the last 3 runs", "peak_at": 90.0, "release_at": 300.0, "release_hold": 2 * GIB,
             "log": None, "peak_cores": 7.5},
            {"id": "b" * 36, "short": "bbbbbbbb", "lane": "web", "elapsed": 30, "granted": 2, "est_mem": 6 * GIB,
             "mem_now": GIB, "mem_peak": GIB, "label": "pnpm test", "write_dirs": ["/h/t/target/tmp"]}]
        queued = [{"short": "cccccccc", "lane": "heavy", "waited": 400, "est_mem": 20 * GIB, "held": False,
                   "reason": "needs 20G of memory; 2.0G is free after the 16G floor and what running builds may still grow into"}]
        return {"time": 1794600000.0,
                "machine": {"cores": 32, "busy": 20.0, "mem_total": 128 * GIB, "mem_available": 60 * GIB,
                            "swap_total": 8 * GIB, "swap_used": 6 * GIB, "psi_cpu": {"some": 3.0},
                            "psi_memory": {"some": 0.5, "full": 0.1}, "psi_io": {},
                            "filesystems": [{"mount": "/", "device": "nvme0n1p3", "free": 1000 * GIB, "size": 1860 * GIB,
                                             "floor": 372 * GIB, "read": 18 * MIB, "write": 45 * MIB}]},
                "groups": {"builds": {"cores": 9.0, "mem": 5 * GIB, "swap": 0, "read_rate": MIB, "write_rate": 2 * MIB,
                                      "known": True, "psi_mem": {"some": 0.2}},
                           "ci": {"cores": 6.0, "mem": 8 * GIB, "swap": 0, "known": True},
                           "containers": {"cores": 0.0, "mem": 0, "swap": 0, "known": True},
                           "apps": {"cores": 2.0, "mem": 14 * GIB, "swap": GIB, "known": True}},
                "builds": {"a" * 36: {"cores": 1.5, "mem": 4 * GIB, "swap": 0, "read_rate": 3 * MIB, "write_rate": 0,
                                      "psi_mem": {"some": 12.0}, "events": {"high": 2, "oom_kill": 0}},
                           "b" * 36: {"cores": 1.9, "mem": GIB, "swap": 0, "psi_mem": {}}},
                "dir_sizes": {"/h/t/target": (41 * GIB, False, 60.0)},
                "overview": {"version": "0.2.0", "paused": None, "running": running, "queued": queued,
                             "budget": {"cpu_cap": 28.8, "growth_reserved": 30 * GIB, "free_mem": 2 * GIB},
                             "config": {"mem_floor": 16 * GIB, "disk_floor": "20%"}}}

    def draw(self, snap, width=220, height=0, **kw):
        with mock.patch.object(buildq, "safe_dir", side_effect=lambda p: p if isinstance(p, str) and p.startswith("/h/") else None):
            return buildq.render_top(snap, width, height, **kw)

    @staticmethod
    def marked(lines):
        """The running build the screen marks as selected."""
        return next(t for t, _ in lines if re.search(r"(▶|>) [0-9a-f]{8}", t))

    def test_the_screen(self):
        lines, order = self.draw(self.snap(), detail=True, selected="a" * 36)
        text = "\n".join(t for t, _ in lines)
        for expected in ("20.0 of 32 busy · target 28.8", "60G free", "+30G held", "6.0G of 8.0G",
                         "/: 1000G free of 1860G · floor 372G · read 18M/s · write 45M/s", "CI 6.0c 8.0G",
                         "other 3.0c", "pressure: io -", "2 running", "1 waiting", "aaaaaaaa", "1.5/8", "4.0G/18G/20G",
                         "≥41G", "cargo test --locked·red", "memory 2m00s", "would keep 2.0G", "cccccccc",
                         "MEMORY", "CPU", "LANES", "▓ used", "▒ held for growth", "builds together",
                         "2.0G is free after the 16G floor"):
            self.assertIn(expected, text)
        self.assertNotIn("\x1b", text)
        self.assertEqual(order, ["a" * 36, "b" * 36])
        colour = next(c for t, c in lines if "aaaaaaaa" in t and "1.5/8" in t)
        self.assertEqual(colour, "bad")  # 12% memory pressure: it is being slowed

    def test_the_selection_follows_the_build_not_the_row(self):
        for sort in ("age", "cpu"):
            lines, order = self.draw(self.snap(), selected="b" * 36, sort=sort)
            self.assertIn("bbbbbbbb", self.marked(lines), sort)
        self.assertEqual(self.draw(self.snap(), sort="cpu")[1][0], "b" * 36)  # 1.9 cores before 1.5

    def test_a_small_screen_scrolls_to_the_selected_build_and_keeps_its_detail(self):
        rows = [{"id": f"{i:036d}", "short": f"{i:08d}", "lane": "heavy", "elapsed": 1000 - i, "granted": 2,
                 "est_mem": GIB, "mem_now": 0, "mem_peak": 0, "label": f"job {i}", "write_dirs": []} for i in range(20)]
        for pick in (0, 4, 8, 10, 18, 19):
            lines, order = self.draw(self.snap(rows), width=80, height=24, selected=f"{pick:036d}", detail=True)
            self.assertLessEqual(len(lines), 24)
            self.assertTrue(all(len(t) <= 80 for t, _ in lines))
            self.assertIn(f"{pick:08d}", self.marked(lines), pick)
            self.assertTrue(any(f"{pick:036d}" in t for t, _ in lines), pick)  # its detail's title
            self.assertTrue(any("above," in t and "below" in t for t, _ in lines), pick)
        log = [f"LOG{i:02d}" for i in range(12)]
        lines = self.draw(self.snap(rows), width=80, height=24, selected=f"{3:036d}", detail=True, log_lines=log)[0]
        text = "\n".join(t for t, _ in lines)
        self.assertIn("LOG11", text)  # the newest lines are the ones kept
        narrow = self.draw(self.snap(rows), width=70, height=24)[0]
        row = next(t for t, _ in narrow if "00000000" in t)
        self.assertIn("job 0", row)  # the command keeps its room
        self.assertNotIn("LANE", next(t for t, _ in narrow if "TICKET" in t))
        short = self.draw(self.snap(rows), width=100, height=12, selected=f"{9:036d}")[0]
        self.assertIn("00000009", self.marked(short))  # the machine's panel gave way
        header = next(t for t, _ in lines if "TICKET" in t)
        self.assertIn("COMMAND", header)
        self.assertNotIn("SWAP", header)  # 80 columns: the last optional columns go first
        self.assertIn("LANE", header)

    def test_shared_folders_are_marked_and_unknown_sizes_say_so(self):
        snap = self.snap()
        snap["dir_sizes"]["/h/t/target/tmp"] = (GIB, True, 5.0)
        lines = self.draw(snap)[0]  # one build's folder lies inside the other's: both are marked
        self.assertTrue(any("41G*" in t and "aaaaaaaa" in t for t, _ in lines))
        self.assertTrue(any("1.0G*" in t and "bbbbbbbb" in t for t, _ in lines))
        snap["overview"]["running"][1]["write_dirs"] = ["/h/t/target"]  # the same folder
        self.assertTrue(any("41G*" in t and "bbbbbbbb" in t for t, _ in self.draw(snap)[0]))
        snap["dir_sizes"] = {}
        self.assertTrue(any("…" in t and "aaaaaaaa" in t for t, _ in self.draw(snap)[0]))

    def test_ascii_when_the_terminal_isnt_utf8(self):
        buildq.use_ascii()
        snap = self.snap()
        snap["overview"]["running"][1]["label"] = "pnpm test \u00e9t\u00e9\ttab\x9bx"
        text = "\n".join(t for t, _ in self.draw(snap, selected="a" * 36, detail=True)[0])
        text.encode("ascii")  # nothing a non-UTF-8 terminal can't show
        self.assertIn("> aaaaaaaa", text)
        self.assertIn("tab", text)

    def test_the_memory_ledger_fills_what_its_parts_add_up_to(self):
        cells = buildq.ledger_cells([(3, "u", "mem"), (3, "h", "held"), (0, "x", "other"), (4, "o", "other")], 10, 7)
        self.assertEqual("".join(t for t, _ in cells), "uuhhooo")  # rounded as they add up, none lost or doubled
        self.assertEqual(len("".join(t for t, _ in buildq.ledger_cells([(20, "u", "")], 10, 7))), 7)  # never past the bar
        lines, _ = self.draw(self.snap(), width=150)
        bar = next(t for t, _ in lines if "▓" in t and "▒" in t and "■" in t)
        self.assertLess(bar.index("▓"), bar.index("█"))  # the builds, then other work, the floor, the room
        self.assertLess(bar.index("█"), bar.index("░"))
        self.assertLess(bar.index("░"), bar.index("■"))

    def test_the_cpu_graph_starts_from_the_analytics(self):
        path = os.path.join(self.enterContext(tempfile.TemporaryDirectory()), "metrics.db")
        import sqlite3
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE minute (t REAL, cpu_busy REAL, cpu_busy_max REAL, cores INTEGER, mem_total INTEGER,"
                   " mem_available INTEGER, builds_mem INTEGER, growth INTEGER)")
        now = 1794600000.0
        db.executemany("INSERT INTO minute VALUES (?,?,?,?,?,?,?,?)",
                       [(now - 3600, 31.0, 32.0, 32, 128 * GIB, 64 * GIB, GIB, 0),  # older than the graph
                        (now - 600, 16.0, 30.0, 32, 128 * GIB, 64 * GIB, GIB, 0),
                        (now - 540, 24.0, 30.0, 32, 128 * GIB, 64 * GIB, GIB, 0),
                        (now - 480, 32.0, 32.0, 32, 128 * GIB, 64 * GIB, GIB, 0)])
        db.commit()
        db.close()
        hist = buildq.TopHistory()
        hist.backfill(path, now)
        self.assertEqual((len(hist.minutes), len(hist.samples)), (3, 0))
        steps = hist.buckets(1, 30, now)  # minute steps; each minute's row stands at its middle
        self.assertEqual(steps[:19], [None] * 19)  # nothing before the first minute
        self.assertEqual(steps[19:22], [16.0, 24.0, 32.0])  # each minute's average (not its busiest moment)
        self.assertEqual(steps[22:], [None] * 8)  # and no plateau carried on after the last
        half = hist.buckets(1, 60, now)  # half-minute steps: a line between the minutes, not flat steps
        self.assertEqual((half[40], half[41]), (18.0, 22.0))  # a straight line between the minutes
        lone = buildq.TopHistory()
        lone.minutes = [(now - 870, 12.0, 32, 64 * GIB, GIB, 0, 128 * GIB)]  # one minute, no neighbours
        self.assertIn(12.0, lone.buckets(1, 30, now))
        hist.backfill(path, now)
        self.assertEqual(len(hist.minutes), 3)  # only once
        buildq.TopHistory().backfill(os.path.join(os.path.dirname(path), "missing.db"), now)  # no analytics: empty
        lines, _ = self.draw(self.snap(), width=150, hist=hist)
        graph = [t for t, _ in lines if "│32" in t.replace(" ", "")]
        self.assertTrue(graph and any("\u2801" <= ch <= "\u28ff" for ch in graph[0]))  # braille, as btop draws

    def test_steps_are_fixed_to_the_clock_and_averaged(self):
        hist = buildq.TopHistory()
        snap = self.snap()
        for t, busy in ((0, 10.0), (20, 30.0), (70, 5.0)):
            s2 = dict(snap, time=1794600000.0 + t)
            s2["machine"] = dict(snap["machine"], busy=busy)
            hist.add(s2)
        before = hist.buckets(1, 30, 1794600075.0)
        self.assertEqual(before[-2:], [20.0, 5.0])  # the average in each minute; the newest still filling
        self.assertEqual(hist.buckets(1, 30, 1794600079.0), before)  # nothing moves within a step
        self.assertEqual(hist.buckets(1, 30, 1794600121.0)[-3:], [20.0, 5.0, None])  # a new step: one along
        sparse = buildq.TopHistory()  # a sample every 40 s, steps of 20 s: the line between, not gaps
        for t, busy in ((0, 8.0), (40, 16.0)):
            s3 = dict(snap, time=1794600000.0 + t)
            s3["machine"] = dict(snap["machine"], busy=busy)
            sparse.add(s3)
        steps = sparse.buckets(1, 90, 1794600040.0)
        self.assertEqual(steps[-3:], [8.0, 14.0, 16.0])  # the middle step is 30 s along a 40 s line

    def test_braille_has_two_steps_a_cell_and_a_baseline(self):
        self.assertEqual(buildq.braille_rows([0, 4], 1, 4, baseline=False), ["\u28b8"])  # right column full
        self.assertEqual(buildq.braille_rows([4, 4], 1, 4, baseline=False), ["\u28ff"])
        self.assertEqual(buildq.braille_rows([0, 0], 2, 4), ["\u2800", "\u28c0"])  # a baseline dot each
        self.assertEqual(buildq.braille_rows([None, None], 2, 4), ["\u2800", "\u2800"])  # no data: blank, as btop
        self.assertEqual(buildq.braille_rows([2, 6], 2, 8, baseline=False), ["\u28a0", "\u28fc"])  # 2 and 4 dots low, 0 and 2 high
        self.assertEqual(buildq.gradient_style(0, 8), "cpu0")
        self.assertEqual(buildq.gradient_style(7, 8), "cpu7")  # green at the bottom, red at the top
        buildq.use_ascii()
        hist = buildq.TopHistory()
        hist.add(self.snap())
        rows = buildq.graph_rows(hist, 1, 10, 3, 32, self.snap()["time"])
        "".join(rows).encode("ascii")  # one step a cell, in ASCII, without braille

    def test_the_timeline_and_the_help(self):
        snap = self.snap()
        snap["overview"]["running"][0]["from_ci"] = True
        finished = [{"short": "dddddddd", "lane": "check", "submitted": snap["time"] - 1500, "started": snap["time"] - 1400,
                     "ended": snap["time"] - 900, "peak": 20 * GIB, "from_ci": False, "state": "done"}]
        lines, _ = self.draw(snap, width=120, height=30, view="timeline", finished=finished)
        text = "\n".join(t for t, _ in lines)
        self.assertIn("TIMELINE", text)
        rows = {m: next(t for t, _ in lines if m in t) for m in ("aaaaaaaa", "bbbbbbbb", "cccccccc", "dddddddd")}
        self.assertTrue(rows["aaaaaaaa"].lstrip("│ ").startswith("CI aaaaaaaa"))
        self.assertIn("█", rows["dddddddd"])  # it ran, with a lot of memory
        self.assertIn("·", rows["cccccccc"])  # it waits
        self.assertNotIn("█", rows["cccccccc"].split("heavy", 1)[1])
        self.assertLess(text.index("dddddddd"), text.index("cccccccc"))  # oldest first, newest at the bottom
        lines, _ = self.draw(snap, width=120, height=30, show_help=True)
        text = "\n".join(t for t, _ in lines)
        for expected in ("HELP", "the timeline", "held for it to grow into", "behind N", "a long wait, or a CI job's build"):
            self.assertIn(expected, text)

    def test_the_layout_follows_the_width(self):
        wide = "\n".join(t for t, _ in self.draw(self.snap(), width=150, height=40)[0])
        self.assertIn("LANES", wide)
        self.assertIn("╭─ CPU", wide)
        narrow = "\n".join(t for t, _ in self.draw(self.snap(), width=90, height=40)[0])
        self.assertNotIn("LANES", narrow)
        self.assertNotIn("╭─ CPU", narrow)
        self.assertIn("CPU ", narrow.splitlines()[0])  # a bar in the title line instead
        self.assertIn("MEMORY", narrow)
        many = self.snap()
        many["overview"]["running"] = [dict(many["overview"]["running"][1], id=f"{i:036d}", short=f"{i:08d}",
                                            elapsed=i) for i in range(40)]
        for snap, pick, short in ((self.snap(), "a" * 36, "aaaaaaaa"), (many, f"{7:036d}", "00000007")):
            for width in (50, 79, 80, 90, 100, 110, 150, 300):
                for height in range(12, 41):
                    for detail, log in ((False, None), (True, None), (True, [f"line {i}" for i in range(30)])):
                        lines, _ = self.draw(snap, width=width, height=height, selected=pick, detail=detail, log_lines=log)
                        where = (width, height, detail, bool(log), len(snap["overview"]["running"]))
                        self.assertEqual(len(lines), height, where)
                        self.assertTrue(all(buildq.text_width(t) <= width for t, _ in lines), where)
                        self.assertIn(short, self.marked(lines), where)  # the selected build is in view
                        if log and any("the end of" in t for t, _ in lines):
                            self.assertIn("line 29", "\n".join(t for t, _ in lines), where)  # the newest kept
                        borders = [i for i, (t, _) in enumerate(lines) if t.startswith("╰")]
                        tops = [i for i, (t, _) in enumerate(lines) if t.startswith("╭")]
                        self.assertEqual(len(borders), len(tops), where)  # no box drawn over another

    def queue_in_a_jam(self, snap, free=-6 * GIB):
        snap["overview"]["budget"].update(free_mem=free, held_mem=223 * GIB)  # (summed over all in line: not shown)
        snap["overview"]["queued"] = [
            {"short": "cccccccc", "lane": "heavy", "waited": 400, "est_mem": 20 * GIB, "held": True, "waiting_for": "memory",
             "reason": "needs 20G of memory; 0M is free after the 16G floor and what running builds may still grow into"},
            {"short": "dddddddd", "lane": "heavy", "waited": 390, "est_mem": 2 * GIB, "held": True, "waiting_for": "held",
             "held_for": "memory", "reason": "the room it needs is held for a build that has waited longer"},
            {"short": "eeeeeeee", "lane": "heavy", "waited": 300, "est_mem": 2 * GIB, "held": False, "waiting_for": "cpu",
             "reason": "needs 8 cores; 2.0 of 28.8 are free (20.0 busy, 0.0 expected from builds just started)"},
            {"short": "ffffffff", "lane": "heavy", "waited": 200, "est_mem": 300 * GIB, "held": False, "waiting_for": "too-big",
             "reason": "needs 300G of memory, more than this machine has; cancel it or give its lane a smaller MemoryMax"},
            {"short": "gggggggg", "lane": "web", "waited": 100, "est_mem": GIB, "held": False, "waiting_for": "held",
             "held_for": "memory", "reason": "the room it needs is held for a build that has waited longer"}]
        return snap

    def test_every_wait_says_why_and_what_it_lacks(self):
        snap = self.queue_in_a_jam(self.snap())
        lines, _ = self.draw(snap, width=260)
        rows = {m: next(t for t, _ in lines if m in t and "needs" in t) for m in ("cccccccc", "dddddddd", "eeeeeeee", "ffffffff", "gggggggg")}
        self.assertIn("⚑ in line", rows["cccccccc"])
        self.assertIn("20G short", rows["cccccccc"])  # what the free room lacks, never more than it needs
        self.assertIn("0M is free after the 16G floor", rows["cccccccc"])
        self.assertNotIn("needs 20G of memory;", rows["cccccccc"])  # (said in its column)
        self.assertIn("behind 1 in line", rows["dddddddd"])  # its room is free, kept for the one ahead
        self.assertIn("behind 2 in line", rows["gggggggg"])
        self.assertIn("needs 8 cores; 2.0 of 28.8 are free", rows["eeeeeeee"])  # not hidden for starting "needs"
        self.assertNotIn("short", rows["eeeeeeee"])
        self.assertIn("cancel it or give its lane a smaller MemoryMax", rows["ffffffff"])
        text = "\n".join(t for t, _ in lines)
        self.assertIn("over by 6.0G", text)  # more held than free: not "room 0M"
        self.assertIn("1 for memory, the closest 20G short", text)
        self.assertIn("2 in line, with room kept for them", text)
        self.assertNotIn("223G", text)  # room kept, summed over everyone in line, would read as more than the machine
        self.assertNotIn("next", text)

    def test_behind_is_the_daemons_count_when_it_gives_one(self):
        snap = self.queue_in_a_jam(self.snap())
        snap["overview"]["queued"][4]["behind"] = 1  # two in line ahead, but only one binds it (#62)
        row = next(t for t, _ in self.draw(snap, width=260)[0] if "gggggggg" in t)
        self.assertIn("behind 1 in line", row)

    def test_a_jam_on_a_normal_terminal_still_lists_waiting_builds(self):
        snap = self.queue_in_a_jam(self.snap())
        snap["overview"]["queued"] *= 5  # 25 waiting
        snap["overview"]["running"] = [dict(snap["overview"]["running"][1], id=f"{i:036d}", short=f"{i:08d}")
                                       for i in range(8)]
        for width, height in ((80, 24), (100, 30), (150, 24), (80, 18), (80, 12)):
            lines, _ = self.draw(snap, width=width, height=height)
            text = "\n".join(t for t, _ in lines)
            self.assertIn("cccccccc", text, (width, height))  # a waiting build, not just a count
            self.assertIn("more waiting", text, (width, height))  # (the count, in the box's border)

    def test_each_build_is_drawn_holding_what_the_daemon_holds_for_it(self):
        snap = self.snap()
        a, b = snap["overview"]["running"]
        snap["builds"]["b" * 36]["mem"] = GIB
        a.update(est_mem=20 * GIB, growth=3 * GIB, released=True)  # let go of after its peak: 3G kept, not 16G
        b.update(est_mem=6 * GIB, growth=5 * GIB)
        snap["overview"]["budget"]["growth_reserved"] = 8 * GIB
        holds = buildq.build_holds(snap, buildq.running_order(snap, "age"))
        self.assertEqual(holds, {"a" * 36: (4 * GIB, 3 * GIB), "b" * 36: (GIB, 5 * GIB)})
        lines, _ = self.draw(snap, width=200, height=40)
        row = next(t for t, _ in lines if "aaaaaaaa" in t and "/" in t)
        self.assertIn("▓▓▒····· 4.0G", row)  # 4G used and 3G held of 20G; the rest let go
        text = "\n".join(t for t, _ in lines)
        self.assertIn("builds 5.0G +8.0G held", text)
        a["released"] = b["released"] = False  # nothing let go: each row shows the rest of its plan held
        row = next(t for t, _ in self.draw(snap, width=200, height=40)[0] if "bbbbbbbb" in t and "/" in t)
        self.assertIn("▓▒▒▒▒▒▒▒", row)
        del a["growth"], b["growth"], a["released"], b["released"]  # an older daemon: its total shared out by the plans
        lines, _ = self.draw(snap, width=200, height=40)
        row = next(t for t, _ in lines if "aaaaaaaa" in t and "/" in t)
        self.assertIn("·", row)  # (its share of the 8G held, not the whole rest of its 20G plan)
        holds = buildq.build_holds(snap, buildq.running_order(snap, "age"))
        self.assertAlmostEqual(sum(h for _, h in holds.values()), 8 * GIB, delta=2)  # (rounded down per build)

    def test_the_selected_build_keeps_its_warning(self):
        c, _ = buildq.render_screen(self.snap(), 200, 40, selected="a" * 36)
        y = next(i for i, t in enumerate(c.lines()) if "▶ aaaaaaaa" in t)
        x = c.lines()[y].index("aaaaaaaa")
        self.assertEqual(c.styles[y][x], "sel_bad")  # under memory pressure on the selection bar
        self.assertEqual(c.styles[y][x - 2], "sel")  # the bar runs across the row, as btop's
        self.assertEqual(c.styles[y][c.lines()[y].index("cargo test")], "sel")
        other = next(i for i, t in enumerate(c.lines()) if "bbbbbbbb" in t and "/" in t)
        self.assertNotIn("sel", c.styles[other])

    def test_every_filesystem_has_its_line(self):
        snap = self.snap()
        snap["machine"]["filesystems"].append({"mount": "/var/lib/containers", "device": "sdb1", "free": 10 * GIB,
                                               "size": 500 * GIB, "floor": 100 * GIB, "read": 0, "write": 0})
        for width, height in ((150, 0), (150, 40), (90, 40), (90, 0)):
            text = "\n".join(t for t, _ in self.draw(snap, width=width, height=height)[0])
            self.assertIn("DISK /:", text, (width, height))
            self.assertIn("DISK /var/lib/containers: 10G free of 500G", text, (width, height))

    def test_a_wide_character_cut_in_half_leaves_no_half(self):
        c = buildq.Canvas(10, 1)
        c.put(0, 0, "日本語")
        c.put(0, 1, "x")  # over the right half of 日
        c.put(0, 4, "y")  # over the left half of 語
        self.assertEqual(c.lines()[0], " x本y")
        self.assertEqual(sum(buildq.text_width(t) for t, _ in c.runs(0)), 10)
        c = buildq.Canvas(6, 1)
        c.put(0, 0, "a日b")
        c.put(0, 0, "語")  # a wide one over a narrow one and the left half of another
        self.assertEqual(sum(buildq.text_width(t) for t, _ in c.runs(0)), 6)

    def test_older_analytics_and_finished_builds(self):
        import sqlite3
        path = os.path.join(self.enterContext(tempfile.TemporaryDirectory()), "metrics.db")
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE minute (t REAL, cpu_busy REAL, cores INTEGER, mem_total INTEGER, mem_available INTEGER)")
        now = 1794600000.0
        db.execute("INSERT INTO minute VALUES (?,?,?,?,?)", (now - 300, 12.0, 32, 128 * GIB, 64 * GIB))
        db.execute("CREATE TABLE build (id TEXT, lane TEXT, submitted INTEGER, ended INTEGER, attempt_wall REAL, wall REAL,"
                   " mem_peak INTEGER, from_ci INTEGER, state TEXT)")
        db.executemany("INSERT INTO build VALUES (?,?,?,?,?,?,?,?,?)", [
            ("1" * 36, "heavy", now - 1200, now - 600, 300.0, 400.0, 20 * GIB, 1, "failed"),
            ("2" * 36, "web", now - 7200, now - 7000, 100.0, 100.0, GIB, 0, "succeeded"),  # ended before the half hour
            ("3" * 36, "web", now - 900, now - 100, None, 50.0, GIB, 0, "succeeded")])
        db.commit()
        db.close()
        hist = buildq.TopHistory()
        hist.backfill(path, now)  # no growth, no builds_mem: what there is still fills the graph
        self.assertEqual([m[1] for m in hist.minutes], [12.0])
        done = buildq.finished_builds(path, now, skip={"3" * 36})
        self.assertEqual([(d["short"], d["started"], d["from_ci"]) for d in done], [("11111111", now - 900, True)])

    def test_the_timeline_keeps_the_running_builds(self):
        snap = self.snap()
        finished = [{"short": f"{i:08d}", "lane": "check", "submitted": snap["time"] - 1700 + i, "started": snap["time"] - 1600 + i,
                     "ended": snap["time"] - 1000 + i, "peak": GIB, "from_ci": False, "state": "failed" if i == 59 else "succeeded"}
                    for i in range(60)]
        lines, _ = self.draw(snap, width=120, height=20, view="timeline", finished=finished)
        text = "\n".join(t for t, _ in lines)
        for short in ("aaaaaaaa", "bbbbbbbb", "cccccccc", "00000059"):
            self.assertIn(short, text)  # the running and waiting builds, then the newest finished ones
        self.assertIn("not shown", text)
        c, _ = buildq.render_screen(snap, 120, 20, view="timeline", finished=finished)
        y = next(i for i, t in enumerate(c.lines()) if "00000059" in t)
        self.assertIn("bad", c.styles[y][27:])  # a failed build isn't drawn as a success

    def test_with_no_daemon_it_still_shows_the_machine(self):
        snap = self.snap()
        snap["overview"] = None
        lines, order = self.draw(snap)
        text = "\n".join(t for t, _ in lines)
        self.assertIn("daemon not answering", text)
        self.assertEqual(order, [])

    def test_only_folders_inside_home_are_measured(self):
        home = os.path.join(self.tmp.name, "home")
        os.makedirs(os.path.join(home, "target", "deep"))
        os.symlink("/", os.path.join(home, "root"))
        with mock.patch.dict(os.environ, HOME=home):
            self.assertEqual(buildq.safe_dir(os.path.join(home, "target")), os.path.realpath(os.path.join(home, "target")))
            for bad in ("/", "//", home, os.path.join(home, "root"), self.tmp.name, os.path.join(home, "missing"), None):
                self.assertIsNone(buildq.safe_dir(bad), bad)
            row = {"write_dirs": [os.path.join(home, "target", "deep"), os.path.join(home, "target"), "/"]}
            self.assertEqual(buildq.folders_of(row), [os.path.realpath(os.path.join(home, "target"))])

    def test_folder_sizes_count_hard_links_once(self):
        folder = os.path.join(self.tmp.name, "target")
        os.makedirs(os.path.join(folder, "deep"))
        with open(os.path.join(folder, "deep", "a"), "wb") as f:
            f.write(os.urandom(200_000))
        os.link(os.path.join(folder, "deep", "a"), os.path.join(folder, "b"))
        size, complete, cut = buildq.DirSizes.measure(folder, 30)
        self.assertEqual((complete, cut), (True, False))
        self.assertLess(size, 2 * 200_000)  # the linked file once
        self.assertGreaterEqual(size, 200_000)
        self.assertEqual(buildq.DirSizes.measure(os.path.join(self.tmp.name, "missing"), 30)[:2], (None, False))

    def test_du_vanishing_entries_are_fine_unreadable_ones_are_partial(self):
        class Done:
            def __init__(self, out, err, code):
                self.out, self.err, self.returncode = out, err, code
            def communicate(self, timeout=None):
                return self.out, self.err
        for err, complete in ((b"du: cannot access 'x/y': No such file or directory\n", True),
                              (b"du: cannot read directory 'x/z': Permission denied\n", False)):
            with mock.patch.object(buildq.subprocess, "Popen", return_value=Done(b"4096\tx\n", err, 1)):
                self.assertEqual(buildq.DirSizes.measure("x", 5), (4096, complete, False))

    def test_a_new_stacked_device_makes_the_holders_be_read_again(self):
        buildq._HOLDERS.clear()
        self.addCleanup(buildq._HOLDERS.clear)
        buildq._HOLDERS["259:0"] = ([], time.monotonic())  # read before dm-0 existed
        holders = {"/sys/dev/block/259:0/holders": [], "/sys/dev/block/259:0/p3/holders": ["dm-0"],
                   "/sys/dev/block/253:0/holders": []}
        with mock.patch.object(buildq.glob, "glob", side_effect=lambda p: ["/sys/dev/block/259:0/p3/holders"]
                               if p.startswith("/sys/dev/block/259:0/") else []), \
                mock.patch.object(buildq.os, "listdir", side_effect=lambda p: holders.get(p, [])), \
                mock.patch.object(buildq, "read_text", side_effect=lambda p: "253:0\n" if p.endswith("/dm-0/dev") else None):
            first = {"259:0": (30, 0), "253:0": (10, 0)}
            buildq.io_rate({"259:0": (0, 0)}, first, 1.0)  # dm-0 is new: what is stacked is read afresh
            self.assertEqual(buildq.io_rate(first, {"259:0": (60, 0), "253:0": (20, 0)}, 1.0)[0], 30)  # not 40

    def test_the_holders_of_a_disk_include_its_partitions(self):
        buildq._HOLDERS.clear()
        self.addCleanup(buildq._HOLDERS.clear)
        holders = {"/sys/dev/block/259:0/holders": [], "/sys/dev/block/259:0/nvme0n1p3/holders": ["dm-0"]}
        with mock.patch.object(buildq.glob, "glob", return_value=["/sys/dev/block/259:0/nvme0n1p3/holders"]), \
                mock.patch.object(buildq.os, "listdir", side_effect=lambda p: holders[p]), \
                mock.patch.object(buildq, "read_text", side_effect=lambda p: "253:0\n" if p == "/sys/class/block/dm-0/dev" else None):
            self.assertEqual(buildq.holders_of("259:0"), ["253:0"])

    def test_du_runs_in_the_c_locale_and_wide_text_fits(self):
        seen = {}

        class Done:
            returncode = 0
            def communicate(self, timeout=None):
                return b"1\tx\n", b""
        def popen(command, **kw):
            seen.update(kw.get("env") or {})
            return Done()
        with mock.patch.object(buildq.subprocess, "Popen", side_effect=popen):
            buildq.DirSizes.measure("x", 5)
        self.assertEqual(seen.get("LC_ALL"), "C")
        self.assertEqual(buildq.text_width("日本x"), 5)
        for odd in ("\u202e", "\u061c", "\ufeff", "\u0600", "\u2028", "\U000e0041"):
            self.assertNotIn(odd, buildq.clean("ls " + odd + "x"))
        self.assertIn("\u200c", buildq.clean("\u0645\u06cc\u200c\u062e"))  # Persian keeps its non-joiner
        self.assertEqual(buildq.fit("日本語", 5), "日本")

    def test_the_interval_is_checked(self):
        self.assertEqual(buildq.interval_arg("2"), 2.0)
        for bad in ("-1", "0", "0.1", "nan", "inf", "x"):
            with self.assertRaises(argparse.ArgumentTypeError, msg=bad):
                buildq.interval_arg(bad)

    def test_which_encodings_want_ascii(self):
        self.assertTrue(buildq.wants_ascii("ANSI_X3.4-1968"))  # LC_ALL=C, as nl_langinfo and curses say
        self.assertTrue(buildq.wants_ascii("ascii"))
        self.assertTrue(buildq.wants_ascii(None))
        self.assertFalse(buildq.wants_ascii("UTF-8"))
        self.assertFalse(buildq.wants_ascii("utf_8"))

    def test_no_color_turns_colour_off(self):
        self.assertTrue(buildq.colour_wanted({}))
        self.assertFalse(buildq.colour_wanted({"NO_COLOR": "1"}))
        self.assertTrue(buildq.colour_wanted({"NO_COLOR": ""}))  # set but empty: no-color.org says ignore it

    def test_a_terminal_this_machine_has_no_terminfo_for_falls_back(self):
        import curses
        env, tried = {"TERM": "xterm-ghostty"}, []

        def wrapper(run):
            tried.append(env["TERM"])
            if env["TERM"] == "xterm-ghostty":
                raise curses.error("setupterm: could not find terminal")
            return run(None)
        code, why = buildq.curses_with_fallback(lambda screen: 7, wrapper, env)
        self.assertEqual((code, tried), (7, ["xterm-ghostty", "xterm-256color"]))
        self.assertIn("no terminfo for TERM=xterm-ghostty", why)
        self.assertIn("infocmp -x xterm-ghostty", why)

    def test_no_terminal_at_all_is_one_snapshot_and_says_why(self):
        import curses

        def wrapper(run):
            raise curses.error("setupterm: could not find terminal")
        code, why = buildq.curses_with_fallback(lambda screen: 0, wrapper, {"TERM": "dumb"})
        self.assertIsNone(code)
        self.assertIn("one snapshot instead", why)

    def test_a_curses_error_once_running_isnt_taken_for_the_terminal(self):
        import curses

        def run(screen):
            raise curses.error("addwstr() returned ERR")
        with self.assertRaises(curses.error):
            buildq.curses_with_fallback(run, lambda r: r(None), {"TERM": "xterm-ghostty"})

    def test_the_screen_never_waits_on_the_daemon(self):
        import threading
        release = threading.Event()

        def slow():
            release.wait(5)
            return {"overview": {}}
        fetcher = buildq.StatusFetcher(0.05, ask=slow).start()
        self.addCleanup(fetcher.stop)
        self.addCleanup(release.set)
        started = time.monotonic()
        self.assertEqual(fetcher.latest(), (0, None))  # at once, while the daemon is still answering
        self.assertLess(time.monotonic() - started, 0.5)
        release.set()
        self.assertTrue(fetcher.answered.wait(5))
        version, status = fetcher.latest()
        self.assertEqual((version >= 1, status), (True, {"overview": {}}))

    def test_sigterm_and_sighup_end_it_as_an_exception(self):
        import signal as signals
        saved = {s: signals.getsignal(s) for s in (signals.SIGTERM, signals.SIGHUP)}
        self.addCleanup(lambda: [signals.signal(s, h) for s, h in saved.items()])
        buildq.exit_on_signals()
        for sig in (signals.SIGTERM, signals.SIGHUP):
            with self.assertRaises(SystemExit) as caught:
                os.kill(os.getpid(), sig)
                time.sleep(1)  # (the handler runs at the next bytecode)
            self.assertEqual(caught.exception.code, 128 + sig)

    def test_no_color_keeps_the_terminal_s_own_colours(self):
        calls = []

        class Curses:
            A_BOLD, A_REVERSE, A_DIM, COLOR_CYAN, COLOR_GREEN, COLOR_YELLOW, COLOR_RED, COLOR_BLUE, COLOR_MAGENTA = \
                1 << 21, 1 << 18, 1 << 20, 6, 2, 3, 1, 4, 5
            COLOR_WHITE, COLOR_BLACK, COLORS = 7, 0, 8
            error = Exception

            def has_colors(self):
                return True

            def use_default_colors(self):
                calls.append("default")

            def init_pair(self, *a):
                calls.append("pair")

            def color_pair(self, i):
                return i << 8
        plain = buildq.init_colours(Curses(), wanted=False)
        self.assertEqual(calls, ["default"])  # the terminal's own colours, not white on black
        self.assertEqual((plain[""], plain["head"]), (0, Curses.A_BOLD))  # headings keep their bold
        calls.clear()
        coloured = buildq.init_colours(Curses(), wanted=True)  # 8 colours: btop's TTY theme
        self.assertEqual(calls.count("pair"), 9)
        self.assertNotEqual(coloured["ok"], 0)
        self.assertEqual(coloured["dim"], 0)  # nothing to read in blue
        self.assertNotEqual(coloured["sel_bad"], coloured["sel"])  # a warning on the bar: its own background
        rich = Curses()
        rich.COLORS = 256
        calls.clear()
        coloured = buildq.init_colours(rich, wanted=True)  # btop's Default theme
        self.assertEqual(calls.count("pair"), len({fg for fg, _ in buildq.TOP_COLOURS_256.values()}) + 3)
        self.assertTrue(coloured["sel"] & Curses.A_BOLD)
        self.assertNotEqual(coloured["dim"], coloured["faint"])
        class Exhausted(Curses):  # real curses raises ValueError past the terminal's pairs
            COLORS, COLOR_PAIRS = 256, 16

            def init_pair(self, n, *a):
                if n >= self.COLOR_PAIRS:
                    raise ValueError("Color pair is greater than COLOR_PAIRS-1")
                calls.append("pair")
        few = buildq.init_colours(Exhausted(), wanted=True)  # too few pairs for btop's theme: its TTY theme
        self.assertEqual(few["dim"], 0)
        self.assertNotEqual(few["ok"], 0)
        Exhausted.COLOR_PAIRS = 8
        self.assertEqual(buildq.init_colours(Exhausted(), wanted=True).get("ok", 0), 0)  # too few for that too: plain
        direct = Curses()
        direct.COLORS, direct.COLOR_PAIRS = 0x1000000, 0x10000  # xterm-direct: numbers are RGB, not indexes
        calls.clear()
        self.assertEqual(buildq.init_colours(direct, wanted=True)["dim"], 0)  # the eight colours' theme
        self.assertEqual(calls.count("pair"), 9)
        capped = Curses()
        capped.COLORS, capped.COLOR_PAIRS = 32767, 0x10000  # direct colour capped by an older ncurses
        capped.tigetflag = lambda name: 1 if name == "RGB" else -1
        self.assertEqual(buildq.init_colours(capped, wanted=True)["dim"], 0)

    def test_a_closed_terminal_ends_with_its_signal_s_code_not_a_traceback(self):
        import curses

        def wrapper(run):
            try:
                run(None)
            finally:  # as curses.wrapper's teardown fails on a hung-up terminal
                raise curses.error("nocbreak() returned ERR")

        def run(screen):
            raise SystemExit(129)
        with self.assertRaises(SystemExit) as caught:
            buildq.curses_with_fallback(run, wrapper, {"TERM": "xterm-256color"})
        self.assertEqual(caught.exception.code, 129)
        env, calls = {"TERM": "xterm-ghostty"}, []

        def fallback_wrapper(run):  # the first try fails setupterm; the fallback runs, then hangs up
            calls.append(env["TERM"])
            if len(calls) == 1:
                raise curses.error("setupterm: could not find terminal")
            return wrapper(run)
        with self.assertRaises(SystemExit) as caught:
            buildq.curses_with_fallback(run, fallback_wrapper, env)
        self.assertEqual((caught.exception.code, calls), (129, ["xterm-ghostty", "xterm-256color"]))

        def interrupted(screen):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            buildq.curses_with_fallback(interrupted, wrapper, {"TERM": "xterm-256color"})

    def test_the_fetcher_survives_an_unexpected_error(self):
        answers = iter([{"overview": {}}, OSError(23, "too many open files"), {"overview": {"x": 1}}])

        def ask():
            a = next(answers, {"overview": {"x": 1}})
            if isinstance(a, Exception):
                raise a
            return a
        fetcher = buildq.StatusFetcher(0.01, ask=ask).start()
        self.addCleanup(fetcher.stop)
        deadline = time.monotonic() + 5
        while fetcher.latest()[0] < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertGreaterEqual(fetcher.latest()[0], 3)  # it went on asking
        self.assertTrue(fetcher.thread.is_alive())


class ClientAndGuardHelpers(unittest.TestCase):
    def test_caller_recognises_antigravity_conversation_id(self):
        cleared = {k: "" for k in ("BUILDQ_CALLER", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID",
                                   "T3CODE_THREAD_ID", "ANTIGRAVITY_CONVERSATION_ID")}
        with mock.patch.dict(os.environ, {**cleared, "ANTIGRAVITY_CONVERSATION_ID": "conv-42"}):
            self.assertEqual(buildq.caller(), "conv-42")
        with mock.patch.dict(os.environ, {**cleared, "T3CODE_THREAD_ID": "thread-9",
                                          "ANTIGRAVITY_CONVERSATION_ID": "conv-42"}):
            self.assertEqual(buildq.caller(), "thread-9")

    def test_tail_offset_handles_edges_and_multi_chunk_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            def check(data: bytes, lines: int, expected: bytes):
                path = os.path.join(tmp, "log")
                with open(path, "wb") as f:
                    f.write(data)
                off = buildq.tail_offset(path, lines)
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
