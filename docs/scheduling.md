# How buildq decides

When a build starts, in what order, how many cores it gets, and what happens when memory runs short. [docs/design.md](design.md) has the internals.

## When a build starts

The daemon re-plans every second and whenever a build arrives or ends. It walks the queue in
order and starts every build that fits:

| Test | A build starts only if |
| ---- | ---------------------- |
| Memory | its expected peak ≤ `MemAvailable` − what running builds may still grow into − `mem_floor`. Never skipped. |
| Cores | its minimum cores fit in what is free: `cpu_target` × cores − busy cores − cores that builds just started haven't reached yet + cores that resizable builds could give back. Skipped when nothing is running, so outside load can slow the queue but never stall it, and for lanes with `waits_for_cpu = false`. |
| Pressure | memory pressure (PSI `full avg10`) ≤ `mem_psi_block`; CPU pressure (`some avg10`) ≤ `cpu_psi_block` while a build runs (not for lanes with `waits_for_cpu = false`) |
| Count | fewer than `max_jobs` builds run, not counting up to `extra_jobs` of its own lane |
| Disk | every filesystem it writes to has at least `disk_floor` free (a size, or a percentage of that filesystem's size, such as `"20%"`): its directory's, its `CARGO_TARGET_DIR`'s and `TMPDIR`'s, buildq's state directory's, and, for an image build (docker, podman, buildah), the container storage's (`container_storage`). This gates starts only: builds already running can still write below it. |
| Workspace | in an exclusive lane, no other build of that lane or of its `workspace_group` runs in its workspace (its `CARGO_TARGET_DIR`, or its git worktree), and the workspace isn't held for a build of the group that goes first (one that has waited longest, or a CI job's with `ci_first`); they would only fight over the build directory |

## Order

