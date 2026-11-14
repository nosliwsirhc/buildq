# CI jobs

Queueing GitHub Actions steps on a self-hosted runner, `ci_first`, and a new step's first run.

## Queueing a workflow step

The shims and the Rust guard let GitHub Actions jobs through, so CI runs beside the queue: counted
as machine load, but never waiting its turn. On a self-hosted runner on the same machine, running
as the same user, a workflow step can queue its heavy command instead:

```yaml
steps:
  - run: |
      b=$(command -v buildq || echo "$HOME/.local/bin/buildq")
      if [ -x "$b" ]; then
        exec env BUILDQ_FALLBACK=run "$b" run --name "ci:$GITHUB_JOB" -- cargo test --locked
      fi
      echo "::warning::no build queue (buildq) on this runner; running unqueued"
      exec cargo test --locked
```

- **`exec`** makes buildq the step's process, so cancelling the job reaches it and cancels the build.
- **`BUILDQ_FALLBACK=run`**, set on the command itself, runs the step unqueued if the daemon is down,
  rather than waiting.
- **`~/.local/bin`:** a runner whose service starts `run.sh` directly doesn't read its `.path` file
  (only `bin/runsvc.sh` does), so its jobs get systemd's bare `PATH`. Look there as above, or give the
  runner's unit `Environment=PATH=…`.
- **GitHub statuses:** a build from an Actions job never posts a `buildq/…` success; the job reports
  its own result.

On a runner without buildq (GitHub-hosted, or another machine), the step warns and runs as before.

## Waiting, cancelling and `ci_first`

While the queue is paused (`buildq pause`), a queued step waits like any other build,
up to the job's timeout. Cancelling the job cancels the build. CI jobs share turns fairly with
everything else (each runner is a caller of its own), unless `ci_first = true`: then a CI job's
build gets a head start (not in a lane that may pass held room), with its room held from builds of its priority or lower from the start (see
[Order](scheduling.md#order)). Turn it on when CI steps wait out their jobs' time limits on a
busy machine. A `ci` lane can give them a priority or caps
of their own (see [config.example.toml](../config.example.toml)).

Going first can't free memory that running builds already use, so a CI step that needs a lot
(a lint that peaks at 16 GB, say) can still wait out a long build. With `make_room_for_ci = "5m"`,
a CI job's build that has been queued that long and is waiting for memory that buildq's builds hold
stops the newest of
the builds that can safely start over (a compile or test run, or one submitted `--restartable`), of
its priority or lower and not a CI job's. It stops the one whose memory covers what the step lacks,
or, if no one build does, the newest few that do together, and none if stopping all of them wouldn't
make it fit. Each stopped build is queued again with its place and yields to that CI build: it
doesn't start, whatever its lane, until that build has started (or for 30 minutes at most), and then
goes first. cargo keeps the crates it finished, so it repeats only the ones it was compiling. A
build is stopped this way at most twice, and never for a CI step in a lane with `may_pass_held`,
which holds no room. Pausing a build wouldn't help: a paused build keeps its memory.

Two settings tune it (#38). `make_room_behind_agents = false` leaves a CI step that waits partly
behind agents' builds that have waited longer than it (and so go first) to wait its turn: stopping
builds for it would mostly let those go first. Those builds aren't stopped for a CI step once they
start either. `make_room_spare_after = "20m"` never stops a build
that has run that long, since starting it over would lose the most work. Compare them on your own
builds with the replay simulator ([Benchmarking](benchmark.md)).

## A new step's first run

A step's first run is planned at its lane's default memory, since its command has no history, and a
run cancelled with its job teaches the history nothing. If a new step keeps timing out in the
queue, run it once by hand through `buildq run` (the command alone teaches the queue nothing) so the
history learns its real size. The history is kept per
repository, lane and command (`<repository>|<lane>|<command words>`, where the repository is the
name of its main checkout's folder, `filedrop` for a runner's `_work/filedrop/filedrop`), so run it
from a checkout whose folder has the same name, with the same `--lane` and the same command. The
command words are the tool and what it runs: cargo's subcommand and packages, a JS tool's script, a
script's name. A script listed in `script_labels` and run straight after its interpreter also gets
the argument right after it, when that is a plain lowercase word (letters, `-` and `_`, at most 24;
no digits): `bash persistent-cargo-target.sh architecture`, for a wrapper whose runs differ by that
label. An option before the script (`bash -e wrap.sh …`) turns it off. Listing a script trusts that
argument to be a public label, since it is kept in the history, and each label then starts with no
history of its own: its first run is planned at the lane's default.
