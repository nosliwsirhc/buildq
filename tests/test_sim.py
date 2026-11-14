"""The replay simulator (bench/sim.py): it drives the real daemon, deterministically."""
import importlib.machinery
import importlib.util
import json
import os
import io
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


sim = load("sim", os.path.join(ROOT, "bench", "sim.py"))
scenarios = sim.builtin
GIB = sim.GIB
CONFIG = sim.DEFAULT_CONFIG
BUILDQD = sim.DEFAULT_BUILDQD


def agents(n, peak, wall, start=0, step=10, **extra):
    """n agent cargo test builds, each in its own target folder, with a history of their size."""
    return [scenarios.build(start + i * step, ("cargo", "test", "-p", f"c{i}"), peak, wall, 6, shape=scenarios.TESTS,
                            repo="app", caller=f"session-{i}", workspace=f"wt{i}", history=True, **extra)
            for i in range(n)]


def ci_step(t, peak, wall, name="lint", shape=scenarios.EARLY, **extra):
    return scenarios.build(t, ("pnpm", "eslint"), peak, wall, 4, shape=shape, lane="heavy", ci=True,
                           repo="app", caller="runner-1", name=name, history=True, **extra)


def scenario(jobs, **machine):
    return {"name": "test", "machine": dict(scenarios.WORKSTATION, **machine), "jobs": jobs, "seed_history": True}


class SimCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = unittest.mock.patch.dict(os.environ, {"TMPDIR": self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)

    def config(self, **changes):
        """bench.toml with some top-level settings changed."""
        with open(CONFIG) as f:
            text = f.read()
        for key, value in changes.items():
            line = next((l for l in text.splitlines() if l.startswith(f"{key} =")), None)
            text = text.replace(line, f"{key} = {value}") if line else f"{key} = {value}\n{text}"  # (before any table)
        path = os.path.join(self.tmp.name, f"config-{len(os.listdir(self.tmp.name))}.toml")
        with open(path, "w") as f:
            f.write(text)
        return path

    def run_sim(self, jobs, config=CONFIG, **machine):
        summary, _ = sim.simulate(scenario(jobs, **machine), config, BUILDQD, name=f"buildqd_t{id(jobs)}")
        return summary


class Replay(SimCase):
    def test_the_same_input_gives_the_same_summary(self):
        jobs = agents(6, 12, 300) + [ci_step(120, 14, 60)]
        a = self.run_sim(jobs)
        b = self.run_sim(jobs)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
        self.assertEqual(a["outcomes"], {"succeeded": 7})

    def test_a_burst_keeps_the_memory_floor(self):
        summary = self.run_sim(agents(12, 12, 200, step=1))
        self.assertEqual(summary["oom_kills"], {"own_cap": 0, "machine": 0})
        self.assertGreaterEqual(summary["least_free_gib"], 16)  # mem_floor
        self.assertGreater(summary["wait"]["agent"]["max"], 0)  # not all of them fit at once

    def test_ci_first_lowers_a_ci_build_s_wait(self):
        # Without ci_first the queued agent builds, expected to finish sooner, would go first.
        jobs = agents(9, 10, 300, step=5) + [ci_step(60, 14, 900)]
        off = self.run_sim(jobs, self.config(ci_first="false", make_room_for_ci='"0"'))
        on = self.run_sim(jobs, self.config(make_room_for_ci='"0"'))
        self.assertLess(on["wait"]["ci"]["p50"], off["wait"]["ci"]["p50"])

    def test_make_room_for_ci_stops_builds_only_when_on(self):
        jobs = agents(6, 12, 1800, step=5) + [ci_step(60, 16, 60)]
        off = self.run_sim(jobs, self.config(make_room_for_ci='"0"'))
        on = self.run_sim(jobs)
        self.assertEqual(off["stops_for_ci"], 0)
        self.assertGreater(on["stops_for_ci"], 0)
        self.assertLess(on["wait"]["ci"]["max"], off["wait"]["ci"]["max"])
        self.assertGreater(on["stopped_run_s"], 0)

    def test_a_ci_job_past_its_time_limit_is_cancelled(self):
        jobs = agents(6, 12, 3000, step=5) + [ci_step(60, 16, 60, timeout_s=600)]
        summary = self.run_sim(jobs, self.config(make_room_for_ci='"0"'))
        self.assertEqual((summary["ci_timed_out"], summary["ci_timed_out_in_queue"]), (1, 1))
        self.assertEqual(summary["outcomes"].get("cancelled"), 1)

    def test_a_configuration_that_doesnt_load_is_an_error_not_the_defaults(self):
        bad = self.config(max_jobs='"eight"')
        with self.assertRaises(ValueError):
            self.run_sim(agents(1, 1, 10), bad)

    def test_a_lane_time_limit_ends_a_build(self):
        jobs = [scenarios.build(0, ("pnpm", "vitest"), 1, 120, 1, lane="web", repo="app", history=True)]
        with open(CONFIG) as f:
            text = f.read().replace('timeout = "45m"', 'timeout = "60s"')
        path = os.path.join(self.tmp.name, "short.toml")
        with open(path, "w") as f:
            f.write(text)
        summary = self.run_sim(jobs, path)
        self.assertEqual(summary["outcomes"], {"failed": 1})

    def test_builds_sharing_a_workspace_take_turns(self):
        jobs = [scenarios.build(i, ("cargo", "build"), 2, 100, 2, repo="app", caller=f"s{i}",
                                workspace="/shared/target", history=True) for i in range(2)]
        _, s = sim.simulate(scenario(jobs), CONFIG, BUILDQD, name="buildqd_ws")
        first, second = sorted(s.records.values(), key=lambda r: r["submitted"])
        self.assertGreaterEqual(second["first_start"], first["ended"])

    def test_a_tenth_with_no_sample_isnt_zero_memory(self):
        job = sim.Job.__new__(sim.Job)
        job.spec = {"wall": 47, "profile": [1024, 0, 1024, 0, 1024, 0, 1024, 0, 1024, 0]}
        self.assertEqual({job.memory_at(p) for p in range(47)}, {1024 * sim.MIB})

    def test_memory_limits_apply_in_a_build_s_last_second(self):
        jobs = [scenarios.build(0, ("pnpm", "vitest"), 20, 1, 1, shape=(1.0,) * 10, lane="web", repo="app")]
        summary = self.run_sim(jobs)
        self.assertEqual((summary["oom_kills"]["own_cap"], summary["outcomes"]), (1, {"failed": 1}))

    def test_the_daemon_sees_a_build_s_last_second(self):
        jobs = [scenarios.build(0, ("cargo", "build"), 3, 1, 1, shape=(1.0,) * 10, repo="app")]  # one second
        _, s = sim.simulate(scenario(jobs), CONFIG, BUILDQD, name="buildqd_last")
        (t,) = s.daemon.tickets.values()
        self.assertEqual(t.mem_peak, 3 * GIB)

    def test_a_run_cut_off_by_its_horizon_says_so(self):
        state = os.path.join(self.tmp.name, "cut")
        os.makedirs(state)
        s = sim.Simulation(scenario(agents(1, 2, 600)), CONFIG, BUILDQD, state, name="buildqd_cut")
        summary = s.run(horizon=60)
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["makespan_s"], 60)

    def test_re_planning_a_build_that_outgrows_its_estimate_keeps_the_floor(self):
        results = {}
        for outgrown in (True, False):
            state = tempfile.mkdtemp(dir=self.tmp.name)
            s = sim.Simulation(scenarios.outgrows_estimate(), CONFIG, BUILDQD, state, name=f"buildqd_og{outgrown}")
            if not outgrown:
                s.mod.Daemon.outgrown = lambda daemon, t: None  # a daemon without it
            results[outgrown] = s.run()
        self.assertEqual(results[True]["below_floor_s"], 0)
        self.assertGreater(results[False]["below_floor_s"], 0)  # so a change that loses it shows

    def test_past_the_peak_lets_holds_go(self):
        on = self.run_sim(scenarios.ci_behind_agents()["jobs"])
        off = self.run_sim(scenarios.ci_behind_agents()["jobs"], self.config(release_after_peak="false"))
        self.assertGreater(on["released_after_peak"], 0)
        self.assertEqual(off["released_after_peak"], 0)

    def test_memory_running_short_stops_a_build_and_queues_it_again(self):
        # Their history says 3 GB; they use 22 GB each, on a machine with room for two of them.
        jobs = [scenarios.build(i * 5, ("cargo", "build", "-p", f"c{i}"), 22, 600, 4, shape=(0.2, 0.6, 1, 1, 1, 1, 1, 1, 1, 1),
                                repo="app", caller=f"s{i}", workspace=f"w{i}", history=True, history_peak=3 * GIB)
                for i in range(4)]
        summary = self.run_sim(jobs, mem_total=80 * GIB, background_mem=0)
        self.assertGreater(summary["stops_for_memory"], 0)
        self.assertEqual(summary["oom_kills"]["machine"], 0)
        self.assertEqual(summary["outcomes"], {"succeeded": 4})
        off = self.run_sim(jobs, self.config(shed_memory="false"), mem_total=80 * GIB, background_mem=0)
        self.assertGreater(off["oom_kills"]["machine"], 0)  # nothing gives memory back: the kernel kills

    def test_compare_shows_the_head_alone_when_the_base_cant_load_the_configuration(self):
        trace = os.path.join(self.tmp.name, "one.jsonl")
        with open(trace, "w") as f:
            f.write(json.dumps(scenarios.build(0, ("cargo", "build"), 2, 30, 2, repo="app")) + "\n")
        with open(CONFIG) as f:
            text = f.read()
        bad = os.path.join(self.tmp.name, "unknown.toml")
        with open(bad, "w") as f:
            f.write("not_a_setting = 1\n" + text)
        args = sim.argparse.Namespace(scenario=[trace], base=BUILDQD, head=BUILDQD, config=CONFIG, base_config=bad,
                                      summary_file=None, seed_history=False)
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            sim.cmd_compare(args)
        self.assertIn("can't run this configuration", out.getvalue())

    def test_a_lane_the_configuration_doesnt_have_falls_to_the_lane_rules(self):
        jobs = [scenarios.build(0, ("cargo", "check"), 1, 30, 1, lane="l123abc", repo="app")]
        _, s = sim.simulate(scenario(jobs), CONFIG, BUILDQD, name="buildqd_lane")
        (t,) = s.daemon.tickets.values()
        self.assertEqual((t.lane, t.state), ("check", "succeeded"))

    def test_a_script_stopped_for_memory_fails_and_counts_as_a_stop(self):
        jobs = [scenarios.build(i * 5, ("bash", f"build{i}.sh"), 22, 600, 4, shape=(0.2, 0.6, 1, 1, 1, 1, 1, 1, 1, 1),
                                repo="app", caller=f"s{i}", workspace=f"w{i}", history=True, history_peak=3 * GIB)
                for i in range(4)]
        summary = self.run_sim(jobs, mem_total=80 * GIB, background_mem=0)
        self.assertGreater(summary["stops_for_memory"], 0)  # it couldn't start over, so it failed: still a stop
        self.assertGreater(summary["outcomes"].get("failed", 0), 0)

    def test_hooks_never_run(self):
        mark = os.path.join(self.tmp.name, "hook-ran")
        summary = self.run_sim(agents(1, 1, 10), self.config(on_finish=json.dumps(f"touch {mark}")))
        self.assertEqual(summary["outcomes"], {"succeeded": 1})
        self.assertFalse(os.path.exists(mark))

    def test_a_ci_build_that_ends_as_its_limit_comes_isnt_timed_out(self):
        jobs = [scenarios.build(0, ("pnpm", "eslint"), 1, 60, 1, lane="heavy", ci=True, repo="app", caller="runner-1",
                                timeout_s=60, history=True)]
        summary = self.run_sim(jobs)
        self.assertEqual((summary["ci_timed_out"], summary["outcomes"]), (0, {"succeeded": 1}))

    def test_a_build_that_ends_gives_its_memory_back_that_second(self):
        flat = (1.0,) * 10
        jobs = [scenarios.build(0, ("cargo", "build", "-p", "brief"), 25, 1, 1, shape=flat, repo="app", caller="a",
                                workspace="wa", history=True, history_peak=10 * GIB),
                scenarios.build(0, ("cargo", "build", "-p", "long"), 20, 300, 1, shape=flat, repo="app", caller="b",
                                workspace="wb", history=True, history_peak=10 * GIB)]
        # 45 GB in use in the brief build's one second, of 47: had it still counted after it ended, the long
        # build would be stopped for memory (under a quarter of the floor free).
        summary = self.run_sim(jobs, mem_total=47 * GIB, background_mem=0)
        self.assertEqual((summary["stops_for_memory"], summary["outcomes"]), (0, {"succeeded": 2}))

    def test_a_build_s_last_second_counts_toward_busy_cores(self):
        state = os.path.join(self.tmp.name, "busy")
        os.makedirs(state)
        spec = scenarios.build(0, ("cargo", "build"), 1, 1, 8, repo="app", history=True)
        s = sim.Simulation(scenario([spec], background_cpu=0.0), CONFIG, BUILDQD, state, name="buildqd_busy")
        s.submit(spec, 0)
        s.clock.now += 1
        s.step()  # its one second of work: it ends in it
        self.assertEqual(s.machine.busy_now, 8.0)
        self.assertGreater(s.machine.busy, 0.0)

    def test_a_cut_off_run_is_flagged_in_a_comparison(self):
        done = {"complete": True, "makespan_s": 600.0,
                "turnaround": {"agent": {"p50": 60.0, "p95": 60.0, "max": 60.0}},
                "wait": {"ci": {"p50": None, "p95": None, "max": None},
                "agent": {"p50": 0.0, "p95": 0.0, "max": 0.0}}, "ci_timed_out": 0, "stops_for_memory": 0,
                "stops_for_ci": 0, "stopped_run_s": 0.0, "cancelled_run_s": 0.0,
                "oom_kills": {"own_cap": 0, "machine": 0}, "below_floor_s": 0, "idle_core_s_while_waiting": 0.0}
        cut = dict(done, complete=False, makespan_s=21600.0)
        text = sim.compare_markdown("x", done, cut)
        self.assertIn("| makespan | 10.0m | 6.0h | cut off |", text)
        self.assertIn("The head run was cut off", text)

    def test_the_built_in_scenarios_load(self):
        for name, make in scenarios.SCENARIOS.items():
            s = make()
            self.assertTrue(s["jobs"], name)
            self.assertEqual(s, make(), name)  # generated from a fixed seed


