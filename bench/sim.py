#!/usr/bin/env python3
"""Replay builds against buildqd's own scheduler, on a simulated machine and clock (#34).

The daemon is the real one, loaded from a file (`--buildqd`), so main's and a branch's can be run on
the same input. What it would read from the machine and from systemd comes from the simulation
instead: a clock that moves a second at a time, the machine's free memory and busy cores, and each
build's memory and CPU (through the module's cgroup_stats). Builds follow a recorded shape: their
memory over the run, and how many cores they kept busy. See docs/benchmark.md for the model and its
simplifications.

    sim.py run SCENARIO [--config FILE] [--buildqd FILE] [--json] [--jobs FILE]
    sim.py compare SCENARIO... [--base FILE] [--head FILE] [--base-config FILE] [--config FILE]
    sim.py export --db metrics.db [--since T] [--until T] [--anonymize] > trace.jsonl
    sim.py scenarios

A SCENARIO is a built-in name (bench/scenarios.py) or a trace file (.jsonl, one build per line).
Stdlib only; Python 3.11+.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.machinery
import importlib.util
import json
import math
import os
import shutil
import sqlite3
import sys
import tempfile
import time as real_time
import uuid as real_uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import scenarios as builtin  # noqa: E402

GIB = 1 << 30
MIB = 1 << 20
EPOCH = 1_800_000_000.0  # the simulated clock starts here: a fixed instant, so runs repeat exactly
CI_ENV = {"GITHUB_ACTIONS": "true"}
DEFAULT_TIMEOUT = 50 * 60  # a CI job's time limit, unless the trace gives one
DEFAULT_CONFIG = os.path.join(HERE, "bench.toml")
DEFAULT_BUILDQD = os.path.join(ROOT, "bin", "buildqd")


def load_buildqd(path: str, name: str):
    """buildqd as a module of its own, so two versions can be loaded side by side."""
    loader = importlib.machinery.SourceFileLoader(name, os.path.abspath(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class Clock:
    """Stands in for the time module inside the loaded daemon: every reading is the simulated now."""

    def __init__(self):
        self.now = EPOCH

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now - EPOCH + 1_000_000.0

    def sleep(self, seconds: float) -> None:
        pass

    def strftime(self, fmt: str, when=None) -> str:
        return real_time.strftime(fmt, real_time.gmtime(self.now) if when is None else when)

    def __getattr__(self, name):
        return getattr(real_time, name)


class Ids:
    """Stands in for the uuid module: ticket ids are numbered, so two runs name builds alike."""

    def __init__(self):
        self.n = 0

    def uuid4(self):
        self.n += 1
        return real_uuid.UUID(int=(self.n << 96) | self.n)

    def __getattr__(self, name):
        return getattr(real_uuid, name)


class Machine:
    """What the daemon reads about the machine, set by the simulation each second."""

    def __init__(self, ncpu: int, mem_total: int):
        self.ncpu, self.mem_total = ncpu, mem_total
        self.mem_available = mem_total
        self.busy = self.busy_now = 0.0
        self.swap_used = 0
        self.psi_cpu = self.psi_mem = self.psi_mem_some = 0.0

    def sample(self, now):
        pass

    def summary(self):
        return {}


class Job:
    """One attempt of a build, as the simulation runs it."""

    def __init__(self, ticket, spec: dict, started: float):
        self.ticket, self.spec, self.started = ticket, spec, started
        self.progress = 0.0  # recorded seconds of work done
        self.cpu_usec = 0
        self.mem = 0
        self.used_cores = 0.0
        self.frozen = False
        self.alive = True

    def wanted_cores(self) -> float:
        if self.frozen:
            return 0.0
        avg = max(float(self.spec.get("avg_cores") or 1.0), 0.1)
        return min(float(max(self.ticket.granted, 1)), avg)

    def memory_at(self, progress: float) -> int:
        wall = max(float(self.spec["wall"]), 1.0)
        profile = self.spec.get("profile")
        share = min(max(progress / wall, 0.0), 1.0)
        if profile:
            return int(profile[min(len(profile) - 1, int(share * len(profile)))]) * MIB
        peak = int(self.spec["peak_mem"])
        return int(peak * min(1.0, share / 0.2)) if share < 0.2 else peak  # up to its peak in the first fifth


class Runner:
    """Stands in for buildqd's systemd runner: launching starts a simulated job."""

    name = "systemd"  # so lane caps count as enforced, as they are in production
    can_freeze = True

    def __init__(self, sim: "Simulation"):
        self.sim = sim

    def prepare(self, ticket, slice_name):
        ticket.unit = f"buildq-{ticket.id}.service"
        ticket.cg = f"/sim/{ticket.id}"

    def launch(self, ticket, lane, slice_name):
        self.sim.launch(ticket)
        return None, True

    def unit_state(self, ticket):
        return "active" if self.alive(ticket) else "gone"

    def result(self, ticket):
        return ""

    def cleanup(self, ticket):
        return None

    def alive(self, ticket):
        job = self.sim.jobs.get(ticket.id)
        return bool(job and job.alive)

    def stop(self, ticket, force=False):
        self.sim.kill(ticket, cancelled=bool(ticket.cancel_requested))
        return None

    def freeze(self, ticket, frozen):
        job = self.sim.jobs.get(ticket.id)
        if job is not None:
            job.frozen = frozen
        return None

    @staticmethod
    def check_lane(lane):
        return None


