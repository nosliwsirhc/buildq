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
python3 bench/sim.py check                      # each scenario twice: the results must match
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
| makespan | 1.6h | 1.8h | +11% |
| CI wait p50 | 23.9m | 4.8m | -80% |
| CI wait max | 31.0m | 5.0m | -84% |
| agent wait p50 | 30.5m | 34.3m | +13% |
| CI timed out | 1 | 0 | -100% |
| stops (memory + CI) | 0 | 3 | +3 |
| running time lost to stops | 0s | 34.0m | +34.0m |
| running time lost to cancels | 19.0m | 0s | -100% |

Temporary state goes under `$TMPDIR` and is deleted after each run.

## In CI

The `replay` workflow (`.github/workflows/sim.yml`) runs `check` on every push and pull request. On
a pull request it also runs `compare` with the base branch's daemon against the pull request's, and
writes the table to the job summary. A result that is slower or faster never fails the job: only a
crash, or a run that doesn't repeat exactly, does. If the base daemon can't load the bench
configuration (it predates a setting the configuration uses), that scenario shows the head's
results only.

## Scenarios

The built-in scenarios (`bench/scenarios.py`) are synthetic and generated from fixed seeds. Their
sizes are modelled on one busy workstation's measured builds, but carry no names from it. The
machine has 32 cores and 125 GB, with 26 GB and 3 cores taken by other work.

| Scenario | What it is |
|---|---|
| `burst` | ten cargo builds submitted within ten seconds |
| `ci-behind-agents` | seven long agent test builds fill the machine; a release's CI steps arrive behind them, two of them big (16 and 12.6 GB) |
| `outgrows-estimate` | a build whose history says 4 GB grows to 22 GB while others fill the machine |
| `mixed-day` | two hours of agents' builds (cargo, checks, front-end, Python tests), a release every 30 minutes, a pull request's CI check every ten |

Every command in them starts with a history of one earlier run of its size, as on a machine that has
built it before (`seed_history`).

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
| `history`, `history_peak` | optional: give its command a history of one earlier run, of `history_peak` bytes |

## A trace from your own machine

```sh
python3 bench/sim.py export --db ~/.local/state/buildq/metrics.db \
    --since 2026-11-14T06:00 --until 2026-11-14T08:00 --anonymize > ~/morning.jsonl
python3 bench/sim.py run ~/morning.jsonl
```

`export` reads finished builds from the analytics database (`metrics = true`) and writes them as a
trace. Their memory profile, cores and run time are what the machine measured.

**Never commit a real trace.** It holds your repositories' names, the commands your sessions ran
and who ran them. `--anonymize` hashes repository names, callers and the words of a command after
its tool and subcommand, and drops build names. That hides them from a casual reader, but a hash of a
common word can be guessed. Keep traces on your machine; the scenarios in the repository are
synthetic.

## The model

The daemon is the real one. What it would read from the machine and from systemd comes from the
simulation, and that part is simplified:

- **Time** moves one second at a time. Each second the simulation does what the daemon's own tick
  does: check, shed memory, schedule, make room for CI, and rebalance cores every five seconds. The
  memory watch, the slice guard, source checks, GitHub statuses and the analytics are left out.
- **Cores:** a build keeps busy the cores it is granted, up to its recorded average. When builds want
  more cores than the machine has, everyone slows down evenly.
- **Run time:** a build has the recorded run's work to do. With fewer cores than it used, it takes
  longer in proportion; it never goes faster than it was recorded.
- **Memory** follows the recorded profile over the run. Without a profile, it rises to its peak over
  the first fifth. Page cache, swap and memory pressure aren't modelled: memory PSI stays at 0, so
  admission never waits on memory pressure. CPU pressure is the share of the cores asked for that
  the machine couldn't give.
- **Running out:** a build over its lane's `MemoryMax` is killed, and fails. If all the builds
  together pass the machine's memory, the largest is killed, as the kernel would.
- **A stopped build starts over from the beginning** with all of its work to do again. A real cargo
  build keeps the crates it finished, so the simulation overstates what a stop costs.
- **CI jobs** are cancelled at their time limit, as GitHub cancels them, whether queued or running.
- **Other work** (sessions, idle CI runners, containers) holds a fixed amount of memory and cores.
  There is no disk: `disk_floor` is 0 in the bench configuration.
- **Jobservers** aren't modelled beyond the cores granted.

## Reading the summary

| Line | Meaning |
|---|---|
| makespan | from the first submission to the last build's end |
| wait p50 / p95 / max | from submission to first start; a build that never started counts until it ended. By lane, and for CI against agents |
| CI timed out (in the queue) | CI jobs cancelled at their time limit, and how many of those never started |
| stops for memory / for CI | builds stopped and queued again by `shed_memory` and by `make_room_for_ci` |
| running time lost to stops / cancels | how long the builds stopped or cancelled had been running |
| OOM kills | over a build's own cap, or the machine out of memory |
| idle core-seconds while waiting | cores under the CPU target left unused while some build waited |
| least memory free | the lowest free memory seen; admission aims to keep `mem_floor` free |
| planned / peak | what builds were planned at against what they used: the median ratio, and how many used more than planned |
