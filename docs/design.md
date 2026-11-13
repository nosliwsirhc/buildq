# Design

How buildq works inside. The README covers installing and using it.

## The pieces

| Piece | What it does |
| ----- | ------------ |
| `buildq` | The client: submits builds, follows them, shows status. It never runs a build itself (except in a fallback). |
| `buildqd` | The daemon: holds the queue, measures the machine and the builds, decides who starts and with how many cores, starts builds, resizes their jobservers, keeps history. |
| `buildqd --supervise DIR` | The supervisor: one per build, inside its service. Runs the command, writes its output and exit status. |
| `buildq-shim` | Optional: stands in for `cargo`, `pnpm`, ... on `PATH` and sends heavy invocations through `buildq run`. |
| `buildq-rustc-guard` | Optional: cargo's rustc wrapper; refuses compiles outside a buildq build. |
| `skills/buildq/SKILL.md` | Optional: a skill for coding agents on using the queue, installed with `install.sh --skill`, with the machine's own notes (`~/.config/buildq/agents.md`) added at its end. |
| `buildq.socket` | Holds the Unix socket (`$XDG_RUNTIME_DIR/buildq.sock`, mode 0600) so it stays open while the daemon restarts. |
| `buildq.service` | Runs the daemon. `KillMode=process`: stopping the daemon stops nothing else. |
| `buildq.slice` | The parent of every build's service: one CPU and IO weight for all builds, and a memory backstop. |

## A ticket's life

```
queued ──► starting ──► running ──► succeeded | failed
   │                       │
   └──► cancelled ◄────────┘              (lost: it vanished without an exit status)
```

1. **Submit.** The client sends the command, its working directory, its whole environment, and
   any options. The daemon picks the lane (named, or by the lane rules), creates
   `tickets/<uuid>/` with `ticket.json`, `env.json` (mode 0600) and an empty `output.log`, and
   answers with the ticket.
2. **Queue.** Every second, the daemon refreshes each queued ticket's estimate (history and
   configuration may have changed), puts the queue in order, and starts every ticket that fits.
3. **Start.** The daemon fixes the build's cores (and the memory they need), creates its jobserver
   if it gets one, writes the parallelism settings to `parallel.json`, saves the ticket with the
   name its service will have, and runs
   `systemd-run --user --unit=buildq-<uuid>.service --slice=buildq.slice --property=... -- buildqd --supervise DIR`.
   The ticket is `starting` until `systemd-run` returns (killed after 60 s if it hangs), then
   `running`.
4. **Supervise.** The supervisor claims `env.json` by renaming it (so a second supervisor for the
   same build, from a late launch after a restart, finds nothing and runs nothing), records its own
   pid, deletes the environment once read (it can hold secrets), adds `parallel.json`'s settings that
   the caller didn't set, applies the caller's soft limits, and holds the build's jobserver open for
   the build's whole life. It runs the command in a process group of its own with stdin closed and
   stdout and stderr on two pipes, copying them to `stdout.log`, `stderr.log` and the combined
   `output.log`, each capped (keeping the end); it reads in bounded batches so a flood of output can't
   hold off the timeout. It forwards signals to the group and enforces the timeout (SIGTERM, then
   SIGKILL 30 s later, exit 124). When the command exits, it stops whatever is left in its group, and
   anything else whose environment carries this build's `BUILDQ_JOB` (processes that left the
   group), records `oom_kill` from its own cgroup, and writes `result.json`, whatever went wrong.
5. **Finish.** The daemon sees `result.json` and ends the turn once the build's service is empty;
   past 15 s it kills what is left, and keeps the turn until that is gone. It records the run in
   history, asks systemd to unload the service, saves the final state, tells anyone watching, and
   runs the finish hooks. A build whose service vanished without a `result.json` was killed as a
   whole; systemd's `Result=` for it tells an out-of-memory kill (the kernel or systemd-oomd) from
   others.

