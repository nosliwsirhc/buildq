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
| `… builds are running, the most allowed` | Every turn is taken. | Wait. |
| `another build in the same workspace is running` | Same `CARGO_TARGET_DIR` or git worktree; two builds would fight over it. | Wait. A new target directory means a cold rebuild, which costs the machine far more than the wait. |
| `memory pressure is …%` / `CPU pressure is …%` | The machine is struggling right now. | Wait. |
| `… even with no builds running only … would be free: other work holds the rest` | Work outside the queue (CI jobs, containers) holds the memory it needs. Nothing is held back for it, and it starts when that work ends. | Wait. Tell the owner if it lasts. |
| `needs …, more than this machine has` | It can never fit as it is. | Cancel it. Run a smaller job (one crate or test target), or tell the owner. |
| `only … of disk is free where it runs` | The disk is nearly full. | Don't build. Tell the owner. |
| `failed (exit …, killed for memory)` | The build reached its own lane's memory cap and was stopped. | Run a smaller job. Don't look for a way around the cap. |
| `failed (exit 137)` without "killed for memory", or `stopped for memory (systemd-oomd or the kernel)` | The machine as a whole ran short, and something outside the build stopped it. | Run it again once: it queues until there is room. If it happens again, tell the owner. |
| `waited on memory …s …; it went over its MemoryHigh` | It ran, but was slowed at its memory limit. | Nothing. Mention it to the owner if a command keeps doing it. |
| `buildq: refusing to compile <crate> outside the build queue` | cargo ran outside a queued build, here where Rust compiles must be queued. | Run the same cargo command through the queue. |
| `the queue daemon isn't answering …` | The daemon is down. A `run` waits for it. | Check `systemctl --user status buildq`. Tell the owner if it stays down. |

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

The command decides its lane: front-end tools, Rust, .NET and so on, as this machine's
configuration says. A lane sets a build's priority, cores and memory cap.
