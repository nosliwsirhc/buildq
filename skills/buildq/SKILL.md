---
name: buildq
description: How to run compiles, test suites, image builds and other heavy commands on a machine whose builds share one queue (buildq). Covers running a command through the queue, dropping a long job off and checking back by ticket, what each wait message means, and what to do when a build is killed for memory or a Rust compile is refused. Use before running cargo, make, ninja, dotnet, a JS build or test suite, docker build, or a push whose hook runs tests, and whenever buildq or its wrapper prints a ticket, a wait reason or a refusal.
---

# Building on a machine that queues its builds

This machine's builds share its cores and memory through **buildq**, a queue. You hand a command to
the queue, and it starts the command once the machine has room for it. Room means the build's
expected memory fits in what is free, and its cores fit under the CPU target. Both are measured
across the whole machine, so CI jobs and containers count too. The queue gives each build its
cores and resizes cargo, make and ninja builds while they run. Waiting is normal and nothing is
wrong while a build waits.

If the section "On this machine" at the end names a wrapper, type the wrapper's form of each
command below. It is the same queue.

## Run a command

A short command (minutes) runs in the queue and behaves like the command itself. Its output
streams, and its exit code comes back:

    buildq run -- <command> [args...]

For a long command, drop it off: a full test suite, a release build, anything that could outlast
your tool call's time limit.

    T=$(buildq submit -- <command> [args...])   # prints a ticket (a UUID) and returns at once
    buildq wait "$T" --timeout 9m                # its exit code once it ends; 124 with "still queued/running": wait again
    buildq logs "$T" --tail 100                  # the end of its output (--follow to stream)
    buildq status "$T"                           # state, wait reason, cores, memory, estimate
    buildq cancel "$T"

- Claude Code: run `buildq wait "$T"` as a background command, and you are told when it ends.
- A `buildq run` belongs to whoever called it. If that call is interrupted, killed or timed out,
  the build is cancelled within half a minute. A dropped-off build keeps running whatever happens
  to the caller. After a timeout, wait on the ticket. Never start the same build again.
- Commands run inside a build (a script the build runs) go straight through: they share the
  build's turn. `buildq submit` refuses there.
- Submit a script that only builds and runs tests with `--restartable` (`heavy-run --restartable
  bash run.sh`). buildq can't see inside a script, so without it a script stopped for memory fails
  instead of being started over, and it can never be stopped to let a waiting CI job's build in.
  Plain `cargo build`/`test`, `dotnet build`/`test`, `tsc`, `eslint` and `vitest` need nothing.
  Never mark a script that pushes, uploads, deploys or sends anything.
- When a dropped-off build fails, `buildq wait` ends with its last 20 lines of output: the error
  is usually there, with no need to ask for the log.

## The same source builds once

The queue records the commit you build from, and a fingerprint of your changes on top
(`buildq status <ticket>` shows "commit 4f1c2a0 + uncommitted changes"). Send the same command on
the same source again, from the same directory, with the same environment and options, and:

- **It is queued or running already:** you get its ticket and follow that build. That is the
  build to wait on. With `run`, Ctrl-C then stops your following; the build goes on for whoever else
  waits for it. `buildq cancel` won't stop a shared build unless you add `--all`: only do that if
  you know nobody else needs it.
- **A check passed on it in the last two hours** (cargo check, clippy or test, dotnet test,
  `tsc --noEmit`, eslint, `vitest run`): you get that result and its output at once, and nothing
  runs. Change the source, and it runs.
- **A build that makes files, or a script, passed on it** (cargo build, a bundle, `run-tests.sh`):
  it runs again, and the queue says it passed before. Its output may have changed since, and a
  script might do anything.

To run it anyway (a flaky test you are checking, a database that changed), add `--again`. To take
an earlier pass of a script on the same source, add `--reuse`. Don't add `--again` by habit:
running the same suite twice on the same source is exactly what this saves.

The fingerprint covers what git sees in the repository. It doesn't cover path dependencies
outside it, your toolchain, or ignored files other than `.env*`. After changing one of those, use
`--again`.

## Don't

- Don't set parallelism yourself: no `-j`, `CARGO_BUILD_JOBS`, `--test-threads` or `MAKEFLAGS`.
  The build gets `BUILDQ_CORES` and a jobserver sized to the machine. A `-j` you add fights the
  queue's sizing or is removed.
- Don't wait for the machine yourself: no watching `uptime`, polling `ps`, `sleep` loops before
  starting, or `nohup … &`. The queue already waits, and agents who all wait for the same quiet
  moment all start at once.
- Don't move a Rust compile into a quicker lane (`--lane`) to skip the line. The lanes' limits
  are sized for what runs in them.

## What the queue tells you

