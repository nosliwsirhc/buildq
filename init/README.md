# Running buildq without systemd

buildq works on any Linux distribution with Python 3.11 or newer — Alpine, Void, Gentoo, Artix,
Devuan, Slackware, WSL2, and Docker/Podman containers — with **no systemd and no root required**.

## Quick start (zero setup)

Run the installer — it uses plain `/bin/sh` (even on minimal Alpine without `bash`), detects your
distro and init system, checks for Python 3.11+ and `flock`, and offers to install anything missing
via your package manager (`apk`, `apt`, `dnf`, `pacman`, `xbps-install`, `zypper`, `emerge`):

```sh
./install.sh --shims --skill
```

Want to check compatibility first without changing anything? Run `./install.sh --check`.
Want it to install missing prerequisites automatically without prompting? Add `--install-deps`.

That's it. The installer detects that systemd isn't running, starts `buildqd` in the background for
your user, puts its socket and logs under `~/.local/state/buildq/`, and verifies it responds.
Because the socket lives in your home state directory rather than a per-login folder, background
services and terminal sessions always talk to the same daemon and builds keep running if you log
out.

Manage the daemon anytime with:

```sh
buildq daemon status                    # check if it's running and which mode it's using
buildq daemon restart                   # restart cleanly (running builds keep going)
buildq daemon reload                    # reload config.toml without restarting
buildq daemon stop                      # stop the daemon
buildq daemon logs --tail 50 --follow   # watch the daemon log
```

> **Containers and packaging:** Pass `--no-start` (`./install.sh --no-start`) when building a
> container image or installing on a machine where a boot-time init service starts `buildqd` for
> you.

## How buildq runs builds on your machine

By default (`runner = "auto"`), buildq automatically uses the strongest mode your system supports:

| Mode | When it's used | What you get | Setup needed |
| --- | --- | --- | --- |
| **`systemd`** | Standard systemd Linux (Ubuntu, Fedora, Arch, Debian) | Kernel memory/CPU/IO caps, kernel pausing, automatic safety ceiling, socket activation | None |
| **`direct`** | Any non-systemd Linux, WSL2, or container | Automatic memory & CPU measurement, learned build sizes, live core resizing, low-RAM pausing, memory-cap watchdog, full child-process cleanup | **None** — works immediately |
| **`cgroup`** | Non-systemd Linux with optional cgroup v2 setup | Same kernel-enforced memory caps, pausing, and dynamic safety ceiling as systemd, without needing systemd | One boot-time script (below) |

Most people on WSL2, containers, or personal non-systemd machines don't need anything beyond the
default `direct` mode — it works out of the box with zero root access.

## Keeping buildq running after a reboot

On a long-running non-systemd machine, pick the ready-made service file for your init system so
`buildqd` starts automatically at boot (and restarts if it ever exits):

- **OpenRC** (Alpine, Gentoo, Artix, Devuan): copy [`openrc/buildqd`](openrc/buildqd) to
  `/etc/init.d/buildqd` and set `BUILDQ_USER` in `/etc/conf.d/buildqd`.
- **runit** (Void, Artix): [`runit/buildqd/run`](runit/buildqd/run) and
  [`runit/buildqd/log/run`](runit/buildqd/log/run) (works as a system service in `/etc/sv/buildqd`
  or a user service in `~/service/buildqd`).
- **s6** (Artix, Alpine): [`s6/buildqd/run`](s6/buildqd/run) and
  [`s6/buildqd/log/run`](s6/buildqd/log/run).
- **dinit** (Chimera, Artix): [`dinit/buildqd`](dinit/buildqd).
- **SysVinit** (Devuan, Slackware): [`sysvinit/buildqd`](sysvinit/buildqd).
- **Simple cron fallback (no root):** add `@reboot $HOME/.local/bin/buildq daemon start` to
  `crontab -e`.

## Optional: kernel-enforced memory limits without systemd

In `direct` mode, buildq checks each build's memory once a second and stops a runaway build if it
passes `MemoryMax`. On a shared non-systemd build server where you want the Linux kernel itself to
enforce hard memory limits instantaneously (just like systemd does), run the included one-time
cgroup helper at boot:

1. Install the helper script:
   ```sh
   sudo install -Dm755 init/cgroup-setup.sh /usr/local/libexec/buildq-cgroup-setup.sh
   ```
2. Either enable `BUILDQ_CGROUP_DELEGATE="yes"` in your init service config (see the comments at
   the top of each template in `init/`), or run it once at boot for your user:
   ```sh
   sudo /usr/local/libexec/buildq-cgroup-setup.sh "$USER"
   ```
3. Start `buildqd` through the init service (which automatically places `buildqd` inside the
   delegated cgroup). Run `buildq daemon status` to confirm it switched to the `cgroup` runner.

If you ever start `buildqd` by hand outside that service, `runner = "auto"` safely falls back to
`direct` mode instead of failing.

<details>
<summary>Technical details: how the cgroup v2 layout works</summary>

`init/cgroup-setup.sh` sets up a two-level cgroup v2 hierarchy following `man 7 cgroups`:

- `/sys/fs/cgroup/buildq-$USER` — **root-owned**, holding the overall memory ceiling (75% soft /
  85% hard RAM by default) so user builds can never raise their own ceiling.
- `/sys/fs/cgroup/buildq-$USER/user` — owned by `$USER`, with two subdirectories: `daemon/` (where
  `buildqd` runs) and `builds/` (where each build gets its own `buildq-<id>` cgroup and
  `slice_guard` dynamically tightens `builds/memory.max` when system memory runs low).

Running `buildqd` inside `user/daemon` satisfies Linux's cgroup v2 common-ancestor delegation rule so
an unprivileged `buildqd` can move each build into `user/builds/buildq-<id>`. To point buildq at a
custom delegated directory instead, set `cgroup_root = "/sys/fs/cgroup/..."` in
`~/.config/buildq/config.toml` or `BUILDQ_CGROUP_ROOT`.

</details>
