# SPDX-License-Identifier: MIT
"""Unit and end-to-end tests for non-systemd Linux support: CgroupRunner, upgraded DirectRunner
(/proc measurement, subreaper cleanup, SIGSTOP/SIGCONT pausing, MemoryMax watchdog), runtime_dir
fallback, rotating log files, `buildq daemon` CLI, and flat-cgroup caller/memory-watch fallbacks."""

from __future__ import annotations

import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

sys.dont_write_bytecode = True
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
SRC = os.path.join(ROOT, "src")
for p in (TESTS_DIR, SRC):
    if p not in sys.path:
        sys.path.insert(0, p)

from buildq import client, common, config, daemon, machine, runners, scheduler, top  # noqa: E402
from buildq.common import GIB, MIB  # noqa: E402
from helpers import BUILDQ, BUILDQD, DaemonCase, FakeMachine, FakeRunner  # noqa: E402


def clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items()
            if not k.startswith("BUILDQ_") and k not in (
                "MAKEFLAGS", "MFLAGS", "CARGO_MAKEFLAGS", "MAKELEVEL",
                "CI", "GITHUB_ACTIONS", "INVOCATION_ID", "T3CODE_THREAD_ID",
            )}


def read_text(path: str) -> str:
    with open(path) as f:
        return f.read()


class RuntimeDirAndLogs(unittest.TestCase):
    def test_runtime_dir_prefers_xdg_then_run_user_then_state_dir(self):
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/custom/run"}, clear=False):
            self.assertEqual(common.runtime_dir(), "/custom/run")
        state = "/var/tmp/bq-st"
        env = {k: v for k, v in os.environ.items() if k != "XDG_RUNTIME_DIR"}
        env["BUILDQ_STATE_DIR"] = state
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(common.os, "lstat", side_effect=FileNotFoundError):
            self.assertEqual(common.runtime_dir(), os.path.join(state, "run"))

    def test_runtime_dir_falls_back_to_tmp_when_state_path_too_long_for_af_unix(self):
        long_state = "/" + ("a" * 95)
        env = {k: v for k, v in os.environ.items() if k != "XDG_RUNTIME_DIR"}
        env["BUILDQ_STATE_DIR"] = long_state
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(common.os, "lstat", side_effect=FileNotFoundError):
            self.assertEqual(common.runtime_dir(), f"/tmp/buildq-{common.UID}")

    def test_ensure_private_dir_refuses_symlinks_and_fixes_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "real")
            os.makedirs(real, mode=0o755)
            os.chmod(real, 0o755)
            common.ensure_private_dir(real)
            self.assertEqual(stat.S_IMODE(os.lstat(real).st_mode), 0o700)

            link = os.path.join(tmp, "link")
            os.symlink(real, link)
            with self.assertRaises(PermissionError):
                common.ensure_private_dir(link)

    def test_log_file_writes_timestamps_and_rotates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "buildqd.log")
            # Run in a fresh subprocess so helpers.py's silent common.log stub isn't involved.
            code = (
                "import sys; sys.path.insert(0, sys.argv[1])\n"
                "from buildq import common\n"
                "common.configure_log_file(sys.argv[2], max_bytes=64 << 10)\n"
                "common.log('first line')\n"
                "common.log('x' * (65 << 10))\n"
                "common.log('after rotate')\n"
            )
            res = subprocess.run([sys.executable, "-c", code, SRC, path], capture_output=True, text=True, timeout=15)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertTrue(os.path.isfile(path + ".1"))
            with open(path + ".1") as f:
                first_line = f.readline()
            self.assertRegex(first_line, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4} first line\n$")
            with open(path) as f:
                self.assertIn("after rotate", f.read())


