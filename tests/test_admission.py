"""Unit tests for admission, lanes, fair shares, deduplication, priority, and abandoned submits."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import BASE_CONFIG, DaemonCase, GIB, SOURCE  # noqa: E402
from buildq import client, commands, common, config, daemon, machine, ticket  # noqa: E402


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
        with mock.patch.object(machine, "disk_space", return_value=(300 * GIB, 1900 * GIB)):
            t = self.submit()
        self.assertEqual(t.reason_cat, "disk")
        self.assertIn("20% of its 1900G, 380G", t.reason)
        with mock.patch.object(machine, "disk_space", return_value=(400 * GIB, 1900 * GIB)):
            self.assertNotEqual(self.submit().reason_cat, "disk")
        cfg = config.Config(config.tomllib.loads('disk_floor = "20%"'), 128 * GIB, 32)
        self.assertEqual((cfg.disk_floor, cfg.disk_floor_share, cfg.summary()["disk_floor"]), (0, 0.2, "20%"))
        for bad in ('"100%"', '"x%"', '"-5%"'):
            with self.assertRaises(ValueError, msg=bad):
                config.Config(config.tomllib.loads(f"disk_floor = {bad}"), 128 * GIB, 32)

    def test_the_state_directory_and_container_storage_count_too(self):
        self.daemon.cfg.disk_floor, self.daemon.cfg.disk_floor_share = 10 * GIB, 0.0
        storage = os.path.join(self.tmp.name, "storage")
        os.makedirs(storage)
        self.daemon.cfg.container_storage = [storage, os.path.join(self.tmp.name, "not-there")]
        work = self.workdir()
        roomy, short = (500 * GIB, 1900 * GIB), (GIB, 1900 * GIB)
        # each path on a filesystem of its own here, as the stand-in says
        with mock.patch.object(machine, "disk_space",
                               side_effect=lambda path: short if path == self.state else roomy), \
                mock.patch.object(machine, "fs_device", side_effect=hash):
            t = self.submit(cwd=work)
        self.assertEqual(t.reason_cat, "disk")
        self.assertIn(f"free at {self.state}", t.reason)  # its log would go there
        with mock.patch.object(machine, "disk_space",
                               side_effect=lambda path: short if path == storage else roomy), \
                mock.patch.object(machine, "fs_device", side_effect=hash):
            image = self.submit(("docker", "build", "."), cwd=self.workdir())
            cargo = self.submit(("cargo", "build"), cwd=self.workdir())
        self.assertEqual(image.reason_cat, "disk")
        self.assertIn(f"free at {storage}", image.reason)
        self.assertNotEqual(cargo.reason_cat, "disk")  # only an image build writes there
        cfg = config.Config({"container_storage": ["~/x"]}, 64 * GIB, 16)
        self.assertEqual(cfg.container_storage, [os.path.expanduser("~/x")])
        for bad in ("/x", [""], [3]):
            with self.assertRaises(ValueError, msg=bad):
                config.Config({"container_storage": bad}, 64 * GIB, 16)

    def test_each_filesystem_is_held_to_its_own_share(self):
        self.daemon.cfg.disk_floor, self.daemon.cfg.disk_floor_share = 0, 0.125
        spaces = {"/big": (400 * GIB, 1900 * GIB), "/small": (GIB, 16 * GIB), "/gone": None}
        with mock.patch.object(machine, "disk_space", side_effect=lambda path: spaces.get(path, (GIB, 16 * GIB))):
            t = self.submit()  # (held at submission too, by its working directory)
        t.disk_paths = ["/big", "/gone", "/small"]
        plenty = (900 * GIB, 1900 * GIB)  # buildq's state directory
        with mock.patch.object(machine, "disk_space", side_effect=lambda path: spaces.get(path, plenty)):
            self.daemon.schedule(self.now)
        self.assertEqual(t.reason_cat, "disk")
        self.assertIn("1.0G of disk is free", t.reason)
        self.assertIn("12.5% of its 16G, 2.0G", t.reason)  # the small one, by its own size
        spaces["/small"] = (4 * GIB, 16 * GIB)
        with mock.patch.object(machine, "disk_space", side_effect=lambda path: spaces.get(path, plenty)):
            self.daemon.schedule(self.now + 1)
        self.assertNotEqual(t.reason_cat, "disk")
        self.daemon.cfg.disk_floor, self.daemon.cfg.disk_floor_share = 500 * GIB, 0.0
        with mock.patch.object(machine, "disk_space", side_effect=lambda path: spaces.get(path, (GIB, 16 * GIB))):
            u = self.submit()
        u.disk_paths = ["/big"]
        with mock.patch.object(machine, "disk_space", side_effect=lambda path: spaces.get(path, plenty)):
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
        again = daemon.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                                     queries=common.InlineExecutor())
        self.addCleanup(lambda: (again.stop_memory_watch(), [again.close_jobserver(x) for x in again.tickets.values()]))
        again.load_tickets(self.now + 10)
        self.assertEqual(again.tickets[check.id].workspace_group, "heavy")
        with open(os.path.join(check.dir, "ticket.json")) as f:
            self.assertEqual(json.load(f)["workspace_group"], "heavy")  # saved at once, not only in memory

    def restart(self, config):
        with open(self.config_path, "w") as f:
            f.write(config)
        again = daemon.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                                     queries=common.InlineExecutor())
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
        def make_config(extra, base=BASE_CONFIG):
            return config.Config(config.tomllib.loads(base + extra), 128 * GIB, 32)
        cfg = make_config("[lanes.check]\n" + self.CHECK_LANE + "\n")
        self.assertEqual((cfg.lanes["check"].workspace_group, cfg.summary()["lanes"]["check"]["workspace_group"]),
                         ("heavy", "heavy"))
        for bad, said in (('workspace_group = "carg"', "shared with no other lane"),
                          ('workspace_group = ""', "must be a name"),
                          ('workspace_group = "web"', "must be exclusive")):
            with self.assertRaisesRegex(ValueError, said, msg=bad):
                make_config(f"[lanes.check]\ncores = 2\n{bad}\n")
        grouped = BASE_CONFIG.replace("[lanes.heavy]\n", '[lanes.heavy]\nworkspace_group = "cargo"\n')
        with self.assertRaisesRegex(ValueError, "lane heavy's group is 'cargo'"):
            make_config('[lanes.check]\ncores = 2\nworkspace_group = "heavy"\n', base=grouped)
        with self.assertRaisesRegex(ValueError, "lane heavy's group is 'cargo'"):  # even with a third lane beside it
            make_config('[lanes.check]\ncores = 2\nworkspace_group = "heavy"\n'
                        '[lanes.test]\ncores = 2\nworkspace_group = "heavy"\n', base=grouped)
        self.assertEqual(make_config('[lanes.check]\ncores = 2\nworkspace_group = "check"\n').lanes["check"].workspace_group,
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
        clean = client.source_of(self.repo)
        self.assertEqual((clean["dirty"], client.source_of(self.repo)), (False, clean))
        with open(os.path.join(self.repo, "new.txt"), "w") as f:
            f.write("x")
        dirty = client.source_of(self.repo)
        self.assertTrue(dirty["dirty"])
        self.assertNotEqual(dirty["fingerprint"], clean["fingerprint"])
        os.chmod(os.path.join(self.repo, "new.txt"), 0o755)  # same size and time, another mode
        self.assertNotEqual(client.source_of(self.repo)["fingerprint"], dirty["fingerprint"])

    def test_no_record_where_git_would_run_a_filter_or_hide_changes(self):
        with open(os.path.join(self.repo, ".gitattributes"), "w") as f:
            f.write("*.bin filter=lfs\n")  # untracked still: git applies it all the same
        self.assertIsNone(client.source_of(self.repo))
        os.remove(os.path.join(self.repo, ".gitattributes"))
        self.git("update-index", "--skip-worktree", "a.txt")
        self.assertIsNone(client.source_of(self.repo))
        self.git("update-index", "--no-skip-worktree", "a.txt")
        self.assertIsNotNone(client.source_of(self.repo))

    def test_no_record_with_a_repository_inside(self):
        nested = os.path.join(self.repo, "nested")
        os.makedirs(nested)
        self.git("init", "-q", where=nested)
        with open(os.path.join(nested, "b.txt"), "w") as f:
            f.write("b")
        self.git("add", "b.txt", where=nested)
        self.git("commit", "-qm", "b", where=nested)
        self.assertIsNone(client.source_of(self.repo))
        with open(os.path.join(self.repo, ".git", "info", "exclude"), "a") as f:
            f.write("nested/\n")  # ignored, it is no longer part of the tree's source
        self.assertIsNotNone(client.source_of(self.repo))

    def test_a_deleted_tracked_attributes_file_means_no_record(self):
        with open(os.path.join(self.repo, ".gitattributes"), "w") as f:
            f.write("*.txt text\n")
        self.git("add", ".gitattributes")
        self.git("commit", "-qm", "attributes")
        self.assertIsNotNone(client.source_of(self.repo))
        os.remove(os.path.join(self.repo, ".gitattributes"))  # git falls back to the index's copy
        self.assertIsNone(client.source_of(self.repo))

    def test_a_symlinked_attributes_file_means_no_record(self):
        with open(os.path.join(self.repo, ".gitattributes"), "w") as f:
            f.write("*.txt text\n")
        self.git("add", ".gitattributes")
        self.git("commit", "-qm", "attributes")
        os.remove(os.path.join(self.repo, ".gitattributes"))
        with open(os.path.join(self.tmp.name, "harmless"), "w") as f:
            f.write("")
        os.symlink(os.path.join(self.tmp.name, "harmless"), os.path.join(self.repo, ".gitattributes"))
        self.assertIsNone(client.source_of(self.repo))  # git reads the index's copy, not the link

    def test_a_relative_attributes_file_is_read_from_the_top(self):
        os.makedirs(os.path.join(self.repo, "src"))
        with open(os.path.join(self.repo, "src", "x.txt"), "w") as f:
            f.write("x")
        self.git("add", "src/x.txt")
        self.git("commit", "-qm", "src")
        with open(os.path.join(self.repo, ".git", "local-attributes"), "w") as f:
            f.write("*.txt filter=clean-it\n")
        self.git("config", "core.attributesFile", ".git/local-attributes")
        self.assertIsNone(client.source_of(os.path.join(self.repo, "src")))

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
        self.assertIsNone(client.source_of(self.repo))

    def test_an_ignored_attributes_file_still_counts(self):
        with open(os.path.join(self.repo, ".git", "info", "exclude"), "a") as f:
            f.write(".gitattributes\n")
        with open(os.path.join(self.repo, ".gitattributes"), "w") as f:
            f.write("*.txt filter=clean-it\n")
        self.assertIsNone(client.source_of(self.repo))

    def test_an_attributes_file_that_is_a_pipe_doesnt_hang_it(self):
        os.mkfifo(os.path.join(self.repo, ".gitattributes"))
        started = time.monotonic()
        self.assertIsNone(client.source_of(self.repo))  # not knowing what it says means no record
        self.assertLess(time.monotonic() - started, 4)

    def test_a_pipe_doesnt_hang_it(self):
        os.mkfifo(os.path.join(self.repo, "pipe"))
        started = time.monotonic()
        self.assertIsNotNone(client.source_of(self.repo))
        self.assertLess(time.monotonic() - started, 4)



class Pausing(DaemonCase):
    def test_a_paused_queue_starts_nothing_and_stays_paused_across_a_restart(self):
        running = self.submit(cores=2)
        self.daemon.pause("rebooting at noon", self.now)
        waiting = self.submit(cores=1)
        self.assertEqual((running.state, waiting.state, waiting.reason_cat), ("running", "queued", "paused"))
        self.assertIn("rebooting at noon", waiting.reason)
        again = daemon.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                                     queries=common.InlineExecutor())
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
        self.daemon.drop_unwatched(self.now + 100 + daemon.Daemon.VACATED_SECONDS + 1)
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
        again = daemon.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                                     queries=common.InlineExecutor())
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
        self.assertTrue(commands.paths_inside(["cargo", "test", "--manifest-path=sub/Cargo.toml"], "/r", "/r"))
        self.assertFalse(commands.paths_inside(["cargo", "test", "--manifest-path=/elsewhere/Cargo.toml"], "/r", "/r"))
        repo = self.workdir("linked")
        os.symlink(self.tmp.name, os.path.join(repo, "external"))  # a one-word path that leads outside
        self.assertFalse(commands.paths_inside(["tsc", "--noEmit", "-p", "external"], repo, repo))
        self.assertTrue(commands.paths_inside(["cargo", "test", "test_name"], repo, repo))
        os.symlink(self.tmp.name, os.path.join(repo, "ext deps"))  # a name with a space in it, leading outside
        self.assertFalse(commands.paths_inside(["cargo", "test", "--manifest-path", "ext deps/Cargo.toml"], repo, repo))
        self.assertFalse(commands.paths_inside(["tsc", "--noEmit", "--project=ext deps/tsconfig.json"], repo, repo))
        outside_file = os.path.join(self.tmp.name, "lint-me.js")
        open(outside_file, "w").close()
        os.symlink(outside_file, os.path.join(repo, "--external.js"))
        for argv in (["env", "CFLAGS=-isystemexternal", "cargo", "check"],  # -isystem + external: where it splits can't be told
                     ["eslint", "--", "--external.js"],  # after --, a name, whatever it starts with
                     ["cargo", "clippy", "--", "-Lext deps/lib"],  # a name with a space, run on to its option
                     ["env", "CARGO_ENCODED_RUSTFLAGS=--sysroot\x1fext deps", "cargo", "check"],  # cargo splits at \x1f
                     ["vitest", "run", "-r=..", "--config", "x.ts"], ["vitest", "run", "-sr", ".."],
                     ["../other/check.sh"]):  # the command's own path (with --reuse)
            self.assertFalse(commands.paths_inside(argv, repo, repo), argv)
        for argv in (["cargo", "test", "--", "--test-threads=1"], ["cargo", "clippy", "--", "-D", "warnings"],
                     ["/usr/bin/env", "cargo", "test"], ["cargo", "test", "-F", "Cli"]):
            self.assertTrue(commands.paths_inside(argv, repo, repo), argv)
        inside = commands.paths_inside
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
        self.assertIsNone(commands.sane_source({**SOURCE, "commit": "not-a-sha"}))

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
            self.assertTrue(commands.reusable_check(list(argv)), argv)
        for argv in not_checks:
            self.assertFalse(commands.reusable_check(list(argv)), argv)
        for argv in (("cargo", "build"), ("cargo", "test"), ("dotnet", "build"), ("tsc",), ("ninja",),
                     ("ninja", "-C", "build", "test")):
            self.assertTrue(commands.restart_safe(list(argv)), argv)
        for argv in (("sh", "run-tests.sh"), ("git", "push"), ("cargo", "run"), ("pnpm", "build"), ("make",),
                     ("ninja", "install"), ("ninja", "deploy")):
            self.assertFalse(commands.restart_safe(list(argv)), argv)

    def test_the_environment_that_counts(self):
        digest = commands.env_digest
        self.assertEqual(digest({"PATH": "/bin", "PWD": "/a", "SHLVL": "1", "CLAUDE_CODE_SESSION_ID": "x"}),
                         digest({"PATH": "/bin", "PWD": "/b", "SHLVL": "2", "CLAUDE_CODE_SESSION_ID": "y"}))
        for name in ("ANTIGRAVITY_AGENT", "ANTIGRAVITY_AGENTAPI_EXE", "ANTIGRAVITY_APP_DATA_DIR",
                     "ANTIGRAVITY_CONVERSATION_ID", "ANTIGRAVITY_CSRF_TOKEN", "ANTIGRAVITY_LS_ADDRESS",
                     "ANTIGRAVITY_LS_VERSION", "ANTIGRAVITY_PROJECT_ID", "ANTIGRAVITY_SOURCE_METADATA",
                     "ANTIGRAVITY_TRAJECTORY_ID"):
            self.assertEqual(digest({name: "a"}), digest({name: "b"}), name)
            self.assertEqual(digest({name: "a"}), digest({}), name)
        for name in ("DATABASE_URL", "API_TOKEN", "SESSION_BACKEND", "DB_PASSWORD", "ANTIGRAVITY_CUSTOM_FLAG"):
            self.assertNotEqual(digest({name: "a"}), digest({name: "b"}), name)



class AbandonedSubmits(DaemonCase):
    """#71: a dropped-off build whose agent session has ended, and that nobody waits on, is cancelled
    while it's still queued."""

    def setUp(self):
        super().setUp()
        self.machine.mem_available = 15 * GIB  # nothing fits: everything waits
        me = os.getpid()
        self.alive = [me, machine.proc_identity(me)[0]]
        self.gone = [me, self.alive[1] + 1]  # (another start time: the pid was reused)
        self.real_live_threads = machine.live_threads
        self.above = {}  # a client's pid -> the agent processes the daemon finds above it
        self.threads = {}  # a T3 thread -> its agent processes now
        for name, fake in (("agents_above", lambda pid: [list(a) for a in self.above.get(pid, ())]),
                           ("live_threads", lambda: dict(self.threads))):
            patch = mock.patch.object(machine, name, fake)
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
        with mock.patch.object(machine, "live_threads", side_effect=OSError("no /proc")):
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
        with mock.patch.object(machine, "live_threads", self.real_live_threads), \
                mock.patch.object(machine, "own_pids", return_value=[os.getpid()]), \
                mock.patch.object(machine, "is_agent", side_effect=OSError(24, "Too many open files")):
            self.daemon.drop_abandoned(self.now + 601)  # the look can't tell who runs: nothing is called gone
        self.assertEqual((t.cancel_requested, t.agent_now), (False, "alive"))

    def test_a_process_that_cant_be_read_isnt_called_gone(self):
        t = self.drop_off(self.alive, submission="s")
        with mock.patch.object(machine, "proc_stat", return_value=None):  # (out of descriptors, say)
            self.assertFalse(machine.proc_gone(*self.alive))  # it is there: can't tell
            self.daemon.drop_abandoned(self.now + 10_000)
        self.assertEqual((t.cancel_requested, t.agent_now), (False, "alive"))
        self.assertTrue(machine.proc_gone(*self.gone))  # another process has that pid now
        self.assertTrue(machine.proc_gone(2 ** 40, 1))  # nothing has
        self.assertTrue(machine.proc_gone(float("inf"), 1))

    def test_zero_turns_it_off(self):
        self.daemon.cfg.abandoned_seconds = 0
        t = self.drop_off(self.gone, submission="s")
        self.daemon.drop_abandoned(self.now + 10_000)
        self.assertEqual((t.cancel_requested, t.agent_now), (False, None))

    def test_the_agent_is_kept_across_a_restart_and_the_time_starts_again(self):
        t = self.drop_off(self.gone, submission="s", thread="thread-1")
        t.wanted_at = self.now - 5000
        self.daemon.save(t)
        again = daemon.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                               queries=common.InlineExecutor())
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
        self.assertEqual(machine.sane_agents(saved),
                         {"ok": good, "list": {"pids": [[5, 6]], "thread": None}})
        self.assertEqual(machine.sane_agents(["x"]), {})
        self.assertIsNone(machine.proc_identity(float("inf")))  # (no error from a number that isn't a pid)



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
        above = machine.agents_above(child)
        self.assertEqual(above[0][0], proc.pid)  # (the suite's own agent session, if any, follows it)
        self.assertTrue(machine.proc_alive(*above[0]))
        self.assertEqual(machine.live_threads().get(self.thread), [above[0]])
        proc.kill()
        proc.wait()
        self.assertFalse(machine.proc_alive(*above[0]))
        self.assertNotIn(self.thread, machine.live_threads())

    def test_no_agent_above_init_or_a_pid_that_isnt_one(self):
        self.assertEqual(machine.agents_above(1), [])
        self.assertEqual(machine.agents_above(None), [])
        self.assertEqual(machine.agents_above(2 ** 40), [])

    def test_the_programs_that_count(self):
        for exe in ("/home/u/.local/share/claude/versions/2.1.328",
                    "/home/u/.claude/remote/ccd-cli/2.1.321-3fb3e6595884",  # the desktop app's
                    "/home/u/.local/share/claude/versions/2.1.300 (deleted)"):  # replaced by an update
            self.assertTrue(machine.AGENT_EXE.search(exe), exe)
        for exe in ("/usr/bin/python3.13", "/usr/bin/node", "/home/u/.claude/skills/x/run.sh",
                    "/home/u/Code/claude/versions/notes/readme"):
            self.assertFalse(machine.AGENT_EXE.search(exe), exe)

    def test_antigravity_process_counts_as_an_agent(self):
        for name in ("antigravity", "agy"):
            fake = os.path.join(self.tmp.name, name)
            os.symlink(sys.executable, fake)
            proc, child = self.session(exe=fake, env=os.environ)
            above = machine.agents_above(child)
            self.assertEqual(above[0][0], proc.pid, name)
            self.assertTrue(machine.proc_alive(*above[0]), name)
            proc.kill()
            proc.wait()
            self.assertFalse(machine.proc_alive(*above[0]), name)



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
        self.assertEqual(config.Config({"ci_first": True, "ci_deadline": "20m"}, 64 * GIB, 16).ci_deadline, 1200.0)
        with self.assertRaises(ValueError):
            config.Config({"ci_deadline": "20m"}, 64 * GIB, 16)  # it needs ci_first
        with self.assertRaises(ValueError):
            config.Config({"ci_first": True, "ci_deadline": "5m", "starve_seconds": 600}, 64 * GIB, 16)  # under starve
        with self.assertRaises(ValueError):
            config.Config({"ci_first": True, "ci_deadline": "20m", "starve_seconds": 60}, 64 * GIB, 16)  # undone

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
        cfg = config.Config({"ci_first": True}, 64 * GIB, 16)
        self.assertTrue(cfg.ci_first)
        self.assertTrue(cfg.summary()["ci_first"])
        self.assertFalse(config.Config({}, 64 * GIB, 16).ci_first)
        with self.assertRaises(ValueError):
            config.Config({"ci_first": "sometimes"}, 64 * GIB, 16)



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
        kept = ticket.Ticket.from_record(json.loads(json.dumps(new.record())), new.dir)
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
        self.assertEqual(config.Config({}, 64 * GIB, 16).make_room_for_ci, 0.0)
        cfg = config.Config({"ci_first": True, "make_room_for_ci": "5m"}, 64 * GIB, 16)
        self.assertEqual((cfg.make_room_for_ci, cfg.summary()["make_room_for_ci"]), (300.0, 300.0))
        with self.assertRaises(ValueError):
            config.Config({"make_room_for_ci": "5m"}, 64 * GIB, 16)  # its CI build would hold no room
        with self.assertRaises(ValueError):
            config.Config({"ci_first": True, "make_room_for_ci": "soon"}, 64 * GIB, 16)

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
        cfg = config.Config({}, 64 * GIB, 16)
        self.assertEqual((cfg.make_room_behind_agents, cfg.make_room_spare_after), (True, 0.0))
        cfg = config.Config({"ci_first": True, "make_room_for_ci": "5m", "make_room_behind_agents": False,
                              "make_room_spare_after": "20m"}, 64 * GIB, 16)
        self.assertEqual((cfg.make_room_behind_agents, cfg.make_room_spare_after), (False, 1200.0))
        self.assertEqual(cfg.summary()["make_room_spare_after"], 1200.0)
        with self.assertRaises(ValueError):
            config.Config({"make_room_spare_after": "soon"}, 64 * GIB, 16)

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



if __name__ == "__main__":
    unittest.main()