A build started by `buildq run` is *attached*: once no client has followed it for
`unwatched_seconds`, it is cancelled. Builds from `buildq submit` are not.

### Protocol

Newline-delimited JSON over the socket. Requests carry `op`:

| op | Answer |
| -- | ------ |
| `submit` (`argv`, `cwd`, `env`, `lane`, `cores`, `mem`, `priority`, `timeout`, `name`, `notify`, `watch`, `attach`, `submission`, `rlimits`, `umask`) | `{"type":"ticket", ...}`; with `watch`, later updates on the same connection. A `submission` id the daemon has seen returns that ticket again. |
| `watch` (`ticket`) | the ticket now, then `{"type":"wait", ...}` while it waits (when the reason changes, and every 60 s) and `{"type":"ticket", ...}` whenever its state changes, until it finishes |
| `status` (`tickets`: optional) | given tickets' rows, or an overview of the machine, the budget and every active ticket |
| `list` (`all`) | every ticket, newest first; without `all`, those that finished in the last day |
| `cancel` (`ticket`) | the ticket, cancelled or stopping |
| `ping` | `{"type":"pong","version":...}` |

The daemon answers only connections from its own user (`SO_PEERCRED`), and the client checks the
same of the daemon.

## Admission

Each pass computes:

```
growth     = Σ running builds of max(0, expected_mem − mem_now)
ramping    = Σ builds started < ramp_seconds ago of max(0, granted − cores_now)
shrinkable = Σ running resizable builds of (granted − min_cores)
free_mem   = MemAvailable − growth − mem_floor
free_cpu   = cpu_target × cores − busy − ramping
```

and walks the queue in order, starting each ticket for which, in this order:

1. its working directory still exists (otherwise it fails at once, without a turn);
2. its expected memory is at most the machine's memory less `mem_floor` (otherwise it waits with
   "too big", holding nothing);
3. its filesystem has `disk_floor` free;
4. in an exclusive lane, no build of the same lane runs in its workspace;
5. memory pressure is at most `mem_psi_block`;
6. it has a turn: its lane has a free `extra_jobs` turn, or fewer than `max_jobs` builds hold
   ordinary turns;
7. its expected memory ≤ `free_mem`, less any room held for starving builds;
8. no build runs, or CPU pressure is at most `cpu_psi_block`;
9. no build runs, or its minimum cores ≤ `free_cpu + shrinkable`, less any held room.

Tests 8 and 9 don't apply to lanes with `waits_for_cpu = false`: such a build starts on its `need`
cores (its lane's `min_cores`, or for a build that ignores the job count what it uses, which is
then not capped at the CPU target either), more if they are free, however busy the CPU is. It
passes the cores held for starving builds, and when it starves itself it holds memory but no cores.
Its cores come off `free_cpu` like any other's, so nothing that does wait for cores starts behind
it, and the next resize takes the overshoot back from resizable builds (it cuts whoever is furthest
above their minimum). CPU is the resource that can be overcommitted safely: too much of it slows
builds down, where too much memory stops the machine.

When a ticket starts, its memory and cores come off the budget for the rest of the pass. The first
test that fails is the reason its client sees.

**Growth, not just usage.** `MemAvailable` already reflects what each running build holds now. The
danger is what it will grow into: a Rust build can spend a minute at 2 GB and then reach 20 GB in
its last crate. So each running build's expected peak, less what it holds, is set aside until it
ends.

**Ramp.** A build takes a few seconds to fill its cores, and busy cores are a ten-second average.
Without the ramp term, a burst of submissions would all see the same idle machine.

**The lone build.** Tests 8 and 9 are skipped when nothing runs, so outside load can slow the queue
but not stop it. Memory has no such exception.

**Holding room.** Once a ticket has waited `starve_seconds`, its memory and minimum cores are
subtracted for every ticket behind it, except tickets in lanes with `may_pass_held`. Room is never
held for a ticket that couldn't fit even with every buildq build gone, so an oversized ticket can't
stop the queue.

