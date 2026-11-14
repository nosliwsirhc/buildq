"""End-to-end tests: a real buildqd on a private socket, driven through the real buildq client.

By default the daemon runs builds as plain processes (BUILDQ_RUNNER=direct), so these tests need
no systemd user session. With BUILDQ_TEST_SCOPES=1 on a machine that has one, every test runs again
with builds as systemd services (ScopeTests), plus checks that only systemd can do.
"""

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

sys.dont_write_bytecode = True
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILDQD = os.path.join(ROOT, "bin", "buildqd")
BUILDQ = os.path.join(ROOT, "bin", "buildq")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

CONFIG = """
max_jobs = 1
unwatched_seconds = 3
mem_floor = "64M"
disk_floor = "0"
mem_psi_block = 1000
cpu_psi_block = 1000
default_lane = "small"
[env]
BUILDQ_TEST_SLOT = "slot-{id}"
[lanes.small]
cores = 1
mem = "32M"
exclusive = false
[lanes.capped]
cores = 1
mem = "32M"
exclusive = false
properties = { MemoryMax = "256M" }
[lanes.fast]
cores = 1
mem = "32M"
exclusive = false
priority = 100
extra_jobs = 1
waits_for_cpu = false
[[lane_rules]]
match = "^echo-fast "
lane = "fast"
"""


def clean_env():
    return {k: v for k, v in os.environ.items()
            if not k.startswith("BUILDQ_") and k not in ("MAKEFLAGS", "MFLAGS", "CARGO_MAKEFLAGS", "MAKELEVEL",
                                                         "CI", "GITHUB_ACTIONS", "INVOCATION_ID")}


class Daemon:
    def __init__(self, tmp, runner="direct", config=CONFIG):
        self.socket = os.path.join(tmp, "q.sock")
        self.state = os.path.join(tmp, "state")
        self.config = os.path.join(tmp, "config.toml")
        self.runner = runner
        with open(self.config, "w") as f:
            f.write(config)
        self.log_path = os.path.join(tmp, "daemon.log")
        self.proc = None

    def start(self):
        log = open(self.log_path, "a")
        env = dict(clean_env(), PYTHONDONTWRITEBYTECODE="1", BUILDQ_RUNNER=self.runner)
        self.proc = subprocess.Popen([sys.executable, BUILDQD, "--socket", self.socket, "--state-dir", self.state,
                                      "--config", self.config], stdout=log, stderr=subprocess.STDOUT, env=env)
        log.close()
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.request({"op": "ping"}) is not None:
                return
            time.sleep(0.1)
        raise RuntimeError("buildqd didn't start:\n" + self.log())

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(10)

    def log(self):
        with open(self.log_path) as f:
            return f.read()

    def request(self, message):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5)
        try:
            sock.connect(self.socket)
            sock.sendall(json.dumps(message).encode() + b"\n")
            data = b""
            while not data.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    return None
                data += chunk
            return json.loads(data)
        except OSError:
            return None
        finally:
            sock.close()

    def ticket(self, ref):
        reply = self.request({"op": "status", "tickets": [ref]})
        return reply["tickets"][0] if reply and reply.get("tickets") else None


