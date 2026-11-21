# SPDX-License-Identifier: MIT
"""Build runners (SystemdRunner, DirectRunner) and GNU make FIFO jobserver."""

from __future__ import annotations

import fcntl
import os
import shutil
import signal
import struct
import subprocess
import termios

from . import common, machine
from .common import DEVNULL
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
# Running builds: as transient systemd services, or as plain processes


class SystemdRunner:
    """Each build is buildq-<ticket>.service in the configured slice, with its lane's properties."""

    name = "systemd"

    def __init__(self, cg_base: str):
        self.cg_base = cg_base

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
        with open(os.path.join(ticket.dir, "launch.err"), "wb") as err:
            proc = subprocess.Popen(
                ["systemd-run", "--user", "--quiet", f"--unit={unit}", f"--slice={slice_name}",
                 "--property=KillMode=mixed", "--property=OOMPolicy=continue", "--property=TimeoutStopSec=60",
                 *properties, "--", *common.daemon_command(), "--supervise", ticket.dir],
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

    can_freeze = True

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
    def check_lane(lane: Lane) -> str | None:
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


class DirectRunner:
    """Each build's supervisor is a plain process in a session of its own: no limits, no measurement.
    What a build leaves behind is found by the BUILDQ_JOB in its environment and stopped."""

    name = "direct"

    def prepare(self, ticket, slice_name: str) -> None:
        pass

    def result(self, ticket) -> str:
        return ""

    def cleanup(self, ticket):
        return None

    def unit_state(self, ticket) -> str:
        sup = common.read_json(os.path.join(ticket.dir, "supervisor.json")) or {}
        if machine.proc_alive(sup.get("pid"), sup.get("start")):
            ticket.pid, ticket.pid_start = sup["pid"], sup["start"]
            return "pending"
        return "gone"

    def launch(self, ticket, lane: Lane, slice_name: str):
        proc = subprocess.Popen([*common.daemon_command(), "--supervise", ticket.dir], stdin=DEVNULL, stdout=DEVNULL,
                                stderr=DEVNULL, start_new_session=True)
        ticket.pid = proc.pid
        ident = machine.proc_identity(proc.pid)
        ticket.pid_start = ident[0] if ident else None
        return proc, True

    def alive(self, ticket) -> bool:
        if machine.proc_alive(ticket.pid, ticket.pid_start):
            return True
        return bool(machine.tagged_processes(ticket.id))  # the supervisor is gone; is anything of the build left?

    can_freeze = False  # no cgroup of its own to freeze

    def freeze(self, ticket, frozen: bool):
        return None

    def stop(self, ticket, force: bool = False):
        if machine.proc_alive(ticket.pid, ticket.pid_start):
            try:
                os.kill(ticket.pid, signal.SIGKILL if force else signal.SIGTERM)
            except OSError:
                pass
        if force or not machine.proc_alive(ticket.pid, ticket.pid_start):
            for pid in machine.tagged_processes(ticket.id):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        return None

    @staticmethod
    def check_lane(lane: Lane) -> str | None:
        return None

