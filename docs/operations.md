# Operating buildq

Logs, the memory watch, restarts, files, the analytics, `buildq top`, configuration errors, reboots, reports and GitHub statuses.

## Logs

`journalctl --user -u buildq` has a line for each build queued, started, resized and
finished, with its estimate, wait, cores, peak memory and time spent waiting on memory.

## When memory is tight

When less than twice `mem_floor` is available, or every task on the machine has
stalled on memory at once 5% of the time, buildq records every 30 seconds what is available, swap
and memory pressure, and the six cgroups holding the most memory, builds or not: Docker
containers, CI runners, editors, agent sessions. It stops once more than 2.5 times `mem_floor` is
free, with memory pressure under 1%, for half a minute. The lines go to the journal at warning priority (`journalctl --user -u
buildq -p warning`) and to `~/.local/state/buildq/memory.log`, which is synced to disk with each
entry: after a reset the journal can be missing its last minutes, and `memory.log` isn't.

## Restarts

Restarts are safe. systemd holds the socket, so clients connecting during a restart wait
rather than fail. Builds run in services of their own and carry on; queued tickets are on disk.
The next daemon picks both up.

## Files

They are in `~/.local/state/buildq`: `tickets/<id>/` (the ticket, its output log, its
result), `history.jsonl` (one line per finished build; delete it to forget every estimate),
`memory.log` (the memory watch, kept to about 1 MiB plus one older file), `metrics.db` (the
analytics, below), and `config.last-good.toml`. Everything is private to the user.

## Analytics

With `metrics = true` (the default), the daemon keeps `metrics.db`, a SQLite file:
- one row a minute, kept 14 days, and one an hour, kept a year: CPU busy and the target, free
  memory and the floor, what is held for running builds to grow into and the room left, swap and
  pressure, builds running and waiting by reason, and the cores and memory of buildq's builds, CI
  runners, containers and the user's apps;
- one row per finished build, kept 180 days: seconds waited by reason, the memory planned for it
  and where that estimate came from, its peak and when it came, what it used after the peak, a
  ten-point memory profile, and cores granted and used.

