# SPDX-License-Identifier: MIT
"""Shared constants, paths, logging, JSON helpers, and size/duration parsing."""

from __future__ import annotations

import json
import math
import os
import re
import socket
import struct
import subprocess
import sys
import threading
from concurrent.futures import Future

VERSION = "0.2.0"
KIB, MIB, GIB = 1 << 10, 1 << 20, 1 << 30
UID = os.getuid()
FINISHED = ("succeeded", "failed", "cancelled", "lost")
DEVNULL = subprocess.DEVNULL
CI_UNITS = ("actions.runner.*", "gha-*", "*-runner-*", "gitlab-runner*")

_DAEMON_ENTRYPOINT: str | None = None
_CLIENT_ENTRYPOINT: str | None = None


def set_daemon_entrypoint(path: str) -> None:
    """Record the executable script path when invoked via `bin/buildqd` or an installed copy."""
    global _DAEMON_ENTRYPOINT
    _DAEMON_ENTRYPOINT = os.path.abspath(path)


def set_client_entrypoint(path: str) -> None:
    """Record the executable script path when invoked via `bin/buildq` or an installed copy."""
    global _CLIENT_ENTRYPOINT
    _CLIENT_ENTRYPOINT = os.path.abspath(path)


def daemon_command() -> list[str]:
    """Command prefix to invoke buildqd (used to spawn `--supervise`)."""
    if _DAEMON_ENTRYPOINT:
        return [sys.executable, _DAEMON_ENTRYPOINT]
    return [sys.executable, "-m", "buildq.daemon"]


def client_command() -> list[str]:
    """Command prefix to invoke buildq (used by the daemon for `buildq source`)."""
    if _DAEMON_ENTRYPOINT:
        # Prefer the resolved launcher's own directory so an external symlink doesn't mix installs.
        for base in (os.path.dirname(os.path.realpath(_DAEMON_ENTRYPOINT)), os.path.dirname(_DAEMON_ENTRYPOINT)):
            beside = os.path.join(base, "buildq")
            if os.path.isfile(beside):
                return [sys.executable, beside]
    return [sys.executable, "-m", "buildq.client"]


def package_root() -> str:
    """Absolute parent directory containing the running `buildq` package."""
    return os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def client_bin_dir() -> str:
    """Directory holding the running `buildq` executable (for finding sibling buildq-shim)."""
    candidates: list[str] = []
    if _CLIENT_ENTRYPOINT:
        candidates.extend([os.path.dirname(os.path.realpath(_CLIENT_ENTRYPOINT)), os.path.dirname(_CLIENT_ENTRYPOINT)])
    else:
        if sys.argv and sys.argv[0] and os.path.isfile(sys.argv[0]):
            candidates.extend([os.path.dirname(os.path.realpath(sys.argv[0])), os.path.dirname(os.path.abspath(sys.argv[0]))])
        # Module-mode (`python -m buildq.client`) fallbacks: checkout bin/ and matching scripts dir.
        import sysconfig
        candidates.append(os.path.normpath(os.path.join(package_root(), "..", "bin")))
        scripts = sysconfig.get_path("scripts")
        if scripts:
            candidates.append(os.path.realpath(scripts))
        candidates.append(os.path.dirname(os.path.abspath(sys.executable)))
    for directory in candidates:
        if os.path.isfile(os.path.join(directory, "buildq-shim")):
            return directory
    return candidates[0] if candidates else os.path.dirname(os.path.realpath(__file__))


def module_pythonpath(existing: str | None = None, *, always: bool = False) -> str | None:
    """PYTHONPATH placing the running package root first for `-m buildq.*` child processes.
    Needed when running via `-m` (no entrypoint script) or when a stripped child env could drop
    user site-packages (`always=True`)."""
    if not always and _DAEMON_ENTRYPOINT is not None:
        return None
    root = package_root()
    if existing:
        parts = [p for p in existing.split(os.pathsep) if p]
        if parts and os.path.isabs(parts[0]) and os.path.realpath(parts[0]) == root:
            return existing
        kept = [p for p in parts if not (os.path.isabs(p) and os.path.realpath(p) == root)]
        return os.pathsep.join([root, *kept])
    return root


def runtime_dir() -> str:
    return os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{UID}"


def default_socket() -> str:
    return os.path.expanduser(os.environ.get("BUILDQ_SOCKET") or os.path.join(runtime_dir(), "buildq.sock"))


