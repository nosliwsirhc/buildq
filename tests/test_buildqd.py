"""Unit tests for buildqd: admission, cores, order, turns, history, keys, configuration, the supervisor."""

import importlib.machinery
import importlib.util
import json
import os
import shutil
import signal
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
buildqd.log = lambda message, level=None: None  # keep test output readable
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
        self.daemon = buildqd.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner)
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
        again = buildqd.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner)
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
        again = buildqd.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner)
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
                      ("pnpm", "run", "build:deploy"), ("sh", "run-tests.sh"), ("git", "push"), ("make", "test")]
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
        self.assertEqual(digest({"PATH": "/bin", "PWD": "/a", "SHLVL": "1", "CLAUDE_CODE_SESSION_ID": "x"}),
                         digest({"PATH": "/bin", "PWD": "/b", "SHLVL": "2", "CLAUDE_CODE_SESSION_ID": "y"}))
        for name in ("DATABASE_URL", "API_TOKEN", "SESSION_BACKEND", "DB_PASSWORD"):
            self.assertNotEqual(digest({name: "a"}), digest({name: "b"}), name)


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
        again = buildqd.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner)
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
        again = buildqd.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner)
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
        again = buildqd.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner)
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
        again = buildqd.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner)
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

    def test_off_means_nothing_is_sent(self):
        t = self.submit(("cargo", "test"), source=SOURCE)
        self.finish(t, 0)
        self.settle()
        self.daemon.cfg.github_status = False  # turned off with a post still queued
        self.drain(self.now + 60)
        self.assertEqual(self.calls(), [])


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


if __name__ == "__main__":
    unittest.main()