class ConfigAndProperties(unittest.TestCase):
    def make(self, text: str) -> config.Config:
        return config.Config(config.tomllib.loads(text), 64 * GIB, 16)

    def test_cgroup_runner_and_root_validation(self):
        cfg = self.make('runner = "cgroup"\ncgroup_root = "/sys/fs/cgroup/buildq-alice"\n')
        self.assertEqual(cfg.runner, "cgroup")
        self.assertEqual(cfg.cgroup_root, "/sys/fs/cgroup/buildq-alice")
        for bad in (
            'cgroup_root = "relative/path"',
            'cgroup_root = "/tmp/cgroup"',
            'cgroup_root = "/sys/fs/cgroup/../etc"',
        ):
            with self.assertRaises(ValueError, msg=bad):
                self.make(bad)

    def test_translate_cgroup_properties_maps_all_supported_keys(self):
        cfg = self.make("""
            default_lane = "heavy"
            [lanes.heavy]
            cores = 4
            mem = "4G"
            [lanes.heavy.properties]
            MemoryHigh = "50%"
            MemoryMax = "40G"
            MemorySwapMax = "infinity"
            CPUWeight = 200
            IOWeight = 150
            TasksMax = 2048
            CPUQuota = "250%"
            Nice = 5
            UnknownSystemdProp = "ignored"
        """)
        files, hints = runners.translate_cgroup_properties(cfg.lanes["heavy"], 64 * GIB)
        self.assertEqual(files["memory.high"], str(32 * GIB))
        self.assertEqual(files["memory.max"], str(40 * GIB))
        self.assertEqual(files["memory.swap.max"], "max")
        self.assertEqual(files["cpu.weight"], "200")
        self.assertEqual(files["io.weight"], "default 150")
        self.assertEqual(files["pids.max"], "2048")
        self.assertEqual(files["cpu.max"], "250000 100000")
        self.assertEqual(hints, {"nice": 5})

    def test_translate_cgroup_properties_rejects_bad_values(self):
        for prop in (
            'CPUWeight = 0',
            'IOWeight = 20000',
            'CPUQuota = "200"',
            'TasksMax = "150%"',
            'Nice = 99',
        ):
            cfg = self.make(f'default_lane = "a"\n[lanes.a]\nproperties = {{ {prop} }}\n')
            with self.assertRaises(ValueError, msg=prop):
                runners.translate_cgroup_properties(cfg.lanes["a"], 64 * GIB)

    def test_lane_nice_maps_low_cpu_weight_and_explicit_nice(self):
        low = self.make('default_lane = "a"\n[lanes.a]\nproperties = { CPUWeight = 10 }\n').lanes["a"]
        normal = self.make('default_lane = "a"\n[lanes.a]\nproperties = { CPUWeight = 100 }\n').lanes["a"]
        explicit = self.make('default_lane = "a"\n[lanes.a]\nproperties = { Nice = 7 }\n').lanes["a"]
        self.assertGreater(runners.lane_nice(low), 10)
        self.assertIsNone(runners.lane_nice(normal))
        self.assertEqual(runners.lane_nice(explicit), 7)

    def test_default_config_and_example_toml_work_on_small_memory_machines(self):
        small = config.Config({}, 2 * GIB, 2)
        self.assertEqual(small.lanes["default"].mem, 512 * MIB)
        self.assertEqual(small.lanes["default"].cores, 2)
        big = config.Config({}, 64 * GIB, 16)
        self.assertEqual(big.lanes["default"].mem, 4 * GIB)
        example_raw = config.tomllib.loads(read_text(os.path.join(ROOT, "config.example.toml")))
        example_small = config.Config(example_raw, 2 * GIB, 2)
        self.assertEqual(example_small.lanes["default"].mem, 512 * MIB)