| Message | Meaning | Do |
| --- | --- | --- |
| `needs 20G of memory; 9.6G is free after the …` | Its expected peak doesn't fit yet. Memory is never overcommitted. | Wait. |
| `needs 4 cores; 1.2 of 28.8 are free …` | The CPU is at its target. | Wait. |
| `the room it needs is held for a build that has waited longer` | A long-waiting build goes first. | Wait. |
| `the room it needs is held for a CI job's build, which goes first` | With `ci_first`, a CI job's step goes before builds of its priority or lower. | Wait. |
| `another build in the same workspace is a CI job's, which goes first, and is held (…)` | The same, for the workspace. | Wait. |
| `… builds are running, the most allowed` | Every turn is taken. | Wait. |
| `another build in the same workspace is running` | Same `CARGO_TARGET_DIR` or git worktree; two builds would fight over it. | Wait. A new target directory means a cold rebuild, which costs the machine far more than the wait. |
| `memory pressure is …%` / `CPU pressure is …%` | The machine is struggling right now. | Wait. |
| `… even with no builds running only … would be free: other work holds the rest` | Work outside the queue (CI jobs, containers) holds the memory it needs. Nothing is held back for it, and it starts when that work ends. | Wait. Tell the owner if it lasts. |
| `needs …, more than this machine has` | It can never fit as it is. | Cancel it. Run a smaller job (one crate or test target), or tell the owner. |
| `only … of disk is free at <path>` | The disk is nearly full there (the build's folders, buildq's state, or container storage for an image build). | Don't build. Tell the owner. |
| `failed (exit …, killed for memory)` | The build reached its own lane's memory cap and was stopped. | Run a smaller job. Don't look for a way around the cap. |
| `failed (exit 137)` without "killed for memory", or `stopped for memory (systemd-oomd or the kernel)` | The machine as a whole ran short, and something outside the build stopped it. | Run it again once: it queues until there is room. If it happens again, tell the owner. |
| `waited on memory …s …; it went over its MemoryHigh` | It ran, but was slowed at its memory limit. | Nothing. Mention it to the owner if a command keeps doing it. |
| `buildq: refusing to compile <crate> outside the build queue` | cargo ran outside a queued build, here where Rust compiles must be queued. | Run the same cargo command through the queue. |
| `the queue daemon isn't answering …` | The daemon is down. A `run` waits for it. | Check `systemctl --user status buildq`. Tell the owner if it stays down. |
| `the queue is paused (…)` | The owner paused it (a reboot, maintenance). Running builds carry on. | Wait, or drop the build off and check back later. Don't resume it yourself. |
| `paused: the machine is short of memory` | Other work took the memory. Your build is frozen where it was. | Wait. It goes on by itself once memory is back; if memory stays short, it may be stopped (and queued again if it can safely start over). |
| `stopped to free memory, and queued again at the front` | The machine ran very short. Your build was stopped, and starts over from the beginning once there is room. | Keep waiting on the same ticket. Don't submit it again. |
| `stopped to make room for a CI job's build …` | A CI step had waited for memory your build held. Your build was stopped and queued again; it starts over once that step has started, and then goes first. | Keep waiting on the same ticket. Don't submit it again. |
| `stopped to free memory … It isn't started again by itself` | The machine ran very short. It was a script or a push, and starting it over might repeat what it did. | Check what it had done, then run it again. If it only builds and tests, submit it with `--restartable` next time. |
| `the same command on the same source is already running as ticket …` | Someone (maybe you, before a timeout) started this exact build. | Wait on that ticket. |
| `this exact source passed this command … ago` | It already passed on this source. | Nothing to do. `--again` only if you have a reason. |

## Exit codes

`run` and `wait` exit with the build's own code (128 + N if signal N ended it, 124 if its own
`--timeout` ran out). The client itself exits 2 when it refuses a request, 75 when it can't reach
the daemon, 124 when `wait --timeout` runs out first, and 125 when
the build ended without a code (cancelled before it ran, or lost). A build that ran out of its
own time limit ends 124 too, and says "timed out": don't wait on it again. After a 75 from
`submit`, check `buildq status` before submitting again: if the daemon went away mid-reply, the
build may be queued already.

## Looking at the queue

- `buildq status`: the machine's load, what runs, what waits and why, recent results.
- `buildq list`: every ticket.
- `buildq status <ticket>`: one ticket. A short prefix of the ticket works.
- `buildq help agents`: this page, for an agent without skills.

## Questions about the machine

When the owner asks how busy the machine has been, what builds wait for, or whether capacity sits
idle, answer from the queue's analytics rather than guessing:

- `buildq report --capacity` (`--hourly`, `--since 2d`): by day, the build-time spent waiting for
  memory, for cores and for anything else; the cores, memory and growth room idle or held while
  builds waited on memory; cores used by builds, CI, containers and the user's apps; and how
  close builds' peaks came to what was planned.
- `buildq report` (`--by lane`, `--by caller`): per command, runs, failures, time, waits, peaks
  and kills.
- `buildq top --once`: the machine and every running build right now: CPU, memory, swap, pressure, disk I/O and the size of each build's folders (it measures them first for up to 20 seconds; "…" means not measured yet), plus what waits and why. Without `--once` it is a live screen for the owner, like `btop`.
- `buildq query "SELECT …"`: anything else, read-only. `buildq query` alone lists the tables:
  `minute` (14 days), `hour` (a year), `build` (180 days, one row per finished build, with what it
  waited for and its memory profile).

The command decides its lane: front-end tools, Rust, .NET and so on, as this machine's
configuration says. A lane sets a build's priority, cores and memory cap.
