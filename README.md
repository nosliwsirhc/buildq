# buildq

A load-aware build queue for one shared Linux machine. Callers drop off a build and get a ticket;
buildq runs it when the machine has room, sized to fit.

When several people, coding agents and CI runners share a build box, each starts compiles and
test suites without knowing what the others are doing. Left alone, they pile on until the machine
runs out of memory. Put behind a fixed number of slots, they wait in line while most of the cores
sit idle: a slot held by a single-threaded link step, or by a test suite waiting on a database,
counts the same as one held by a 16-core compile.

buildq replaces the slot count with measurement:

- **Admission by what the machine is doing.** A build starts when its expected peak memory fits in
  what is free, after setting aside room for running builds to grow into, and when there are cores
  for it. Busy cores and available memory are measured machine-wide, so CI runners, containers and
  anything started outside the queue count too.
- **Learned sizes.** Every build runs in its own cgroup. Its peak memory, peak cores, and how long
  it took are recorded under a key for the command (repository, tool, subcommand, package), and
  the next run of that command is planned with those numbers.
- **Cores sized to the build and to the machine.** Each build starts with as many cores as are free,
  between its lane's minimum and what its history says it can use. Cargo, make and ninja get a
  [jobserver](#cores) of their own, which buildq resizes while they run: they grow into idle cores
  and give cores back when another build needs them.
- **Quick builds first.** Lanes have priorities, and within a lane the build expected to finish
  soonest goes first. A lane of quick builds can have turns of its own and can start even when the
  CPU is busy, so a front-end build never waits behind a long compile.
- **Caps.** Each lane can give its builds systemd limits (`MemoryHigh`, `MemoryMax`, ...). All
  builds share one slice whose memory limit buildq keeps at what the machine actually has free, so
  when other work grows, builds are slowed or stopped, not the machine.
- **Memory given back.** When other work takes the machine's memory anyway, buildq pauses its
  newest build, then stops one and queues it again at the front (or, for a command that can't
  safely start over, fails it), before the machine can stall.
- **One build per source.** Each ticket records the commit it was submitted on, and a fingerprint of
  the changes on top. The same command on the same source joins the build that is already running,
  and a check (a test suite, clippy, a type check) that already passed on it isn't run again.
- **Fair shares.** At the same priority, a session with fewer builds running goes first.
- **Tickets.** A build dropped off with `buildq submit` doesn't depend on whoever submitted it, its
  terminal, or the daemon: agents can submit, go on working, and check back. `buildq run` behaves
  like running the command directly, for anyone who wants to wait.
- **Small.** Python 3.11 standard library, a POSIX shell script and three systemd user units. No
  root.

```console
$ buildq submit -- cargo test -p api
3f2a91c0-7c1e-4d55-9a0e-2b8f1c6d9e11
buildq: ticket 3f2a91c0-7c1e-4d55-9a0e-2b8f1c6d9e11 [heavy] queued for 0s: needs 18G of memory; 9.6G is free ...

$ buildq status 3f2a
ticket 3f2a91c0-7c1e-4d55-9a0e-2b8f1c6d9e11  (heavy lane)
  state     running for 4m12s, after 1m03s in the queue
  command   cargo test -p api
  in        ~/src/project/api
  using     7.6 cores, 11G now; peak 14G, 8.0 cores
  cores     8 given (resized live through its jobserver); it can use up to 12
  expected  8 cores, 18G (largest of the last 6 runs)
  log       ~/.local/state/buildq/tickets/3f2a91c0-.../output.log

$ buildq wait 3f2a; echo "exit $?"
buildq: 3f2a91c0 succeeded after 6m40s; peak 14G, 8.0 cores
exit 0
```

## Install

Requirements: Linux with cgroup v2, Python 3.11 or newer, and a systemd user session (without one,
builds still queue but run without limits and unmeasured).

```sh
git clone https://github.com/nosliwsirhc/buildq.git
cd buildq
./install.sh                  # the daemon and the client
./install.sh --shims          # optional: heavy commands go through the queue by themselves
./install.sh --rustc-guard=~/src   # optional: cargo under ~/src refuses to compile Rust outside the queue
./install.sh --skill          # optional: a skill that teaches coding agents to use the queue
```

`install.sh` puts `buildq`, `buildqd`, `buildq-shim` and `buildq-rustc-guard` in `~/.local/bin`
(`PREFIX` changes that), writes `~/.config/buildq/config.toml` if there isn't one, installs
`buildq.socket`, `buildq.service` and `buildq.slice` into `~/.config/systemd/user` with a fixed
memory backstop for all builds together (75%/85% of memory, in `buildq.slice.d`), and starts the
daemon. While it runs, the daemon lowers that limit to what the machine has free, never above it.
Run it again to upgrade: running builds carry on and queued ones keep their place.

For a build server, keep the daemon running while you're logged out: `loginctl enable-linger "$USER"`.
`./install.sh --uninstall` removes everything except your configuration and history.

## Use

```sh
buildq submit [OPTIONS] [--] COMMAND [ARGS...]   # queue a build; prints its ticket
buildq run    [OPTIONS] [--] COMMAND [ARGS...]   # queue it, stream its output, exit with its code
buildq status [TICKET...] [--json]               # a ticket in detail, or the queue and the machine
buildq list   [--all] [--json]                   # every ticket: queued, running, recently finished
buildq wait   TICKET [--timeout DURATION]        # block until it finishes; exit with its code
buildq logs   TICKET [--follow] [--tail N]       # its output
buildq cancel [--all] TICKET...                  # take it out of the queue, or stop it (--all: a shared one)
buildq pause [REASON] / buildq resume            # start nothing new (running builds carry on), or go on
buildq report [--since 7d] [--by command|lane|caller] [--json]   # what the history says
buildq help agents                               # the page for coding agents
buildq ping                                      # check that the daemon answers
```

A ticket is a UUID; any unique prefix of four or more characters works in its place.

| Option (submit, run) | Effect |
| -------------------- | ------ |
| `--lane NAME` | the kind of build; otherwise the [lane rules](#lanes) decide |
| `--cores N` | use exactly N cores, in place of the learned size |
| `--mem SIZE` | expect at least this peak memory; it can raise the learned estimate, never lower it |
| `--priority N` | higher goes first (added to the lane's priority) |
| `--timeout DURATION` | stop the build after this long; it then fails with exit 124 |
| `--name TEXT` | a name to show in place of the command |
| `--notify COMMAND` | a shell command to run when it finishes |
| `--again` | run it even if the same command on the same source is queued, running, or passed within `reuse_window` |
| `--reuse` | take an earlier pass of the same command on the same source as its result, even for a command that isn't a check |
| `--restartable` | it is safe to stop and start over from the beginning (it only builds and tests): stopped to free memory, it is queued again instead of failed |

A build runs with the environment, working directory, umask and resource limits (open files,
stack, core size) of the command that submitted it, and no stdin or terminal. `buildq run` streams its
stdout to stdout and its stderr to stderr, so `buildq run -- cargo build --message-format=json | jq`
works; `buildq logs` shows both together.

**Exit codes.** `run` and `wait` exit with the build's exit code (128 + N if signal N ended it, 124
if it timed out), and 125 if it ended without one (cancelled before it ran, or lost). The client
itself exits 2 for a refused request, 75 when the daemon can't be reached (`submit`, or
`BUILDQ_FALLBACK=fail`), and 124 when `wait --timeout` runs out while the build goes on.

**The same source, the same build.** When a build is submitted in a git worktree, the client
records the commit and a fingerprint of everything on top of it that git can see: the diff of
tracked files, the contents, modes and link targets of untracked files, and the `.env*` files git
ignores. It takes tens of milliseconds on a large repository, runs no content filter and writes
nothing into it (`BUILDQ_SOURCE=off` skips it). There is no record (so nothing matches) in a
repository with a content filter in its attributes (LFS, git-crypt, nbstripout), a tracked
`.gitattributes` deleted from the tree, files marked skip-worktree or assume-unchanged, a
submodule, a repository or worktree inside it that
git doesn't ignore (list it in `.git/info/exclude`, for example `.worktrees/`), more than 64 MiB of
changes or 20,000 untracked or ignored files, a git slower than five seconds, or at `/` or the home
directory. `buildq status TICKET` shows the record
("commit 4f1c2a0 + uncommitted changes"). The daemon fingerprints the tree again as the build
starts and as it ends; a build whose tree changed meanwhile says so, and matches nothing later.

A submission of the same command on the same source, from the same directory, in the same lane,
environment (less variables that only differ between sessions, such as terminal and agent-session
ids; secrets count, as a hash) and settings (timeout, cores, memory, umask, limits), whose
arguments name no path outside the tree:

- joins the build if one is queued or running: it gets that ticket and follows it. A shared build
  stops only when nobody waits for it any more: a dropped-off submission waits until the build ends,
  a `run` while it follows; an interrupted `run` (the one that made it or one that joined) just
  stops following while others wait, and `buildq cancel` asks for `--all` before stopping a build
  others wait for. A resent submission gets the same answer it got the first time. A
  joiner's `--notify` runs when it ends;
- is answered by an earlier pass within `reuse_window` (2 hours) if the command is a check: cargo
  check, clippy, test or `nextest run`, dotnet test, `tsc --noEmit`, eslint, `vitest run`, with no
  `--fix`, `-u`, `--update-snapshots`, watch mode or `--no-run`. `run` prints that run's output
  and exits 0, and nothing runs. A build that makes files (cargo build, a bundle, docs) or a
  script runs again, with a note that it passed before: its output may have changed or gone since,
  and a script might do anything. `--reuse` takes the earlier pass for those too;
- never matches a failure, a cancelled or stopped build, or a build without a source record.

`--again` runs it regardless.

**`run` and `submit` differ in who the build belongs to.** A build started by `buildq run` belongs
to that command: interrupting it (Ctrl-C, SIGTERM, SIGHUP) cancels the build, and `run` then ends
by the same signal, so a calling script stops as it would have. If `run` is killed outright
(SIGKILL, as editors and tool timeouts do), the build is cancelled once nobody has followed it for
`unwatched_seconds` (30 s), so abandoned builds don't hold turns. A build dropped off with `buildq
submit` belongs to its ticket and runs to the end whoever is watching.

### Being told when a build finishes

Nothing can push a message into a program that isn't listening, but there are three ways to get
the news without polling:

- `buildq wait TICKET` returns the moment the build ends, with its exit code. An agent harness
  that runs commands in the background and reports when they exit (Claude Code does) turns that
  into a notification.
- `--notify COMMAND` runs a command when the build ends, with `BUILDQ_TICKET`, `BUILDQ_STATE`,
  `BUILDQ_EXIT`, `BUILDQ_LOG` and more set: a desktop notification, a chat message, anything.
- `on_finish` in the configuration does the same for every build.
- When `buildq wait` sees a build fail, it ends with the last 20 lines of the build's output, so the
  error is there without asking for the log.

### Environment

| Variable | Effect |
| -------- | ------ |
| `BUILDQ_SOCKET` | the daemon's socket (default `$XDG_RUNTIME_DIR/buildq.sock`) |
| `BUILDQ_FALLBACK` | what `buildq run` does when the daemon can't be reached. Unset: wait for it. `fail`: exit 75. `run`: run the command unqueued. Anything else: a command prefix to run it through instead. |
| `BUILDQ_AS` | the name of a wrapper in front of buildq. buildq's hints then name the wrapper's commands, so its users are told what they type (`wrapper --submit <command>`, not `buildq submit -- <command>`). The wrapper must take `--submit COMMAND...`, `--wait TICKET [--timeout D]`, `--status [TICKET]`, `--logs TICKET` and `--cancel TICKET` and pass them to buildq's commands of the same names; that is all the hints use. |
| `BUILDQ_SOURCE` | `off` stops the client recording the source a build is of (no joining or reuse then) |
| `BUILDQ_CALLER` | who is submitting, for fair shares; by default the agent session (`CLAUDE_CODE_SESSION_ID`, `CODEX_THREAD_ID`, `T3CODE_THREAD_ID`), else the cgroup the client runs in (a CI runner's service, a terminal's scope) |
| `BUILDQ_JOB`, `BUILDQ_TICKET` | set inside every build to its ticket. A `buildq run` inside a build runs its command directly, because the build already holds a turn. buildq checks that the build really is running. |
| `BUILDQ_CORES` | set inside every build: the cores it was given |

## How it decides

The daemon re-plans every second and whenever a build arrives or ends. It walks the queue in
order and starts every build that fits:

| Test | A build starts only if |
| ---- | ---------------------- |
| Memory | its expected peak ≤ `MemAvailable` − what running builds may still grow into − `mem_floor`. Never skipped. |
| Cores | its minimum cores fit in what is free: `cpu_target` × cores − busy cores − cores that builds just started haven't reached yet + cores that resizable builds could give back. Skipped when nothing is running, so outside load can slow the queue but never stall it, and for lanes with `waits_for_cpu = false`. |
| Pressure | memory pressure (PSI `full avg10`) ≤ `mem_psi_block`; CPU pressure (`some avg10`) ≤ `cpu_psi_block` while a build runs (not for lanes with `waits_for_cpu = false`) |
| Count | fewer than `max_jobs` builds run, not counting up to `extra_jobs` of its own lane |
| Disk | its filesystem has at least `disk_floor` free |
| Workspace | in an exclusive lane, no other build of that lane runs in its workspace (its `CARGO_TARGET_DIR`, or its git worktree); they would only fight over the build directory |

**Order.** A build that has waited `starve_seconds`, or was stopped once to free memory, goes
first, and the room it needs is held for it, so later builds can't keep passing it (lanes with
`may_pass_held` still may). After that, the higher priority first (the lane's plus the ticket's
own); within a priority, the build whose caller has the fewest builds running (a fair share: one
session's burst of builds doesn't keep the others waiting), then the build expected to finish
soonest (the median time of its command's runs), then the oldest. A build that doesn't fit doesn't
block those behind it. While the queue is paused (`buildq pause`), nothing new starts.

**When memory runs short anyway.** Admission never overcommits, but other work can grow after a
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

**Estimates.** A command's expected peak memory is the largest of its runs within `history_days`
(every day's biggest run is kept, however many smaller ones follow), with a margin. More cores mean
more compilers at once, so a build given more cores than its peak was measured with needs
proportionally more, up to its lane's `MemoryMax`; the cores a build starts with are cut to what
memory allows. A running build that passes its estimate is planned at its lane's `MemoryMax` from
then on, so the room it may still grow into stays held. A cargo build whose target directory has
no build yet counts as a full build, at least the lane's default. A run that a signal ended
teaches nothing; a run killed at its own `MemoryMax` raises the estimate to that cap.

### Cores

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
build (scripts that call them too), `false` for none. Every five seconds buildq hands idle cores to
builds that are using all of theirs, if the memory more cores need is free, and takes cores back when
the machine is over its target or a queued build needs them. A build whose history shows it keeps
using many more cores than it is given (it ignores the job count) is planned at what it uses, up to
its lane's `max_cores`.

A lane with `waits_for_cpu = false` doesn't wait for cores: when the CPU is at its target, its build
starts anyway on the cores it needs (its `min_cores`, or what it was seen to use if it ignores the
job count; more when they are free), and at the next resize the resizable builds give that much
back. It also passes the cores held for a long-waiting build. Meant for quick builds that should
never queue behind long ones; memory still decides when they start.

### Lanes

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

[config.example.toml](config.example.toml) lists every key. `properties` are passed to
`systemd-run --property`, so any service property works (`CPUWeight`, `IOWeight`, `TasksMax`,
...), and systemd checks them when the configuration loads. Past `MemoryHigh` the kernel slows a
build and reclaims its memory; past `MemoryMax` it stops that build alone. `CPUWeight` and
`IOWeight` (default 100) say how the kernel shares an oversubscribed CPU or disk between builds: a
web lane at 400 beside cargo at 100 gets four times the share while both want more. `buildq status <ticket>`
and the journal say how long a build waited on memory and whether it went over its `MemoryHigh`:
a command that keeps doing so is being slowed by its cap, which is worth raising if the machine
has the room.

## Keeping builds in the queue

Telling agents to use the queue works most of the time. Two optional pieces make it the default:

- **Shims** (`./install.sh --shims`, then put `~/.local/share/buildq/shims` first on `PATH`, after
  anything else that changes it, such as nvm or rustup). The shims stand in for `cargo`, `pnpm`,
  `npm`, `npx`, `yarn`, `make`, `ninja` and `dotnet`. A heavy command runs as `buildq run -- <the
  real tool> ...`: any cargo subcommand that compiles (everything but `fmt`, `metadata`, `tree`,
  `clean` and the like, external subcommands included), npm and pnpm scripts named like
  `build*`, `test*`, `lint*` or `e2e*`, `vitest run`, `make` (except long-running targets such as
  `dev` or `up`), `dotnet build/test`. Anything else, and any watch mode, runs directly, as does
  everything inside a build, in GitHub Actions jobs (`GITHUB_ACTIONS` and `RUNNER_NAME` set; `CI=true`
  alone isn't enough, since people set it to turn watch modes off), and under rust-analyzer.
  `cargo run` and `dotnet run` build in the queue, then run the program directly, with stdin.
  `BUILDQ_SHIM=off` turns them off for one command.
- **The Rust guard** (`./install.sh --rustc-guard=~/src,~/work`). Cargo runs every `rustc` under those
  directories through `buildq-rustc-guard`, which refuses to compile unless the process is inside a
  buildq build. It checks the build's cgroup, which the kernel sets and an environment variable
  can't fake. It lets through version queries, GitHub Actions jobs, editors
  (`rust-analyzer`), and anything listed in `~/.config/buildq/guard.env`, where `RUN_WITH` also sets
  the command its refusal tells you to use (default `buildq run --`). Prefer directories to the
  bare `--rustc-guard`, which writes `$CARGO_HOME/config.toml`: that file is also read by CI jobs on
  the machine and by containers that mount `~/.cargo`, where the wrapper's path doesn't exist.

Neither is a security boundary: anyone can call a tool by its full path or point `RUSTC_WRAPPER`
elsewhere. They keep well-meaning agents and scripts in line. A hard lock needs root: run the
agents as a user that can't execute compilers and can only submit to the queue.

## Coding agents

Coding agents are the case buildq was built for. They learn to use it in three places, from least
to most effort for them:

- **Where they hit it.** With the shims and the Rust guard, an agent's plain `cargo test` goes
  through the queue, or is refused with the command to use instead. buildq's own messages say what
  to do next: `buildq run` prints the ticket and how to look at it, warns when the command's history
  says it usually runs longer than a tool call may last (and how to drop it off instead), and
  `buildq submit` says how to wait for its ticket. A wrapper sets `BUILDQ_AS` and the guard's
  `RUN_WITH` so that all of these name the wrapper's commands.
- **A skill.** `./install.sh --skill` links a `buildq` skill into `~/.claude/skills` (Claude Code),
  `~/.codex/skills` (Codex) and `~/.agents/skills`, for whichever of those agents' directories exist.
  An agent loads it only when it is about to build or meets a ticket or a refusal. It covers running
  a command, dropping a long one off, what each wait message means and what to do about it, and
  what not to do (set `-j`, wait on `uptime`, start a build again after a timeout). Notes for one
  machine go in `~/.config/buildq/agents.md`, which `--skill` adds at the skill's end ("On this
  machine"): a wrapper, local rules, where build output goes. Run `./install.sh --skill` again
  after editing it. The skill's source is [skills/buildq/SKILL.md](skills/buildq/SKILL.md).
- **Their standing instructions** (`AGENTS.md`, `CLAUDE.md`): one rule ("run heavy commands through
  the queue") is enough once the skill has the details.

Two more things help. A pre-command hook enforces it where the harness has one (for example a
Claude Code `PreToolUse` hook on `Bash`; inside a build, `BUILDQ_JOB` is set). And long builds are
submitted, then waited on: `T=$(buildq submit -- cargo test)`, then `buildq wait "$T" --timeout 9m`
as many times as needed, which fits a tool call's time limit. Or run `buildq wait "$T"` as a
background command if the harness reports when one ends.

There is no MCP server, on purpose. A build has to start from the agent's shell, with that shell's
working directory, environment and limits. An MCP tool runs in the harness's process instead, and
would quietly build with the wrong ones. For watching the queue, `buildq status --json` and `buildq
list --json` already give structured output.

## Migrating from a slot script

If heavy jobs run today behind a `flock`-based semaphore, list its lock files under
`[[flock_slots]]`: builds holding one are counted as running builds, measured, with their growth
held. Keep the old script as the fallback (`BUILDQ_FALLBACK="old-slot-script"`), so builds still
have a gate while the daemon is down. Polling can't stop the two from admitting into the same free
memory at the same moment, so don't run both as gates side by side for long.

## Operate

- **Logs:** `journalctl --user -u buildq` has a line for each build queued, started, resized and
  finished, with its estimate, wait, cores, peak memory and time spent waiting on memory.
- **When memory is tight** (less than twice `mem_floor` available, or every task on the machine
  stalled on memory at once 5% of the time), buildq records every 30 seconds what is available, swap
  and memory pressure, and the six cgroups holding the most memory, builds or not: Docker
  containers, CI runners, editors, agent sessions. It stops once more than 2.5 times `mem_floor` is
  free, with memory pressure under 1%, for half a minute. The lines go to the journal at warning priority (`journalctl --user -u
  buildq -p warning`) and to `~/.local/state/buildq/memory.log`, which is synced to disk with each
  entry: after a reset the journal can be missing its last minutes, and `memory.log` isn't.
- **Restarts are safe.** systemd holds the socket, so clients connecting during a restart wait
  rather than fail. Builds run in services of their own and carry on; queued tickets are on disk.
  The next daemon picks both up.
- **Files** are in `~/.local/state/buildq`: `tickets/<id>/` (the ticket, its output log, its
  result), `history.jsonl` (one line per finished build; delete it to forget every estimate),
  `memory.log` (the memory watch, kept to about 1 MiB plus one older file), and
  `config.last-good.toml`. Everything is private to the user.
- **A broken configuration** is reported in the journal; the daemon keeps the last good one.
- **Before a reboot:** `buildq pause "rebooting at noon"`, wait for the running builds to finish
  (`buildq status`), reboot, then `buildq resume`. The pause is kept across restarts, and queued
  builds wait with that reason.
- **`buildq report`** sums up the history per command (or `--by lane`, `--by caller`): runs,
  failures, median time and queue wait, peak memory, runs that went over their `MemoryHigh`, time
  spent waiting on memory, and kills. That is the evidence for a lane's caps: a command whose runs
  keep going over `MemoryHigh` with long memory waits is held back by it. The history keeps each
  command's last 100 runs and its biggest run of each day, so older numbers are a sample.
- **GitHub.** With `github_status = true`, a check (as above) or a build given a `--name` that
  passes on a commit with no changes on top, its tree the same when it started and ended, posts a
  `success` status on that commit (context `buildq/<name or command>`, so a name is visible on the
  repository) through `gh`, so its pull request shows it was verified here. A later failure of the
  same check on the same commit (its own verdict, an exit of 1 to 127; not a kill, a timeout or a
  stop to free memory) posts `failure`; other failures aren't posted. Results are ordered by when
  their builds ended, so an older one never replaces a newer one. A commit GitHub
  doesn't have yet is tried again after the next `git push` run through buildq from that
  repository, and every ten minutes, for a day; the first refusal is logged with `gh`'s message.
  Statuses go to the `origin` remote's repository (in a fork, the fork). The daemon looks for `gh`
  on its `PATH`, then in `~/.local/bin` and `~/bin`, since a systemd user service's `PATH` often
  has neither.

## CI jobs

The shims and the Rust guard let GitHub Actions jobs through, so CI runs beside the queue: counted
as machine load, but never waiting its turn. On a self-hosted runner on the same machine, running
as the same user, a workflow step can queue its heavy command instead:

```yaml
env:
  BUILDQ_FALLBACK: run          # if the daemon is down, run the step unqueued rather than wait
steps:
  - run: |
      q=; command -v buildq >/dev/null && q="buildq run --name ci:${{ github.job }} --"
      $q cargo test --locked
```

On a runner without buildq (GitHub-hosted, or another machine), `q` stays empty and the command
runs as before. While the queue is paused (`buildq pause`), such a step waits like any other build,
up to the job's timeout. Cancelling the job cancels the build. CI jobs then share turns fairly with
everything else (each runner is a caller of its own); a `ci` lane can give them a priority or caps
of their own (see [config.example.toml](config.example.toml)).

## Limitations

- **One machine and one user.** The socket is private to the user who runs the daemon. Several users
  each need their own daemon, and those daemons don't coordinate, though each sees the others'
  load in the machine-wide numbers.
- **Estimates, not guarantees.** A command that suddenly needs far more than before is planned by
  its history. Per-build `MemoryMax` and the slice backstop are what keep that from hurting the
  machine.
- **Sampling.** Memory is sampled once a second from the build's cgroup (anonymous memory, shared
  memory and unreclaimable kernel memory; page cache is left out because the kernel can take it
  back). A spike shorter than a second can be missed.
- **Work outside the build's cgroup.** Anything a build hands to a daemon runs in that daemon's
  cgroup: a test database in Docker, and above all `docker build`, whose compile runs in dockerd.
  It counts as machine load but not in the build's history, so give such commands a lane whose
  `min_mem` is what they really need, and route them there with a lane rule. (That is why the
  shims leave `docker` alone.)
- **Fair shares count builds, not their size.** A caller with one 16-core build running goes before
  one with two 1-core builds. Priorities still come first.
- **Reuse trusts the source, as git sees it.** A check that passed is taken as passing again on
  the same source, environment and settings. The fingerprint doesn't see what lies outside the
  repository (a path dependency in `../shared`, `~/.cargo/config.toml`, installed toolchains) or
  ignored files other than `.env*`, and a test can depend on other things (a database's contents,
  the network, the time). `--again` runs it; `reuse_window = "0"` turns reuse off.
- **A daemon restart forgets running builds' environments,** so a build stopped to free memory
  after one fails instead of being queued again.

## Alternatives

Before writing buildq we looked for something that already did this. None did, but several had
ideas buildq has taken since (see [Credits](#credits)):

- **[qex](https://github.com/stephenc/qex)** is the closest: a local job queue for coding agents
  that learns per-command sizes. It admits jobs by their claimed cores and memory against a budget
  and checks free memory before each start. It doesn't measure CPU load it didn't start, doesn't
  cap a job's memory, and doesn't size a job's parallelism.
- **Slurm** (single node) allocates the CPUs and memory a job *requests*, so a job that requests
  eight cores and uses one still holds eight, and it doesn't see load it didn't schedule. It needs
  root and several daemons.
- **pueue**, **task-spooler** and **GNU parallel's `sem`** run up to a fixed number of jobs at once:
  the slot model buildq replaces.
- **`batch`** (atd) starts queued jobs when the load average drops, but runs them detached, with
  output by mail.
- **systemd on its own** gives cgroup limits and weights, but no queue.

## Credits

buildq takes ideas, and no code, from these projects. Thank you to their authors.

- **[qex](https://github.com/stephenc/qex):**
  - A repeat submission joins the job already running. qex's `--dedupe-key` asks the caller for a key; buildq derives one from the source being built.
  - `wait` ends a failed job with its last lines of output.
  - The agents' page is built into the program (`qex help agents`; here, `buildq help agents`).
  - Agents get a skill on using the queue.
- **[GNU parallel](https://www.gnu.org/software/parallel/):**
  - With [`--memsuspend`](https://www.gnu.org/software/parallel/parallel.html), jobs are suspended when memory runs low.
  - With [`--memfree`](https://www.gnu.org/software/parallel/parallel.html), the youngest job is killed and put back on the queue.
  - buildq's memory shedding pauses its newest build first, then stops it and queues it again.
- **[Slurm](https://slurm.schedmd.com/):**
  - [Preemption that requeues](https://slurm.schedmd.com/preempt.html) a stopped job instead of failing it.
  - [Fair-share](https://slurm.schedmd.com/classic_fair_share.html) priority, here between sessions rather than users.
  - [`sacct`](https://slurm.schedmd.com/sacct.html)-style accounting, which became `buildq report`.
- **[pueue](https://github.com/Nukesor/pueue):** a paused queue starts no new tasks while the running ones carry on, as `buildq pause` and `buildq resume` do.
- **[systemd](https://systemd.io/):** buildq is built on systemd's features:
  - transient services and slices;
  - socket activation;
  - `MemoryHigh` and `MemoryMax`;
  - `CPUWeight` and `IOWeight`;
  - the freezer (`systemctl freeze`) that pauses a build.

## Develop

```sh
python3 -m unittest discover -s tests -v                    # unit and end-to-end tests
BUILDQ_TEST_SCOPES=1 python3 -m unittest discover -s tests  # also with builds as systemd services
```

The end-to-end tests start a private daemon on a temporary socket and never touch an installed one.
[docs/design.md](docs/design.md) describes the internals.

## License

[MIT](LICENSE)
