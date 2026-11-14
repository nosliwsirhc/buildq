"""Built-in replay scenarios: synthetic, generated from fixed seeds, so they are the same on every machine.

Each scenario is a dict: name, about, machine (ncpu, mem_total, background_mem, background_cpu), jobs
(one trace entry per build; see docs/benchmark.md), and seed_history (whether each command starts
with a history of one earlier run, as on a machine that has built it before). Sizes are modelled
on one busy workstation's measured builds, with no names from it.
"""
from __future__ import annotations

import random

GIB = 1 << 30
MIB = 1 << 20

WORKSTATION = {"ncpu": 32, "mem_total": 125 * GIB, "background_mem": 26 * GIB, "background_cpu": 3.0}

# The shape of a build's memory over its run, in tenths, as a share of its peak.
LINK_AT_END = (0.3, 0.4, 0.5, 0.6, 0.6, 0.7, 0.8, 1.0, 0.9, 0.5)  # a cargo build: the link peaks late
TESTS = (0.4, 0.7, 0.9, 1.0, 1.0, 1.0, 0.95, 0.9, 0.9, 0.6)  # a test run: up early, then flat
EARLY = (1.0, 0.9, 0.8, 0.8, 0.7, 0.7, 0.6, 0.6, 0.5, 0.4)  # a lint or a check: loads everything first


def profile(peak: float, shape=LINK_AT_END) -> list[int]:
    return [max(1, int(peak * s / MIB)) for s in shape]


def build(t, argv, peak_gib, wall, cores, *, shape=LINK_AT_END, **extra) -> dict:
    peak = int(peak_gib * GIB)
    return {"t": round(float(t), 1), "argv": list(argv), "wall": float(wall), "avg_cores": float(cores),
            "peak_mem": peak, "profile": profile(peak, shape), **extra}


def release(t: float, n: int, *, heavy_lints: bool) -> list[dict]:
    """One release's CI steps on four runners. With heavy_lints, eslint and infra-check are the
    sizes measured before they were cut (16 and 12.5 GB, heavy lane); otherwise the cut sizes, web lane."""
    ci = {"ci": True, "repo": "app", "history": True}
    steps = []
    if heavy_lints:
        steps += [build(t, ("pnpm", "eslint"), 16.2, 80, 4, shape=EARLY, lane="heavy", caller="runner-1",
                        name=f"r{n}:eslint", **ci),
                  build(t + 5, ("pnpm", "vitest", "infra"), 12.6, 47, 8, shape=TESTS, lane="heavy", caller="runner-2",
                        name=f"r{n}:infra-check", **ci)]
    else:
        steps += [build(t, ("pnpm", "eslint"), 5.7, 221, 1, shape=EARLY, lane="web", caller="runner-1",
                        name=f"r{n}:eslint", **ci),
                  build(t + 5, ("pnpm", "vitest", "infra"), 3.9, 47, 8, shape=TESTS, lane="web", caller="runner-2",
                        name=f"r{n}:infra-check", **ci)]
    steps += [build(t + 10, ("cargo", "test"), 18.0, 1400, 8, shape=TESTS, lane="heavy", caller="runner-3",
                    workspace="ci-rust-tests", name=f"r{n}:rust-tests", **ci),
              build(t + 15, ("cargo", "build", "--release", "-p", "checker"), 0.8, 10, 1, lane="check",
                    caller="runner-4", workspace="ci-checker", name=f"r{n}:checker", **ci),
              build(t + 200, ("pnpm", "vitest"), 3.2, 600, 4.6, shape=TESTS, lane="web", caller="runner-1",
                    name=f"r{n}:vitest", **ci)]
    return steps


def burst() -> dict:
    """Ten agent cargo builds submitted within ten seconds: memory decides how many start."""
    rng = random.Random(1)
    jobs = [build(i, ("cargo", "build", "-p", f"crate{i}"), rng.uniform(6, 14), rng.uniform(600, 900),
                  rng.uniform(6, 10), repo="app", caller=f"session-{i % 4}", workspace=f"wt{i}", history=True)
            for i in range(10)]
    return {"name": "burst", "about": "ten cargo builds at once", "machine": dict(WORKSTATION), "jobs": jobs,
            "seed_history": True}