class Harness(unittest.TestCase):
    """A private daemon, a working directory and a client environment for each test."""

    runner = "direct"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.daemon = Daemon(self.tmp.name, self.runner, self.config())
        self.daemon.start()
        self.work = os.path.join(self.tmp.name, "work")
        os.makedirs(self.work)
        bin_dir = os.path.join(self.tmp.name, "bin")
        os.makedirs(bin_dir)
        with open(os.path.join(bin_dir, "echo-fast"), "w") as f:  # a command the lane rules send to "fast"
            f.write("#!/bin/sh\necho \"$@\"\n")
        os.chmod(os.path.join(bin_dir, "echo-fast"), 0o755)
        self.env = dict(clean_env(), BUILDQ_SOCKET=self.daemon.socket, BUILDQ_STATE_DIR=self.daemon.state,
                        PYTHONDONTWRITEBYTECODE="1", PATH=bin_dir + os.pathsep + os.environ["PATH"])

    def config(self):
        return CONFIG

    def tearDown(self):
        reply = self.daemon.request({"op": "list", "all": True}) or {}
        for row in reply.get("tickets", []):
            if row["state"] in ("queued", "starting", "running") and not row.get("outside"):
                self.daemon.request({"op": "cancel", "ticket": row["id"]})
        deadline = time.time() + 30
        while time.time() < deadline:
            reply = self.daemon.request({"op": "status"}) or {}
            if not reply.get("running"):
                break
            time.sleep(0.2)
        self.daemon.stop()
        self.tmp.cleanup()

    def q(self, *args, env=None, timeout=90, **kwargs):
        return subprocess.run([sys.executable, BUILDQ, *args], env=env or self.env, cwd=self.work,
                              capture_output=True, text=True, timeout=timeout, **kwargs)

    def q_background(self, *args, env=None):
        return subprocess.Popen([sys.executable, BUILDQ, *args], env=env or self.env, cwd=self.work,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def submit(self, *args):
        result = self.q("submit", *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout.strip(), UUID)
        return result.stdout.strip()

    def wait_for(self, ref, states, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            row = self.daemon.ticket(ref)
            if row and row["state"] in states:
                return row
            time.sleep(0.1)
        self.fail(f"{ref} never reached {states}; daemon log:\n{self.daemon.log()}")

    @staticmethod
    def close(proc):
        for pipe in (proc.stdout, proc.stderr):
            if pipe:
                pipe.close()



class EndToEnd(Harness):
    def test_submit_status_wait_and_logs(self):
        ticket = self.submit("--", "sh", "-c", "echo hello; exit 3")
        waited = self.q("wait", ticket)
        self.assertEqual(waited.returncode, 3, waited.stderr)
        self.assertIn("failed (exit 3)", waited.stderr)
        self.assertEqual(self.q("logs", ticket).stdout, "hello\n")
        detail = self.q("status", ticket[:8])
        self.assertIn("failed (exit 3", detail.stdout)
        self.assertIn("sh -c", detail.stdout)
        rows = json.loads(self.q("status", ticket, "--json").stdout)
        self.assertEqual((rows[0]["state"], rows[0]["exit"]), ("failed", 3))
        deadline = time.time() + 20
        found = ""
        while time.time() < deadline and ticket not in found:  # the analytics are written by a thread
            found = self.q("query", f"SELECT id, state, exit FROM build WHERE id = '{ticket}'").stdout
            time.sleep(0.2)
        self.assertIn(f"{ticket}\tfailed\t3", found, self.daemon.log())
        self.assertIn(ticket[:8], self.q("list").stdout)

    def git_repo(self):
        """Make the working directory a repository with one commit, so builds record their source."""
        git = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "init.defaultBranch=main"]
        subprocess.run([*git, "init", "-q"], cwd=self.work, check=True)
        with open(os.path.join(self.work, "a.txt"), "w") as f:
            f.write("one\n")
        subprocess.run([*git, "add", "a.txt"], cwd=self.work, check=True)
        subprocess.run([*git, "commit", "-qm", "one"], cwd=self.work, check=True)

    def test_the_same_build_of_the_same_source_runs_once(self):
        self.git_repo()
        first = self.submit("--", "sh", "-c", "sleep 2; echo done")
        joined = self.q("submit", "--", "sh", "-c", "sleep 2; echo done")
        self.assertEqual(joined.stdout.strip(), first, joined.stderr)
        self.assertIn("already", joined.stderr)
        detail = self.q("status", first[:8]).stdout
        self.assertIn("source    commit", detail)
        with open(os.path.join(self.work, "a.txt"), "w") as f:
            f.write("two\n")  # an uncommitted change: different source, a build of its own
        changed = self.submit("--", "sh", "-c", "sleep 2; echo done")
        self.assertNotEqual(changed, first)
        self.assertIn("+ uncommitted changes", self.q("status", changed[:8]).stdout)
        self.assertEqual(self.q("wait", first).returncode, 0)
        ran = self.q("run", "--reuse", "--", "sh", "-c", "sleep 2; echo done")
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertNotIn("passed this command", ran.stderr)  # the source changed since: it ran
        mine = re.search(r"ticket ([0-9a-f-]{36})", ran.stderr).group(1)
        deadline = time.time() + 20
        while time.time() < deadline and not self.daemon.ticket(mine).get("source_verified"):
            time.sleep(0.1)  # its tree is checked again as it ends
        self.assertTrue(self.daemon.ticket(mine).get("source_verified"), self.daemon.log())
        self.assertIn("the same when the build started and ended", self.q("status", mine[:8]).stdout)
        def settled(row):  # changed (seen at its start or its end), or checked at both
            return row.get("source_changed") or row.get("source_verified") is not None

        deadline = time.time() + 20
        while time.time() < deadline and not settled(self.daemon.ticket(first)):
            time.sleep(0.1)
        verdict = self.daemon.ticket(first)
        self.assertTrue(settled(verdict), self.daemon.log())
        said = ("it changed after it was recorded" if verdict.get("source_changed")
                else "the same when the build started and ended" if verdict.get("source_verified")
                else "it couldn't be checked")  # (a stalled test edits after it ended; a check can fail)
        self.assertIn(said, self.q("status", first[:8]).stdout)
        again = self.q("run", "--reuse", "--", "sh", "-c", "sleep 2; echo done")
        self.assertIn("passed this command", again.stderr)
        self.assertEqual(again.stdout, "done\n")  # the earlier run's output, at once

    def test_top_shows_a_running_build(self):
        ticket = self.submit("--", "sh", "-c", "end=$(($(date +%s)+30)); while [ $(date +%s) -lt $end ]; do :; done")
        self.wait_for(ticket, ("running",))
        out = self.q("top", "--once")
        self.assertEqual(out.returncode, 0, out.stderr)
        for expected in ("CPU", "MEM", "DISK", "1 running", ticket[:8]):
            self.assertIn(expected, out.stdout)
        row = next(line for line in out.stdout.splitlines() if ticket[:8] in line)
        if self.runner == "systemd":  # a cgroup of its own: its CPU is measured
            time.sleep(1)
            out = self.q("top", "--once")
            row = next(line for line in out.stdout.splitlines() if ticket[:8] in line)
            found = re.search(r"(\d+\.\d)/\d+", row)
            self.assertIsNotNone(found, out.stdout)
            used = float(found.group(1))
            self.assertGreater(used, 0.2, out.stdout)
        self.q("cancel", ticket)

    def test_pause_and_resume(self):
        self.assertEqual(self.q("pause", "maintenance").returncode, 0)
        ticket = self.submit("--", "true")
        self.assertIn("PAUSED", self.q("status").stdout)
        self.assertIn("maintenance", self.daemon.ticket(ticket)["reason"])
        self.assertEqual(self.q("resume").returncode, 0)
        self.assertEqual(self.q("wait", ticket, "--timeout", "30s").returncode, 0)

    def test_a_failed_wait_shows_the_end_of_the_output(self):
        ticket = self.submit("--", "sh", "-c", "echo first; echo the-error >&2; exit 2")
        waited = self.q("wait", ticket)
        self.assertEqual(waited.returncode, 2)
        self.assertIn("the last 20 lines", waited.stderr)
        self.assertIn("the-error", waited.stderr)

    def test_report_and_help(self):
        for _ in range(2):
            self.assertEqual(self.q("run", "--", "sh", "-c", "exit 0").returncode, 0)
        report = self.q("report")
        self.assertEqual(report.returncode, 0, report.stderr)
        self.assertIn("RUNS", report.stdout)
        rows = json.loads(self.q("report", "--json").stdout)
        self.assertEqual(sum(r["runs"] for r in rows), 2)
        helped = self.q("help", "agents")
        self.assertEqual(helped.returncode, 0, helped.stderr)
        self.assertIn("name: buildq", helped.stdout)

    def test_a_build_that_can_never_fit_is_said_to_be_cancelled(self):
        submitted = self.q("submit", "--mem", "100T", "--", "true")
        self.assertIn("can't fit on this machine as it is: cancel it", submitted.stderr)
        self.q("cancel", submitted.stdout.strip())

    def test_hints_name_the_wrapper_in_front(self):
        submitted = self.q("submit", "--", "true")
        self.assertIn(f"buildq wait {submitted.stdout.strip()[:8]} --timeout 9m", submitted.stderr)
        self.env["BUILDQ_AS"] = "~/bin/heavy"
        try:
            submitted = self.q("submit", "--", "true")
            self.assertIn(f"~/bin/heavy --wait {submitted.stdout.strip()[:8]} --timeout 9m", submitted.stderr)
            ran = self.q("run", "--", "true")
            self.assertIn("`~/bin/heavy --status ", ran.stderr)
        finally:
            del self.env["BUILDQ_AS"]

    def test_run_streams_output_and_exits_with_the_build(self):
        result = self.q("run", "--", "sh", "-c", "echo out; echo err >&2; exit 7")
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(result.stdout, "out\n")  # stdout to stdout, stderr to stderr
        self.assertIn("err\n", result.stderr)
        self.assertRegex(result.stderr, UUID)

    def test_builds_take_turns(self):
        started = time.time()
        first = self.q_background("run", "sleep", "1.5")
        second = self.q_background("run", "sleep", "1.5")
        self.assertEqual(first.wait(60), 0)
        self.assertEqual(second.wait(60), 0)
        self.assertGreaterEqual(time.time() - started, 3.0)
        self.assertIn("waiting", first.stderr.read() + second.stderr.read())
        self.close(first), self.close(second)

    def test_a_fast_lane_goes_first_and_has_its_own_turn(self):
        slow = self.submit("sleep", "8")  # long enough that the fast one is done first on a busy machine
        self.wait_for(slow, ("running",))
        queued_slow = self.submit("sleep", "1")
        fast = self.submit("echo-fast", "quick")  # its lane rule picks the fast lane
        row = self.wait_for(fast, ("succeeded",), timeout=10)  # beside the running build, past the queued one
        self.assertEqual(row["lane"], "fast")
        waiting = self.daemon.ticket(queued_slow)  # (on a busy machine it may have started since: not before)
        self.assertTrue(waiting["state"] == "queued" or waiting["started"] >= row["ended"], waiting)

    def test_interrupting_run_cancels_the_build(self):
        client = self.q_background("run", "sleep", "30")
        ticket = UUID.search(client.stderr.readline()).group(0)
        self.wait_for(ticket, ("running",))
        client.send_signal(signal.SIGTERM)
        self.assertEqual(client.wait(60), -signal.SIGTERM)  # it ends by the signal, as an interrupted command does
        self.close(client)
        self.wait_for(ticket, ("cancelled",))

    def test_a_run_nobody_follows_is_cancelled(self):
        client = self.q_background("run", "sleep", "60")
        ticket = UUID.search(client.stderr.readline()).group(0)
        self.wait_for(ticket, ("running",))
        client.kill()  # SIGKILL: no chance to cancel it itself
        client.wait()
        self.close(client)
        self.wait_for(ticket, ("cancelled",), timeout=30)  # after unwatched_seconds

    def test_a_dropped_off_build_runs_on_alone(self):
        ticket = self.submit("sh", "-c", "sleep 5; echo done")
        self.assertEqual(self.q("wait", ticket).returncode, 0)  # well past unwatched_seconds
        self.assertEqual(self.q("logs", ticket).stdout, "done\n")

    def test_submit_inside_a_build_is_refused(self):
        result = self.q("run", "sh", "-c", f"{sys.executable} {BUILDQ} submit true; echo $?")
        self.assertEqual(result.stdout.strip(), "2")

    def test_environment_cores_and_nested_runs(self):
        result = self.q("run", "sh", "-c", 'echo "$BUILDQ_TEST_SLOT $BUILDQ_JOB $BUILDQ_CORES"; '
                                           f'{sys.executable} {BUILDQ} run sh -c "echo nested"')
        self.assertEqual(result.returncode, 0, result.stderr)
        first, nested = result.stdout.splitlines()
        slot, job, cores = first.split()
        self.assertEqual((slot, cores), (f"slot-{job}", "1"))
        self.assertEqual(nested, "nested")  # ran directly, though max_jobs = 1 and its parent holds the turn

    def test_a_fake_build_id_doesnt_skip_the_queue(self):
        env = dict(self.env, BUILDQ_JOB="00000000-0000-0000-0000-000000000000")
        result = self.q("run", "true", env=env)
        self.assertEqual(result.returncode, 0)
        self.assertRegex(result.stderr, UUID)  # it was queued like any other build

    def test_cancel_and_list(self):
        running = self.submit("sleep", "30")
        self.wait_for(running, ("running",))
        queued = self.submit("sleep", "1")
        self.assertEqual(self.daemon.ticket(queued)["state"], "queued")
        self.assertEqual(self.q("cancel", queued).returncode, 0)
        self.assertEqual(self.daemon.ticket(queued)["state"], "cancelled")
        listing = self.q("list").stdout
        self.assertIn(running[:8], listing)
        self.assertIn(queued[:8], listing)
        self.q("cancel", running)
        self.wait_for(running, ("cancelled",))

    def test_timeout(self):
        result = self.q("run", "--timeout", "1", "--", "sleep", "30")
        self.assertEqual(result.returncode, 124, result.stderr)
        self.assertIn("timed out", result.stderr)

    def test_notify_runs_when_the_build_ends(self):
        marker = os.path.join(self.tmp.name, "marker")
        ticket = self.submit("--notify", f'echo "$BUILDQ_STATE $BUILDQ_EXIT" > {marker}', "--", "true")
        self.assertEqual(self.q("wait", ticket).returncode, 0)
        for _ in range(100):
            if os.path.exists(marker) and os.path.getsize(marker):
                break
            time.sleep(0.05)
        with open(marker) as f:
            self.assertEqual(f.read().strip(), "succeeded 0")

    def test_refusals(self):
        self.assertEqual(self.q("run", "--lane", "nope", "true").returncode, 2)
        self.assertEqual(self.q("status", "zzzz").returncode, 1)
        self.assertEqual(self.q("wait", "zzzz").returncode, 1)

    def test_status_overview(self):
        ticket = self.submit("sleep", "3")
        self.wait_for(ticket, ("running",))
        text = self.q("status").stdout
        self.assertIn("running 1 of at most 1", text)
        self.assertIn("sleep 3", text)

    def test_builds_survive_a_daemon_restart(self):
        running = self.submit("sh", "-c", "sleep 3; echo first")
        self.wait_for(running, ("running",))
        queued = self.submit("sh", "-c", "echo second")
        self.daemon.stop()
        self.daemon.start()
        self.assertEqual(self.q("wait", running).returncode, 0)
        self.assertEqual(self.q("wait", queued).returncode, 0)
        self.assertEqual(self.q("logs", running).stdout + self.q("logs", queued).stdout, "first\nsecond\n")

    def test_processes_that_leave_the_group_are_stopped_with_the_build(self):
        pidfile = os.path.join(self.tmp.name, "escaped.pid")
        result = self.q("run", "sh", "-c", f"setsid sleep 300 >/dev/null 2>&1 & echo $! > {pidfile}; echo started")
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(pidfile) as f:
            pid = int(f.read())
        deadline = time.time() + 30
        while time.time() < deadline and os.path.exists(f"/proc/{pid}"):
            time.sleep(0.2)
        self.assertFalse(os.path.exists(f"/proc/{pid}"), "a process that left its group outlived the build")

    def test_a_resent_submission_runs_once(self):
        message = {"op": "submit", "submission": "same-one", "argv": ["sleep", "1"], "cwd": self.work,
                   "env": {"PATH": os.environ["PATH"]}}
        first = self.daemon.request(message)
        second = self.daemon.request(message)
        self.assertEqual(first["id"], second["id"])

    @unittest.skipUnless(shutil.which("make"), "needs GNU make")
    def test_make_follows_its_jobserver(self):
        version = subprocess.run(["make", "--version"], capture_output=True, text=True).stdout
        match = re.search(r"GNU Make (\d+)\.(\d+)", version)
        if not match or (int(match.group(1)), int(match.group(2))) < (4, 4):
            self.skipTest("needs GNU make 4.4 or newer (fifo jobservers)")
        with open(os.path.join(self.work, "Makefile"), "w") as f:
            f.write("T := t1 t2 t3 t4 t5 t6\nall: $(T)\n$(T):\n"
                    "\t@mkdir -p running; touch running/$@; ls running | wc -l >> counts; sleep 1; rm running/$@\n")
        started = time.time()
        result = self.q("run", "--cores", "2", "--", "make", "-j", "all")  # its -j gives way to the jobserver
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertNotIn("jobserver unavailable", result.stdout)
        with open(os.path.join(self.work, "counts")) as f:
            most = max(int(line) for line in f if line.strip())
        self.assertEqual(most, 2)  # two at a time: the token in its pipe and the one every make has
        self.assertGreaterEqual(time.time() - started, 3.0)


class Enforcement(Harness):
    """The shim sends heavy commands through the queue; the rustc guard refuses compiles outside it."""

    def make_tools(self):
        real = os.path.join(self.tmp.name, "real")
        shims = os.path.join(self.tmp.name, "shims")
        os.makedirs(real)
        os.makedirs(shims)
        for tool in ("make", "cargo"):
            with open(os.path.join(real, tool), "w") as f:
                f.write(f"#!/bin/sh\necho \"{tool} $* job=${{BUILDQ_JOB:-none}}\"\n")
            os.chmod(os.path.join(real, tool), 0o755)
            os.symlink(os.path.join(ROOT, "bin", "buildq-shim"), os.path.join(shims, tool))
        return dict(self.env, PATH=os.pathsep.join([shims, real, self.env["PATH"]]))

    def test_the_shim_queues_heavy_commands_only(self):
        env = self.make_tools()
        heavy = subprocess.run(["cargo", "build", "--release"], env=env, cwd=self.work, capture_output=True,
                               text=True, timeout=60)
        self.assertEqual(heavy.returncode, 0, heavy.stderr)
        self.assertRegex(heavy.stdout, r"^cargo build --release job=[0-9a-f-]{36}$")  # it ran as a build
        light = subprocess.run(["cargo", "fmt"], env=env, cwd=self.work, capture_output=True, text=True, timeout=30)
        self.assertEqual(light.stdout, "cargo fmt job=none\n")  # straight through
        dry = subprocess.run(["make", "-n"], env=env, cwd=self.work, capture_output=True, text=True, timeout=30)
        self.assertEqual(dry.stdout, "make -n job=none\n")
        off = subprocess.run(["make"], env=dict(env, BUILDQ_SHIM="off"), cwd=self.work, capture_output=True,
                             text=True, timeout=30)
        self.assertEqual(off.stdout, "make  job=none\n")
        run = subprocess.run(["cargo", "run", "--bin", "x"], env=env, cwd=self.work, capture_output=True, text=True,
                             timeout=60)  # the build in the queue, then the program here
        build_line, run_line = run.stdout.splitlines()
        self.assertRegex(build_line, r"^cargo build --bin x job=[0-9a-f-]{36}$")
        self.assertEqual(run_line, "cargo run --bin x job=none")

    def test_the_rustc_guard(self):
        rustc = os.path.join(self.tmp.name, "fake-rustc")
        with open(rustc, "w") as f:
            f.write("#!/bin/sh\necho compiled\n")
        os.chmod(rustc, 0o755)
        guard = os.path.join(ROOT, "bin", "buildq-rustc-guard")
        env = {k: v for k, v in self.env.items() if k not in ("CI", "GITHUB_ACTIONS")}
        env["XDG_CONFIG_HOME"] = self.tmp.name  # no guard.env
        with open("/proc/self/cgroup") as f:
            if "/buildq.slice/" in f.read():  # the guard passes every compile inside a queued build
                self.skipTest("the suite runs inside a buildq build, where the guard refuses nothing")

        def compile_(extra_env=None, *args):
            return subprocess.run([guard, rustc, *(args or ("--crate-name", "demo", "src/lib.rs"))],
                                  env={**env, **(extra_env or {})}, capture_output=True, text=True, timeout=30)

        refused = compile_()
        self.assertEqual(refused.returncode, 1)
        self.assertIn("refusing to compile demo", refused.stderr)
        self.assertEqual(compile_(None, "-vV").stdout, "compiled\n")  # cargo's version query
        self.assertEqual(compile_({"CI": "true"}).returncode, 1)  # CI=true alone isn't a CI job
        self.assertEqual(compile_({"GITHUB_ACTIONS": "true", "RUNNER_NAME": "r1"}).stdout, "compiled\n")
        self.assertEqual(compile_({"BUILDQ_JOB": "00000000-0000-0000-0000-000000000000"}).returncode, 1)
        inside = self.q("run", "--", guard, rustc, "--crate-name", "demo", "src/lib.rs")  # inside a build
        self.assertEqual((inside.returncode, inside.stdout), (0, "compiled\n"), inside.stderr)
        self.assertIn("    buildq run -- cargo ...", refused.stderr)
        os.makedirs(os.path.join(self.tmp.name, "buildq"), exist_ok=True)
        with open(os.path.join(self.tmp.name, "buildq", "guard.env"), "w") as f:
            f.write('RUN_WITH="~/bin/heavy"\n')
        self.assertIn("    ~/bin/heavy cargo ...", compile_().stderr)  # the machine's wrapper


class SocketActivation(unittest.TestCase):
    """systemd hands the daemon its listening socket (fd 3, LISTEN_FDS=1). The socket belongs to
    systemd: stopping the daemon must leave its file in place for the next daemon."""

    def test_the_socket_outlives_the_daemon(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "q.sock")
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(path)
            listener.listen(16)
            fd = listener.fileno()
            env = dict(clean_env(), LISTEN_FDS="1", BUILDQ_RUNNER="direct", PYTHONDONTWRITEBYTECODE="1")
            script = f'exec 3<&{fd}; export LISTEN_PID=$$; exec "$0" "$@"'
            with open(os.path.join(tmp, "config.toml"), "w") as f:
                f.write(CONFIG)
            proc = subprocess.Popen(["sh", "-c", script, sys.executable, BUILDQD, "--socket", path, "--state-dir",
                                     os.path.join(tmp, "state"), "--config", os.path.join(tmp, "config.toml")],
                                    env=env, pass_fds=[fd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                client = Daemon(tmp)
                client.socket = path
                deadline = time.time() + 20
                while time.time() < deadline and client.request({"op": "ping"}) is None:
                    time.sleep(0.1)
                self.assertIsNotNone(client.request({"op": "ping"}), "the daemon didn't answer on the passed socket")
            finally:
                proc.terminate()
                proc.wait(15)
            self.assertTrue(os.path.exists(path), "stopping the daemon removed systemd's socket file")
            listener.close()


class Fallback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = dict(clean_env(), BUILDQ_SOCKET=os.path.join(self.tmp.name, "q.sock"), PYTHONDONTWRITEBYTECODE="1")

    def tearDown(self):
        self.tmp.cleanup()

    def q(self, *args, **extra):
        return subprocess.run([sys.executable, BUILDQ, *args], env={**self.env, **extra},
                              capture_output=True, text=True, timeout=60)

    def test_explicit_choices(self):
        self.assertEqual(self.q("run", "true", BUILDQ_FALLBACK="fail").returncode, 75)
        self.assertEqual(self.q("run", "echo", "hi", BUILDQ_FALLBACK="run").stdout, "hi\n")
        self.assertEqual(self.q("run", "true", BUILDQ_FALLBACK="echo via").stdout, "via true\n")
        self.assertEqual(self.q("submit", "true").returncode, 75)

    def test_by_default_run_waits_for_the_daemon(self):
        client = subprocess.Popen([sys.executable, BUILDQ, "run", "echo", "queued at last"], env=self.env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(3)
        self.assertIsNone(client.poll(), "it ran without the daemon")
        daemon = Daemon(self.tmp.name)
        daemon.start()
        try:
            out, err = client.communicate(timeout=60)
            self.assertEqual((client.returncode, out), (0, "queued at last\n"), err)
            self.assertIn("waiting for it", err)
        finally:
            daemon.stop()


def scopes_work():
    try:
        return subprocess.run(["systemd-run", "--user", "--scope", "--quiet", "true"], capture_output=True,
                              timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@unittest.skipUnless(os.environ.get("BUILDQ_TEST_SCOPES") and scopes_work(),
                     "set BUILDQ_TEST_SCOPES=1 on a machine with a systemd user session")
class ScopeTests(EndToEnd):
    """Every end-to-end test again, with builds as systemd services, and what only systemd can do."""

    runner = "systemd"

    def config(self):
        return 'slice = "buildqtest.slice"\n' + CONFIG

    def test_a_build_runs_in_its_service_with_its_lane_limits(self):
        script = 'cat /proc/self/cgroup; cat "/sys/fs/cgroup$(cut -d: -f3 /proc/self/cgroup)/memory.max"'
        result = self.q("run", "--lane", "capped", "sh", "-c", script)
        self.assertEqual(result.returncode, 0, result.stderr)
        cgroup, memory_max = result.stdout.splitlines()
        self.assertRegex(cgroup, r"/buildqtest\.slice/buildq-[0-9a-f-]{36}\.service$")
        self.assertEqual(memory_max, str(256 << 20))

    def test_the_daemon_measures_the_build(self):
        burn = "import time\nx = b'x' * (96 << 20)\nt = time.time()\nwhile time.time() - t < 4: pass\n"
        ticket = self.submit(sys.executable, "-c", burn)
        deadline = time.time() + 20
        row = None
        while time.time() < deadline:
            row = self.daemon.ticket(ticket)
            if row["state"] == "running" and row["mem_now"] > 64 << 20:
                break
            time.sleep(0.2)
        self.assertGreater(row["mem_now"], 64 << 20)
        self.assertEqual(self.q("wait", ticket).returncode, 0)
        with open(os.path.join(self.daemon.state, "history.jsonl")) as f:
            record = json.loads(f.readlines()[-1])
        self.assertGreater(record["peak_mem"], 64 << 20)
        self.assertGreater(record["peak_cores"], 0.5)

    def test_the_slice_guard_limits_the_slice(self):
        # The guard writes the slice's memory.max on the daemon's rebalance (every five seconds),
        # once the slice exists: a build that outlasts one sees a number there, not systemd's "max".
        script = 'sleep 7; cat "/sys/fs/cgroup$(dirname "$(cut -d: -f3 /proc/self/cgroup)")/memory.max"'
        result = self.q("run", "sh", "-c", script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout.strip(), r"^\d+$")  # bytes: the guard's limit



if __name__ == "__main__":
    unittest.main()