class CgroupRunnerUnit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "buildq-user")
        self.builds = os.path.join(self.root, "builds")
        os.makedirs(self.builds)
        with open(os.path.join(self.builds, "memory.max"), "w") as f:
            f.write(f"{16 * GIB}\n")

    def tearDown(self):
        self.tmp.cleanup()

    def _make_runner(self) -> runners.CgroupRunner:
        r = runners.CgroupRunner.__new__(runners.CgroupRunner)
        r.root = self.root
        r.builds_dir = self.builds
        r.mem_total = 64 * GIB
        r.initial_memory_max = str(16 * GIB)
        r._ready = True
        return r

    def test_prepare_freeze_kill_result_and_cleanup(self):
        r = self._make_runner()
        t = types.SimpleNamespace(id="11111111-1111-1111-1111-111111111111", requeues=0, unit=None, cg=None,
                                 pid=None, pid_start=None)
        r.prepare(t, "buildq.slice")
        self.assertEqual(t.unit, f"buildq-{t.id}")
        self.assertEqual(t.cg, os.path.join(self.builds, t.unit))
        os.makedirs(t.cg)

        # Freeze / freeze_state
        with open(os.path.join(t.cg, "cgroup.freeze"), "w") as f:
            f.write("0\n")
        with open(os.path.join(t.cg, "cgroup.events"), "w") as f:
            f.write("populated 1\nfrozen 0\n")
        r.freeze(t, True)
        self.assertEqual(read_text(os.path.join(t.cg, "cgroup.freeze")).strip(), "1")
        with open(os.path.join(t.cg, "cgroup.events"), "w") as f:
            f.write("populated 1\nfrozen 1\n")
        self.assertEqual(r.freeze_state(t), (True, True, False))

        # Force stop uses cgroup.kill
        with open(os.path.join(t.cg, "cgroup.kill"), "w") as f:
            f.write("0\n")
        r.stop(t, force=True)
        self.assertEqual(read_text(os.path.join(t.cg, "cgroup.kill")).strip(), "1")

        # OOM verdict distinguishes own-cap oom-kill vs parent slice-oom-kill
        with open(os.path.join(t.cg, "memory.events.local"), "w") as f:
            f.write("oom 1\noom_kill 1\n")
        with open(os.path.join(t.cg, "memory.events"), "w") as f:
            f.write("oom 1\noom_kill 1\n")
        self.assertEqual(r.result(t), "oom-kill")

        with open(os.path.join(t.cg, "memory.events.local"), "w") as f:
            f.write("oom 0\noom_kill 0\n")
        self.assertEqual(r.result(t), "slice-oom-kill")

        # Cleanup removes empty build dir
        for name in os.listdir(t.cg):
            os.remove(os.path.join(t.cg, name))
        r.cleanup(t)
        self.assertFalse(os.path.exists(t.cg))

    def test_guard_slice_updates_and_restores_cgroup_builds_memory_max(self):
        r = self._make_runner()
        with open(os.path.join(self.builds, "memory.current"), "w") as f:
            f.write(f"{4 * GIB}\n")
        cfg_path = os.path.join(self.tmp.name, "config.toml")
        with open(cfg_path, "w") as f:
            f.write('mem_floor = "4G"\nslice_guard = true\n')
        m = FakeMachine(mem_total=64 * GIB, mem_available=10 * GIB)
        d = daemon.Daemon(cfg_path, os.path.join(self.tmp.name, "state"), machine=m, runner=r)
        d.guard_slice()
        # held (4G) + (avail 10G - floor/2 2G) = 12G (< initial 16G)
        self.assertEqual(read_text(os.path.join(self.builds, "memory.max")).strip(), str(12 * GIB))
        d.guard_slice(stopping=True)
        self.assertEqual(read_text(os.path.join(self.builds, "memory.max")).strip(), str(16 * GIB))

    def test_probe_root_enforces_cgroup_v2_common_ancestor_rule(self):
        root = "/sys/fs/cgroup/buildq-alice"
        writable_ancestors = {
            f"{root}/cgroup.procs",
            f"{root}/builds/cgroup.procs",
        }

        def fake_access(path: str, mode: int) -> bool:
            if path.endswith("/cgroup.procs"):
                return path in writable_ancestors or ".buildq-probe-" in path
            return True

        with mock.patch.object(runners.machine, "is_cgroup_v2", return_value=True), \
                mock.patch.object(runners.machine, "cgroup_pids", return_value=[]), \
                mock.patch.object(runners.machine, "cgroup_controllers", return_value={"cpu", "memory"}), \
                mock.patch.object(runners.machine, "cgroup_subtree_controllers", return_value={"cpu", "memory"}), \
                mock.patch.object(runners.os.path, "realpath", side_effect=lambda p: p), \
                mock.patch.object(runners.os.path, "isdir", return_value=True), \
                mock.patch.object(runners.os.path, "isfile", return_value=True), \
                mock.patch.object(runners.os, "makedirs"), \
                mock.patch.object(runners.os, "rmdir"), \
                mock.patch.object(runners.os, "geteuid", return_value=1000), \
                mock.patch.object(runners.os, "access", side_effect=fake_access):
            # Outside delegated subtree (common ancestor = /sys/fs/cgroup, not writable) -> False
            with mock.patch.object(runners.machine, "proc_cgroup", return_value="/sys/fs/cgroup"):
                self.assertFalse(runners.CgroupRunner._probe_root(root))
            with mock.patch.object(runners.machine, "proc_cgroup", return_value="/sys/fs/cgroup/openrc.buildqd"):
                self.assertFalse(runners.CgroupRunner._probe_root(root))
            # Inside delegated subtree (<root>/daemon, common ancestor = <root>, writable) -> True
            with mock.patch.object(runners.machine, "proc_cgroup", return_value=f"{root}/daemon"):
                self.assertTrue(runners.CgroupRunner._probe_root(root))


