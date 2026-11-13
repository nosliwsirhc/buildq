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
buildq cancel TICKET...                          # take it out of the queue, or stop it
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

A build runs with the environment, working directory, umask and resource limits (open files,
stack, core size) of the command that submitted it, and no stdin or terminal. `buildq run` streams its
stdout to stdout and its stderr to stderr, so `buildq run -- cargo build --message-format=json | jq`
works; `buildq logs` shows both together.

**Exit codes.** `run` and `wait` exit with the build's exit code (128 + N if signal N ended it, 124
if it timed out), and 125 if it ended without one (cancelled before it ran, or lost). The client
itself exits 2 for a refused request, 75 when the daemon can't be reached (`submit`, or
`BUILDQ_FALLBACK=fail`), and 124 when `wait --timeout` runs out while the build goes on.

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

### Environment

| Variable | Effect |
| -------- | ------ |
| `BUILDQ_SOCKET` | the daemon's socket (default `$XDG_RUNTIME_DIR/buildq.sock`) |
| `BUILDQ_FALLBACK` | what `buildq run` does when the daemon can't be reached. Unset: wait for it. `fail`: exit 75. `run`: run the command unqueued. Anything else: a command prefix to run it through instead. |
| `BUILDQ_AS` | the name of a wrapper in front of buildq. buildq's hints then name the wrapper's commands, so its users are told what they type (`wrapper --submit <command>`, not `buildq submit -- <command>`). The wrapper must take `--submit COMMAND...`, `--wait TICKET [--timeout D]`, `--status [TICKET]`, `--logs TICKET` and `--cancel TICKET` and pass them to buildq's commands of the same names; that is all the hints use. |
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

**Order.** A build that has waited `starve_seconds` goes first, and the room it needs is held for
it, so later builds can't keep passing it (lanes with `may_pass_held` still may). After that, the
higher priority first (the lane's plus the ticket's own); within a priority, the build expected to
finish soonest (the median time of its command's runs); then the oldest. A build that doesn't fit
doesn't block those behind it.

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
properties = { MemoryHigh = "24G", MemoryMax = "26G" }

[lanes.web]                        # front-end builds: quick, so first, with turns of their own
cores = 4
max_cores = 8
mem = "6G"
priority = 100
extra_jobs = 3
may_pass_held = true
waits_for_cpu = false
exclusive = false
properties = { MemoryHigh = "10G", MemoryMax = "12G" }

[[lane_rules]]                     # the command picks the lane when its caller doesn't
match = "^(pnpm|npm|npx|yarn|vite|vitest|tsc|eslint|node) "
lane = "web"
```

[config.example.toml](config.example.toml) lists every key. `properties` are passed to
`systemd-run --property`, so any service property works (`CPUWeight`, `IOWeight`, `TasksMax`,
...), and systemd checks them when the configuration loads. Past `MemoryHigh` the kernel slows a
build and reclaims its memory; past `MemoryMax` it stops that build alone. `buildq status <ticket>`
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
- **Fairness between callers** comes from priorities, quickest-first and one build per workspace,
  not from shares per caller.

## Alternatives

Before writing buildq we looked for something that already did this:

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

## Develop

```sh
python3 -m unittest discover -s tests -v                    # unit and end-to-end tests
BUILDQ_TEST_SCOPES=1 python3 -m unittest discover -s tests  # also with builds as systemd services
```

The end-to-end tests start a private daemon on a temporary socket and never touch an installed one.
[docs/design.md](docs/design.md) describes the internals.

## License

[MIT](LICENSE)