def default_state_dir() -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.expanduser(os.environ.get("BUILDQ_STATE_DIR") or os.path.join(base, "buildq"))


def default_config() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.expanduser(os.environ.get("BUILDQ_CONFIG") or os.path.join(base, "buildq", "config.toml"))


_log_lock = threading.Lock()


def to_journal() -> bool:
    """Whether stdout is the journal: JOURNAL_STREAM holds its device and inode (sd_journal's way of
    telling; a shell in a terminal that systemd started inherits the variable, not the stream)."""
    try:
        device, inode = (int(x) for x in os.environ.get("JOURNAL_STREAM", "").split(":"))
        st = os.fstat(sys.stdout.fileno())
        return (st.st_dev, st.st_ino) == (device, inode)
    except (ValueError, OSError, AttributeError):
        return False


def log(message: str, level: int | None = None) -> None:
    """A line for the journal, written whole even with threads logging; `level` (syslog: 4 is
    warning) is marked only when stdout really is the journal."""
    if level is not None and to_journal():  # journald reads the level of each line by itself
        message = "\n".join(f"<{level}>{line}" for line in message.split("\n"))
    with _log_lock:
        sys.stdout.write(message + "\n")
        sys.stdout.flush()


def write_json(path: str, data) -> None:
    """Write a JSON file atomically: readers see the old contents or the new, never a part."""
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, path)


def read_json(path: str):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def peer_cred(sock: socket.socket) -> tuple[int, int] | None:
    """(pid, uid) of the process at the other end of a Unix socket, or None."""
    try:
        pid, uid, _gid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                                             struct.calcsize("3i")))
        return pid, uid
    except (OSError, AttributeError):
        return None


# --------------------------------------------------------------------------------------------
# Sizes, durations, numbers


def parse_size(value, total: int | None = None) -> int:
    """'16G', '512M', '1.5T', '2048K', a plain number of bytes, or '10%' of `total` -> bytes."""
    if isinstance(value, bool):
        raise ValueError(f"not a size: {value!r}")
    if isinstance(value, (int, float)):
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"not a size: {value!r}")
        return int(value)
    text = str(value).strip()
    if text.endswith("%"):
        if total is None:
            raise ValueError(f"a percentage needs a total: {value!r}")
        share = float(text[:-1])
        if not 0 <= share <= 100:
            raise ValueError(f"not a percentage: {value!r}")
        return int(share / 100 * total)
    match = re.fullmatch(r"([0-9]*\.?[0-9]+)\s*([kKmMgGtT]?)(?:i?[bB])?", text)
    if not match:
        raise ValueError(f"not a size: {value!r}")
    unit = {"": 1, "k": KIB, "m": MIB, "g": GIB, "t": 1 << 40}[match.group(2).lower()]
    return int(float(match.group(1)) * unit)


def parse_duration(value) -> float:
    """'90', '90s', '10m', '2h', '7d' -> seconds."""
    if isinstance(value, bool):
        raise ValueError(f"not a duration: {value!r}")
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        match = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*([smhd]?)\s*", str(value))
        if not match:
            raise ValueError(f"not a duration: {value!r}")
        seconds = float(match.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError(f"not a duration: {value!r}")
    return seconds


def boolean(value, what: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{what} must be true or false")
    return value


def number(value, what: str, low: float, high: float, integer: bool = False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{what} must be a number")
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{what} must be between {low:g} and {high:g}")
    if integer and int(value) != value:
        raise ValueError(f"{what} must be a whole number")
    return int(value) if integer else float(value)


def fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


def fmt_size(n: float | None) -> str:
    n = n or 0
    if abs(n) >= 10 * GIB:
        return f"{n / GIB:.0f}G"
    if abs(n) >= GIB:
        return f"{n / GIB:.1f}G"
    return f"{n / MIB:.0f}M"



def sane_count(value, limit: float = 1e12) -> float | None:
    """A non-negative, finite number no larger than `limit` from a file another process wrote, or
    None. (float() of a huge integer raises OverflowError; NaN fails every comparison.)"""
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if 0 <= value <= limit else None



PENDING = object()  # a query run off the event loop whose answer isn't in yet


class InlineExecutor:
    """Runs each query at once, on the caller's thread (tests)."""

    def submit(self, fn, *args) -> Future:
        future: Future = Future()
        try:
            future.set_result(fn(*args))
        except BaseException as e:  # (as a pool's future would hold it)
            future.set_exception(e)
        return future

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        pass