class FallbacksUnit(unittest.TestCase):
    def test_caller_falls_back_on_flat_cgroup(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("BUILDQ_CALLER", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID",
                            "T3CODE_THREAD_ID", "ANTIGRAVITY_CONVERSATION_ID",
                            "TMUX_PANE", "STY", "TERM_SESSION_ID", "KITTY_WINDOW_ID", "WEZTERM_PANE")}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("builtins.open", mock.mock_open(read_data="0::/\n")):
            c = client.caller()
            self.assertIsNotNone(c)
            self.assertTrue(c.startswith("sid-"), c)
        with mock.patch.dict(os.environ, {**env, "TMUX_PANE": "%7"}, clear=True), \
                mock.patch("builtins.open", mock.mock_open(read_data="0::/\n")):
            self.assertEqual(client.caller(), "tmux:%7")

    def test_biggest_processes_returns_sorted_entries(self):
        top_procs, complete = machine.biggest_processes(count=3, at_least=1)
        self.assertTrue(complete)
        self.assertGreaterEqual(len(top_procs), 1)
        self.assertTrue(top_procs[0][2].startswith("pid "))
        self.assertGreater(top_procs[0][0] + top_procs[0][1], 0)

    def test_pid_is_buildqd_rejects_unrelated_process(self):
        self.assertFalse(machine.pid_is_buildqd(os.getpid()))
        self.assertFalse(machine.pid_is_buildqd(1))
        self.assertFalse(machine.pid_is_buildqd(-1))

    def test_default_socket_uses_explicit_state_dir(self):
        env = {k: v for k, v in os.environ.items() if k not in ("XDG_RUNTIME_DIR", "BUILDQ_SOCKET", "BUILDQ_STATE_DIR")}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(common.os, "lstat", side_effect=FileNotFoundError):
            self.assertEqual(common.default_socket("/var/tmp/bq-explicit"), "/var/tmp/bq-explicit/run/buildq.sock")

    def test_make_supports_fifo_detects_gnu_make_4_3_vs_4_4(self):
        scheduler._MAKE_FIFO_CACHE.clear()
        with tempfile.TemporaryDirectory() as tmp:
            fake_make = os.path.join(tmp, "make")
            with open(fake_make, "w") as f:
                f.write("#!/bin/sh\n")
            os.chmod(fake_make, 0o755)
            with mock.patch.object(scheduler.subprocess, "run",
                                   return_value=types.SimpleNamespace(stdout="GNU Make 4.3\nBuilt for x86_64")):
                self.assertFalse(scheduler._make_supports_fifo(fake_make, None))
            scheduler._MAKE_FIFO_CACHE.clear()
            with mock.patch.object(scheduler.subprocess, "run",
                                   return_value=types.SimpleNamespace(stdout="GNU Make 4.4.1\nBuilt for x86_64")):
                self.assertTrue(scheduler._make_supports_fifo(fake_make, None))
        scheduler._MAKE_FIFO_CACHE.clear()


class DirectRunnerEndToEnd(unittest.TestCase):
    """End-to-end tests of the upgraded DirectRunner + `buildq daemon` CLI without systemd."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sock = os.path.join(self.tmp.name, "q.sock")
        self.state = os.path.join(self.tmp.name, "state")
        self.cfg = os.path.join(self.tmp.name, "config.toml")
        with open(self.cfg, "w") as f:
            f.write("""