Rows past their age are deleted once an hour (and the build table is kept to 100,000 rows), and
the space is reused, so the file levels off at a few tens of megabytes. A thread of its own writes it; if it can't, that is logged once and the
queue carries on. `buildq report --capacity` sums it up by day (`--hourly` by hour);
`buildq query "SELECT …"` answers anything else, read-only (`buildq query` alone lists the
tables, and [docs/design.md](design.md#analytics) describes every column).

## `buildq top`

`buildq top` is a live view of the queue, like `btop`, in boxed panels:
- **CPU** (beside MEMORY on a terminal 110 columns or wider; otherwise a bar in the title line):
  the last half hour as a graph, with busy cores against the queue's target and CPU pressure.
  Drawn as btop draws its graphs: braille, two steps to a cell, coloured by row from green at the
  bottom to red at the top. Steps are fixed to the clock and hold the average of their samples, so
  the graph moves half a cell as each step closes. Its first minutes come from the analytics
  (`metrics.db`, each minute's average, as a line between minutes), so it isn't empty when top
  starts; where there is no data at all it is blank.
- **MEMORY, as a ledger:** one bar where every part of the machine's memory has its place: each
  running build's use (`▓`) and what is held for it to grow into (`▒`), everything else (`█`), the
  floor (`░`) and the room a new build can start in (`■`). When more is held than is free, it says
  "over by" instead of the room. Below it:
  - how many builds wait, how many of them for memory and how far short of the free room the closest
    is, and how many are in line (see Waiting);
  - swap and memory pressure;
  - who uses the machine: buildq's builds, CI runners, containers and your apps, each with its
    cores, memory and swap, and I/O pressure;
  - buildq's builds together: their memory pressure and disk reads and writes;
  - one DISK line per filesystem builds write to (`/` always): free space against `disk_floor`,
    worked out for that filesystem, with read and write rates.
- **Running, one row per build:**
  - its cores used out of those given (`▮▯`);
  - its memory now, its peak and its plan; the bar is its plan: `▓` what it uses, `▒` what is held for
    it to grow into, `·` what was let go after its peak;
  - as width allows: the space its folders take, its own memory pressure (how much the kernel is
    slowing it), its disk read and write rates (I/O through dm-crypt or LVM counted once; a
    mirror's writes count each copy, and a filesystem in a loop file counts twice) and its swap;
  - `CI` before the command of a CI job's build.

  The folders are its `CARGO_TARGET_DIR` (for cargo with none, the `target/` beside its
  `Cargo.lock`) and its `TMPDIR`, only those inside your home. They're measured with `ionice -c3
  du -sx` in the background, each at most every five minutes. "≥" means only in part, "…" not
  yet, "?" it couldn't be, and "*" a folder another running build shares.
- **Waiting:** the waiting builds in the order they get a turn, each with a bar of how long it has
  waited against the longest wait, what it needs, and why it waits, in the daemon's words. A build
  waiting for memory says how much the free room lacks ("short"). `⚑ in line` marks a build that
  room is kept for, because it has waited long or is a CI job's build: builds behind it don't take
  that room, and one whose own room is free says how many builds in line hold room that binds it
  ("behind N in line": a CI job's build binds only builds of its priority or lower). When more
  builds wait than fit, the box's bottom border says how many more.
- **LANES** (on a wide terminal): what each lane has running and waiting.
- **The timeline** (`t`): the last half hour, a row per build, newest at the bottom: `·` while it
  waited, a bar while it ran (darker for more memory; green if it succeeded, red if it failed or was
  lost, dim if it was cancelled),
  under a graph of the machine's memory. The running and waiting builds always have a row; when
  there are more than fit, the oldest finished ones go first.
- **Keys:**
  - `s` sorts by age, CPU, memory, I/O or disk;
  - ↑↓ (or `j`/`k`) select a build: the selection stays on that build when the order changes,
    the list scrolls on a small screen, and narrow terminals drop the last columns first, and LANE
    and AGE under 80 columns;
  - Enter shows its plan, pressure, waits and folders;
  - `l` shows the end of its output (with Enter, the newest lines are kept);
  - `t` the timeline and back, `?` what each key and glyph means, `q` quits.
- **Small screens:** the selected build always stays in view. An open detail pane gets up to half
  of what is left, then the machine's panels, then the rest of the builds. Under 18 rows the
  machine's panels give way, and the title line says CPU and memory instead.
- **Colours:** btop's Default theme (from btop's source, Apache-2.0) on a 256-colour terminal:
  readable text in light grey, darker greys only for decoration, panel frames in btop's box colours,
  and the selected row as one dark-red bar with white text. On 8 colours, btop's TTY theme, with
  nothing to read in blue. Built for a dark background, as btop is.
- **Terminals:**
  - it draws with ASCII when the terminal isn't UTF-8 (`LC_ALL=C`), as curses itself writes;
  - `NO_COLOR` turns colour off; every part also has its own glyph, so nothing depends on colour;
  - the "updated" time at the bottom counts from the daemon's last answer, and turns yellow when
    that is more than three refreshes old;
  - a terminal this machine has no terminfo entry for (Ghostty or kitty over SSH, say) is drawn as
    `xterm-256color`, and on exit it says how to install the real entry;
  - with no usable terminal at all (`TERM` unset or `dumb`, or `--once`, or output to a pipe) it
    prints one snapshot.
- **The daemon** is asked on a thread of its own, so keys never wait on it; while it doesn't
  answer, the screen says so. `SIGTERM` and a closed terminal end it cleanly.

`--once` prints one snapshot, measuring folders first for up to 20 seconds. It's also what you
get without a terminal. `top` reads the cgroups and `/proc` itself, so watching costs the daemon
one status request per refresh. Without a UTF-8 locale it draws in ASCII.

## A broken configuration

A broken configuration is reported in the journal; the daemon keeps the last good one.

## Before a reboot

`buildq pause "rebooting at noon"`, wait for the running builds to finish
(`buildq status`), reboot, then `buildq resume`. The pause is kept across restarts, and queued
builds wait with that reason.

## `buildq report`

`buildq report` sums up the history per command (or `--by lane`, `--by caller`): runs,
failures, median time and queue wait, peak memory, runs that went over their `MemoryHigh`, time
spent waiting on memory, and kills. That is the evidence for a lane's caps: a command whose runs
keep going over `MemoryHigh` with long memory waits is held back by it. The history keeps each
command's last 100 runs and its biggest run of each day, so older numbers are a sample.

## GitHub statuses

With `github_status = true`, a check (see [usage](usage.md#the-same-source-the-same-build)) or a build given a `--name` that
passes on a commit with no changes on top, its tree the same when it started and ended, posts a
`success` status on that commit (context `buildq/<name or command>`, so a name is visible on the
repository) through `gh`, so its pull request shows it was verified here. A later failure of the
same check on the same commit (its own verdict, an exit of 1 to 127; not a kill, a timeout or a
stop to free memory) posts `failure`; other failures aren't posted. Results are ordered by when
their builds ended, so an older one never replaces a newer one. A commit GitHub
doesn't have yet is tried again after the next `git push` run through buildq from that
repository, and with exponential backoff (10 minutes up to 2 hours), for up to 6 hours (or a day
for transient errors; permanent 4xx refusals are dropped at once); the first refusal is logged with
`gh`'s message.
Statuses go to the `origin` remote's repository (in a fork, the fork). A build from a GitHub
Actions job never posts `success`, since the job reports its own result. Its failure can still
turn red a `success` posted on the same commit. The daemon runs the `gh` the `gh` setting names
(its full path), else looks on its `PATH`, then in `~/.local/bin` and `~/bin`, since a systemd user
service's `PATH` often has neither. Name it for Homebrew on Linux, `/snap/bin`, `~/go/bin` or a
version manager's install.
