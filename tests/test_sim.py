"""The replay simulator (bench/sim.py): it drives the real daemon, deterministically."""
import importlib.machinery
import importlib.util
import json
import os
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


def ci_step(t, peak, wall, name="lint", **extra):
    return scenarios.build(t, ("pnpm", "eslint"), peak, wall, 4, shape=scenarios.EARLY, lane="heavy", ci=True,
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
            line = next(l for l in text.splitlines() if l.startswith(f"{key} ="))
            text = text.replace(line, f"{key} = {value}")
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
                 GIB, json.dumps([1] * 10), 1.0, None, "cancelled", None, 2, None)]
        db.executemany("INSERT INTO build (id, ended, submitted, key, label, name, lane, caller, from_ci, wall, "
                       "mem_peak, profile, avg_cores, exit, state, repo, requeues, attempt_wall) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        return db

    def test_a_metrics_db_becomes_a_trace(self):
        out = sim.export_rows(self.metrics(), None, None, anonymize=False)
        self.assertEqual([e["t"] for e in out], [0.0, 60.0, 100.0])
        self.assertEqual(out[0]["argv"], ["cargo", "test", "-p", "private-crate"])
        self.assertEqual((out[0]["ci"], out[0]["lane"], out[0]["profile"]), (True, "heavy", [100, 200, 300]))
        self.assertEqual((out[1]["ci"], out[1]["exit"], out[1]["peak_mem"]), (False, 1, GIB))
        self.assertEqual(out[0]["workspace"], "/home/someone/secret/target")  # builds that shared it take turns
        # cancelled, with no exit code: a failure; stopped twice, with no attempt_wall: no profile to place
        self.assertEqual((out[2]["exit"], out[2]["state"], out[2]["requeued"]), (1, "cancelled", 2))
        self.assertNotIn("profile", out[2])

    def test_anonymized_traces_name_nothing(self):
        out = sim.export_rows(self.metrics(), None, None, anonymize=True)
        text = json.dumps(out)
        for word in ("secretrepo", "private-crate", "private_script", "runner-9", "session-x", "ci:", "someone"):
            self.assertNotIn(word, text)
        self.assertEqual(out[0]["argv"][:3], ["cargo", "test", "-p"])  # the tool, its subcommand and options stay

    def test_an_exported_trace_replays(self):
        path = os.path.join(self.tmp.name, "trace.jsonl")
        with open(path, "w") as f:
            for entry in sim.export_rows(self.metrics(), None, None, anonymize=True):
                f.write(json.dumps(entry) + "\n")
        summary, _ = sim.simulate(sim.load_scenario(path), CONFIG, BUILDQD, name="buildqd_trace")
        self.assertEqual(summary["builds"], 3)
        self.assertEqual(summary["outcomes"], {"failed": 2, "succeeded": 1})



if __name__ == "__main__":
    unittest.main()