class Simulation:
    def __init__(self, scenario: dict, config_path: str, buildqd_path: str, state_dir: str, name: str = "buildqd_sim"):
        self.scenario = scenario
        self.state_dir = state_dir
        self.clock = Clock()
        self.mod = load_buildqd(buildqd_path, name)
        self.patch_module()
        machine = scenario["machine"]
        self.background_mem = machine.get("background_mem", 0)
        self.background_cpu = machine.get("background_cpu", 0.0)
        self.machine = Machine(int(machine["ncpu"]), int(machine["mem_total"]))
        self.jobs: dict[str, Job] = {}  # live attempts by ticket id
        self.specs: dict[str, dict] = {}  # ticket id -> its trace entry
        self.records: dict[str, dict] = {}  # ticket id -> what happened to it
        self.repo_of: dict[str, str] = {}
        self.cold_of: dict[str, bool] = {}
        self.events = {"machine_oom": 0, "own_cap_oom": 0, "stopped_seconds": 0.0, "cancelled_seconds": 0.0,
                       "idle_core_seconds": 0.0, "ci_timed_out": 0, "min_available": None}
        self.log_lines: list[str] = []
        self.submitting: tuple[dict, dict] | None = None
        config = os.path.join(state_dir, "config.toml")
        shutil.copyfile(config_path, config)
        self.daemon = self.mod.Daemon(config, os.path.join(state_dir, "state"), machine=self.machine,
                                      runner=Runner(self), queries=self.mod.InlineExecutor())
        self.daemon.source_command = None

    # ---- the module's view of the world

    def patch_module(self):
        mod, sim = self.mod, self
        mod.time = self.clock
        mod.uuid = Ids()
        mod.log = lambda message, level=None: sim.log_lines.append(f"{sim.clock.now - EPOCH:8.0f} {message}")
        mod.cgroup_stats = self.stats
        mod.cgroup_swap = lambda path: 0
        mod.cgroup_memory_waits = lambda path: None
        mod.cgroup_freeze_set = lambda path: self.frozen(path)
        mod.cgroup_frozen = lambda path: self.frozen(path)
        mod.parent_freeze_set = lambda path: False
        mod.repo_name = lambda cwd: sim.repo_of.get(os.path.abspath(cwd), os.path.basename(cwd))
        mod.cargo_cold = lambda argv, cwd, env: sim.cold_of.get(os.path.abspath(cwd), False)

    def job_at(self, path: str | None) -> Job | None:
        if not path or not path.startswith("/sim/"):
            return None
        return self.jobs.get(path.rsplit("/", 1)[1])

    def stats(self, path: str):
        job = self.job_at(path)
        if job is None or not job.alive:
            return None
        return job.cpu_usec, job.mem, 0

    def frozen(self, path: str):
        job = self.job_at(path)
        return None if job is None else job.frozen

    # ---- builds

    def workdir(self, spec: dict, n: int) -> str:
        repo = spec.get("repo", "project")
        path = os.path.join(self.state_dir, "work", f"{repo}-{n}")
        os.makedirs(os.path.join(path, ".git"), exist_ok=True)
        self.repo_of[os.path.abspath(path)] = repo
        self.cold_of[os.path.abspath(path)] = bool(spec.get("cold"))
        return path

    def env_of(self, spec: dict) -> dict:
        env = dict(CI_ENV, RUNNER_NAME=spec.get("caller", "runner")) if spec.get("ci") else {}
        if spec.get("workspace"):
            target = os.path.join(self.state_dir, "targets", spec["workspace"])
            os.makedirs(target, exist_ok=True)
            env["CARGO_TARGET_DIR"] = target
        return env

    def submit(self, spec: dict, n: int):
        msg = {"argv": list(spec["argv"]), "cwd": self.workdir(spec, n), "lane": spec.get("lane"),
               "env": self.env_of(spec), "caller": spec.get("caller", f"agent-{n}"),
               "name": spec.get("name", " ".join(spec["argv"])[:60])}
        if spec.get("restartable"):
            msg["restartable"] = True
        record = {"name": msg["name"], "lane": None, "ci": bool(spec.get("ci")), "submitted": self.clock.now - EPOCH,
                  "first_start": None, "ended": None, "attempts": 0, "outcome": None, "peak": 0, "planned": 0}
        self.submitting = (spec, record)  # (the daemon may start it before submit returns)
        t, error = self.daemon.submit(msg, self.clock.now)
        self.submitting = None
        if error:
            raise RuntimeError(f"submitting {spec.get('name') or spec['argv']}: {error}")
        self.specs.setdefault(t.id, spec)
        self.records.setdefault(t.id, record)["lane"] = t.lane
        return t

    def launch(self, ticket):
        if ticket.id not in self.specs and self.submitting is not None:
            self.specs[ticket.id], self.records[ticket.id] = self.submitting
        rec = self.records[ticket.id]
        rec["attempts"] += 1
        if rec["first_start"] is None:
            rec["first_start"] = self.clock.now - EPOCH
        rec["planned"] = max(rec["planned"], int(ticket.est_mem))
        self.jobs[ticket.id] = Job(ticket, self.specs[ticket.id], self.clock.now)

    def end(self, job: Job, result: dict | None, discarded: str | None = None):
        job.alive = False
        if discarded:  # its run so far is lost: stopped (it starts over) or cancelled
            self.events[f"{discarded}_seconds"] += self.clock.now - job.started
        if result is not None:
            with open(os.path.join(job.ticket.dir, "result.json"), "w") as f:
                json.dump(result, f)

    def kill(self, ticket, cancelled: bool):
        job = self.jobs.get(ticket.id)
        if job is not None and job.alive:
            self.end(job, None, discarded="cancelled" if cancelled else "stopped")

    def seed_history(self):
        """Give each command of the scenario a history of its own, as a machine that has run them before."""
        seen = set()
        for n, spec in enumerate(self.scenario["jobs"]):
            if not spec.get("history", self.scenario.get("seed_history")):
                continue
            cwd = self.workdir(spec, 10_000 + n)
            lane = spec.get("lane") or self.daemon.cfg.lane_for(list(spec["argv"]))
            key = self.mod.command_key(list(spec["argv"]), cwd, lane, self.daemon.cfg.script_labels)
            if key in seen:
                continue
            seen.add(key)
            peak = int(spec.get("history_peak", spec["peak_mem"]))
            cores = max(1, math.ceil(float(spec.get("avg_cores") or 1)))
            self.daemon.history.add({"key": key, "lane": lane, "t": round(self.clock.now), "wall": float(spec["wall"]),
                                     "exit": 0, "peak_mem": peak, "peak_cores": float(spec.get("avg_cores") or 1),
                                     "avg_cores": float(spec.get("avg_cores") or 1), "granted": cores,
                                     "mem_cores": cores, "elastic": False, "cold": False, "oom": False})

    # ---- the clock

    def step(self):
        """One simulated second of the machine: builds use cores and memory, finish, or are killed."""
        live = [j for j in self.jobs.values() if j.alive]
        cap = max(self.machine.ncpu - self.background_cpu, 1.0)
        demand = sum(j.wanted_cores() for j in live)
        share = min(1.0, cap / demand) if demand > 0 else 1.0
        for job in live:
            job.used_cores = job.wanted_cores() * share
            job.cpu_usec += int(job.used_cores * 1e6)
            avg = max(float(job.spec.get("avg_cores") or 1.0), 0.1)
            job.progress += min(job.used_cores / avg, 1.0) if avg >= 1 else (1.0 if job.used_cores else 0.0)
            job.mem = job.memory_at(job.progress)
            if job.progress >= float(job.spec["wall"]):
                job.mem = 0
                self.end(job, {"exit": int(job.spec.get("exit", 0))})
                continue
            lane = self.daemon.cfg.lanes.get(job.ticket.lane)
            mem_cap = getattr(lane, "mem_cap", None) if lane else None
            if mem_cap and job.mem > mem_cap:  # over its own MemoryMax: the kernel kills it
                self.events["own_cap_oom"] += 1
                job.mem = 0
                self.end(job, {"exit": 137, "oom_kills": 1, "hit_own_cap": True})
        live = [j for j in self.jobs.values() if j.alive]
        used = self.background_mem + sum(j.mem for j in live)
        while used > self.machine.mem_total and live:  # the machine is out: the kernel kills the largest
            victim = max(live, key=lambda j: j.mem)
            self.events["machine_oom"] += 1
            used -= victim.mem
            victim.mem = 0
            self.end(victim, {"exit": 137, "oom_kills": 1, "hit_own_cap": False})
            live.remove(victim)
        self.machine.mem_available = max(0, self.machine.mem_total - used)
        low = self.events["min_available"]
        self.events["min_available"] = self.machine.mem_available if low is None else min(low, self.machine.mem_available)
        busy = self.background_cpu + sum(j.used_cores for j in live)
        self.machine.busy = self.machine.busy_now = busy
        self.machine.psi_cpu = 100.0 * max(0.0, demand - cap) / demand if demand > cap else 0.0
        queued = any(t.state == "queued" for t in self.daemon.tickets.values())
        if queued:
            self.events["idle_core_seconds"] += max(0.0, self.machine.ncpu * self.daemon.cfg.cpu_target - busy)

    def ci_timeouts(self, now: float):
        """A CI job that runs past its time limit is cancelled by GitHub, and its build with it."""
        for tid, spec in self.specs.items():
            t = self.daemon.tickets.get(tid)
            if t is None or not spec.get("ci") or t.state in self.mod.FINISHED or t.cancel_requested:
                continue
            if now - t.submitted >= float(spec.get("timeout_s", DEFAULT_TIMEOUT)):
                self.events["ci_timed_out"] += 1
                self.records[tid]["timed_out_queued"] = t.state == "queued"
                self.daemon.cancel(t, now)

    def run(self, horizon: float | None = None) -> dict:
        d = self.daemon
        self.seed_history()
        arrivals = sorted(enumerate(self.scenario["jobs"]), key=lambda p: (float(p[1]["t"]), p[0]))
        last = float(arrivals[-1][1]["t"]) if arrivals else 0.0
        horizon = horizon if horizon is not None else last + 6 * 3600
        i, s = 0, 0
        while True:
            self.clock.now = EPOCH + s
            now = self.clock.now
            self.step()
            while i < len(arrivals) and float(arrivals[i][1]["t"]) <= s:
                n, spec = arrivals[i]
                self.submit(spec, n)
                i += 1
            self.ci_timeouts(now)
            d.check(now)
            d.shed_memory(now)
            d.schedule(now)
            make_room = getattr(d, "make_room_for_ci", None)
            if make_room is not None:
                make_room(now)
            if s % 5 == 0:
                d.rebalance(now)
            if i >= len(arrivals) and all(t.state in self.mod.FINISHED for t in d.tickets.values()):
                break
            if s >= horizon:
                break
            s += 1
        for t in d.tickets.values():
            rec = self.records.get(t.id)
            if rec is None:
                continue
            rec["ended"] = (t.ended - EPOCH) if t.ended else None
            rec["outcome"] = t.state
            rec["requeues"] = t.requeues
            rec["ci_stops"] = getattr(t, "ci_stops", 0)
            rec["peak"] = max(int(t.mem_peak or 0), int(getattr(t, "attempts_peak", 0) or 0))
            d.close_jobserver(t)
        d.queries.shutdown()
        return summarize(self, s)