**Order.** A ticket that has waited `starve_seconds` goes first (unless its lane may pass held
room). Then tickets go by priority (lane plus ticket, highest first), then by expected duration (the
median wall time of the command's successful runs, or of all its runs if none succeeded; shortest
first, unknown last), then by submission.

**Memory follows cores.** A build's memory estimate was measured at some number of cores. Given more,
it is assumed to need proportionally more, up to its lane's enforced `MemoryMax`. The cores a build
starts with are cut until their memory fits, and the rebalancer grows a build only if the memory
for its extra cores is free, sharing what is free among the builds it grows.

**The slice guard.** Every five seconds the daemon sets `buildq.slice`'s `memory.max` to what its
builds hold (`memory.current`) plus what the machine has free above half of `mem_floor`, at least
2 GB above what they hold, and never above the `MemoryMax` the slice is configured with. When other
work on the machine grows, the room for builds shrinks with it, and past it builds are stopped
inside their slice rather than elsewhere. It never writes below what the builds hold, so the kernel
needn't reclaim anything when it is written. When the daemon stops, the configured limit is put
back. It is a poll: other work that grows faster than five seconds allow can still outrun it.

## Cores

At start, a build gets `max(min_cores, min(want, ⌊free_cpu⌋))` cores, or, when nothing else runs,
up to its want of whatever is free. *Want* comes from history:

