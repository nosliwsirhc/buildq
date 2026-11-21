# Running buildq without systemd

buildq runs on any Linux distribution with Python 3.11+ — no systemd required.

## How it chooses a runner (`runner = "auto"`)

| Priority | Runner | What you get | Requirements |
| --- | --- | --- | --- |
| 1 | `systemd` | Transient user services, kernel cgroup v2 limits (`MemoryHigh`/`MemoryMax`/`CPUWeight`/`IOWeight`), `cgroup.freeze`, `cgroup.kill`, `slice_guard`, socket activation | systemd user session |
| 2 | `cgroup` | Pure Linux cgroup v2 per-build isolation, kernel `memory.high`/`memory.max`/`cpu.weight`/`io.weight`/`pids.max`, `cgroup.freeze`, `cgroup.kill`, `slice_guard` — **full feature parity without systemd** | Writable delegated cgroup v2 directory (e.g. `init/cgroup-setup.sh` or `cgroup_root` in `config.toml`) |
| 3 | `direct` | Pure Linux `/proc` measurement (`smaps_rollup` PSS + `PR_SET_CHILD_SUBREAPER` process-tree CPU), learned history, live jobserver rebalancing, `SIGSTOP`/`SIGCONT` memory-shedding pauses, 1s daemon `MemoryMax` watchdog, `nice` priority, full process-tree cleanup | Plain Linux kernel; zero root, zero systemd, zero cgroup delegation |

## Quick start (any non-systemd Linux, WSL2, or container)

```sh
./install.sh --shims --skill
```

Without systemd, `install.sh` starts `buildqd` as a detached user daemon (`buildq daemon restart`),
writes logs to `~/.local/state/buildq/buildqd.log` (rotated at 4 MiB), and sets up `--shims` and
`--rustc-guard` as usual.

Manage the daemon anytime with:

```sh
buildq daemon status
buildq daemon restart
buildq daemon reload
buildq daemon stop
buildq daemon logs --tail 50 --follow
```

For container image builds (`Dockerfile` / `Containerfile`) or packaging, pass `--no-start` to
install binaries, shims, and the skill without launching a background process during the build.

## Optional: enabling kernel cgroup v2 isolation (`cgroup` runner)

On OpenRC, runit, s6, dinit, or sysvinit hosts with Linux cgroup v2 mounted at `/sys/fs/cgroup`,
install `init/cgroup-setup.sh` (e.g. `install -Dm755 init/cgroup-setup.sh /usr/local/libexec/buildq-cgroup-setup.sh`)
and delegate a user-owned subtree once at boot:

```sh
sudo /usr/local/libexec/buildq-cgroup-setup.sh "$USER"
```

This creates `/sys/fs/cgroup/buildq-$USER` with `daemon/` (for `buildqd` itself) and `builds/`
(capped at 75%/85% RAM with `cpu`/`memory`/`io`/`pids` delegated). Starting `buildqd` inside
`buildq-$USER/daemon` — which the init templates in this directory do automatically when
`BUILDQ_CGROUP_DELEGATE="yes"` (or `run-in-cgroup = /buildq-<user>/daemon` in dinit) is set —
satisfies Linux's cgroup v2 common-ancestor delegation rule so unprivileged `buildqd` can place
each build in its own child cgroup under `builds/buildq-<id>`. If `buildqd` is started manually
outside that subtree, `runner = "auto"` cleanly falls back to `direct`. To point at a different
delegated path, set `cgroup_root = "/sys/fs/cgroup/..."` in `~/.config/buildq/config.toml` or
`BUILDQ_CGROUP_ROOT`.

## Boot-time service templates

- **OpenRC** (Alpine, Gentoo, Artix, Devuan): [`openrc/buildqd`](openrc/buildqd) — set
  `BUILDQ_USER` (and optionally `BUILDQ_CGROUP_DELEGATE="yes"`) in `/etc/conf.d/buildqd`.
- **runit** (Void, Artix): [`runit/buildqd/run`](runit/buildqd/run) and
  [`runit/buildqd/log/run`](runit/buildqd/log/run) — supports both system `/etc/sv/buildqd` (with
  `conf`) and user `~/service/buildqd`.
- **s6** (Artix, Alpine): [`s6/buildqd/run`](s6/buildqd/run) and
  [`s6/buildqd/log/run`](s6/buildqd/log/run).
- **dinit** (Chimera, Artix): [`dinit/buildqd`](dinit/buildqd).
- **SysVinit** (Devuan, Slackware): [`sysvinit/buildqd`](sysvinit/buildqd).
- **Cron fallback**: `@reboot $HOME/.local/bin/buildq daemon start`.