class Export(SimCase):
    def metrics(self):
        buildqd = load("buildqd_for_export", BUILDQD)
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute("CREATE TABLE build (" + ", ".join(f"{n} {t}" for n, t in buildqd.BUILD_COLUMNS) + ")")
        rows = [("a", 1000, 900, "secretrepo|heavy|cargo test -p private-crate", "ci:x", "ci:secretrepo:tests",
                 "heavy", "runner-9", 1, 600.0, 12 * GIB, json.dumps([100, 200, 300]), 6.5, 0, "succeeded",
                 "/home/someone/secret/target", 0, 600.0),
                ("b", 2000, 960, "secretrepo|web|pnpm vitest", None, None, "web", "session-x", 0, 120.0, GIB,
                 None, 2.0, 1, "failed", None, 0, 120.0),
                ("c", 3000, 1000, "secretrepo|heavy|python3 private_script.py", None, None, "heavy", "s", 0, 1000.0,
                 GIB, json.dumps([1] * 10), 1.0, None, "cancelled", None, 2, None),
                ("d", 3100, 1010, "secretrepo|clientlane|deploy-acmeclient.sh acmeprod", None, None, "clientlane", "s",
                 0, 10.0, GIB, None, 1.0, 0, "succeeded", None, 0, 10.0),
                ("e", 3200, 1020, "secretrepo|heavy|make acme-firmware flash -j8", None, None, "heavy", "s", 0, 10.0,
                 GIB, None, 1.0, 0, "succeeded", None, 0, 10.0)]
        db.executemany("INSERT INTO build (id, ended, submitted, key, label, name, lane, caller, from_ci, wall, "
                       "mem_peak, profile, avg_cores, exit, state, repo, requeues, attempt_wall) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        db.execute("UPDATE build SET workspace = 'tree:/home/someone/secretrepo' WHERE id = 'a'")
        db.execute("INSERT INTO build (id, ended, submitted, key, lane, wall, mem_peak, state, requeues, attempt_wall) "
                   "VALUES ('z', 4000, 1100, 'secretrepo|heavy|cargo build', 'heavy', 90.0, 1, 'cancelled', 1, 0)")
        return db

    def test_a_metrics_db_becomes_a_trace(self):
        out = sim.export_rows(self.metrics(), None, None, anonymize=False)
        self.assertEqual([e["t"] for e in out], [0.0, 60.0, 100.0, 110.0, 120.0])
        self.assertEqual(out[0]["argv"], ["cargo", "test", "-p", "private-crate"])
        self.assertEqual((out[0]["ci"], out[0]["lane"], out[0]["profile"]), (True, "heavy", [100, 200, 300]))
        self.assertEqual((out[1]["ci"], out[1]["exit"], out[1]["peak_mem"]), (False, 1, GIB))
        self.assertEqual(out[0]["workspace"], "tree:/home/someone/secretrepo")  # what it wrote to, not its source repo
        self.assertNotIn("workspace", out[1])  # no workspace recorded: none made up
        self.assertEqual(len(out), 5)  # 'z' never ran its last attempt: left out
        # cancelled, with no exit code: a failure; stopped twice, with no attempt_wall: no profile to place
        self.assertEqual((out[2]["exit"], out[2]["state"], out[2]["requeued"]), (1, "cancelled", 2))
        self.assertNotIn("profile", out[2])

    def test_a_window_starts_with_the_builds_already_in_the_machine(self):
        # 'a' (900-1000) was running at 990: it enters as it started. 'b' (960, ran 1880-2000) was
        # queued: it enters as the window begins. Both keep their real submission.
        out = sim.export_rows(self.metrics(), 990, None, anonymize=False)
        self.assertEqual([e["t"] for e in out], [0.0, 90.0, 100.0, 110.0, 120.0])
        self.assertEqual([e.get("warmup", False) for e in out], [True, True, False, False, False])
        self.assertEqual([e.get("submitted_at") for e in out[:3]], [0.0, 60.0, None])
        self.assertEqual(len(sim.export_rows(self.metrics(), 1000, None, anonymize=False)), 4)  # 'a' had ended

    def test_a_window_replays_the_wait_its_builds_had_behind_one_already_running(self):
        # A (10000-11000) held workspace w; B, submitted at 10100, waited for it and ran 11000-12000;
        # C came in at 11500 and waited 500s for B. From 11500, A is gone, but B enters as it really
        # started, so C waits as long. D was cancelled while queued; the only whole run of its command
        # (A) ended before the window, and is what it replays as.
        buildqd = load("buildqd_for_window", BUILDQD)
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute("CREATE TABLE build (" + ", ".join(f"{n} {t}" for n, t in buildqd.BUILD_COLUMNS) + ")")
        rows = [("a", 10000, 11000, 1000.0, "succeeded", "app|heavy|cargo test -p a", "w", 0.0),
                ("b", 10100, 12000, 1000.0, "succeeded", "app|heavy|cargo test -p b", "w", 900.0),
                ("c", 11500, 12100, 100.0, "succeeded", "app|heavy|cargo test -p c", "w", 500.0),
                ("d", 11400, 11600, 0.0, "cancelled", "app|heavy|cargo test -p a", None, 200.0),
                ("e", 19000, 22000, 3000.0, "succeeded", "app|heavy|cargo test -p a", None, 0.0)]  # after --until
        db.executemany("INSERT INTO build (id, submitted, ended, wall, attempt_wall, state, key, lane, workspace, "
                       "waited, mem_peak, avg_cores, exit) VALUES (?,?,?,?,?,?,?,'heavy',?,?,?,1.0,0)",
                       [(i, s, e, w, w, st, k, ws, wt, GIB) for i, s, e, w, st, k, ws, wt in rows])
        out = sim.export_rows(db, 11500, 13000, anonymize=False)
        self.assertEqual([(e["t"], e.get("submitted_at"), e.get("warmup", False)) for e in out],
                         [(0.0, -900.0, True), (500.0, 400.0, True), (500.0, None, False)])  # b, d, c
        d = out[1]
        self.assertEqual((d["wall"], d["cancel_at"]), (1000.0, 200.0))  # A's run (not e's, after), cancelled 200s in
        path = os.path.join(self.tmp.name, "window.jsonl")
        with open(path, "w") as f:
            for entry in out:
                f.write(json.dumps(entry) + "\n")
        summary, _ = sim.simulate(sim.load_scenario(path), CONFIG, BUILDQD, name="buildqd_window")
        agent = summary["against_recorded"]["agent"]
        self.assertEqual(agent["builds"], 1)  # c alone: b and d are warm-up
        self.assertAlmostEqual(agent["p50"][1], 500.0, delta=15)  # it waited for b, as it did
        self.assertEqual(summary["cancelled_as_in_trace"], 1)
        # With a lead-in, A replays too and B waits for it in the replay: C still waits 500s.
        out = sim.export_rows(db, 11500, 13000, anonymize=False, lead_in=1000)
        self.assertEqual([(e["t"], e.get("submitted_at"), e.get("warmup", False)) for e in out],
                         [(0.0, 0.0, True), (500.0, 100.0, True), (1400.0, None, True), (1500.0, None, False)])
        with open(path, "w") as f:
            for entry in out:
                f.write(json.dumps(entry) + "\n")
        summary, _ = sim.simulate(sim.load_scenario(path), CONFIG, BUILDQD, name="buildqd_lead_in")
        self.assertAlmostEqual(summary["against_recorded"]["agent"]["p50"][1], 500.0, delta=15)

    def test_a_last_attempt_that_never_ran_is_placed_as_queued(self):
        # Requeued, then cancelled while queued again (attempt_wall 0): it was queued when the window
        # began, whatever its earlier attempts took, so it enters then and holds w as its whole run
        # does; the build submitted at 5000 waits for it, as it did.
        buildqd = load("buildqd_for_attempt", BUILDQD)
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute("CREATE TABLE build (" + ", ".join(f"{n} {t}" for n, t in buildqd.BUILD_COLUMNS) + ")")
        db.executemany("INSERT INTO build (id, submitted, ended, wall, attempt_wall, state, key, lane, workspace, "
                       "waited, requeues, mem_peak, avg_cores, exit) VALUES (?,?,?,?,?,?,?,'heavy','w',?,?,?,1.0,?)",
                       [("run", 0, 1000, 1000.0, 1000.0, "succeeded", "app|heavy|cargo test", 0.0, 0, GIB, 0),
                        ("x", 1000, 6000, 3000.0, 0.0, "cancelled", "app|heavy|cargo test", 2000.0, 1, GIB, None),
                        ("m", 5000, 7100, 100.0, 100.0, "succeeded", "app|heavy|cargo check", 1000.0, 0, GIB, 0)])
        out = sim.export_rows(db, 5000, None, anonymize=False)
        self.assertEqual([(e["t"], e.get("submitted_at")) for e in out], [(0.0, -4000.0), (0.0, None)])
        path = os.path.join(self.tmp.name, "attempt.jsonl")
        with open(path, "w") as f:
            for entry in out:
                f.write(json.dumps(entry) + "\n")
        summary, _ = sim.simulate(sim.load_scenario(path), CONFIG, BUILDQD, name="buildqd_attempt")
        self.assertAlmostEqual(summary["against_recorded"]["agent"]["p50"][1], 1000.0, delta=15)

    def test_a_warm_up_build_is_as_old_as_it_was_from_the_daemon_s_first_pass(self):
        state = os.path.join(self.tmp.name, "aged")
        os.makedirs(state)
        s = sim.Simulation(scenario([]), CONFIG, BUILDQD, state, name="buildqd_aged")
        seen = []
        schedule = s.daemon.schedule
        s.daemon.schedule = lambda now, *a, **k: (seen.append(next(reversed(s.daemon.tickets.values())).submitted),
                                                  schedule(now, *a, **k))[1]
        s.submit(dict(scenarios.build(0, ("cargo", "test"), 1, 60, 1, repo="app"), submitted_at=-2000.0), 0)
        self.assertEqual(seen, [sim.EPOCH - 2000])  # the pass in submit saw its real age
        self.assertEqual(s.daemon.schedule.__name__, "<lambda>")  # (what was there before is back)

    def test_a_ci_build_that_ran_past_the_default_limit_keeps_its_longer_one(self):
        buildqd = load("buildqd_for_limit", BUILDQD)
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute("CREATE TABLE build (" + ", ".join(f"{n} {t}" for n, t in buildqd.BUILD_COLUMNS) + ")")
        db.executemany("INSERT INTO build (id, submitted, ended, wall, state, key, lane, from_ci, mem_peak, exit) "
                       "VALUES (?,?,?,?,?,'app|heavy|cargo test','heavy',1,?,0)",
                       [("long", 0, 4000, 1000.0, "succeeded", GIB), ("short", 100, 700, 600.0, "succeeded", GIB)])
        out = sim.export_rows(db, None, None, anonymize=False)
        self.assertEqual([e.get("timeout_s") for e in out], [4060, None])
        args = sim.argparse.Namespace(db=":memory:", since=None, until=None, anonymize=False, lead_in="1h")
        err = io.StringIO()
        with unittest.mock.patch("sys.stderr", err):
            self.assertEqual(sim.cmd_export(args), 2)  # a lead-in needs a window to lead into
        self.assertIn("--since", err.getvalue())

    def test_anonymized_traces_name_nothing(self):
        out = sim.export_rows(self.metrics(), None, None, anonymize=True)
        text = json.dumps(out)
        for word in ("secretrepo", "private-crate", "private_script", "runner-9", "session-x", "ci:", "someone",
                     "acme", "clientlane"):
            self.assertNotIn(word, text)
        self.assertEqual(out[4]["argv"][0], "make")  # a public tool
        self.assertEqual(out[4]["argv"][3], "-j8")  # and options stay
        self.assertEqual(out[0]["argv"][:3], ["cargo", "test", "-p"])  # the tool, its subcommand and options stay

    def test_a_build_cancelled_while_queued_replays_as_its_command_runs_until_its_cancel(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        buildqd = load("buildqd_for_cancel", BUILDQD)
        db.execute("CREATE TABLE build (" + ", ".join(f"{n} {t}" for n, t in buildqd.BUILD_COLUMNS) + ")")
        db.execute("INSERT INTO build (id, ended, submitted, key, lane, wall, attempt_wall, mem_peak, avg_cores, state, "
                   "exit) VALUES ('ok', 1400, 1000, 'app|heavy|cargo test', 'heavy', 400.0, 400.0, ?, 4.0, "
                   "'succeeded', 0)", (8 * GIB,))
        db.execute("INSERT INTO build (id, ended, submitted, key, lane, wall, mem_peak, state) "
                   "VALUES ('gone', 1300, 1100, 'app|heavy|cargo test', 'heavy', 0, 0, 'cancelled')")
        out = sim.export_rows(db, None, None, anonymize=False)
        gone = out[1]
        self.assertEqual((gone["wall"], gone["peak_mem"], gone["cancel_at"], gone["exit"]), (400.0, 8 * GIB, 200.0, 0))
        self.assertNotIn("unfinished", gone)  # a whole run of its command says how it would have gone
        self.assertNotIn("cancel_at", out[0])

    def test_a_cancelled_build_known_only_in_part_never_ends_by_itself(self):
        job = dict(scenarios.build(0, ("cargo", "test"), 2, 120, 2, repo="app"), cancel_at=300, unfinished=True)
        summary, _ = sim.simulate(scenario([job]), CONFIG, BUILDQD, name="buildqd_unfinished")
        self.assertEqual((summary["outcomes"], summary["cancelled_as_in_trace"]), ({"cancelled": 1}, 1))
        self.assertAlmostEqual(summary["makespan_s"], 300, delta=5)

    def test_a_build_cancelled_while_still_queued(self):
        jobs = agents(6, 12, 900, step=1)  # the machine full for 15 minutes
        jobs.append(dict(scenarios.build(10, ("cargo", "test", "-p", "late"), 12, 300, 2, repo="app", caller="z",
                                         workspace="wz", history=True), cancel_at=120))
        _, s = sim.simulate(scenario(jobs), CONFIG, BUILDQD, name="buildqd_qcancel")
        late = next(r for r in s.records.values() if "late" in r["name"])
        self.assertEqual((late["outcome"], late["first_start"], late.get("cancelled_by_trace")), ("cancelled", None, True))

    def test_tools_that_ignore_their_grant_run_at_their_own_pace(self):
        spec = scenarios.build(0, ("pnpm", "vitest"), 1, 60, 4, lane="web", repo="app", history=True)
        state = os.path.join(self.tmp.name, "pace")
        os.makedirs(state)
        s = sim.Simulation(scenario([spec], background_cpu=0.0), CONFIG, BUILDQD, state, name="buildqd_pace")
        s.submit(spec, 0)
        (t,) = s.daemon.tickets.values()
        t.granted = 1  # as on a full machine: the web lane starts it on one core
        s.clock.now += 1
        s.step()
        self.assertEqual(s.machine.busy_now, 4.0)  # vitest starts a worker per core all the same

    def test_a_jobserver_doesnt_make_a_tool_hold_to_its_grant(self):
        state = os.path.join(self.tmp.name, "grant")
        os.makedirs(state)
        specs = [scenarios.build(0, ("pnpm", "vitest"), 1, 60, 8, lane="heavy", repo="app"),  # gets a jobserver
                 scenarios.build(0, ("cmake", "--build", "."), 1, 60, 8, repo="app"),
                 scenarios.build(0, ("env", "X=1", "dotnet", "build"), 1, 60, 8, repo="app")]
        s = sim.Simulation(scenario(specs), CONFIG, BUILDQD, state, name="buildqd_grant")
        honors = []
        for n, spec in enumerate(specs):
            t = s.submit(spec, n)
            honors.append((t.jobserver is not None, s.honors_grant(t)))
        self.assertEqual(honors[0], (True, False))  # vitest ignores the jobserver it was handed
        self.assertEqual([h for _, h in honors[1:]], [True, True])  # CMAKE_BUILD_PARALLEL_LEVEL, DOTNET_PROCESSOR_COUNT

    def test_a_cancel_frees_its_memory_in_the_same_second(self):
        # Room for two of these 25G builds (29G planned, 69G above the floor); the CI build waits until
        # the first is cancelled.
        jobs = [dict(scenarios.build(0, ("cargo", "test", "-p", "a"), 25, 900, 2, shape=(1.0,) * 10, repo="app",
                                     caller="a", workspace="wa", history=True), cancel_at=200),
                scenarios.build(1, ("cargo", "test", "-p", "b"), 25, 900, 2, shape=(1.0,) * 10, repo="app", caller="b",
                                workspace="wb", history=True),
                ci_step(5, 25, 60, shape=(1.0,) * 10)]
        summary, s = sim.simulate(scenario(jobs, background_mem=40 * GIB), CONFIG, BUILDQD, name="buildqd_refresh")
        ci = next(r for r in s.records.values() if r["ci"])
        self.assertEqual(ci["first_start"], 200.0)  # in the second of the cancel, not the one after
        self.assertEqual(summary["stops_for_ci"], 0)

    def test_turnaround_counts_a_stopped_build_s_second_wait(self):
        records = [{"ci": False, "submitted": 0.0, "first_start": 0.0, "ended": 900.0},
                   {"ci": False, "submitted": 0.0, "first_start": 10.0, "ended": 100.0}]
        self.assertEqual(sim.turnaround_stats(records, 1000)["max"], 900.0)  # started at once, ended late
        self.assertEqual(sim.wait_stats(records, 1000)["max"], 10.0)

    def test_a_replayed_build_is_cancelled_when_the_trace_says(self):
        job = dict(scenarios.build(0, ("cargo", "test"), 2, 600, 2, repo="app"), cancel_at=120)
        summary, s = sim.simulate(scenario([job]), CONFIG, BUILDQD, name="buildqd_cancel")
        self.assertEqual((summary["outcomes"], summary["cancelled_as_in_trace"]), ({"cancelled": 1}, 1))
        self.assertAlmostEqual(summary["makespan_s"], 120, delta=5)

    def minutes(self, rows, average=False):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute("CREATE TABLE minute (t INTEGER, cores INTEGER, mem_total INTEGER, mem_available INTEGER, "
                   "builds_mem INTEGER, cpu_busy REAL, builds_cores REAL"
                   + (", builds_mem_avg INTEGER)" if average else ")"))
        width = 8 if average else 7
        db.executemany(f"INSERT INTO minute VALUES ({','.join('?' * width)})", rows)
        return db

    def test_other_work_s_memory_comes_from_the_minute_rows(self):
        # builds_mem_avg: the builds' own average over the minute, set against the machine's
        db = self.minutes([(960, 32, 125 * GIB, 100 * GIB, 0, 6.0, 4.0, 5 * GIB),       # others: 20G, 2 cores
                           (1020, 32, 125 * GIB, 60 * GIB, 0, 20.0, 8.0, 15 * GIB)],    # others: 50G, 12 cores
                          average=True)
        out = sim.export_machine(db, 1000, None, 1000)
        self.assertEqual(out["machine"], {"ncpu": 32, "mem_total": 125 * GIB})
        self.assertEqual(out["background"], [[-40.0, 20 * GIB, 2.0], [20.0, 50 * GIB, 12.0]])
        path = os.path.join(self.tmp.name, "with-machine.jsonl")
        with open(path, "w") as f:
            f.write(json.dumps(out) + "\n")
            f.write(json.dumps(scenarios.build(0, ("cargo", "build"), 1, 600, 1, repo="app")) + "\n")
        loaded = sim.load_scenario(path)
        self.assertEqual(loaded["machine"]["background"], out["background"])
        self.assertNotIn("background", sim.load_scenario(path, background=False)["machine"])  # --no-background
        state = os.path.join(self.tmp.name, "bg")
        os.makedirs(state)
        s = sim.Simulation(loaded, CONFIG, BUILDQD, state, name="buildqd_bg")
        s.clock.now = sim.EPOCH + 10
        s.step()
        self.assertEqual((s.background_mem, s.background_cpu), (20 * GIB, 2.0))
        s.clock.now = sim.EPOCH + 20
        s.step()
        self.assertEqual(s.machine.mem_available, 125 * GIB - 50 * GIB)  # it follows the recorded minute

    def test_without_the_builds_average_their_readings_either_side_of_the_minute_are_taken(self):
        # builds_mem is one reading as a minute ends. A 30G build that ended mid-minute 1020 left
        # 15G in that minute's average memory; the readings either side (30G, 0) average 15G too.
        db = self.minutes([(900, 32, 125 * GIB, 75 * GIB, 30 * GIB, 6.0, 4.0),    # before the window: its reading only
                           (960, 32, 125 * GIB, 75 * GIB, 30 * GIB, 6.0, 4.0),    # others: 20G
                           (1020, 32, 125 * GIB, 90 * GIB, 0, 6.0, 4.0)])         # others: still 20G
        out = sim.export_machine(db, 1000, None, 1000)
        self.assertEqual([b[:2] for b in out["background"]], [[-40.0, 20 * GIB], [20.0, 20 * GIB]])

    def test_minutes_that_can_t_say_what_the_builds_used_are_left_out_and_noted(self):
        db = self.minutes([(960, 32, 125 * GIB, 100 * GIB, None, 6.0, 4.0),     # before the builds' columns
                           (1020, 32, 125 * GIB, 100 * GIB, 5 * GIB, 6.0, 4.0),  # no reading before it
                           (1080, 32, 125 * GIB, 100 * GIB, 5 * GIB, 6.0, None),  # first after a restart
                           (1140, 32, 125 * GIB, 100 * GIB, 5 * GIB, 6.0, 4.0),
                           (1800, 32, 125 * GIB, 90 * GIB, 5 * GIB, 6.0, 4.0),   # after a gap: no reading before
                           (1860, 32, 125 * GIB, 90 * GIB, 5 * GIB, 6.0, 4.0)])
        notes: list[str] = []
        out = sim.export_machine(db, 960, None, 960, notes)
        self.assertEqual([b[:2] for b in out["background"]], [[180.0, 20 * GIB], [900.0, 30 * GIB]])
        text = " ".join(notes)
        self.assertIn("4 minute(s)", text)
        self.assertIn("gap", text)
        self.assertIn("begin 3.0m after the first build", text)
        notes.clear()
        self.assertIsNone(sim.export_machine(db, 99_000, None, 99_000, notes))  # no rows that far on
        self.assertIn("built-in background", notes[0])

    def test_export_writes_the_machine_from_the_first_build_to_after_the_last_ended(self):
        db = self.metrics()  # the builds before --until (1015) came in from 900, and ended by 3100
        db.execute("CREATE TABLE minute (t INTEGER, cores INTEGER, mem_total INTEGER, mem_available INTEGER, "
                   "builds_mem INTEGER, cpu_busy REAL, builds_cores REAL, builds_mem_avg INTEGER)")
        db.executemany("INSERT INTO minute VALUES (?,?,?,?,?,?,?,?)",
                       [(t, 32, 125 * GIB, 100 * GIB, 0, 6.0, 4.0, 0) for t in range(600, 8000, 60)])
        db.execute("UPDATE minute SET mem_available = ? WHERE t = 3000", (80 * GIB,))  # after --until's end
        path = os.path.join(self.tmp.name, "metrics.db")
        db.commit()  # (a backup waits for the source's open write to end)
        disk = sqlite3.connect(path)
        db.backup(disk)
        disk.close()
        args = sim.argparse.Namespace(db=path, since="900", until="1015", anonymize=True, lead_in=None)
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch("sys.stdout", out), unittest.mock.patch("sys.stderr", err):
            sim.cmd_export(args)
        lines = [json.loads(line) for line in out.getvalue().splitlines()]
        machine, jobs = lines[0], lines[1:]
        self.assertEqual(jobs[0]["t"], 0.0)  # the machine's steps and the builds share their zero
        steps = {b[0]: b[1] for b in machine["background"]}
        self.assertEqual(min(steps), -60.0)  # the minute the first build came in (900 is in 840's)
        self.assertEqual(steps[3000 - 900], 45 * GIB)  # past --until: builds still running then had it
        self.assertLess(max(steps), 3200 + 3600 - 900)  # an hour past the last end, no more
        self.assertEqual(err.getvalue(), "")

    def test_anonymizing_hashes_option_values(self):
        argv = ["cargo", "--manifest-path=/home/someone/client/Cargo.toml", "--token=hunter2", "-C/home/someone/fw",
                "--password=123456789", "-p123456789", "--token=test", "--token", "424242",
                "--profile=test", "--jobs=8", "-j8", "-j", "8", "-p", "--release", "test", "--", "-"]
        words = sim.anonymous_argv(argv)
        for secret in ("someone", "client", "hunter2", "fw", "123456789", "424242", "--token=test"):
            self.assertNotIn(secret, json.dumps(words))
        self.assertEqual(words[1].split("=")[0], "--manifest-path")
        self.assertTrue(words[3].startswith("-C") and words[3] != argv[3])
        self.assertTrue(words[4].startswith("--password=") and words[5].startswith("-p") and words[5] != argv[5])
        self.assertEqual(words[0], "cargo")
        self.assertEqual(words[9:16], ["--profile=test", "--jobs=8", "-j8", "-j", "8", "-p", "--release"])
        self.assertNotEqual(words[16], "test")  # after an option, a common word may be its value
        self.assertEqual(words[17:], ["--", "-"])
        hashed = lambda argv: [w != a for w, a in zip(sim.anonymous_argv(argv), argv)]  # noqa: E731
        # Only the subcommands a command starts with keep their names; any word after an option doesn't.
        self.assertEqual(hashed(["cargo", "test", "-p", "test"]), [False, False, False, True])
        self.assertEqual(hashed(["pnpm", "--filter", "build", "test"]), [False, False, True, True])
        self.assertEqual(hashed(["cargo", "test", "--token", "test"]), [False, False, False, True])
        self.assertEqual(hashed(["git", "commit", "-m", "test"]), [False, True, False, True])  # (commit: not a public word)
        self.assertEqual(hashed(["python3", "-m", "unittest", "discover", "-s", "tests"]), [False] * 5 + [True])
        self.assertEqual(hashed(["cargo", "+nightly", "check"]), [False, False, False])
        self.assertEqual(hashed(["pnpm", "exec", "vitest", "run", "--shard", "1"]), [False] * 5 + [True])
        self.assertEqual(hashed(["cargo", "build", "--profile", "release"]), [False] * 4)  # (as command keys write it)
        self.assertEqual(sim.rank_correlation([1, 1, 2, 3], [5, 5, 6, 7]), 1.0)  # ties share their ranks

    def test_a_replay_is_set_beside_what_really_happened(self):
        jobs = [dict(scenarios.build(i * 5, ("cargo", "test", f"-p{i}"), 4, 300, 2, repo="app"),
                     recorded_wait=float(i * 100)) for i in range(4)]
        jobs.append(dict(scenarios.build(0, ("cargo", "test", "-pw"), 4, 300, 2, repo="app"),
                         recorded_wait=5000.0, warmup=True))  # in the machine before the window: not compared
        summary, _ = sim.simulate(scenario(jobs), CONFIG, BUILDQD, name="buildqd_recorded")
        agent = summary["against_recorded"]["agent"]
        self.assertEqual(agent["builds"], 4)
        self.assertEqual(agent["total_min"][0], 10.0)  # 0 + 100 + 200 + 300 seconds
        self.assertIn("agent wait, recorded / replayed (4)", sim.table(summary))
        self.assertEqual(sim.rank_correlation([1, 2, 3, 4], [10, 20, 30, 40]), 1.0)
        self.assertEqual(sim.rank_correlation([1, 2, 3, 4], [40, 30, 20, 10]), -1.0)
        self.assertIsNone(sim.rank_correlation([1, 2, 3], [5, 5, 5]))
        self.assertEqual(sim.ranks([3.0, 1.0, 3.0]), [2.5, 1.0, 2.5])

    def test_an_exported_trace_replays(self):
        path = os.path.join(self.tmp.name, "trace.jsonl")
        with open(path, "w") as f:
            for entry in sim.export_rows(self.metrics(), None, None, anonymize=True):
                f.write(json.dumps(entry) + "\n")
        summary, _ = sim.simulate(sim.load_scenario(path), CONFIG, BUILDQD, name="buildqd_trace")
        self.assertEqual(summary["builds"], 5)
        self.assertEqual(summary["outcomes"], {"cancelled": 1, "failed": 1, "succeeded": 3})  # cancelled as it was



if __name__ == "__main__":
    unittest.main()