# ---- the summary

def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    k = (len(values) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def waits(records: list[dict], end: float) -> list[float]:
    """Time from submission to first start; a build that never started counts until it ended."""
    out = []
    for r in records:
        start = r["first_start"] if r["first_start"] is not None else (r["ended"] if r["ended"] is not None else end)
        out.append(max(0.0, start - r["submitted"]))
    return out


def wait_stats(records: list[dict], end: float) -> dict:
    w = waits(records, end)
    r = lambda v: None if v is None else round(v, 1)  # noqa: E731
    return {"builds": len(records), "p50": r(percentile(w, 0.5)), "p95": r(percentile(w, 0.95)), "max": r(max(w, default=None))}


def summarize(sim: Simulation, seconds: int) -> dict:
    records = list(sim.records.values())
    end = float(seconds)
    lanes = sorted({r["lane"] for r in records})
    finished = [r for r in records if r["outcome"] == "succeeded"]
    under = [r for r in finished if r["peak"] > r["planned"] > 0]
    ratios = [r["planned"] / r["peak"] for r in finished if r["peak"] > 0 and r["planned"] > 0]
    ends = [r["ended"] for r in records if r["ended"] is not None]
    first = min((r["submitted"] for r in records), default=0.0)
    outcomes: dict[str, int] = {}
    for r in records:
        outcomes[r["outcome"] or "unfinished"] = outcomes.get(r["outcome"] or "unfinished", 0) + 1
    return {
        "scenario": sim.scenario.get("name", "trace"),
        "builds": len(records),
        "makespan_s": round((max(ends) if ends else end) - first, 1),
        "outcomes": dict(sorted(outcomes.items())),
        "wait": {"all": wait_stats(records, end),
                 "ci": wait_stats([r for r in records if r["ci"]], end),
                 "agent": wait_stats([r for r in records if not r["ci"]], end),
                 **{f"lane:{lane}": wait_stats([r for r in records if r["lane"] == lane], end) for lane in lanes}},
        "ci_timed_out": sim.events["ci_timed_out"],
        "ci_timed_out_in_queue": sum(1 for r in records if r.get("timed_out_queued")),
        "stops_for_memory": sum(max(0, (r.get("requeues") or 0) - (r.get("ci_stops") or 0)) for r in records),
        "stops_for_ci": sum(r.get("ci_stops") or 0 for r in records),
        "stopped_run_s": round(sim.events["stopped_seconds"], 1),
        "cancelled_run_s": round(sim.events["cancelled_seconds"], 1),
        "oom_kills": {"own_cap": sim.events["own_cap_oom"], "machine": sim.events["machine_oom"]},
        "idle_core_s_while_waiting": round(sim.events["idle_core_seconds"], 1),
        "least_free_gib": round((sim.events["min_available"] or 0) / GIB, 1),
        "planned_over_peak": {"median": None if not ratios else round(percentile(ratios, 0.5), 2),
                              "under_planned": len(under)},
    }


def table(summary: dict) -> str:
    rows = [("builds", summary["builds"]), ("makespan", fmt_s(summary["makespan_s"]))]
    for group, w in summary["wait"].items():
        if w["builds"]:
            rows.append((f"wait {group} p50 / p95 / max ({w['builds']})",
                         f"{fmt_s(w['p50'])} / {fmt_s(w['p95'])} / {fmt_s(w['max'])}"))
    rows += [("CI timed out (in the queue)", f"{summary['ci_timed_out']} ({summary['ci_timed_out_in_queue']})"),
             ("stops for memory / for CI", f"{summary['stops_for_memory']} / {summary['stops_for_ci']}"),
             ("running time lost to stops / cancels", f"{fmt_s(summary['stopped_run_s'])} / "
                                                      f"{fmt_s(summary['cancelled_run_s'])}"),
             ("OOM kills (own cap / machine)", f"{summary['oom_kills']['own_cap']} / {summary['oom_kills']['machine']}"),
             ("idle core-seconds while waiting", f"{summary['idle_core_s_while_waiting']:.0f}"),
             ("least memory free", f"{summary['least_free_gib']}G"),
             ("planned / peak (median), under-planned", f"{summary['planned_over_peak']['median']}, "
                                                        f"{summary['planned_over_peak']['under_planned']}"),
             ("outcomes", ", ".join(f"{k} {v}" for k, v in summary["outcomes"].items()))]
    width = max(len(k) for k, _ in rows)
    return "\n".join(f"{k:<{width}}  {v}" for k, v in rows)


def fmt_s(seconds) -> str:
    if seconds is None:
        return "-"
    seconds = float(seconds)
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


# ---- running

def load_scenario(name: str) -> dict:
    if name in builtin.SCENARIOS:
        return builtin.SCENARIOS[name]()
    jobs = []
    with open(name) as f:
        for line in f:
            if line.strip():
                jobs.append(json.loads(line))
    first = min((float(j["t"]) for j in jobs), default=0.0)
    for j in jobs:
        j["t"] = float(j["t"]) - first
    return {"name": os.path.basename(name), "machine": dict(builtin.WORKSTATION), "jobs": jobs, "seed_history": False}


def simulate(scenario: dict, config: str, buildqd: str, name: str = "buildqd_sim", keep: str | None = None) -> tuple[dict, Simulation]:
    base = os.environ.get("TMPDIR") or tempfile.gettempdir()
    state = tempfile.mkdtemp(prefix="buildq-sim-", dir=base)
    try:
        sim = Simulation(scenario, config, buildqd, state, name=name)
        summary = sim.run()
        return summary, sim
    finally:
        if keep:
            shutil.copytree(state, keep, dirs_exist_ok=True)
        shutil.rmtree(state, ignore_errors=True)


def cmd_run(args) -> int:
    scenario = load_scenario(args.scenario)
    summary, sim = simulate(scenario, args.config, args.buildqd)
    if args.jobs:
        with open(args.jobs, "w") as f:
            for rec in sim.records.values():
                f.write(json.dumps(rec, sort_keys=True) + "\n")
    if args.log:
        with open(args.log, "w") as f:
            f.write("\n".join(sim.log_lines) + "\n")
    print(json.dumps(summary, indent=1, sort_keys=True) if args.json else f"{summary['scenario']}\n{table(summary)}")
    return 0


COMPARED = [  # (label, how to read it from a summary, lower is better)
    ("makespan", lambda s: s["makespan_s"], fmt_s),
    ("CI wait p50", lambda s: s["wait"]["ci"]["p50"], fmt_s),
    ("CI wait p95", lambda s: s["wait"]["ci"]["p95"], fmt_s),
    ("CI wait max", lambda s: s["wait"]["ci"]["max"], fmt_s),
    ("agent wait p50", lambda s: s["wait"]["agent"]["p50"], fmt_s),
    ("agent wait p95", lambda s: s["wait"]["agent"]["p95"], fmt_s),
    ("CI timed out", lambda s: s["ci_timed_out"], str),
    ("stops (memory + CI)", lambda s: s["stops_for_memory"] + s["stops_for_ci"], str),
    ("running time lost to stops", lambda s: s["stopped_run_s"], fmt_s),
    ("running time lost to cancels", lambda s: s["cancelled_run_s"], fmt_s),
    ("OOM kills", lambda s: s["oom_kills"]["own_cap"] + s["oom_kills"]["machine"], str),
    ("idle core-seconds while waiting", lambda s: s["idle_core_s_while_waiting"], lambda v: f"{v:.0f}"),
]


def compare_markdown(name: str, base: dict, head: dict) -> str:
    lines = [f"#### {name}", "", "| | base | head | change |", "|---|---:|---:|---:|"]
    for label, get, fmt in COMPARED:
        b, h = get(base), get(head)
        if b is None and h is None:
            continue
        if b is None or h is None:
            change = ""
        elif b == h:
            change = "same"
        elif b:
            change = f"{(h - b) / b * 100:+.0f}%"
        else:
            change = f"+{fmt(h)}"
        lines.append(f"| {label} | {fmt(b) if b is not None else '-'} | {fmt(h) if h is not None else '-'} | {change} |")
    return "\n".join(lines)


def cmd_compare(args) -> int:
    names = args.scenario or sorted(builtin.SCENARIOS)
    out = ["### buildq replay: head against base", ""]
    for name in names:
        try:
            base, _ = simulate(load_scenario(name), args.base_config or args.config, args.base, name="buildqd_base")
        except ValueError as e:  # an older daemon that doesn't know a setting the configuration uses
            out += [f"#### {name}", "", f"The base daemon can't run this configuration ({e}); head only:", "",
                    "```", table(simulate(load_scenario(name), args.config, args.head, name="buildqd_head")[0]),
                    "```", ""]
            continue
        head, _ = simulate(load_scenario(name), args.config, args.head, name="buildqd_head")
        out += [compare_markdown(name, base, head), ""]
    text = "\n".join(out)
    print(text)
    if args.summary_file:
        with open(args.summary_file, "a") as f:
            f.write(text + "\n")
    return 0


def cmd_check(args) -> int:
    """Each scenario twice: the summaries must match byte for byte (CI's crash and determinism check)."""
    bad = 0
    for name in args.scenario or sorted(builtin.SCENARIOS):
        a, _ = simulate(load_scenario(name), args.config, args.buildqd, name="buildqd_a")
        b, _ = simulate(load_scenario(name), args.config, args.buildqd, name="buildqd_b")
        same = json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
        print(f"{name}: {'same both times' if same else 'DIFFERENT between runs'}")
        bad += not same
    return 1 if bad else 0


def cmd_scenarios(args) -> int:
    for name in sorted(builtin.SCENARIOS):
        s = builtin.SCENARIOS[name]()
        print(f"{name:<20} {len(s['jobs']):>3} builds  {s.get('about', '')}")
    return 0


# ---- export from a metrics.db

def hashed(prefix: str, word: str) -> str:
    return prefix + hashlib.sha256(word.encode()).hexdigest()[:6]


def export_rows(db: sqlite3.Connection, since: float | None, until: float | None, anonymize: bool) -> list[dict]:
    db.row_factory = sqlite3.Row
    cols = {r[1] for r in db.execute("PRAGMA table_info(build)")}
    query = "SELECT * FROM build WHERE wall > 0 AND submitted IS NOT NULL"
    params: list = []
    if since is not None:
        query += " AND submitted >= ?"
        params.append(since)
    if until is not None:
        query += " AND submitted < ?"
        params.append(until)
    rows = [dict(r) for r in db.execute(query + " ORDER BY submitted, id", params)]
    if not rows:
        return []
    first = rows[0]["submitted"]
    out = []
    for r in rows:
        repo, lane, words = (str(r.get("key") or "").split("|", 2) + ["", ""])[:3]
        argv = words.split() or ["build"]
        profile = None
        if "profile" in cols and r.get("profile"):
            try:
                profile = [int(x) for x in json.loads(r["profile"])]
            except (ValueError, TypeError):
                profile = None
        wall = float(r["wall"])
        avg = r.get("avg_cores") or ((r.get("cpu_seconds") or 0) / wall if wall else 1.0) or 1.0
        caller = str(r.get("caller") or "")
        entry = {"t": round(float(r["submitted"]) - first, 1), "repo": repo or "project", "lane": lane or None,
                 "argv": argv, "ci": bool(r.get("from_ci")), "caller": caller, "wall": round(wall, 1),
                 "avg_cores": round(float(avg), 2), "peak_mem": int(r.get("mem_peak") or 0),
                 "exit": int(r["exit"]) if r.get("exit") is not None else 0}
        if profile:
            entry["profile"] = profile
        if r.get("name"):
            entry["name"] = r["name"]
        if anonymize:
            entry["repo"] = hashed("r", entry["repo"])
            entry["caller"] = hashed("c", caller)
            entry["argv"] = argv[:2] + [w if w.startswith("-") else hashed("w", w) for w in argv[2:]]
            entry.pop("name", None)
        out.append(entry)
    return out


def parse_when(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return real_time.mktime(real_time.strptime(value, "%Y-%m-%dT%H:%M"))


def cmd_export(args) -> int:
    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        for entry in export_rows(db, parse_when(args.since), parse_when(args.until), args.anonymize):
            print(json.dumps(entry, sort_keys=True))
    finally:
        db.close()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="sim.py", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="replay one scenario or trace and print its summary")
    r.add_argument("scenario")
    r.add_argument("--config", default=DEFAULT_CONFIG)
    r.add_argument("--buildqd", default=DEFAULT_BUILDQD)
    r.add_argument("--json", action="store_true")
    r.add_argument("--jobs", help="write what happened to each build here (JSONL)")
    r.add_argument("--log", help="write the daemon's log here")
    c = sub.add_parser("compare", help="replay scenarios with two daemons (or two configs) and compare")
    c.add_argument("scenario", nargs="*")
    c.add_argument("--base", default=DEFAULT_BUILDQD)
    c.add_argument("--head", default=DEFAULT_BUILDQD)
    c.add_argument("--config", default=DEFAULT_CONFIG, help="the head's configuration (and the base's, by default)")
    c.add_argument("--base-config")
    c.add_argument("--summary-file", help="also append the table here ($GITHUB_STEP_SUMMARY)")
    k = sub.add_parser("check", help="replay each scenario twice; fail if the summaries differ")
    k.add_argument("scenario", nargs="*")
    k.add_argument("--config", default=DEFAULT_CONFIG)
    k.add_argument("--buildqd", default=DEFAULT_BUILDQD)
    e = sub.add_parser("export", help="write a trace from a buildq metrics.db (never commit a real one)")
    e.add_argument("--db", required=True)
    e.add_argument("--since", help="epoch seconds, or YYYY-MM-DDTHH:MM local time")
    e.add_argument("--until")
    e.add_argument("--anonymize", action="store_true", help="hash repository names, callers and package words")
    sub.add_parser("scenarios", help="list the built-in scenarios")
    args = p.parse_args(argv)
    return {"run": cmd_run, "compare": cmd_compare, "check": cmd_check, "export": cmd_export,
            "scenarios": cmd_scenarios}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