| What a recent run did | What the next run wants |
| --------------------- | ----------------------- |
| used ≥ 85% of the cores it was given | 1.5 × what it was given |
| used fewer | 1.2 × what it used |
| used, on average over the run, more than 1.2 × what it was given, plus 0.5 | it ignores the job count: it is "rigid", and needs what it used (up to the lane's `max_cores` and the CPU target) |

The largest want among recent runs counts, clamped to the lane's `min_cores` and `max_cores`. A
cold cargo build wants `max_cores`. A rigid build gets no jobserver, and its minimum is its want.

The cores reach the build through its environment (see the README), and, unless the caller brought
its own `MAKEFLAGS`, through a jobserver: a FIFO in the ticket's directory, named in
`CARGO_MAKEFLAGS` (and, for ninja and a `make -j`, in `MAKEFLAGS`, with the command's `-j` removed)
as `--jobserver-auth=fifo:PATH`. It holds `granted − 1` tokens, since every client of a jobserver has
one implicit token. The supervisor holds the FIFO open for the build's whole life, so its tokens
survive a daemon restart.

Every five seconds the daemon rebalances the resizable builds: cargo, make and ninja from the start,
and any other build once one of its processes opens the FIFO or takes a token:

```
spare = cpu_target × cores − max(busy, busy_now) − ramping of other builds − min_cores of the first ticket waiting for cores
```

If `spare ≥ 1` and memory allows, builds that use at least 75% of their cores and want more get
more, the furthest below their want first. If `spare ≤ −1`, builds above their minimum give cores
back, the most above first. The daemon writes tokens into a build's FIFO to grow it, and reads
free ones out to shrink it; tokens in use come back to the FIFO as their processes end, and the
next rebalance takes them then.

## Measurement

**The machine.** `/proc/stat` gives busy cores: all CPU time except idle and iowait, for every
process on the machine, as a ten-second exponential average. `/proc/meminfo` gives
`MemAvailable`, which counts reclaimable page cache as available. `/proc/pressure/*` gives PSI
where the kernel has it.

**A build.** From its service's cgroup, every second: cores from the change in `cpu.stat
usage_usec` over five seconds (the peak counts only windows of three seconds or more), and memory
as `anon + shmem + kernel − slab_reclaimable` from `memory.stat`. `memory.current` and
`memory.peak` also count page cache, which a build fills with its own output. OOM kills come from
`memory.events.local`, which the supervisor reads before its cgroup goes away; its `max` count says
whether the kill came at the build's own `MemoryMax` (it needs more) or from outside (the slice's
limit, systemd-oomd), which doesn't say the build needs more. A build that exits 137 with its peak
near its lane's `MemoryMax` also counts as killed for memory.

**Memory waits.** From the cgroup's `memory.pressure`, the total time some of its tasks, and all of
them, were stalled on memory (reclaim, swap-in, `MemoryHigh` throttling), and from
`memory.events.local` the number of times it went over its own `MemoryHigh`. The daemon reads them
with each sample and the supervisor once more as the build ends, but only from a cgroup of the
build's own (the direct runner has none, so it reports none). They are shown with the ticket,
logged when it finishes, and kept in its history record (`mem_wait_some`, `mem_wait_full`,
`mem_high`). A command whose peak sits at its lane's `MemoryHigh` with long waits is being held
back by that cap. Numbers read back from `result.json` or `ticket.json` count only if they are
finite, non-negative and not absurdly large; anything else is ignored.

**Memory watch.** While `MemAvailable` is under twice `mem_floor`, or memory `full avg10` is 5% or
more, the daemon records every 30 seconds the machine's memory, swap and pressure. Memory `some`
doesn't count: one build slowed at its own `MemoryHigh` keeps it high on a machine with plenty
free. Once more than 2.5 times `mem_floor` is available and `full` is under 1% for 30 seconds, it
says memory is no longer tight. Going in and out of tight in between doesn't log more often.

Nothing slow runs on the daemon's loop. The loop writes the machine's numbers to the journal at
once (at warning priority) and puts them on a queue for `memory.log` in the state directory. A
writer thread appends from that queue and fsyncs each batch, since the journal can lose its last
minutes in a hard reset; it alone rotates the file at about 1 MiB, and each line carries the time
it was recorded. A walker thread then walks `/sys/fs/cgroup`. One walk runs at a time, and a
record that comes in while one runs only asks for the next. A walk reads at most 5,000
directories, and as many again looking for process names, and stops after 20 seconds, saying so.

The walk lists the six cgroups holding the most unreclaimable memory plus swap anywhere on the
machine (swap counts: with zram it is still RAM). It looks only into cgroups whose memory plus
swap is at least 256 MiB. It lists a cgroup whole when none of its children is that large, and
otherwise lists the large children plus the parent's "(the rest)", what its small children hold
together, so many small services don't vanish. Each entry names the largest process found in it,
from a bounded look at up to 500 processes. If a thread can't be started, the journal still gets
the machine's numbers. The daemon stops both threads before it exits. It is evidence, not control:
buildq can't stop work it didn't start, but after a stall the record shows what the memory was
doing up to it.

## History

A finished build adds a line to `history.jsonl` unless it was cancelled, timed out, or ended by a
signal (exit ≥ 128); a build killed at its own memory cap is recorded too. A build too quick to be
sampled is recorded as needing little. The line holds its key, peak memory and the cores it had at
that peak, peak and average cores, the time-weighted cores it was given, wall time, exit status,
and whether it was cold or killed for memory. Each key keeps its last 100 runs and, for 400 days,
each day's biggest run, so a full rebuild isn't forgotten behind a stream of small ones.

The key is `repository|lane|words`. The repository is the name of the git repository holding the
working directory, the same for all its worktrees. The words are what changes the size of the
work:

| Command | Words |
| ------- | ----- |
| `env X=1 nice -n 5 cargo +nightly test --locked -p api --lib -- --nocapture` | `cargo test -p api --lib` |
| `pnpm --filter web run build` | `pnpm --filter web build` |
| `git -C repo push --force-with-lease` | `git push` |
| `bash ./scripts/ci.sh --fast` | `bash ci.sh` |

Wrappers (`env`, `nice`, `timeout`, `sudo`, ...), variable assignments, test filters and everything
after `--` are left out, and any word that could hold a secret (anything with `=`, `@` or `://`)
becomes `…`. History keeps 100 runs per key and `history_keys` keys, the least recently used
dropped first, and compacts itself as it grows.

## Restarts

systemd owns the listening socket, so connections made while the daemon restarts wait in its
backlog. Builds run in their own services and need nothing from the daemon. The next daemon reads
every ticket directory:

- **queued** tickets stay queued, in their order;
- **running** tickets are running again: their jobservers are reopened, and their turns end when
  `result.json` appears or their service is gone;
- **starting** tickets are checked again after ten seconds: running if their service has appeared;
  still waiting while systemd has the service or a job to start it; queued again if systemd has
  neither and `env.json` is still there (no supervisor ever claimed it); lost otherwise. A launch
  that then finds the service already there adopts it.
- tickets keep the runner they started under, whatever the configuration now says, and a cancel
  that was in progress is sent again.

While the daemon stops, nothing new starts: a `stopping` flag stops admission before anything else
is torn down.

## Configuration

The daemon validates every key and range when it loads the file, and asks systemd to accept each
lane's properties (`systemd-run --wait --property=... true`, a transient service, as builds run). A
file that fails is reported in the
journal, and the daemon keeps the configuration in force; at startup it uses
`config.last-good.toml`, the last file that loaded, and only without one the defaults. `SIGHUP`
(`systemctl --user reload buildq`) reloads.

## Flock slots of an older semaphore script

Every second, the daemon reads `/proc/locks`. For each listed lock file whose inode is locked, it
looks for the process of this user whose descriptor for that file carries the lock: the kernel
lists a lock in the `fdinfo` of the descriptor that holds it, and not in a waiter's. Descriptors are
compared with `readlink`, which doesn't touch the file's filesystem. A slot held by such a process
counts as one running build in the entry's lane, measured through the cgroup of the holder's child
if it runs in a scope of its own. The `/proc/locks` pid is no help, since `flock(1)` on an inherited
descriptor exits once it has the lock, and on btrfs its device number differs from `stat()`'s.

## Failure modes

| What happens | Result |
| ------------ | ------ |
| The daemon is down | `run` waits for it, retrying every five seconds and saying so every minute, unless `BUILDQ_FALLBACK` says otherwise. `submit` exits 75. Running builds carry on. |
| The daemon goes away while a client follows a build | The client follows the ticket's files (`result.json`, `output.log`) and reconnects when it can. |
| The daemon restarts | See [Restarts](#restarts). |
| A client is killed | A dropped-off build carries on under its ticket; a `buildq run` build is cancelled once nobody has followed it for `unwatched_seconds`. |
| A build leaves processes behind | The supervisor stops its process group and anything carrying its `BUILDQ_JOB`; systemd stops the rest when the service ends; the turn lasts until the service is empty (killing what is left after 15 s). |
| `systemd-run` hangs | It is killed after 60 s, and systemd is asked whether the service exists. |
| Other work fills the machine | The slice guard shrinks the builds' limit with it; builds are slowed, then stopped inside their slice. The memory watch logs what is holding the memory every 30 seconds. |
| A build runs out of memory at its lane's cap | The kernel kills within that build only (`OOMPolicy=continue` keeps the supervisor alive to report it). The next estimate for that command is at least the cap. |
| All builds together exceed the slice backstop | The kernel slows them, then kills within the slice, not elsewhere. |
| `systemd-run` fails | The ticket fails with systemd's message. |
| The configuration is invalid | The last good one stays in force. |
| A ticket's working directory is deleted while it waits | It fails at its next turn, without taking one. |

## Security

The socket is in the user's runtime directory, mode 0600, and both ends check the peer's uid.
Everything the daemon writes is private to the user (umask 077). A build's environment is kept on
disk only until its supervisor reads it. Status, logs and history show commands with `NAME=value`
arguments and passwords in URLs hidden, and history keys hold no word that could be a secret. The
daemon runs only what its own user submitted, as that user.