max_jobs = 2
mem_floor = "64M"
disk_floor = "0"
cpu_psi_block = 1000
mem_psi_block = 1000
runner = "direct"
default_lane = "small"
[lanes.small]
cores = 2
mem = "32M"
exclusive = false
[lanes.tiny]
cores = 1
mem = "32M"
exclusive = false
properties = { MemoryMax = "48M" }
""")
        self.env = dict(clean_env(), PYTHONDONTWRITEBYTECODE="1",
                        BUILDQ_SOCKET=self.sock, BUILDQ_STATE_DIR=self.state,
                        BUILDQ_CONFIG=self.cfg, BUILDQ_RUNNER="direct")

    def tearDown(self):
        self.q("daemon", "stop")
        self.tmp.cleanup()

    def q(self, *args, timeout=60):
        return subprocess.run([sys.executable, BUILDQ, *args], env=self.env,
                              capture_output=True, text=True, timeout=timeout)

    def test_daemon_cli_full_lifecycle_and_build_survives_restart(self):
        # Stopped initially -> status returns 3
        self.assertEqual(self.q("daemon", "status").returncode, 3)

        # Start
        started = self.q("daemon", "start")
        self.assertEqual(started.returncode, 0, started.stderr + started.stdout)
        self.assertIn("direct runner", started.stdout)
        self.assertTrue(os.path.isfile(os.path.join(self.state, "buildqd.pid")))
        self.assertTrue(os.path.isfile(os.path.join(self.state, "buildqd.log")))

        # Status + reload
        st = self.q("daemon", "status")
        self.assertEqual(st.returncode, 0, st.stderr)
        self.assertIn("is running", st.stdout)
        self.assertEqual(self.q("daemon", "reload").returncode, 0)

        # Submit a 4s build, restart the daemon mid-build, verify build finishes cleanly!
        sub = self.q("submit", "--", sys.executable, "-c", "import time; time.sleep(3); print('survived')")
        self.assertEqual(sub.returncode, 0, sub.stderr)
        tid = sub.stdout.strip()
        time.sleep(0.5)
        restarted = self.q("daemon", "restart")
        self.assertEqual(restarted.returncode, 0, restarted.stderr + restarted.stdout)
        waited = self.q("wait", tid, "--timeout", "30s")
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.assertIn("survived", self.q("logs", tid).stdout)

        # Logs
        logs = self.q("daemon", "logs", "--tail", "20")
        self.assertEqual(logs.returncode, 0, logs.stderr)
        self.assertIn("direct runner", logs.stdout)

        # Stop
        stopped = self.q("daemon", "stop")
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertEqual(self.q("daemon", "status").returncode, 3)

    def test_direct_runner_measures_memory_cpu_learns_history_and_shows_in_top(self):
        self.assertEqual(self.q("daemon", "start").returncode, 0)
        # Allocate ~40 MiB and burn CPU for ~2.5s so /proc measurement & top both see it.
        code = (
            "import time\n"
            "buf = bytearray(40 * 1024 * 1024)\n"
            "for i in range(0, len(buf), 4096): buf[i] = 1\n"
            "end = time.monotonic() + 2.5\n"
            "x = 0\n"
            "while time.monotonic() < end: x += 1\n"
            "print(len(buf), x > 0)\n"
        )
        sub = self.q("submit", "--", sys.executable, "-c", code)
        self.assertEqual(sub.returncode, 0, sub.stderr)
        tid = sub.stdout.strip()
        time.sleep(1.5)
        top_out = self.q("top", "--once")
        self.assertEqual(top_out.returncode, 0, top_out.stderr)
        self.assertIn(tid[:8], top_out.stdout)

        self.assertEqual(self.q("wait", tid, "--timeout", "30s").returncode, 0)
        row = json.loads(self.q("status", tid, "--json").stdout)[0]
        self.assertTrue(row["measured"], row)
        self.assertGreaterEqual(row["mem_peak"], 25 * MIB, row)
        self.assertGreater(row["avg_cores"], 0.1, row)

        # History learned from the measured direct run!
        hist_path = os.path.join(self.state, "history.jsonl")
        self.assertTrue(os.path.isfile(hist_path))
        with open(hist_path) as f:
            entries = [json.loads(line) for line in f if line.strip()]
        self.assertTrue(any(e["peak_mem"] >= 25 * MIB for e in entries), entries)

    def test_direct_runner_memory_max_watchdog_kills_oversized_build(self):
        self.assertEqual(self.q("daemon", "start").returncode, 0)
        # Lane `tiny` has MemoryMax = 48M; allocate 100 MiB and hold for 8s.
        code = (
            "import time\n"
            "buf = bytearray(100 * 1024 * 1024)\n"
            "for i in range(0, len(buf), 4096): buf[i] = 1\n"
            "time.sleep(8)\n"
        )
        res = self.q("run", "--lane", "tiny", "--", sys.executable, "-c", code)
        self.assertNotEqual(res.returncode, 0)
        rows = json.loads(self.q("list", "--all", "--json").stdout)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["oom"], rows[0])
        self.assertIn("MemoryMax", rows[0].get("error") or "", rows[0])

    def test_direct_runner_sigstop_freeze_and_sigcont_thaw(self):
        self.assertEqual(self.q("daemon", "start").returncode, 0)
        flag = os.path.join(self.tmp.name, "tick.txt")
        code = (
            "import sys, time\n"
            "p = sys.argv[1]\n"
            "for i in range(30):\n"
            "    with open(p, 'w') as f: f.write(str(i))\n"
            "    time.sleep(0.1)\n"
        )
        sub = self.q("submit", "--", sys.executable, "-c", code, flag)
        self.assertEqual(sub.returncode, 0, sub.stderr)
        tid = sub.stdout.strip()
        deadline = time.time() + 10
        while time.time() < deadline and not os.path.exists(flag):
            time.sleep(0.05)
        rec = common.read_json(os.path.join(self.state, "tickets", tid, "ticket.json"))
        t = types.SimpleNamespace(id=tid, pid=rec["pid"], pid_start=rec["pid_start"])
        runner = runners.DirectRunner()
        runner.freeze(t, True)
        time.sleep(0.15)
        own, took, _ = runner.freeze_state(t)
        self.assertTrue(own)
        self.assertTrue(took)
        snap1 = read_text(flag)
        time.sleep(0.4)
        snap2 = read_text(flag)
        self.assertEqual(snap1, snap2, "process kept writing while SIGSTOP-paused")
        runner.freeze(t, False)
        self.assertEqual(self.q("wait", tid, "--timeout", "30s").returncode, 0)

    def test_direct_runner_subreaper_tracks_and_cleans_orphaned_env_cleared_grandchild(self):
        self.assertEqual(self.q("daemon", "start").returncode, 0)
        pid_out = os.path.join(self.tmp.name, "orphan.pid")
        # Child spawns a detached, env-cleared grandchild (no BUILDQ_JOB!) and sleeps; cancel must kill it.
        code = (
            "import os, subprocess, sys, time\n"
            "p = subprocess.Popen(['/bin/sleep', '60'], env={}, start_new_session=True)\n"
            "with open(sys.argv[1], 'w') as f: f.write(str(p.pid))\n"
            "time.sleep(60)\n"
        )
        sub = self.q("submit", "--", sys.executable, "-c", code, pid_out)
        self.assertEqual(sub.returncode, 0, sub.stderr)
        tid = sub.stdout.strip()
        deadline = time.time() + 10
        while time.time() < deadline and not os.path.exists(pid_out):
            time.sleep(0.05)
        orphan_pid = int(read_text(pid_out).strip())
        self.assertIsNotNone(machine.proc_stat(orphan_pid))
        # Force-stop via DirectRunner directly to verify tree capture before killing supervisor.
        rec = common.read_json(os.path.join(self.state, "tickets", tid, "ticket.json"))
        t = types.SimpleNamespace(id=tid, pid=rec["pid"], pid_start=rec["pid_start"])
        runners.DirectRunner().stop(t, force=True)
        deadline = time.time() + 5
        while time.time() < deadline and machine.proc_stat(orphan_pid) is not None:
            time.sleep(0.05)
        self.assertIsNone(machine.proc_stat(orphan_pid), "env-cleared detached grandchild leaked")


if __name__ == "__main__":
    unittest.main()
