# SPDX-License-Identifier: MIT
"""Build runners (SystemdRunner, CgroupRunner, DirectRunner) and GNU make FIFO jobserver."""

from __future__ import annotations

import fcntl
import os
import pwd
import re
import shutil
import signal
import struct
import subprocess
import termios
import time

from . import common, machine
from .common import DEVNULL, UID
from .config import Lane


class Jobserver:
    """GNU make's jobserver protocol over a named pipe (`--jobserver-auth=fifo:PATH`), which make
    4.4, cargo and ninja 1.13 understand. Each byte in the pipe is a token: a client reads one
    before starting another process and writes it back when that process ends. The daemon puts
    tokens in to grow a build and takes free ones out to shrink it."""

    def __init__(self, path: str, create: bool, out: int = 0):
        self.path = path
        if create:
            os.mkfifo(path, 0o600)
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        self.out = out  # tokens the daemon has put into circulation
        self.used = False

    def free(self) -> int:
        try:
            return struct.unpack("i", fcntl.ioctl(self.fd, termios.FIONREAD, b"\0\0\0\0"))[0]
        except OSError:
            return 0

    def resize(self, tokens: int) -> None:
        tokens = max(0, tokens)
        if tokens > self.out:
            try:
                self.out += os.write(self.fd, b"+" * (tokens - self.out))
            except BlockingIOError:
                pass
        elif tokens < self.out:
            try:
                taken = len(os.read(self.fd, self.out - tokens))
            except BlockingIOError:
                taken = 0
            self.out -= taken  # tokens in use come back later; the next resize takes them then

    def observe(self) -> None:
        if not self.used and self.free() < self.out:
            self.used = True  # something took a token: this build speaks the protocol

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


# --------------------------------------------------------------------------------------------
# Translating lane properties for non-systemd runners


def _pid_max() -> int:
    try:
        with open("/proc/sys/kernel/pid_max") as f:
            return max(1024, int(f.read().strip()))
    except (OSError, ValueError):
        return 32768


def translate_cgroup_properties(lane: Lane, mem_total: int) -> tuple[dict[str, str], dict[str, int]]:
    """Map systemd-style lane.properties onto Linux cgroup v2 control files and process hints.

    Returns `(cgroup_files, proc_hints)` where `cgroup_files` maps relative cgroupfs filenames
    (e.g. `memory.max`, `cpu.weight`, `io.weight`, `pids.max`) to values and `proc_hints` holds
    optional process-level hints (such as `nice`). Raises ValueError on malformed values for
    supported keys; unknown systemd-only properties are ignored so configs port cleanly."""
    files: dict[str, str] = {}
    hints: dict[str, int] = {}
    for key, raw in lane.properties.items():
        val = str(raw).strip()
        if key in ("MemoryHigh", "MemoryMax", "MemorySwapMax"):
            target = {"MemoryHigh": "memory.high", "MemoryMax": "memory.max",
                      "MemorySwapMax": "memory.swap.max"}[key]
            files[target] = "max" if val.lower() in ("infinity", "max") else str(common.parse_size(val, mem_total))
        elif key == "CPUWeight":
            w = common.number(int(val), f"lanes.{lane.name}.properties.CPUWeight", 1, 10000, integer=True)
            files["cpu.weight"] = str(w)
        elif key == "IOWeight":
            w = common.number(int(val), f"lanes.{lane.name}.properties.IOWeight", 1, 10000, integer=True)
            files["io.weight"] = f"default {w}"
        elif key == "TasksMax":
            if val.lower() in ("infinity", "max"):
                files["pids.max"] = "max"
            elif val.endswith("%"):
                share = float(val[:-1])
                if not 0 < share <= 100:
                    raise ValueError(f"TasksMax percentage must be between 0 and 100: {val!r}")
                files["pids.max"] = str(max(1, int(share / 100.0 * _pid_max())))
            else:
                n = common.number(int(val), f"lanes.{lane.name}.properties.TasksMax", 1, 10_000_000, integer=True)
                files["pids.max"] = str(n)
        elif key == "CPUQuota":
            if not val.endswith("%"):
                raise ValueError(f"CPUQuota must be a percentage (e.g. '200%'): {val!r}")
            pct = float(val[:-1])
            if pct <= 0:
                raise ValueError(f"CPUQuota must be positive: {val!r}")
            period = 100_000
            files["cpu.max"] = f"{max(1000, int(pct / 100.0 * period))} {period}"
        elif key == "Nice":
            n = common.number(int(val), f"lanes.{lane.name}.properties.Nice", -20, 19, integer=True)
            hints["nice"] = n
    return files, hints