A build that has waited `starve_seconds`, or was stopped once to free memory, goes
first, and the room it needs, its workspace included, is held for it, so later builds can't keep
passing it (the workspace binds every lane in its group, a fresh CI build's only its priority or
lower; for the rest, lanes with
`may_pass_held` still may). With `ci_first`, a build from a CI job counts as having waited from the
moment it is queued: a CI step has a job time limit and holds up a merge or a release, while a
session's build can wait. It goes ahead of every build that hasn't waited `starve_seconds`,
whatever its lane, and of the others at its priority, so when room frees up it gets it first. While
it waits, the room it needs is held only from builds of its priority or lower: a higher lane may
still start in room that is free (until the CI build has really waited `starve_seconds`). A
build that has waited three times `starve_seconds`, or was stopped to free memory, goes before
those (a CI build in a lane with `may_pass_held` gets no head start, like any build there). After that, the higher priority
first (the lane's plus the ticket's own); within a priority, the build whose caller has the fewest
builds running (a fair share: one
session's burst of builds doesn't keep the others waiting), then the build expected to finish
soonest (the median time of its command's runs), then the oldest. A build that doesn't fit doesn't
block those behind it. While the queue is paused (`buildq pause`), nothing new starts.

## When memory runs short anyway

Admission never overcommits, but other work can grow after a
build starts: CI jobs, containers, a session. With `shed_memory` on (the default):

- Below half of `mem_floor` available and still falling, buildq pauses the newest build holding
  at least 1 GiB: systemd freezes it, so it asks for no more memory and uses no CPU (when memory
  is low but steady, pausing would free nothing, so nothing is paused). A paused build goes on once
  the machine has `mem_floor` free plus the room it may still grow into. Each attempt may be paused
  for five minutes in all: then it goes on if half the floor is free, and is not paused again, or
  it is stopped (below). Time paused counts neither toward its timeout nor in its learned duration.
- Below a quarter, buildq stops a build at once (a recent pause doesn't hold that up): the newest
  of those whose memory covers the shortfall, else the largest, and never one holding less than
  512 MiB (if what holds the memory isn't buildq's, stopping small builds frees nothing). Memory
  held by builds still being stopped counts as freed, so one shortfall stops one build. A
  compile or test run (cargo, dotnet, tsc, eslint, vitest, ninja) or a build submitted with
  `--restartable` is queued again at the front and starts from the beginning once there is room;
  a client following it keeps waiting and is told why. Anything else (a script, a push) fails,
  saying so, as starting it over might repeat what it had done.

This is GNU parallel's `--memsuspend` and `--memfree`. With swap on zram, which is memory too,
pausing alone frees little; stopping does. A build is queued again at most three times, and not
if the daemon has restarted since it started (its environment isn't kept on disk once it starts).
Paused builds are resumed when the daemon stops.

## Past the peak

A build keeps room held for its expected peak until it ends. A `cargo test`
whose compile peaked at 18 GB holds that through an hour of tests at 2 GB. `release_after_peak`
lets go of most of it once the build is clearly past its peak, keeping what its command's runs
used after their peaks, plus a tenth. All of these must hold for 30 seconds:
- The runs behind the estimate (the biggest, and at least three recent ones) all succeeded, weren't
  slowed at their `MemoryHigh`, and say when they peaked. None of them peaked in its last minute or
  last tenth, and no run in the estimate's window was killed for memory.
- This run has gone a quarter longer than the latest of those peaks and a minute more, and done a
  quarter more CPU work than any of them had by its peak (so a run on fewer cores waits for its
  peak). Its own peak was a minute or more ago.
- It reached four fifths of the peak behind the estimate, on no more cores.
- It now uses (swap included) at most 70% of its peak, and no more than its command used after its
  peaks.
- It isn't a cold build or one given a size (`--mem`). It hasn't been paused or gone over its
  `MemoryHigh` this attempt. It wasn't picked up after a daemon restart. Its lane allows it: `release_after_peak = false` in a
  lane turns it off there, for builds whose memory is used elsewhere, such as `docker build`.

A released build that grows past what was kept (or is killed for memory), or is then paused,
picked up after a restart or slowed at its `MemoryHigh`, is held in full again. A build past its
peak gets no more cores.

The default is `"shadow"`: the queue works all of this out and records it in the analytics, but
keeps holding everything (its only effect on scheduling: a build past its peak gets no more cores,
as with `true`) (a decision made in shadow mode never takes effect, even if the mode is
switched on mid-run). `buildq report --capacity` then says how many attempts were past their peak,
how many later used more than would have been kept and by how much, how much held room it would
have freed, and how many can't say because a restart left them unwatched. Turn it on (`true`) once that shows no build going past what was kept; `false` turns it off.

## Estimates

A command's expected peak memory is the largest of its runs within `history_days`
(every day's biggest run is kept, however many smaller ones follow), with a margin. More cores mean
more compilers at once, so a build given more cores than its peak was measured with needs
proportionally more, up to its lane's `MemoryMax`; the cores a build starts with are cut to what
memory allows. A running build that passes its estimate is planned at its lane's `MemoryMax` from
then on, so the room it may still grow into stays held. A cargo build whose target directory has
no build yet counts as a full build, at least the lane's default. A run that a signal ended
teaches nothing; a run killed at its own `MemoryMax` raises the estimate to that cap.

## Cores

Each build gets a number of cores between its lane's `min_cores` and its *want*: what its history
says it can use. A run that used nearly all the cores it was given wants half as many again next
time; one that left cores idle wants a little more than it used; a build with no history wants the
lane's `cores` (its `max_cores` if it is a cold cargo build). The number goes into its environment,
unless the caller set these: `BUILDQ_CORES`, `CMAKE_BUILD_PARALLEL_LEVEL`, `GOMAXPROCS`,
`DOTNET_PROCESSOR_COUNT` (with MSBuild's node reuse and shared compiler off, so .NET compiles inside
the build where it is measured), `CARGO_BUILD_JOBS` when there is no jobserver, and, in lanes with
`test_threads = true`, `RUST_TEST_THREADS` and `NEXTEST_TEST_THREADS` (off by default: they win over
a project's own pinned thread counts). `make` runs in parallel only when its caller asked (`-j`):
a Makefile not written for it can break.

A build whose tools use the GNU make jobserver protocol, which cargo, make (4.4 and newer) and
ninja (1.13 and newer) do, also gets a jobserver of its own: a named pipe with one token per core
beyond the first, passed in `CARGO_MAKEFLAGS` (cargo hands it to its build scripts) and, for ninja
and a `make -j`, in `MAKEFLAGS`, with the command's own `-j` removed, since make ignores a jobserver
when given `-j` on its command line. A lane's `jobserver` setting says for
which builds: `"tools"` (the default) when the command is cargo, make or ninja, `true` for every
build (scripts that call them too), `false` for none. A build gets none when its caller set `MAKEFLAGS`,
`CARGO_MAKEFLAGS` or `MFLAGS` (that jobserver is the caller's), or when its history shows it ignores
the job count. A jobserver an outer make passed by file descriptor (`--jobserver-auth=R,W`) doesn't
count: those descriptors don't reach the build, so buildq drops it, with its `-j`, keeps the
caller's other make flags, and gives the build its own. Every five seconds buildq hands idle cores to
builds that are using all of theirs, if the memory more cores need is free, and takes cores back when
the machine is over its target or a queued build needs them. A build whose history shows it keeps
using many more cores than it is given (it ignores the job count) is planned at what it uses, up to
its lane's `max_cores`.

A lane with `waits_for_cpu = false` doesn't wait for cores: when the CPU is at its target, its build
starts anyway on the cores it needs (its `min_cores`, or what it was seen to use if it ignores the
job count; more when they are free), and at the next resize the resizable builds give that much
back. It also passes the cores held for a long-waiting build. Meant for quick builds that should
never queue behind long ones; memory still decides when they start.

## Lanes

A lane is a kind of build: its default size, its priority, its systemd limits.

```toml
default_lane = "heavy"

[lanes.heavy]                      # cargo: slow, and sized by its history
cores = 8
min_cores = 2
max_cores = 16
mem = "20G"
properties = { MemoryHigh = "24G", MemoryMax = "26G", CPUWeight = 100, IOWeight = 100 }

[lanes.web]                        # front-end builds: quick, so first, with turns of their own
cores = 4
max_cores = 8
mem = "6G"
priority = 100
extra_jobs = 3
may_pass_held = true
waits_for_cpu = false
exclusive = false
properties = { MemoryHigh = "10G", MemoryMax = "12G", CPUWeight = 400, IOWeight = 400 }

[[lane_rules]]                     # the command picks the lane when its caller doesn't
match = "^(pnpm|npm|npx|yarn|vite|vitest|tsc|eslint|node) "
lane = "web"
```

[config.example.toml](../config.example.toml) lists every key. `properties` are passed to
`systemd-run --property`, so any service property works (`CPUWeight`, `IOWeight`, `TasksMax`,
...), and systemd checks them when the configuration loads. Past `MemoryHigh` the kernel slows a
build and reclaims its memory; past `MemoryMax` it stops that build alone. `CPUWeight` and
`IOWeight` (default 100) say how the kernel shares an oversubscribed CPU or disk between builds: a
web lane at 400 beside cargo at 100 gets four times the share while both want more. `buildq status <ticket>`
and the journal say how long a build waited on memory and whether it went over its `MemoryHigh`:
a command that keeps doing so is being slowed by its cap, which is worth raising if the machine
has the room.

An exclusive lane (the default) runs one build per workspace: its `CARGO_TARGET_DIR`, or its git
worktree. Exclusive lanes with the same `workspace_group` share that rule. A lane's group is its
own name unless it says otherwise, so `workspace_group = "heavy"` joins the heavy lane's group. A
group named with `workspace_group` must be shared with another lane (naming the lane itself is the
default), and every lane in it must be exclusive.

For example, a lane for `cargo check` and `cargo clippy`, smaller than a full build and ahead of
it, should join the heavy lane's group. A check and a `cargo test` in one target folder then never
start together: they would only wait on cargo's lock. [config.example.toml](../config.example.toml)
has such a lane. A running build keeps its workspace under the group it started with, even if a
reload changes or removes its lane.
