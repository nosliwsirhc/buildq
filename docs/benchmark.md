# Benchmarking: replaying builds

Unit tests show that buildq's scheduling does what its code says. They don't show whether a change
makes builds wait less, and a busy machine can't show that either, because its load never repeats.
The replay simulator answers that question. It runs the real daemon (`bin/buildqd`) against a
simulated machine, clock and set of builds. The same input always gives the same result, so two
versions of the daemon, or two configurations, can be compared on identical work.

This is the first part of [#34](https://github.com/nosliwsirhc/buildq/issues/34). The second part,
a spare machine running real builds, is planned there.

## Running it

```sh
python3 bench/sim.py scenarios                  # the built-in scenarios
python3 bench/sim.py run ci-behind-agents       # one scenario's summary (--json for all of it)
python3 bench/sim.py check                      # each scenario twice, the second in a fresh process: they must match
python3 bench/sim.py compare                    # this checkout's daemon against itself, every scenario
```

`run` also takes `--jobs FILE` (what happened to each build, JSONL) and `--log FILE` (the daemon's
own log, in simulated seconds).

**Two versions of the daemon** on the same scenarios:

```sh
git worktree add ../buildq-main main
python3 bench/sim.py compare --base ../buildq-main/bin/buildqd --head bin/buildqd
```

**Two configurations** on one daemon. Copy `bench/bench.toml`, change a setting, and pass the
original as the base:

```sh
sed 's/^make_room_for_ci = "5m"/make_room_for_ci = "0"/' bench/bench.toml > off.toml
python3 bench/sim.py compare ci-behind-agents --base-config off.toml
```

which prints (base: no stops for CI; head: stops after 5 minutes):

| | base | head | change |
|---|---:|---:|---:|
| makespan | 1.6h | 1.8h | +13% |
| CI wait p50 | 23.9m | 4.8m | -80% |
| CI wait max | 31.1m | 5.0m | -84% |
| agent wait p50 | 30.3m | 35.2m | +16% |
| CI timed out | 1 | 1 | same |
| stops (memory + CI) | 0 | 2 | +2 |
| running time lost to stops | 0s | 22.0m | +22.0m |
| running time lost to cancels | 18.9m | 45.1m | +138% |

Temporary state goes under `$TMPDIR` and is deleted after each run.

## In CI

The `replay` workflow (`.github/workflows/sim.yml`) runs `check` on pushes to main and on pull
requests. On a pull request it also runs `compare` with the base branch's daemon against the pull
request's, and writes the table to the job summary; on main it writes each scenario's results. A result that is slower or faster never fails the job: only a
crash, or a run that doesn't repeat exactly, does. A configuration that doesn't load is an error
here, not the daemon's defaults (which would compare something else): if the base daemon can't load
the bench configuration (it predates a setting the configuration uses), that scenario shows the
head's results only.

## Scenarios

The built-in scenarios (`bench/scenarios.py`) are synthetic and generated from fixed seeds. Their
sizes are modelled on one busy workstation's measured builds, but carry no names from it. The
machine has 32 cores and 125 GB, with 26 GB and 3 cores taken by other work.

| Scenario | What it is |
|---|---|
| `burst` | ten cargo builds submitted within ten seconds |
| `ci-behind-agents` | seven long agent test builds fill the machine; a release's CI steps arrive behind them, two of them big (16 and 12.6 GB) |
| `outgrows-estimate` | two builds whose history says 3 GB grow to 24 GB by mid-run, while a stream of 12 GB builds keeps the machine full. The daemon's re-planning of a build past its estimate keeps the memory floor; a daemon without it spends minutes below the floor here |
| `mixed-day` | two hours of agents' builds (cargo, checks, front-end, Python tests), a release every 30 minutes, a pull request's CI check every ten |

Every command in them starts with a history of three earlier runs of its size, as on a machine that
has built it before (`seed_history`): granted its lane's cores, with when each run peaked and what it
used after, taken from its profile, so "past the peak" (`release_after_peak`) has the runs it reads.

A trace file works as a scenario too: one build per line, in JSON.

| Field | Meaning |
|---|---|
| `t` | when it is submitted, in seconds from the first build |
| `argv`, `repo` | the command, and the repository it runs in (its history key) |
| `lane` | optional; without it, the lane rules decide |
| `ci`, `caller` | a CI job's build, and who submitted it (a runner, a session) |
| `wall`, `avg_cores` | how long it ran, and how many cores it kept busy on average |
| `peak_mem`, `profile` | its peak memory in bytes, and the most memory in each tenth of its run in MiB |
| `workspace` | optional: a target folder it shares with other builds, which then take turns |
| `exit`, `restartable`, `timeout_s`, `cold` | optional: its exit code, `--restartable`, a CI job's time limit (default 50 minutes), and whether cargo builds from scratch |
| `history`, `history_peak` | optional: give its command a history of three earlier runs, of `history_peak` bytes |

## A trace from your own machine

```sh
python3 bench/sim.py export --db ~/.local/state/buildq/metrics.db \
    --since 2026-11-14T06:00 --until 2026-11-14T08:00 --anonymize > ~/morning.jsonl
python3 bench/sim.py run ~/morning.jsonl
```

A trace replays with no history unless `--seed-history` is given (`run`, `compare`, `check`): then
each of its commands starts with three earlier runs of its own size. Without it, every build is
planned at its lane's default memory, as on a machine that has never run them.

`export` reads finished builds from the analytics database (`metrics = true`) and writes them as a
trace. Their memory profile, cores and run time are what the machine measured, for their last
attempt. A build that was stopped and queued again (`requeued`) has its last attempt's run time where
the database keeps it (`attempt_wall`, since #39). In an older database it has every attempt's run time
and no profile, so its memory rises to its peak in the first fifth. Each build's workspace comes along,
so builds that shared a target folder take turns in the replay as they did. A build that was
cancelled or lost has no exit code, and replays as a failure; its `state` says what it was.

**Never commit a real trace.** It holds your repositories' names, the commands your sessions ran
and who ran them. `--anonymize` hashes repository names, workspaces, callers, lanes other than the
usual ones (`heavy`, `check`, `web` …), and every word of a command except a known tool (`cargo`,
`pnpm`, `python3`, `make` …), its options and a short list of public subcommands (`build`, `test`,
`vitest`, `unittest` …), and drops build names. A lane the bench configuration doesn't have falls to
the lane rules in the replay. That hides them from a casual reader, but a hash of a
common word can be guessed. Keep traces on your machine; the scenarios in the repository are
synthetic.

## The model

The daemon is the real one. What it would read from the machine and from systemd comes from the
simulation, and that part is simplified:

- **Time** moves one second at a time. Each second the simulation does what the daemon's own tick
  does for scheduling: check, shed memory, schedule, make room for CI, and rebalance cores every
  five seconds. Left out: the memory watch, the slice guard, dropping unwatched builds, flock slots,
  source checks, wait messages, pruning, GitHub statuses, hooks and the analytics.
- **The machine's readings:** busy cores and CPU pressure are ten-second averages, as the daemon's
  are; free memory is read each second.
- **Cores:** a build keeps busy the cores it is granted, up to its recorded average, from its first
  second. When builds want more cores than the machine has, everyone slows down evenly.
- **Run time:** a build has the recorded run's work to do. With fewer cores than it used, it takes
  longer in proportion; it never goes faster than it was recorded.
- **Memory** follows the recorded profile over the run. A tenth with no sample in it (a short run
  is sampled every ten seconds) holds what the last sample did. Without a profile, memory rises to
  its peak over the first fifth. Page cache, swap and memory pressure aren't modelled: memory PSI stays at 0, so
  admission never waits on memory pressure, and a build at its `MemoryHigh` isn't slowed. CPU
  pressure is the share of the cores asked for that the machine couldn't give. A build that ends or
  is stopped gives its memory back at once.
- **Running out:** a build over its lane's `MemoryMax` is killed, and fails. If all the builds
  together pass the machine's memory, the largest is killed, as the kernel would. These limits apply
  in a build's last second too, and the daemon sees that second's use.
- **Time limits:** a build past its lane's `timeout` is stopped and fails (exit 124), as the
  supervisor would stop it.
- **A stopped build starts over from the beginning** with all of its work to do again. A real cargo
  build keeps the crates it finished, so the simulation overstates what a stop costs.
- **CI jobs** are cancelled at their time limit, as GitHub cancels them, whether queued or running.
  The limit counts from when the build was submitted to buildq, not from when the job started.
- **Other work** (sessions, idle CI runners, containers) holds a fixed amount of memory and cores.
  There is no disk: every path has plenty of room, whatever `disk_floor` says.
- **Jobservers** aren't modelled beyond the cores granted.

## Reading the summary

| Line | Meaning |
|---|---|
| makespan | from the first submission to the last build's end; if the run was cut off at its horizon (six hours past the last arrival) before every build finished, the time simulated, marked "cut off" (`complete: false` in the JSON) |
| wait p50 / p95 / max | from submission to first start; a build that never started counts until it ended. By lane, and for CI against agents |
| CI timed out (in the queue) | CI jobs cancelled at their time limit, and how many of those never started |
| stops for memory / for CI | builds stopped by `shed_memory` (queued again, or failed when they couldn't start over) and by `make_room_for_ci` |
| running time lost to stops / cancels | how long the builds stopped or cancelled had been running |
| OOM kills | over a build's own cap, or the machine out of memory |
| idle core-seconds while waiting | cores under the CPU target left unused while some build waited |
| least memory free, time below the floor | the lowest free memory seen, and the seconds spent with less than `mem_floor` free (what admission keeps free for everything else) |
| holds let go past the peak | builds whose held memory `release_after_peak` let go |
| planned / peak | what builds were planned at against what they used: the median ratio, and how many used more than planned |
