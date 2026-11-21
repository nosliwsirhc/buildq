# Running buildq without systemd

buildq runs on any Linux distribution with Python 3.11+ — no systemd required.

## How it chooses a runner (`runner = "auto"`)

| Priority | Runner | What you get | Requirements |
| --- | --- | --- | --- |
| 1 | `systemd` | Transient user services, kernel cgroup v2 limits (`MemoryHigh`/`MemoryMax`/`CPUWeight`/`IOWeight`), `cgroup.freeze`, `cgroup.kill`, `slice_guard`, socket activation, per-slice `systemd-oomd` drop-in | systemd user session |
| 2 | `cgroup` | Pure Linux cgroup v2 per-build isolation, kernel `memory.high`/`memory.max`/`cpu.weight`/`io.weight`/`pids.max`/`cpu.max`, `cgroup.freeze`, `cgroup.kill`, `slice_guard` (no socket activation or systemd-oomd needed) | Writable delegated cgroup v2 directory (e.g. `init/cgroup-setup.sh` or `cgroup_root` in `config.toml`) |
| 3 | `direct` | Pure Linux `/proc` measurement (`smaps_rollup` PSS + `PR_SET_CHILD_SUBREAPER` process-tree CPU), learned history, live jobserver rebalancing, `SIGSTOP`/`SIGCONT` memory-shedding pauses, 1s daemon `MemoryMax` watchdog, `nice` priority, full process-tree cleanup | Plain Linux kernel; zero root, zero systemd, zero cgroup delegation |

## Quick start (any non-systemd Linux, WSL2, or container)

`install.sh` requires `bash` and Python 3.11+ (on minimal Alpine Linux, `apk add bash python3`).

```sh
./install.sh --shims --skill
```

Without systemd, `install.sh` starts `buildqd` as a detached user daemon (`buildq daemon restart`)
listening on the session-independent socket `~/.local/state/buildq/run/buildq.sock` (so boot-time
init services and interactive login shells, including elogind / `pam_rundir` sessions, always agree
on the same socket path and never lose it on logout), writes logs to
`~/.local/state/buildq/buildqd.log` (rotated at 4 MiB), and sets up `--shims` and `--rustc-guard`
as usual.

Manage the daemon anytime with:

```sh
buildq daemon status
buildq daemon restart
buildq daemon reload
buildq daemon stop
buildq daemon logs --tail 50 --follow
```

For container image builds (`Dockerfile` / `Containerfile`), packaging, or hosts where a boot-time
init supervisor (OpenRC, runit, s6, dinit, SysVinit) manages `buildqd`, pass `--no-start` to
install or upgrade binaries without spawning a separate background daemon.

## Optional: enabling kernel cgroup v2 isolation (`cgroup` runner)

On OpenRC, runit, s6, dinit, or sysvinit hosts with Linux cgroup v2 mounted at `/sys/fs/cgroup`,
install `init/cgroup-setup.sh` (`install -Dm755 init/cgroup-setup.sh /usr/local/libexec/buildq-cgroup-setup.sh`)
and delegate a user-owned subtree once at boot:

```sh
sudo /usr/local/libexec/buildq-cgroup-setup.sh "$USER"
```

This creates a two-level hierarchy following `man 7 cgroups`:
- `/sys/fs/cgroup/buildq-$USER` — **root-owned**, holding the aggregate host memory ceiling (75%/85% RAM by default) so user workloads cannot raise their own ceiling;
- `/sys/fs/cgroup/buildq-$USER/user` — delegated to `$USER`, containing `daemon/` (for `buildqd`) and `builds/` (where `slice_guard` dynamically tightens `builds/memory.max` under load and each build runs in `builds/buildq-<id>`).

Starting `buildqd` inside `buildq-$USER/user/daemon` — which the init templates below do
automatically when `BUILDQ_CGROUP_DELEGATE="yes"` (or `run-in-cgroup = /buildq-<user>/user/daemon`
in dinit) is set — satisfies Linux's cgroup v2 common-ancestor delegation rule so unprivileged
`buildqd` can migrate each build into `builds/buildq-<id>`. If `buildqd` is started manually outside
that subtree, `runner = "auto"` cleanly falls back to `direct`. To point at a different delegated
path, set `cgroup_root = "/sys/fs/cgroup/..."` in `~/.config/buildq/config.toml` or
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