def ci_behind_agents() -> dict:
    """Long agent test builds fill the machine; then a release's CI steps arrive, two of them big
    (eslint 16 GB and infra-check 12.6 GB, as measured before they were cut)."""
    rng = random.Random(2)
    jobs = [build(i * 40, ("cargo", "test", "-p", f"crate{i}"), rng.uniform(12, 18), rng.uniform(1500, 2400),
                  rng.uniform(6, 8), shape=TESTS, repo="app", caller=f"session-{i}", workspace=f"wt{i}", history=True)
            for i in range(7)]
    jobs += release(420, 1, heavy_lints=True)
    return {"name": "ci-behind-agents", "about": "a release arrives behind seven long agent test builds",
            "machine": dict(WORKSTATION), "jobs": jobs, "seed_history": True}


def outgrows_estimate() -> dict:
    """Two builds whose history says 3 GB grow to 24 GB by mid-run (full rebuilds after incremental
    ones), while a stream of 12 GB test builds keeps the machine full. The daemon re-plans a build
    that passes its estimate (outgrown), which keeps the memory floor; without that, the machine
    goes below it."""
    rng = random.Random(3)
    rising = (0.15, 0.25, 0.4, 0.6, 0.8, 1.0, 1.0, 1.0, 1.0, 0.5)
    jobs = [build(k * 120, ("cargo", "build", "-p", f"big{k}"), 24, 1800, 8, shape=rising, repo="app",
                  caller=f"session-big{k}", workspace=f"wt-big{k}", history=True, history_peak=3 * GIB)
            for k in range(2)]
    jobs += [build(30 + i * 45, ("cargo", "test", "-p", f"crate{i}"), rng.uniform(11.5, 12.5), 600, 2, shape=TESTS,
                   repo="app", caller=f"session-{i}", workspace=f"wt{i}", history=True)
             for i in range(20)]
    machine = dict(WORKSTATION, background_mem=30 * GIB)
    return {"name": "outgrows-estimate", "about": "two builds grow far past their estimates on a full machine",
            "machine": machine, "jobs": jobs, "seed_history": True}


def mixed_day() -> dict:
    """Two hours of agents' builds (cargo, checks, front-end, Python tests), a release every 30
    minutes, and a pull request's quick CI check every ten."""
    rng = random.Random(4)
    jobs: list[dict] = []
    t, n = 0.0, 0
    kinds = [  # (weight, argv, lane, peak GiB range, wall range, cores range, shape)
        (40, ("cargo", "build"), None, (4, 12), (300, 1200), (6, 10), LINK_AT_END),
        (20, ("cargo", "test"), None, (8, 20), (600, 1800), (6, 8), TESTS),
        (15, ("cargo", "check"), None, (2, 9), (60, 300), (1, 2), EARLY),
        (20, ("pnpm", "vitest"), None, (1, 5), (60, 600), (2, 4), TESTS),
        (5, ("python3", "-m", "unittest"), "python-tests", (0.1, 0.3), (15, 120), (1, 1.2), TESTS),
    ]
    weights = [k[0] for k in kinds]
    while t < 7200:
        t += rng.expovariate(1 / 150)
        if t >= 7200:
            break
        _, argv, lane, mem, wall, cores, shape = rng.choices(kinds, weights)[0]
        session = rng.randrange(6)
        extra = {"lane": lane} if lane else {}
        if argv[0] == "cargo":
            extra["workspace"] = f"session-{session}"  # one target folder per session: its builds take turns
        jobs.append(build(t, (*argv, "-p", f"crate{rng.randrange(12)}") if argv[0] == "cargo" else argv,
                          rng.uniform(*mem), rng.uniform(*wall), rng.uniform(*cores), shape=shape, repo="app",
                          caller=f"session-{session}", history=True, **extra))
        n += 1
    for k, start in enumerate(range(600, 7200, 1800)):
        jobs += release(start, k + 1, heavy_lints=False)
    for k, start in enumerate(range(300, 7200, 600)):
        jobs.append(build(start, ("pnpm", "vitest", "infra"), 3.9, 47, 8, shape=TESTS, lane="web", ci=True,
                          caller=f"runner-{k % 4 + 1}", repo="app", name=f"pr{k}:infra-check", history=True))
    jobs.sort(key=lambda j: j["t"])
    return {"name": "mixed-day", "about": "two hours of agents' builds, four releases, a PR check every ten minutes",
            "machine": dict(WORKSTATION), "jobs": jobs, "seed_history": True}


SCENARIOS = {
    "burst": burst,
    "ci-behind-agents": ci_behind_agents,
    "outgrows-estimate": outgrows_estimate,
    "mixed-day": mixed_day,
}
