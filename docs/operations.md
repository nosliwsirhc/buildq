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

`buildq top` is a live view of the queue, like `btop`:
- **The machine, at the top:**
  - CPU busy against the queue's target;
  - memory used and free, the floor, what is held for running builds to grow into, and the room left;
  - swap;
  - one DISK line per filesystem builds write to (`/` always): free space against `disk_floor`,
    worked out for that filesystem, with read and write rates;
  - CPU, memory and disk pressure ("-" where the kernel doesn't report it).
- **Who uses it:** buildq's builds together, CI runners, containers and your apps, each with its cores, memory and swap.
- **One row per running build:**
  - its CPU used out of the cores given;
  - its memory now, its peak and its plan;
  - its swap;
  - its own memory pressure (how much the kernel is slowing it);
  - its disk read and write rates (I/O through dm-crypt or LVM counted once; a mirror's writes
    count each copy, and a filesystem in a loop file counts twice);
  - the space its folders take: its `CARGO_TARGET_DIR` (for cargo with none, the `target/`
    beside its `Cargo.lock`) and its `TMPDIR`, only those inside your home. They're measured with
    `ionice -c3 du -sx` in the background, each at most every five minutes. "≥" means only in
    part, "…" not yet, "?" it couldn't be, and "*" a folder another running build shares.
- **Below the builds:** the waiting builds and why each waits.
- **Keys:**
  - `s` sorts by age, CPU, memory, I/O or disk;
  - ↑↓ select a build (the selection stays on that build when the order changes; the list
    scrolls on a small screen; narrow terminals drop the last columns first, and LANE and AGE
    under 80 columns);
  - Enter shows its plan, pressure, waits and folders;
  - `l` shows the end of its output;
  - `q` quits.
- **Terminals:**
  - it draws with ASCII when the terminal isn't UTF-8 (`LC_ALL=C`), as curses itself writes;
  - `NO_COLOR` turns colour off;
  - a terminal this machine has no terminfo entry for (Ghostty or kitty over SSH, say) is drawn as
    `xterm-256color`, and on exit it says how to install the real entry;
  - with no usable terminal at all (or `--once`, or output to a pipe) it prints one snapshot.
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
repository, and every ten minutes, for a day; the first refusal is logged with `gh`'s message.
Statuses go to the `origin` remote's repository (in a fork, the fork). A build from a GitHub
Actions job never posts `success`, since the job reports its own result. Its failure can still
turn red a `success` posted on the same commit. The daemon runs the `gh` the `gh` setting names
(its full path), else looks on its `PATH`, then in `~/.local/bin` and `~/bin`, since a systemd user
service's `PATH` often has neither. Name it for Homebrew on Linux, `/snap/bin`, `~/go/bin` or a
version manager's install.
