# Using buildq

The commands, what each option does, and how buildq treats the same build submitted twice. [How buildq decides](scheduling.md) covers when a build starts.

## Commands

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
buildq report --capacity [--hourly] [--since 7d]  # what builds waited for, and what sat idle meanwhile
buildq top [--interval 2] [--once]                # a live view of the machine and each build, like btop
buildq query ["SQL"] [--json]                     # a read-only query on the analytics (none: list the tables)
buildq help agents                               # the page for coding agents
buildq ping                                      # check that the daemon answers
```

A ticket is a UUID; any unique prefix of four or more characters works in its place.

| Option (submit, run) | Effect |
| -------------------- | ------ |
| `--lane NAME` | the kind of build; otherwise the [lane rules](scheduling.md#lanes) decide |
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

## Exit codes

`run` and `wait` exit with the build's exit code (128 + N if signal N ended it, 124
if it timed out), and 125 if it ended without one (cancelled before it ran, or lost). The client
itself exits 2 for a refused request, 75 when the daemon can't be reached (`submit`, or
`BUILDQ_FALLBACK=fail`), and 124 when `wait --timeout` runs out while the build goes on.

## The same source, the same build

When a build is submitted in a git worktree, the client
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

## `run` and `submit`

A build started by `buildq run` belongs
to that command: interrupting it (Ctrl-C, SIGTERM, SIGHUP) cancels the build, and `run` then ends
by the same signal, so a calling script stops as it would have. If `run` is killed outright
(SIGKILL, as editors and tool timeouts do), the build is cancelled once nobody has followed it for
`unwatched_seconds` (30 s), so abandoned builds don't hold turns. A build dropped off with `buildq
submit` belongs to its ticket and runs to the end whoever is watching.

## Being told when a build finishes

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

## Environment variables

| Variable | Effect |
| -------- | ------ |
| `BUILDQ_SOCKET` | the daemon's socket (default `$XDG_RUNTIME_DIR/buildq.sock`) |
| `BUILDQ_FALLBACK` | what `buildq run` does when the daemon can't be reached. Unset: wait for it. `fail`: exit 75. `run`: run the command unqueued. Anything else: a command prefix to run it through instead. |
| `BUILDQ_AS` | the name of a wrapper in front of buildq. buildq's hints then name the wrapper's commands, so its users are told what they type (`wrapper --submit <command>`, not `buildq submit -- <command>`). The wrapper must take `--submit COMMAND...`, `--wait TICKET [--timeout D]`, `--status [TICKET]`, `--logs TICKET` and `--cancel TICKET` and pass them to buildq's commands of the same names; that is all the hints use. |
| `BUILDQ_SOURCE` | `off` stops the client recording the source a build is of (no joining or reuse then) |
| `BUILDQ_CALLER` | who is submitting, for fair shares; by default the agent session (`CLAUDE_CODE_SESSION_ID`, `CODEX_THREAD_ID`, `T3CODE_THREAD_ID`), else the cgroup the client runs in (a CI runner's service, a terminal's scope) |
| `BUILDQ_JOB`, `BUILDQ_TICKET` | set inside every build to its ticket. A `buildq run` inside a build runs its command directly, because the build already holds a turn. buildq checks that the build really is running. |
| `BUILDQ_CORES` | set inside every build: the cores it was given |
