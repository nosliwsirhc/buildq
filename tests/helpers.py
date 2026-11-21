"""Shared test helpers, stand-in machine/runner, and DaemonCase base class."""

import json
import os
import sys
import tempfile
import time
import unittest

sys.dont_write_bytecode = True
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from buildq import common, daemon  # noqa: E402
from buildq.common import GIB, MIB  # noqa: E402

BUILDQD = os.path.join(ROOT, "bin", "buildqd")
BUILDQ = os.path.join(ROOT, "bin", "buildq")
common.log = lambda message, level=None: None  # keep test output readable


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
        self.daemon = daemon.Daemon(self.config_path, self.state, machine=self.machine, runner=self.runner,
                                     queries=common.InlineExecutor())
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