def lane_nice(lane: Lane) -> int | None:
    """Optional positive nice value for non-cgroup runners from Nice or CPUWeight < 100."""
    if "Nice" in lane.properties:
        try:
            n = int(str(lane.properties["Nice"]).strip())
            return max(0, min(19, n)) if n > 0 else None
        except ValueError:
            return None
    if "CPUWeight" in lane.properties:
        try:
            w = int(str(lane.properties["CPUWeight"]).strip())
            if 1 <= w < 100:
                # Map weight 1..99 onto nice 1..19 smoothly (lower weight -> nicer).
                return max(1, min(19, round((100 - w) / 99 * 19)))
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------------------------
# Running builds: systemd user services, direct cgroup v2, or plain Linux processes


class SystemdRunner:
    """Each build is buildq-<ticket>.service in the configured slice, with its lane's properties."""

    name = "systemd"
    can_freeze = True
    has_cgroup = True
    enforces_mem_cap = True

    def __init__(self, cg_base: str):
        self.cg_base = cg_base

    def builds_cgroup(self, slice_name: str) -> str | None:
        return os.path.join(self.cg_base, machine.slice_path(slice_name))

    def slice_memory_max(self, slice_name: str) -> str | None:
        return machine.configured_memory_max(slice_name)

    def prepare(self, ticket, slice_name: str) -> None:
        """Name the build's service before it is started, so a daemon that stops in between can
        still find it."""
        # Each attempt of a build stopped to free memory gets a name of its own: the last one's
        # service may still be loaded while systemd lets it go.
        ticket.unit = f"buildq-{ticket.id}.service" if not ticket.requeues else f"buildq-{ticket.id}-r{ticket.requeues}.service"
        ticket.cg = os.path.join(self.cg_base, machine.slice_path(slice_name), ticket.unit)

    def unit_state(self, ticket) -> str:
        """'pending' if systemd has the service or a job to start it, 'gone' if it has neither,
        'unknown' if systemd can't be asked."""
        try:
            out = subprocess.run(["systemctl", "--user", "show", "-p", "LoadState,ActiveState,Job", ticket.unit],
                                 stdin=DEVNULL, capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return "unknown"
        props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        if not props:
            return "unknown"
        if props.get("LoadState") == "loaded" and (props.get("Job") or props.get("ActiveState") in
                                                   ("active", "activating", "reloading", "deactivating")):
            return "pending"
        return "gone"

    def launch(self, ticket, lane: Lane, slice_name: str):
        self.prepare(ticket, slice_name)
        unit = ticket.unit
        properties = [f"--property={k}={v}" for k, v in lane.properties.items()]
        cmd = common.daemon_command()
        pypath = common.module_pythonpath(os.environ.get("PYTHONPATH"), always=True) if len(cmd) >= 2 and cmd[1] == "-m" else None
        setenv = [f"--setenv=PYTHONPATH={pypath}", "--setenv=PYTHONSAFEPATH=1"] if pypath else []
        with open(os.path.join(ticket.dir, "launch.err"), "wb") as err:
            proc = subprocess.Popen(
                ["systemd-run", "--user", "--quiet", f"--unit={unit}", f"--slice={slice_name}",
                 "--property=KillMode=mixed", "--property=OOMPolicy=continue", "--property=TimeoutStopSec=60",
                 *properties, *setenv, "--", *cmd, "--supervise", ticket.dir],
                stdin=DEVNULL, stdout=DEVNULL, stderr=err)
        return proc, False  # running once systemd-run exits 0

    def alive(self, ticket) -> bool:
        return bool(ticket.cg) and machine.cgroup_populated(ticket.cg)

    def result(self, ticket) -> str:
        """systemd's Result for the build's service: "success", "exit-code", "signal", "oom-kill" (the
        kernel stopped it for memory), "managed-oom-kill" (systemd-oomd stopped it for slice/ancestor
        pressure), ... or "" if systemd can't say. With OOMPolicy=continue, OOM kills leave
        Result=signal while still incrementing OOMKills / ManagedOOMKills, so those are checked
        explicitly (kernel OOMKills first so own-cap kills stay distinguishable from slice-pressure
        managed kills)."""
        if not ticket.unit:
            return ""
        try:
            out = subprocess.run(["systemctl", "--user", "show", "-p", "LoadState,Result,OOMKills,ManagedOOMKills", ticket.unit],
                                 stdin=DEVNULL, capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return ""
        props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        if props.get("LoadState") == "not-found":
            return ""
        # systemd prints UINT64_MAX (18446744073709551615) when an OOM counter is unavailable;
        # sane_count() rejects that sentinel alongside non-numeric or negative values.
        if int(common.sane_count(props.get("OOMKills")) or 0) > 0:
            return "oom-kill"
        if int(common.sane_count(props.get("ManagedOOMKills")) or 0) > 0:
            return "managed-oom-kill"
        return props.get("Result", "").strip()

    def cleanup(self, ticket):
        """Unload the build's service once its outcome is read (a failed one stays loaded until then)."""
        if not ticket.unit:
            return None
        return subprocess.Popen(["systemctl", "--user", "reset-failed", ticket.unit], stdin=DEVNULL,
                                stdout=DEVNULL, stderr=DEVNULL)

    def freeze(self, ticket, frozen: bool):
        """Pause or resume the build's processes with the cgroup freezer: they stop where they are,
        keeping their memory but asking for no more, until thawed. Written to its cgroup.freeze at
        once (as cgroup.kill is), with `systemctl freeze`/`thaw` where that can't be written."""
        if ticket.cg:
            try:
                with open(os.path.join(ticket.cg, "cgroup.freeze"), "w") as f:
                    f.write("1" if frozen else "0")
                return None
            except OSError:
                pass
        if not ticket.unit:
            return None
        return subprocess.Popen(["systemctl", "--user", "freeze" if frozen else "thaw", ticket.unit], stdin=DEVNULL,
                                stdout=DEVNULL, stderr=DEVNULL)

    def freeze_state(self, ticket) -> tuple[bool | None, bool | None, bool]:
        if not ticket.cg:
            return None, None, False
        return (machine.cgroup_freeze_set(ticket.cg), machine.cgroup_frozen(ticket.cg),
                machine.parent_freeze_set(ticket.cg))

    def stop(self, ticket, force: bool = False):
        if not ticket.unit:
            return None
        if force and ticket.cg:
            try:  # every process of the build, frozen ones too, at once (Linux 5.14+)
                with open(os.path.join(ticket.cg, "cgroup.kill"), "w") as f:
                    f.write("1")
                return None
            except OSError:
                pass
        command = ["systemctl", "--user", "kill", "--signal=SIGKILL", ticket.unit] if force else \
                  ["systemctl", "--user", "stop", "--no-block", ticket.unit]
        return subprocess.Popen(command, stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL)

    @staticmethod
    def check_lane(lane: Lane, mem_total: int = 0) -> str | None:
        """Whether systemd accepts the lane's properties; the error if it doesn't."""
        if not lane.properties:
            return None
        try:
            # A transient service, as builds run, so service-only properties are checked too.
            probe = subprocess.run(["systemd-run", "--user", "--wait", "--quiet", "--collect",
                                    *[f"--property={k}={v}" for k, v in lane.properties.items()], "true"],
                                   stdin=DEVNULL, stdout=DEVNULL, stderr=subprocess.PIPE, timeout=30)
        except (OSError, subprocess.SubprocessError) as e:
            return str(e)
        return None if probe.returncode == 0 else probe.stderr.decode(errors="replace").strip()[-300:]

    @staticmethod
    def available() -> bool:
        if not shutil.which("systemd-run") or not shutil.which("systemctl"):
            return False
        try:
            return subprocess.run(["systemctl", "--user", "show-environment"], stdin=DEVNULL, stdout=DEVNULL,
                                  stderr=DEVNULL, timeout=10).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False


class CgroupRunner:
    """Direct Linux cgroup v2 runner without systemd.

    Creates one cgroup per build under `<root>/builds/buildq-<id>[-r<n>]`, writing kernel limits
    (`memory.high`, `memory.max`, `cpu.weight`, `io.weight`, `pids.max`), freezing via
    `cgroup.freeze`, killing via `cgroup.kill`, and guarding `<root>/builds/memory.max` with
    `slice_guard`. Requires a writable delegated cgroup v2 directory (for example created at boot
    by `init/cgroup-setup.sh`, elogind/pam_cgfs, or a private container cgroup namespace)."""

    name = "cgroup"
    can_freeze = True
    has_cgroup = True
    enforces_mem_cap = True

    def __init__(self, root: str | None = None, mem_total: int = 0):
        self.root = root or ""
        self.builds_dir = os.path.join(self.root, "builds") if self.root else ""
        self.mem_total = mem_total
        self.initial_memory_max: str | None = None
        self._ready = False
        if self.root:
            self._prepare_root()

    @staticmethod
    def _enable_controllers(path: str) -> set[str]:
        avail = machine.cgroup_controllers(path)
        current = machine.cgroup_subtree_controllers(path)
        wanted = [c for c in ("cpu", "memory", "io", "pids") if c in avail and c not in current]
        for ctrl in wanted:
            try:
                with open(os.path.join(path, "cgroup.subtree_control"), "w") as f:
                    f.write(f"+{ctrl}\n")
            except OSError:
                continue
        return machine.cgroup_subtree_controllers(path)

    @classmethod
    def _probe_root(cls, root: str, *, allow_move_self: bool = False) -> bool:
        """Whether `root` is a usable cgroup v2 directory where child cgroups have cpu+memory."""
        if not root or not machine.is_cgroup_v2():
            return False
        real = os.path.realpath(root)
        if not (real == "/sys/fs/cgroup" or real.startswith("/sys/fs/cgroup/")):
            return False
        if not os.path.isdir(real) or not os.access(real, os.W_OK | os.X_OK):
            return False
        # Respect cgroup v2 no-internal-processes rule: never touch a shared cgroup with other PIDs.
        procs = machine.cgroup_pids(real)
        if procs:
            if allow_move_self and procs == [os.getpid()]:
                daemon_dir = os.path.join(real, "daemon")
                try:
                    os.makedirs(daemon_dir, exist_ok=True)
                    with open(os.path.join(daemon_dir, "cgroup.procs"), "w") as f:
                        f.write(f"{os.getpid()}\n")
                except OSError:
                    return False
            else:
                # If subtree_control already delegates memory+cpu (e.g. root with subdirs), still check builds_dir.
                sub = machine.cgroup_subtree_controllers(real)
                if not ({"memory", "cpu"} <= sub):
                    return False
        cls._enable_controllers(real)
        builds = os.path.join(real, "builds")
        try:
            os.makedirs(builds, exist_ok=True)
        except OSError:
            return False
        enabled = cls._enable_controllers(builds)
        if not ({"memory", "cpu"} <= enabled):
            # Try leaving `builds` clean if we just created it and controllers aren't available.
            try:
                os.rmdir(builds)
            except OSError:
                pass
            return False
        probe = os.path.join(builds, f".buildq-probe-{os.getpid()}")
        try:
            os.makedirs(probe, exist_ok=True)
            ok = (os.path.isfile(os.path.join(probe, "memory.stat"))
                  and os.path.isfile(os.path.join(probe, "cpu.stat"))
                  and os.path.isfile(os.path.join(probe, "memory.max"))
                  and os.access(os.path.join(probe, "cgroup.procs"), os.W_OK))
            if ok and os.geteuid() != 0:
                # Linux cgroup v2 delegation rule (man 7 cgroups): migrating a child from our
                # current cgroup into `builds/<build>` requires write permission on
                # `<common_ancestor>/cgroup.procs`. Checking up front keeps `runner = "auto"`
                # from selecting CgroupRunner when buildqd was launched outside the subtree.
                own = machine.proc_cgroup(os.getpid())
                if not own:
                    ok = False
                else:
                    common_anc = os.path.commonpath([os.path.realpath(own), os.path.realpath(probe)])
                    ok = os.access(os.path.join(common_anc, "cgroup.procs"), os.W_OK)
        except OSError:
            ok = False
        finally:
            try:
                os.rmdir(probe)
            except OSError:
                pass
        return ok

    @classmethod
    def discover(cls, configured_root: str = "") -> str | None:
        """Find a usable delegated cgroup v2 root, or None if unavailable."""
        if not machine.is_cgroup_v2():
            return None
        candidates: list[tuple[str, bool]] = []
        env_root = os.environ.get("BUILDQ_CGROUP_ROOT", "").strip()
        if env_root:
            candidates.append((env_root, True))
        if configured_root:
            candidates.append((configured_root, True))
        try:
            user = pwd.getpwuid(UID).pw_name
        except KeyError:
            user = ""
        if user:
            candidates.append((f"/sys/fs/cgroup/buildq-{user}", True))
        candidates.append((f"/sys/fs/cgroup/buildq-uid-{UID}", True))
        own = machine.proc_cgroup(os.getpid())
        if own and own != "/sys/fs/cgroup":
            parent = os.path.dirname(own)
            if parent and parent != "/sys/fs/cgroup":
                candidates.append((parent, False))
            candidates.append((os.path.join(own, "buildq"), False))
            candidates.append((own, False))
        seen: set[str] = set()
        for cand, allow_move in candidates:
            norm = os.path.normpath(cand)
            if norm in seen:
                continue
            seen.add(norm)
            if cls._probe_root(norm, allow_move_self=allow_move):
                return os.path.realpath(norm)
        return None

    def _prepare_root(self) -> None:
        self._ready = self._probe_root(self.root, allow_move_self=True)
        if self._ready:
            self.root = os.path.realpath(self.root)
            self.builds_dir = os.path.join(self.root, "builds")
            self.initial_memory_max = machine.cgroup_memory_max(self.builds_dir) or "max"
            # Remove any stale empty build directories left behind by an earlier crash.
            try:
                for name in os.listdir(self.builds_dir):
                    if name.startswith("buildq-"):
                        path = os.path.join(self.builds_dir, name)
                        if os.path.isdir(path) and not machine.cgroup_populated(path):
                            try:
                                os.rmdir(path)
                            except OSError:
                                pass
            except OSError:
                pass

    def builds_cgroup(self, slice_name: str) -> str | None:
        return self.builds_dir if self._ready else None

    def slice_memory_max(self, slice_name: str) -> str | None:
        return self.initial_memory_max or "max"

    def prepare(self, ticket, slice_name: str) -> None:
        ticket.unit = f"buildq-{ticket.id}" if not ticket.requeues else f"buildq-{ticket.id}-r{ticket.requeues}"
        ticket.cg = os.path.join(self.builds_dir, ticket.unit)

    def unit_state(self, ticket) -> str:
        if ticket.cg and machine.cgroup_populated(ticket.cg):
            return "pending"
        sup = common.read_json(os.path.join(ticket.dir, "supervisor.json")) or {}
        if machine.proc_alive(sup.get("pid"), sup.get("start")):
            ticket.pid, ticket.pid_start = sup["pid"], sup["start"]
            return "pending"
        return "gone"

    def launch(self, ticket, lane: Lane, slice_name: str):
        self.prepare(ticket, slice_name)
        os.makedirs(ticket.cg, exist_ok=True)
        # Keep OOM kills per-process (like systemd OOMPolicy=continue) so the supervisor survives to
        # write result.json with memory.events.local counters.
        try:
            with open(os.path.join(ticket.cg, "memory.oom.group"), "w") as f:
                f.write("0\n")
        except OSError:
            pass
        files, hints = translate_cgroup_properties(lane, self.mem_total or 1)
        for fname, val in files.items():
            target = os.path.join(ticket.cg, fname)
            if not os.path.exists(target):
                continue
            try:
                with open(target, "w") as f:
                    f.write(f"{val}\n")
            except OSError as e:
                common.log(f"{ticket.id}: can't write {fname}={val} in {ticket.cg}: {e}")
        if "nice" in hints:
            ticket.nice = hints["nice"]
        cmd = common.daemon_command()
        pypath = common.module_pythonpath(os.environ.get("PYTHONPATH"), always=True) if len(cmd) >= 2 and cmd[1] == "-m" else None
        env = {**os.environ, "PYTHONPATH": pypath, "PYTHONSAFEPATH": "1"} if pypath else None
        proc = subprocess.Popen([*cmd, "--supervise", ticket.dir], stdin=DEVNULL, stdout=DEVNULL,
                                stderr=DEVNULL, start_new_session=True, env=env)
        ticket.pid = proc.pid
        ident = machine.proc_identity(proc.pid)
        ticket.pid_start = ident[0] if ident else None
        return proc, True

    def alive(self, ticket) -> bool:
        if ticket.cg and machine.cgroup_populated(ticket.cg):
            return True
        return machine.proc_alive(ticket.pid, ticket.pid_start)

    def result(self, ticket) -> str:
        if not ticket.cg:
            return ""
        local = machine.cgroup_events(ticket.cg)
        if local.get("oom", 0) > 0 and local.get("oom_kill", 0) > 0:
            return "oom-kill"
        if machine.cgroup_oom_kills(ticket.cg) > 0:
            return "oom-kill" if local.get("oom", 0) > 0 else "slice-oom-kill"
        return ""

    def cleanup(self, ticket):
        if ticket.cg:
            try:
                os.rmdir(ticket.cg)
            except OSError:
                pass
        return None

    def freeze(self, ticket, frozen: bool):
        if ticket.cg:
            with open(os.path.join(ticket.cg, "cgroup.freeze"), "w") as f:
                f.write("1" if frozen else "0")
        return None

    def freeze_state(self, ticket) -> tuple[bool | None, bool | None, bool]:
        if not ticket.cg:
            return None, None, False
        return (machine.cgroup_freeze_set(ticket.cg), machine.cgroup_frozen(ticket.cg),
                machine.parent_freeze_set(ticket.cg))

    def stop(self, ticket, force: bool = False):
        if force and ticket.cg:
            try:
                with open(os.path.join(ticket.cg, "cgroup.kill"), "w") as f:
                    f.write("1")
                return None
            except OSError:
                pass
            # Fallback for Linux < 5.14 without cgroup.kill: kill every PID in cgroup.procs.
            for _ in range(3):
                pids = machine.cgroup_pids(ticket.cg)
                if not pids:
                    break
                for pid in pids:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
            return None
        if machine.proc_alive(ticket.pid, ticket.pid_start):
            try:
                os.kill(ticket.pid, signal.SIGTERM)
            except OSError:
                pass
        return None

    @staticmethod
    def check_lane(lane: Lane, mem_total: int = 0) -> str | None:
        if not lane.properties:
            return None
        try:
            translate_cgroup_properties(lane, mem_total or (1 << 40))
        except (ValueError, TypeError) as e:
            return str(e)
        return None


class DirectRunner:
    """Pure-Linux runner requiring neither systemd nor cgroup delegation.

    Each build runs under a subreaper supervisor in its own session, measured via /proc
    (smaps_rollup PSS + monotonic process-tree CPU), paused via SIGSTOP/SIGCONT, capped at its
    lane's MemoryMax by the daemon watchdog, and cleaned up across both its process tree and
    BUILDQ_JOB-tagged strays."""

    name = "direct"
    can_freeze = True
    has_cgroup = False
    enforces_mem_cap = True

    def builds_cgroup(self, slice_name: str) -> str | None:
        return None

    def slice_memory_max(self, slice_name: str) -> str | None:
        return None

    def prepare(self, ticket, slice_name: str) -> None:
        pass

    def result(self, ticket) -> str:
        return "oom-kill" if getattr(ticket, "cap_killed", False) else ""

    def cleanup(self, ticket):
        return None

    def unit_state(self, ticket) -> str:
        sup = common.read_json(os.path.join(ticket.dir, "supervisor.json")) or {}
        if machine.proc_alive(sup.get("pid"), sup.get("start")):
            ticket.pid, ticket.pid_start = sup["pid"], sup["start"]
            return "pending"
        return "gone"

    def launch(self, ticket, lane: Lane, slice_name: str):
        nice_val = lane_nice(lane)
        if nice_val is not None:
            ticket.nice = nice_val
        cmd = common.daemon_command()
        pypath = common.module_pythonpath(os.environ.get("PYTHONPATH"), always=True) if len(cmd) >= 2 and cmd[1] == "-m" else None
        env = {**os.environ, "PYTHONPATH": pypath, "PYTHONSAFEPATH": "1"} if pypath else None
        proc = subprocess.Popen([*cmd, "--supervise", ticket.dir], stdin=DEVNULL, stdout=DEVNULL,
                                stderr=DEVNULL, start_new_session=True, env=env)
        ticket.pid = proc.pid
        ident = machine.proc_identity(proc.pid)
        ticket.pid_start = ident[0] if ident else None
        return proc, True

    def alive(self, ticket) -> bool:
        if machine.proc_alive(ticket.pid, ticket.pid_start):
            return True
        return bool(machine.tagged_processes(ticket.id))  # the supervisor is gone; is anything of the build left?

    def freeze(self, ticket, frozen: bool):
        """Pause or resume the build's process tree via SIGSTOP/SIGCONT.

        On freeze: stop descendant process groups and PIDs first (re-checking briefly so children
        forked mid-stop are caught), then stop the supervisor so its timeout clock pauses too.
        On thaw: resume the supervisor first, then every descendant process group and PID."""
        if frozen:
            for _ in range(3):
                info = machine.proc_tree_info(ticket.pid, ticket.pid_start, ticket.id)
                for pgid in info["pgids"]:
                    try:
                        os.killpg(pgid, signal.SIGSTOP)
                    except OSError:
                        pass
                for pid in info["descendants"]:
                    try:
                        os.kill(pid, signal.SIGSTOP)
                    except OSError:
                        pass
                if not info["descendants"]:
                    break
                time.sleep(0.01)
            if machine.proc_alive(ticket.pid, ticket.pid_start):
                try:
                    os.kill(ticket.pid, signal.SIGSTOP)
                except OSError:
                    pass
        else:
            if machine.proc_alive(ticket.pid, ticket.pid_start):
                try:
                    os.kill(ticket.pid, signal.SIGCONT)
                except OSError:
                    pass
            info = machine.proc_tree_info(ticket.pid, ticket.pid_start, ticket.id)
            for pgid in info["pgids"]:
                try:
                    os.killpg(pgid, signal.SIGCONT)
                except OSError:
                    pass
            for pid in info["descendants"]:
                try:
                    os.kill(pid, signal.SIGCONT)
                except OSError:
                    pass
        return None

    def freeze_state(self, ticket) -> tuple[bool | None, bool | None, bool]:
        if hasattr(ticket, "_own_stopped") and ticket._own_stopped is not None:
            return ticket._own_stopped, ticket._all_stopped, False
        info = machine.proc_tree_info(ticket.pid, ticket.pid_start, ticket.id)
        return info["own_stopped"], info["all_stopped"], False

    def stop(self, ticket, force: bool = False):
        if force:
            # Capture the full process tree BEFORE killing the supervisor so children that cleared
            # BUILDQ_JOB (e.g. `env -i`) are still terminated cleanly.
            info = machine.proc_tree_info(ticket.pid, ticket.pid_start, ticket.id)
            for pgid in info["pgids"]:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except OSError:
                    pass
            for pid in info["descendants"]:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            if machine.proc_alive(ticket.pid, ticket.pid_start):
                try:
                    os.kill(ticket.pid, signal.SIGKILL)
                except OSError:
                    pass
            for pid in machine.tagged_processes(ticket.id):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            return None
        if machine.proc_alive(ticket.pid, ticket.pid_start):
            try:
                os.kill(ticket.pid, signal.SIGTERM)
            except OSError:
                pass
        else:
            for pid in machine.tagged_processes(ticket.id):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        return None

    @staticmethod
    def check_lane(lane: Lane, mem_total: int = 0) -> str | None:
        if not lane.properties:
            return None
        try:
            translate_cgroup_properties(lane, mem_total or (1 << 40))
        except (ValueError, TypeError) as e:
            return str(e)
        return None
