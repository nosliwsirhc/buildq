# SPDX-License-Identifier: MIT
"""`buildq top`: live terminal monitor for the machine and build queue."""

from __future__ import annotations

import collections
import glob
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

from . import client
from .common import CI_UNITS, MIB

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")


def clean(text: str) -> str:
    """Text safe to draw: no escape sequences; tabs as spaces; every control and format character
    (newlines, C1, bidi and zero-width marks, line separators) replaced, since a label can hold a
    script; in ASCII mode, ASCII only."""
    import unicodedata
    text = ANSI.sub("", str(text).expandtabs(4))
    text = "".join(GLYPHS["ctl"] if unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") and c != "\u200c" else c
                   for c in text)  # (U+200C stays: Persian uses it inside words, and it moves nothing)
    return text.encode("ascii", "replace").decode() if GLYPHS["ctl"] == "?" else text


def char_width(c: str) -> int:
    """Columns a character takes on a terminal: wide (CJK, emoji) 2; marks and format characters
    (variation selectors, joiners) 0. (Drawn text has been through clean(), which replaces most
    format characters; this also serves text that hasn't.)"""
    import unicodedata
    if unicodedata.category(c) in ("Mn", "Me", "Cf") and c != "\u00ad":
        return 0
    return 2 if unicodedata.east_asian_width(c) in "WF" else 1


def text_width(text: str) -> int:
    return sum(char_width(c) for c in text)


def fit(text: str, width: int) -> str:
    """The longest start of `text` that takes at most `width` columns."""
    used, out = 0, []
    for c in text:
        w = char_width(c)
        if used + w > width:
            break
        used += w
        out.append(c)
    return "".join(out)


def read_text(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def pressure_of(text: str | None) -> dict:
    """{"some": avg10, "full": avg10} from a *.pressure file; empty if it can't be read (PSI off)."""
    out = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if parts and parts[0] in ("some", "full"):
            for part in parts[1:]:
                if part.startswith("avg10="):
                    try:
                        out[parts[0]] = float(part[6:])
                    except ValueError:
                        pass
    return out


_HOLDERS: dict[str, tuple[list[str], float]] = {}


def holders_of(device: str) -> list[str]:
    """MAJ:MIN of the devices stacked on a block device or on any of its partitions (dm-crypt,
    LVM, md). Known for a minute at a time: devices can be opened while top runs."""
    known = _HOLDERS.get(device)
    if known is None or time.monotonic() - known[1] > 60:
        base, found = f"/sys/dev/block/{device}", []  # (each once: an LV over two partitions shows twice)
        for holders in [os.path.join(base, "holders"), *glob.glob(os.path.join(base, "*", "holders"))]:
            try:
                names = os.listdir(holders)
            except OSError:
                continue
            for name in names:
                number = (read_text(f"/sys/class/block/{name}/dev") or "").strip()
                if number:
                    found.append(number)
        known = (list(dict.fromkeys(found)), time.monotonic())
        _HOLDERS[device] = known
    return known[0]


def io_counters(text: str | None) -> dict[str, tuple[int, int]]:
    """MAJ:MIN -> (bytes read, bytes written) from a cgroup's io.stat."""
    per = {}
    for line in (text or "").splitlines():
        fields = line.split()
        if not fields:
            continue
        rbytes = wbytes = 0
        for part in fields[1:]:
            name, _, value = part.partition("=")
            if name == "rbytes" and value.isdigit():
                rbytes = int(value)
            elif name == "wbytes" and value.isdigit():
                wbytes = int(value)
        per[fields[0]] = (rbytes, wbytes)
    return per


def io_rate(before: dict, after: dict, span: float) -> tuple[float, float]:
    """Bytes read and written per second between two io.stat readings, each byte once. I/O through
    dm-crypt, LVM or md is charged at the stacked device and again at the disks below (io.stat
    names whole disks). So devices are grouped with what is stacked on them, and each group counts
    the larger of what its bottom devices (nothing below them: the disks) and its top devices
    (nothing stacked on them) did in the interval:
    exact for dm-crypt, LVM, stripes however the bytes were split, and a plain partition beside a
    stacked one; a mirror's writes count each copy. Only devices in both readings count, so a
    stacked device that came or went shows no burst, and a new one makes what is stacked be looked
    at afresh. Loop devices aren't holders: a filesystem in a file counts twice."""
    if span <= 0:
        return 0.0, 0.0
    if any(d not in before for d in after):
        _HOLDERS.clear()  # something new: what is stacked may have changed
    deltas = {d: (max(0, after[d][0] - before[d][0]), max(0, after[d][1] - before[d][1])) for d in after if d in before}
    above = {d: [h for h in dict.fromkeys(holders_of(d)) if h in deltas and h != d] for d in deltas}
    group = {d: d for d in deltas}

    def root(x):
        while group[x] != x:
            group[x] = group[group[x]]
            x = group[x]
        return x

    for d, holders in above.items():
        for h in holders:
            group[root(d)] = root(h)
    stacked_on_something = {h for holders in above.values() for h in holders}
    members = collections.defaultdict(list)
    for d in deltas:
        members[root(d)].append(d)
    read = write = 0.0
    for devices in members.values():
        bottom = [d for d in devices if d not in stacked_on_something]  # nothing below it
        top = [d for d in devices if not above[d]]  # nothing above it
        read += max(sum(deltas[d][0] for d in bottom), sum(deltas[d][0] for d in top))
        write += max(sum(deltas[d][1] for d in bottom), sum(deltas[d][1] for d in top))
    return read / span, write / span


def cgroup_sample(path: str) -> dict | None:
    """What a cgroup is doing now: CPU time, the memory it needs (anon + shmem + kernel less
    reclaimable slab, as the daemon counts it), swap, bytes read and written, pressure, and memory
    events. None if it is gone or only partly readable (it is going)."""
    cpu, memory = read_text(os.path.join(path, "cpu.stat")), read_text(os.path.join(path, "memory.stat"))
    if cpu is None or memory is None:
        return None
    usage = 0
    for line in cpu.splitlines():
        if line.startswith("usage_usec "):
            usage = int(line.split()[1])
    stat = {}
    for line in memory.splitlines():
        name, _, value = line.partition(" ")
        if name in ("anon", "kernel", "shmem", "slab_reclaimable") and value.strip().isdigit():
            stat[name] = int(value)
    need = stat.get("anon", 0) + stat.get("shmem", 0) + max(0, stat.get("kernel", 0) - stat.get("slab_reclaimable", 0))
    swap = (read_text(os.path.join(path, "memory.swap.current")) or "").strip()
    io = io_counters(read_text(os.path.join(path, "io.stat")))
    events = {}
    for line in (read_text(os.path.join(path, "memory.events")) or "").splitlines():
        name, _, value = line.partition(" ")
        if value.strip().isdigit():
            events[name] = int(value)
    return {"usage": usage, "mem": need, "swap": int(swap) if swap.isdigit() else 0, "io": io,
            "psi_mem": pressure_of(read_text(os.path.join(path, "memory.pressure"))),
            "psi_cpu": pressure_of(read_text(os.path.join(path, "cpu.pressure"))),
            "psi_io": pressure_of(read_text(os.path.join(path, "io.pressure"))), "events": events}


def user_manager_dir() -> str:
    for line in (read_text("/proc/self/cgroup") or "").splitlines():
        if line.startswith("0::"):
            match = re.match(r"(.*/user@\d+\.service)(?:/|$)", line[3:].strip())
            if match:
                return "/sys/fs/cgroup" + match.group(1)
    uid = os.getuid()
    return f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service"


def slice_dir(name: str) -> str:
    parts = name[: -len(".slice")].split("-")
    return "/".join("-".join(parts[: i + 1]) + ".slice" for i in range(len(parts)))


def safe_dir(path) -> str | None:
    """A folder `top` may measure: it resolves to an existing directory inside the user's home
    (not home itself). Anything else (/, a symlink out of home, //) is left alone."""
    if not isinstance(path, str) or not path:
        return None
    home = os.path.realpath(os.path.expanduser("~"))
    real = os.path.realpath(path)
    return real if real.startswith(home + os.sep) and os.path.isdir(real) else None


def folders_of(row: dict) -> list[str]:
    """A build's folders, resolved, safe, each once, none inside another."""
    kept: list[str] = []
    for real in sorted({r for r in map(safe_dir, row.get("write_dirs") or []) if r}, key=len):
        if not any(real == k or real.startswith(k + os.sep) for k in kept):
            kept.append(real)
    return kept


class DirSizes:
    """The disk space build folders take, measured in the background by `du -sx` at idle I/O
    priority: it stays on one filesystem, counts a hard-linked file once, never follows a symlink,
    and can be killed. Each folder at most every RESCAN seconds; a `du` past LIMIT seconds is
    stopped and the folder shows as not measured."""

    RESCAN, LIMIT = 300.0, 60.0

    def __init__(self, background: bool = True):
        import atexit
        import threading
        self.proc: subprocess.Popen | None = None  # the du running now
        self.stopping = False
        atexit.register(self.stop)
        self.sizes: dict[str, tuple[int | None, bool, float]] = {}  # folder -> (bytes, complete, measured at)
        self.wanted: list[str] = []
        self.lock = threading.Lock()
        self.wake = threading.Event()
        if background:
            threading.Thread(target=self.run, name="dir-sizes", daemon=True).start()

    def want(self, folders) -> None:
        with self.lock:
            self.wanted = list(dict.fromkeys(folders))
        self.wake.set()

    def get(self, folder: str) -> tuple[int | None, bool, float] | None:
        """(bytes or None, complete, seconds ago) or None if not measured yet."""
        with self.lock:
            found = self.sizes.get(folder)
        return None if found is None else (found[0], found[1], time.monotonic() - found[2])

    def due(self) -> list[str]:
        with self.lock:
            return [f for f in self.wanted if f not in self.sizes or time.monotonic() - self.sizes[f][2] >= self.RESCAN]

    def run(self) -> None:
        while not self.stopping:
            self.wake.wait(5)
            self.wake.clear()
            for folder in self.due():
                if self.stopping:
                    return
                try:
                    self.measure_into(folder, self.LIMIT)
                except Exception:  # a thread that died would leave every folder "not yet" for good
                    pass

    def stop(self) -> None:
        """Kill the du running now: quitting top must not leave a walk going."""
        self.stopping = True
        proc = self.proc
        if proc is not None and proc.poll() is None:
            proc.kill()

    def measure_into(self, folder: str, limit: float, keep_cut: bool = True) -> None:
        if self.stopping:
            return
        size, complete, cut = self.measure(folder, limit, self)
        if cut and not keep_cut:
            return  # (--once: its deadline, not the folder's fault; it shows as not measured yet)
        with self.lock:
            self.sizes[folder] = (size, complete, time.monotonic())

    @staticmethod
    def _run_du(command: list[str], limit: float, owner=None) -> tuple[bytes, bytes, bool]:
        if shutil.which("ionice"):
            command = ["ionice", "-c3", *command]
        try:
            proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, preexec_fn=client.die_with(os.getpid()),
                                    env={**os.environ, "LC_ALL": "C"})  # (its messages are matched below)
        except OSError:
            return b"", b"", False
        if owner is not None:
            owner.proc = proc
        try:
            out, err = proc.communicate(timeout=limit)
            return out or b"", err or b"", False
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                pass  # stuck in the kernel (a hung mount): it goes when it can
            return b"", b"", True
        finally:
            if owner is not None:
                owner.proc = None

    @classmethod
    def measure(cls, folder: str, limit: float, owner=None) -> tuple[int | None, bool, bool]:
        """(bytes or None, complete, cut off by the limit). du exits 1 when an entry vanished mid-walk,
        which a running build does all the time; only an entry it couldn't read makes it partial.
        Falls back from GNU `du -sx --block-size=1` to POSIX/BusyBox `du -sxk` (x1024)."""
        out, err, cut = cls._run_du(["du", "-sx", "--block-size=1", folder], limit, owner)
        if cut:
            return None, False, True
        scale = 1
        match = re.match(rb"\s*(\d+)", out)
        if match is None and any(tok in err for tok in (b"unrecognized option", b"illegal option", b"invalid option")):
            out, err, cut = cls._run_du(["du", "-sxk", folder], limit, owner)
            if cut:
                return None, False, True
            scale = 1024
            match = re.match(rb"\s*(\d+)", out)
        size = int(match.group(1)) * scale if match else None
        unreadable = b"ermission denied" in err or b"Input/output error" in err
        return size, size is not None and not unreadable, False


class TopSampler:
    """One snapshot each refresh: the daemon's overview, the machine (from /proc), every
    filesystem builds write to, the groups that use the machine (buildq's builds, CI runners,
    containers, the user's apps), and every running build's own cgroup. Rates come from the
    change since the last snapshot."""

    def __init__(self, sizes: DirSizes | None = None):
        self.last: dict[str, tuple[float, dict]] = {}
        self.manager = user_manager_dir()
        self.sizes = sizes

    def rate(self, key: str, now: float, values: dict) -> dict:
        before = self.last.get(key)
        self.last[key] = (now, values)
        if before is None or now <= before[0]:
            return {}
        span = now - before[0]
        return {name: max(0.0, (values[name] - before[1].get(name, values[name])) / span)
                for name in values if isinstance(values[name], (int, float))}

    @staticmethod
    def mounts() -> list[tuple[str, str]]:
        """(mount point, source device name) from the mount table, in order (a later mount over the
        same point wins). btrfs gives each subvolume a device number of its own, so a path is matched
        to its mount by the longest mount point it lies under, not by its device number."""
        found = []
        for line in (read_text("/proc/self/mountinfo") or "").splitlines():
            fields = line.split()
            if len(fields) > 4 and " - " in line:
                source = line.split(" - ", 1)[1].split()[1:2]
                device = os.path.basename(os.path.realpath(source[0])) if source and source[0].startswith("/dev/") else ""
                found.append((re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[4]), device))
        return found

    @staticmethod
    def mount_of(path: str, mounts: list[tuple[str, str]]) -> tuple[str, str]:
        real, best = os.path.realpath(path), ("/", "")
        for point, device in mounts:
            if (real == point or real.startswith(point.rstrip("/") + "/")) and len(point) >= len(best[0]):
                best = (point, device)
        return best

    def filesystems(self, now: float, paths, floor_setting) -> list[dict]:
        """`/` and every filesystem a build writes to, once each: free, size, floor, I/O rates."""
        mounts = self.mounts()
        stats = {}
        for line in (read_text("/proc/diskstats") or "").splitlines():
            parts = line.split()
            if len(parts) > 9:
                stats[parts[2]] = (int(parts[5]) * 512, int(parts[9]) * 512)
        out, seen = [], set()
        for path in ["/", *paths]:
            try:
                vfs = os.statvfs(path)
            except OSError:
                continue
            mount, device = self.mount_of(path, mounts)
            key = device or mount
            if key in seen:
                continue  # (btrfs subvolumes of one disk)
            seen.add(key)
            size, free = vfs.f_blocks * vfs.f_frsize, vfs.f_bavail * vfs.f_frsize
            if isinstance(floor_setting, str) and floor_setting.endswith("%"):
                floor = int(float(floor_setting[:-1]) / 100 * size)
            else:
                floor = int(floor_setting or 0)
            fs = {"mount": mount, "device": device, "free": free, "size": size, "floor": floor}
            if device in stats:
                rate = self.rate(f"disk:{device}", now, {"read": stats[device][0], "write": stats[device][1]})
                fs["read"], fs["write"] = rate.get("read"), rate.get("write")
            out.append(fs)
        return out

    def machine(self, now: float, overview: dict | None) -> dict:
        config = (overview or {}).get("config") or {}
        cores = ((overview or {}).get("machine") or {}).get("cores") or os.cpu_count() or 1
        out = {"cores": cores}
        fields = (read_text("/proc/stat") or "cpu 0 0 0 0").splitlines()[0].split()[1:]
        ticks = [int(x) for x in fields[:8]] + [0] * (8 - len(fields[:8]))
        rate = self.rate("machine:cpu", now, {"busy": sum(ticks) - ticks[3] - ticks[4], "total": sum(ticks)})
        if rate.get("total"):
            out["busy"] = rate["busy"] / rate["total"] * (os.cpu_count() or cores)
        info = {}
        for line in (read_text("/proc/meminfo") or "").splitlines():
            name, _, value = line.partition(":")
            if value.split():
                info[name] = int(value.split()[0]) * 1024
        out.update(mem_total=info.get("MemTotal", 0), mem_available=info.get("MemAvailable", 0),
                   swap_total=info.get("SwapTotal", 0), swap_used=info.get("SwapTotal", 0) - info.get("SwapFree", 0))
        for resource in ("cpu", "memory", "io"):
            out[f"psi_{resource}"] = pressure_of(read_text(f"/proc/pressure/{resource}"))
        rows = ((overview or {}).get("running") or []) + ((overview or {}).get("queued") or [])
        paths = list(dict.fromkeys(p for row in rows for p in (row.get("disk_paths") or []) if isinstance(p, str)))
        out["filesystems"] = self.filesystems(now, paths, config.get("disk_floor"))
        return out

    def group_paths(self, config: dict, overview: dict | None = None) -> dict[str, list[str]]:
        import fnmatch
        apps = os.path.join(self.manager, "app.slice")
        if overview is not None and "builds_cg" in overview:
            builds_cg = overview.get("builds_cg")
            builds_list = [builds_cg] if isinstance(builds_cg, str) and builds_cg else []
        else:
            builds_list = [os.path.join(self.manager, slice_dir(config.get("slice") or "buildq.slice"))]
        found = {"builds": builds_list, "ci": [], "containers": [], "apps": [apps] if os.path.isdir(apps) else []}

        def children(path):
            try:
                return sorted(os.listdir(path))
            except OSError:
                return []

        for parent in (self.manager, apps):
            for name in children(parent):
                if name.endswith(".service") and any(fnmatch.fnmatch(name, p) for p in CI_UNITS):
                    found["ci"].append(os.path.join(parent, name))
        system = "/sys/fs/cgroup/system.slice"
        for name in children(system):
            if any(fnmatch.fnmatch(name, p) for p in CI_UNITS) and name.endswith(".service"):
                found["ci"].append(os.path.join(system, name))
            elif (name.startswith("docker-") and name.endswith(".scope")) or name in ("docker.service", "containerd.service"):
                found["containers"].append(os.path.join(system, name))
        for name in children(os.path.join(self.manager, "user.slice")):
            if name.startswith("libpod-") and name.endswith(".scope"):
                found["containers"].append(os.path.join(self.manager, "user.slice", name))
        return found

    def cgroup(self, path: str, now: float) -> dict | None:
        sample = cgroup_sample(path)
        if sample is None:
            self.last.pop(path, None)
            return None
        before = self.last.get(path)
        rate = self.rate(path, now, {"usage": sample["usage"]})
        sample["cores"] = rate["usage"] / 1e6 if rate else None
        if before is not None and "io" in before[1]:
            sample["read_rate"], sample["write_rate"] = io_rate(before[1]["io"], sample["io"], now - before[0])
        else:
            sample["read_rate"] = sample["write_rate"] = None
        self.last[path] = (now, {"usage": sample["usage"], "io": sample["io"]})
        return sample

    def snapshot(self, overview: dict | None) -> dict:
        now = time.monotonic()
        config = (overview or {}).get("config") or {}
        snap = {"time": time.time(), "overview": overview, "machine": self.machine(now, overview), "groups": {},
                "builds": {}, "dir_sizes": {}}
        found = self.group_paths(config, overview)
        samples = {path: self.cgroup(path, now) for paths in found.values() for path in paths}
        apps_dir = os.path.join(self.manager, "app.slice") + os.sep
        for group, paths in found.items():
            total = {"cores": 0.0, "mem": 0, "swap": 0, "read_rate": 0.0, "write_rate": 0.0, "known": True}
            for path in paths:
                sample = samples.get(path)
                if sample is None:
                    continue
                total["mem"] += sample["mem"]
                total["swap"] += sample["swap"]
                if sample["cores"] is None:
                    total["known"] = False  # seen for the first time: no rate yet
                else:
                    total["cores"] += sample["cores"]
                    total["read_rate"] += sample["read_rate"] or 0
                    total["write_rate"] += sample["write_rate"] or 0
                if group == "builds":
                    total["psi_mem"], total["psi_cpu"], total["psi_io"] = sample["psi_mem"], sample["psi_cpu"], sample["psi_io"]
            snap["groups"][group] = total
        apps = snap["groups"].get("apps")
        for path in found["ci"]:  # CI runners in app.slice aren't the user's apps
            sample = samples.get(path)
            if apps and sample and path.startswith(apps_dir):
                apps["mem"] = max(0, apps["mem"] - sample["mem"])
                apps["swap"] = max(0, apps["swap"] - sample["swap"])
                apps["cores"] = max(0.0, apps["cores"] - (sample["cores"] or 0.0))
        folders = []
        for row in (overview or {}).get("running") or []:
            if row.get("cg"):
                sample = self.cgroup(row["cg"], now)
                if sample is not None:
                    snap["builds"][row["id"]] = sample
            elif row.get("measured"):
                # Non-cgroup build (direct runner): use daemon's /proc measurement directly.
                snap["builds"][row["id"]] = {
                    "cores": float(row.get("cores_now") or 0.0),
                    "mem": int(row.get("mem_now") or 0),
                    "swap": int(row.get("swap_now") or 0),
                    "read_rate": None, "write_rate": None,
                    "psi_mem": None, "psi_cpu": None, "psi_io": None, "events": {},
                }
            snap.setdefault("folders", {})[row["id"]] = folders_of(row)
            folders.extend(snap["folders"][row["id"]])
        # Without a builds cgroup (direct runner), populate the builds group from running builds.
        if not found["builds"] and snap["builds"]:
            bgroup = snap["groups"]["builds"]
            for sample in snap["builds"].values():
                bgroup["mem"] += int(sample.get("mem") or 0)
                bgroup["swap"] += int(sample.get("swap") or 0)
                if sample.get("cores") is not None:
                    bgroup["cores"] += float(sample["cores"])
        if self.sizes is not None:
            self.sizes.want(folders)
            snap["dir_sizes"] = {f: self.sizes.get(f) for f in folders}
        return snap


# ---- drawing: lines of (text, colour) so the same code feeds curses and --once

GLYPHS = {"full": "█", "empty": "░", "mark": "▶", "atleast": "≥", "sep": "·", "more": "…", "ctl": "·"}


def use_ascii() -> None:
    """Without a UTF-8 locale, draw with ASCII."""
    GLYPHS.update(full="#", empty=".", mark=">", atleast=">=", sep="|", more="...", ctl="?")
    use_ascii_top()


def bar(fraction: float, width: int) -> str:
    fraction = min(max(fraction, 0.0), 1.0)
    full = int(round(fraction * width))
    return GLYPHS["full"] * full + GLYPHS["empty"] * (width - full)


def level(fraction: float) -> str:
    return "ok" if fraction < 0.6 else "warn" if fraction < 0.85 else "bad"


def rate_text(value) -> str:
    if value is None:
        return "-"
    if value < 1024:
        return "0"
    return f"{value / 1024:.0f}K/s" if value < MIB else client.fmt_size(value) + "/s"


def pct(values: dict, kind: str) -> str:
    """A pressure figure, or "-" when the kernel doesn't say (PSI off)."""
    return f"{values[kind]:.1f}%" if kind in values else "-"


def running_order(snap: dict, sort: str) -> list[dict]:
    """The running builds in the order the screen lists them."""
    builds = snap.get("builds", {})

    def use(row, name):
        return (builds.get(row["id"]) or {}).get(name) or 0

    keys = {"age": lambda r: -(r.get("elapsed") or 0), "cpu": lambda r: -use(r, "cores"),
            "mem": lambda r: -(use(r, "mem") or r.get("mem_now") or 0),
            "io": lambda r: -(use(r, "read_rate") + use(r, "write_rate")),
            "disk": lambda r: -(build_disk(snap, r)[0] or 0)}
    return sorted((snap.get("overview") or {}).get("running") or [], key=keys.get(sort, keys["age"]))


def folders_in(snap: dict, row: dict) -> list[str]:
    found = snap.get("folders", {}).get(row.get("id"))
    return found if found is not None else folders_of(row)


def build_disk(snap: dict, row: dict) -> tuple[int | None, str]:
    """What a build's folders take, and a mark: "" (measured), "≥" (only in part), "…" (not yet),
    "?" (couldn't be), "-" (no folders). The row adds "*" when another running build shares one."""
    folders = folders_in(snap, row)
    if not folders:
        return None, "-"
    measured = [snap.get("dir_sizes", {}).get(folder) for folder in folders]
    if all(found is None for found in measured):
        return None, GLYPHS["more"]  # not measured yet
    if all(found is None or found[0] is None for found in measured):
        return None, "?"  # couldn't be measured
    total = sum(found[0] for found in measured if found and found[0] is not None)
    partial = any(found is None or found[0] is None or not found[1] for found in measured)
    return total, GLYPHS["atleast"] if partial else ""


def shared_folders(snap: dict) -> set[str]:
    """Folders that another running build also writes to: the same folder, or one inside the other."""
    owned = [(row["id"], folder) for row in (snap.get("overview") or {}).get("running") or []
             for folder in folders_in(snap, row)]
    shared = set()
    for i, (a, fa) in enumerate(owned):
        for b, fb in owned[i + 1:]:
            if a != b and (fa == fb or fa.startswith(fb + os.sep) or fb.startswith(fa + os.sep)):
                shared.update((fa, fb))
    return shared


# ---- buildq top: panels on a canvas (docs/operations.md, "buildq top")
#
# The home screen is a control room: the CPU over the last half hour, memory as a ledger (where all of
# it goes: each running build's use and what is held for it to grow into, other work, the floor, the
# room left), the running builds, the waiting ones against that room, and the lanes. `t` shows the
# last half hour as a timeline. Everything is drawn on a Canvas of styled cells, so a line can mix
# colours; with no colour, the glyphs alone say what each part is.

BOX = {"tl": "╭", "tr": "╮", "bl": "╰", "br": "╯", "h": "─", "v": "│"}
TOP = {"braille": True, "used": "▓", "held": "▒", "other": "█", "floor": "░", "room": "■", "spark": "▁▂▃▄▅▆▇█", "core": "▮", "nocore": "▯",
       "wait": "▇", "dot": "·", "flag": "⚑", "next": "◀", "big": "█", "mid": "▓", "small": "▒"}
TOP_SPAN = 1800.0  # the graphs' half hour, in seconds


def use_ascii_top() -> None:
    BOX.update(tl="+", tr="+", bl="+", br="+", h="-", v="|")
    TOP.update(braille=False, used="#", held="=", other="%", floor=".", room="*", spark=" .:-=+*#", core="#", nocore=".",
               wait="=", dot=".",
               flag="!", next="<", big="#", mid="=", small="-")


class Canvas:
    """A screen of cells, each with a style. Panels draw into it; curses shows it a run of one style
    at a time, and `buildq top --once` prints it as plain text."""

    def __init__(self, width: int, height: int):
        self.width, self.height = max(1, width), max(1, height)
        self.chars = [[" "] * self.width for _ in range(self.height)]
        self.styles = [[""] * self.width for _ in range(self.height)]
        self.line_style = [""] * self.height  # what a line says as a whole (a build under pressure)

    def put(self, y: int, x: int, text: str, style: str = "", limit: int | None = None) -> int:
        """Write text at (y, x), clipped to the canvas and to `limit` cells; the cells used. A wide
        character takes two cells, and one that wouldn't fit whole isn't written."""
        if not 0 <= y < self.height or x >= self.width:
            return 0
        end = self.width if limit is None else min(self.width, x + max(0, limit))
        col = x
        for ch in clean(text):  # a label or a log line can hold anything
            w = text_width(ch)
            if w <= 0:
                continue
            if col + w > end:
                break
            if col >= 0:
                row = self.chars[y]
                if row[col] == "" and col > 0:  # over the right half of a wide character: its left goes too
                    row[col - 1] = " "
                if col + w < self.width and row[col + w] == "":  # over the left half: its right goes too
                    row[col + w] = " "
                row[col], self.styles[y][col] = ch, style
                if w == 2:
                    row[col + 1], self.styles[y][col + 1] = "", style
            col += w
        return col - x

    def spans(self, y: int, x: int, parts, limit: int | None = None) -> int:
        used = 0
        for text, style in parts:
            room = None if limit is None else limit - used
            if room is not None and room <= 0:
                break
            used += self.put(y, x + used, text, style, room)
        return used

    def box(self, y: int, x: int, h: int, w: int, title=(), style: str = "box") -> None:
        if h < 2 or w < 4:
            return
        self.put(y, x, BOX["tl"] + BOX["h"] * (w - 2) + BOX["tr"], style)
        for row in range(y + 1, y + h - 1):
            self.put(row, x, BOX["v"], style)
            self.put(row, x + w - 1, BOX["v"], style)
        self.put(y + h - 1, x, BOX["bl"] + BOX["h"] * (w - 2) + BOX["br"], style)
        parts = [(title, "title")] if isinstance(title, str) else list(title)
        if parts and w > 8:
            self.spans(y, x + 2, [(" ", style)] + parts + [(" ", style)], w - 4)

    def lines(self) -> list[str]:
        return ["".join(row).rstrip() for row in self.chars]

    def select(self, y: int, x0: int, x1: int) -> None:
        """Paint cells x0..x1 of row y as selected, as btop does a selected row: one bar across it,
        where a warning keeps its own colour on the bar."""
        for x in range(max(0, x0), min(self.width, x1)):
            st = self.styles[y][x]
            self.styles[y][x] = "sel_bad" if st == "bad" else "sel_warn" if st == "warn" else "sel"

    def runs(self, y: int) -> list[tuple[str, str]]:
        """Row y as (text, style) runs."""
        out: list[tuple[str, str]] = []
        for ch, st in zip(self.chars[y], self.styles[y]):
            if out and out[-1][1] == st:
                out[-1] = (out[-1][0] + ch, st)
            else:
                out.append((ch, st))
        return out


class TopHistory:
    """The machine over the last half hour, for the graphs: a sample per refresh and, before the first,
    the analytics' minutes (metrics.db), so the graphs aren't empty when top starts. Each sample is
    (time, busy cores, cores, memory in use, the builds' use, held for them to grow into, total)."""

    def __init__(self):
        self.samples: collections.deque = collections.deque()
        self.minutes: list = []  # the analytics' minutes before the first sample, at each minute's middle

    def add(self, snap: dict) -> None:
        m, ov = snap["machine"], snap.get("overview") or {}
        if m.get("busy") is None or not m.get("mem_total"):
            return
        builds = (snap["groups"].get("builds") or {}).get("mem") or 0
        held = (ov.get("budget") or {}).get("growth_reserved") or 0
        self.samples.append((snap["time"], m["busy"], m.get("cores") or 1,
                             m["mem_total"] - (m.get("mem_available") or 0), builds, held, m["mem_total"]))
        while self.samples and self.samples[0][0] < snap["time"] - TOP_SPAN:
            self.samples.popleft()

    def backfill(self, path: str, now: float) -> None:
        if self.samples or self.minutes or not os.path.exists(path):
            return
        try:
            import sqlite3
            import urllib.parse
            db = sqlite3.connect(f"file:{urllib.parse.quote(path)}?mode=ro", uri=True, timeout=1)
            try:
                cols = {r[1] for r in db.execute("PRAGMA table_info(minute)")}
                pick = lambda *names: next((n for n in names if n in cols), "NULL")  # noqa: E731
                # (each minute's average: the graph shows the average of each step, as btop does)
                rows = db.execute(f"SELECT t, {pick('cpu_busy')}, {pick('cores')}, {pick('mem_total')}, "
                                  f"{pick('mem_available')}, {pick('builds_mem_avg', 'builds_mem')}, {pick('growth')} "
                                  "FROM minute WHERE t >= ? ORDER BY t", (now - TOP_SPAN,)).fetchall()
            finally:
                db.close()
        except Exception:  # (an older or busy analytics file: the graphs fill as top runs)
            return
        for t, busy, cores, total, avail, used, held in rows:
            if None not in (t, busy, total, avail):
                self.minutes.append((t + 30, busy, cores or 1, total - avail, used or 0, held or 0, total))

    def buckets(self, field: int, n: int, now: float) -> list:
        """A sample's `field` in `n` steps over the half hour, oldest first: the average of the samples
        in each. The steps are fixed to the clock, not to now, so a finished step never changes and
        the graph moves one step at a time; the newest is the one still filling. Before the first
        sample, steps come from the analytics' minutes, drawn as a line between them (and on to the
        first sample) rather than as flat minutes. None where there is nothing to show."""
        if n <= 0:
            return []
        width = TOP_SPAN / n
        newest = int(now // width)
        first = newest - n + 1
        sums, counts = [0.0] * n, [0] * n
        for smp in self.samples:
            i = int(smp[0] // width) - first
            if 0 <= i < n:
                sums[i] += smp[field]
                counts[i] += 1
        out = [sums[i] / counts[i] if counts[i] else None for i in range(n)]
        live = [(smp[0], smp[field]) for smp in self.samples]
        if len(live) > 1:  # (samples further apart than a step: draw the line between them, not gaps)
            j = 0
            for i in range(n):
                mid = (first + i + 0.5) * width
                if out[i] is not None:
                    continue
                while j + 1 < len(live) and live[j + 1][0] < mid:
                    j += 1
                if j + 1 < len(live) and live[j][0] <= mid and live[j + 1][0] - live[j][0] <= 180:
                    (t0, v0), (t1, v1) = live[j], live[j + 1]
                    out[i] = v0 + (v1 - v0) * (mid - t0) / (t1 - t0) if t1 > t0 else v0
        points = [(m[0], m[field]) for m in self.minutes]
        if points and self.samples:
            points.append((self.samples[0][0], self.samples[0][field]))
        start = self.samples[0][0] if self.samples else now + width
        for i in range(n):
            mid = (first + i + 0.5) * width
            if out[i] is not None or mid >= start or not points:
                continue
            for (t0, v0), (t1, v1) in zip(points, points[1:]):
                if t0 <= mid <= t1 and t1 - t0 <= 180:  # (no line across a gap in the analytics)
                    out[i] = v0 + (v1 - v0) * (mid - t0) / (t1 - t0) if t1 > t0 else v0
                    break
        own: dict = {}  # a lone analytics minute (no neighbour to draw a line to) in its own step
        for t, v in points[:len(self.minutes)]:
            i = int(t // width) - first
            if 0 <= i < n and out[i] is None and t < start:
                own.setdefault(i, []).append(v)
        for i, vals in own.items():
            out[i] = sum(vals) / len(vals)
        return out


def finished_builds(path: str, now: float, skip=()) -> list[dict]:
    """The builds that ended in the last half hour, from the analytics, for the timeline."""
    if not os.path.exists(path):
        return []
    try:
        import sqlite3
        import urllib.parse
        db = sqlite3.connect(f"file:{urllib.parse.quote(path)}?mode=ro", uri=True, timeout=1)
        try:
            rows = db.execute("SELECT id, lane, submitted, ended, attempt_wall, wall, mem_peak, from_ci, state "
                              "FROM build WHERE ended >= ? ORDER BY submitted", (now - TOP_SPAN,)).fetchall()
        finally:
            db.close()
    except Exception:
        return []
    out = []
    for tid, lane, submitted, ended, attempt, wall, peak, ci, state in rows:
        if tid in skip or submitted is None or ended is None:
            continue
        ran = float(attempt if attempt is not None else (wall or 0))
        out.append({"short": tid[:8], "lane": lane or "", "submitted": float(submitted), "ended": float(ended),
                    "started": max(float(submitted), float(ended) - ran), "peak": peak or 0, "from_ci": bool(ci),
                    "state": state or ""})
    return out


BRAILLE_LEFT = (0, 0x40, 0x44, 0x46, 0x47)  # dots 7, 3, 2, 1: a left column of 0 to 4 dots, from the bottom
BRAILLE_RIGHT = (0, 0x80, 0xA0, 0xB0, 0xB8)  # dots 8, 6, 5, 4: the right column


def graph_dots(v, row: int, height: int, top: float, baseline: bool) -> int:
    """How many of a cell column's four dots value v fills in row `row` (0 at the bottom) of a graph
    `height` rows tall scaled to `top`; with a baseline, the bottom row always has one."""
    if v is None:
        return 0  # no data: blank, as btop draws it
    if not top:
        d = 0
    else:
        low, high = top * row / height, top * (row + 1) / height
        d = 4 if v >= high else 0 if v <= low else min(4, int((v - low) / (high - low) * 4 + 0.6))
    return max(d, 1) if baseline and row == 0 else d


def braille_rows(values: list, height: int, top: float, baseline: bool = True) -> list[str]:
    """Steps as `height` rows of braille, top row first, two steps to a cell (btop's graphs): the
    newest step is the right half of the last cell, so the graph moves half a cell at a time."""
    if len(values) % 2:
        values = [None] + list(values)
    rows = []
    for i in range(height):
        r = height - 1 - i
        rows.append("".join(chr(0x2800 + BRAILLE_LEFT[graph_dots(a, r, height, top, baseline)]
                                + BRAILLE_RIGHT[graph_dots(b, r, height, top, baseline)])
                            for a, b in zip(values[::2], values[1::2])))
    return rows


def graph_rows(hist: "TopHistory", field: int, cells: int, height: int, top: float, now: float) -> list[str]:
    """A graph `cells` wide: braille (two steps a cell), or in ASCII one step a cell."""
    if TOP["braille"]:
        return braille_rows(hist.buckets(field, 2 * cells, now), height, top)
    return spark_rows(hist.buckets(field, cells, now), height, top)


def gradient_style(row: int, height: int) -> str:
    """The CPU graph's colour for a row (0 at the bottom): green low, yellow, red at the top, by row as
    btop colours its graphs."""
    return f"cpu{round(row * 7 / max(1, height - 1))}"


def spark_rows(values: list, height: int, top: float) -> list[str]:
    """Columns of values as `height` rows of eighth-blocks, top row first."""
    glyphs = TOP["spark"]
    rows = []
    for r in range(height):
        below = (height - 1 - r) * 8
        line = []
        for v in values:
            fill = 0 if v is None else max(0.0, min(1.0, v / top if top else 0.0)) * height * 8 - below
            line.append(glyphs[-1] if fill >= 8 else " " if fill < 1 else glyphs[int(fill) - 1])
        rows.append("".join(line))
    return rows


def time_axis(width: int) -> str:
    """-30m … now, placed along `width` columns."""
    axis = [" "] * width
    for frac, label in ((0.0, "-30m"), (1 / 3, "-20m"), (2 / 3, "-10m"), (1.0, "now")):
        at = min(max(0, int(frac * width) - (len(label) if frac == 1.0 else 0)), width - len(label))
        if at >= 0 and all(c == " " for c in axis[max(0, at - 1):at + len(label)]):
            axis[at:at + len(label)] = label
    return "".join(axis)


def ledger_cells(parts: list[tuple[int, str, str]], total: int, width: int) -> list[tuple[str, str]]:
    """Parts of the machine's memory (bytes, glyph, style) as `width` cells, rounded so the parts
    together fill exactly what they add up to."""
    out, cum, drawn = [], 0, 0
    for size, glyph, style in parts:
        cum += max(0, size)
        cells = min(width, round(cum / max(total, 1) * width)) - drawn
        if cells > 0:
            out.append((glyph * cells, style))
            drawn += cells
    return out


def draw_cpu(c: Canvas, y: int, x: int, h: int, w: int, snap: dict, hist: TopHistory, now: float) -> None:
    m, ov, sep = snap["machine"], snap.get("overview") or {}, GLYPHS["sep"]
    cores = m.get("cores") or 1
    target = (ov.get("budget") or {}).get("cpu_cap") or cores
    busy = m.get("busy")
    goal = f"{target:.1f}".rstrip("0").rstrip(".")
    title = [("CPU", "title")] + ([(f" {sep} {busy:.1f} of {cores} busy {sep} target {goal} {sep} pressure "
                                    f"{pct(m.get('psi_cpu', {}), 'some')}", "")] if busy is not None else
                                  [(" (measuring)", "dim")])
    if busy is not None and len(f"CPU {sep} {busy:.1f} of {cores} busy {sep} target {goal} {sep} pressure 0.0%") > w - 6:
        title = [("CPU", "title"), (f" {sep} {busy:.1f} of {cores} busy {sep} target {goal}", "")]
    c.box(y, x, h, w, title, "box_cpu")
    ih, iw = h - 2, w - 2
    label = len(str(cores)) + 1
    gw, axis = iw - label - 1, ih >= 4
    gh = ih - (1 if axis else 0)
    if gw < 8 or gh < 1:
        return
    rows = graph_rows(hist, 1, gw, gh, cores, now)
    for r, line in enumerate(rows):
        mark = str(cores) if r == 0 else "0" if r == gh - 1 and gh > 1 else ""
        c.put(y + 1 + r, x + 1, mark.rjust(label - 1) + " ", "dim")
        c.put(y + 1 + r, x + 1 + label, line, gradient_style(gh - 1 - r, gh))
    if axis:
        c.put(y + 1 + gh, x + 1 + label, time_axis(gw), "dim")


def wrap_spans(parts: list, width: int) -> list[list]:
    """A line of (text, style) spans as lines of at most `width` cells, broken before a separator
    (" · "); a continued line is indented. A piece longer than a line is left for the panel to cut."""
    sep = f" {GLYPHS['sep']} "
    pieces = [((sep if i else "") + bit, style) for text, style in parts for i, bit in enumerate(text.split(sep))]
    lines: list[list] = []
    line, used = [], 0
    for text, style in pieces:
        if line and used + text_width(text) > width and text.startswith(sep):
            lines.append(line)
            text = "  " + text[len(sep):]
            line, used = [], 0
        line.append((text, style))
        used += text_width(text)
    return lines + ([line] if line else [])


def build_holds(snap: dict, running: list) -> dict:
    """Each running build's (memory it uses, memory held for it to grow into). The daemon says what
    it holds for each (a build past its peak may have been let go of); an older daemon doesn't, and
    then what it holds in all is shared out by the builds' plans."""
    budget = ((snap.get("overview") or {}).get("budget") or {})
    stats = snap.get("builds", {})
    used = {r["id"]: (stats.get(r["id"]) or {}).get("mem", r.get("mem_now") or 0) or 0 for r in running}
    if all("growth" in r for r in running):
        return {r["id"]: (used[r["id"]], max(0, r.get("growth") or 0)) for r in running}
    raw = {r["id"]: max(0, (r.get("est_mem") or 0) - used[r["id"]]) for r in running}
    total, held = sum(raw.values()), budget.get("growth_reserved") or 0
    return {k: (used[k], int(raw[k] * held / total) if total else 0) for k in raw}


def memory_panel(snap: dict, running: list, queued: list, w: int, holds: dict):
    """The MEMORY panel's title, its ledger bar and its lines (each a list of spans), for an inside
    width of w. The ledger has a place for all the machine's memory: each running build's use and
    what is held for it to grow into, other work, the floor and the room (where a new build starts)."""
    m, ov, sep = snap["machine"], snap.get("overview") or {}, GLYPHS["sep"]
    total, avail = m.get("mem_total") or 1, m.get("mem_available") or 0
    budget, cfg = ov.get("budget") or {}, ov.get("config") or {}
    floor = cfg.get("mem_floor") or 0
    room = budget.get("free_mem") or 0  # (below 0: more is held than is free)
    builds_used = sum(u for u, _ in holds.values())
    held_total = sum(h for _, h in holds.values())
    other = max(0, total - avail - builds_used)
    title = [("MEMORY", "title"), (f" {sep} {client.fmt_size(total - avail)} used", level(1 - avail / total)),
             (f" {sep} {client.fmt_size(avail)} free", "")]
    parts = [(size, glyph, style) for row in running
             for size, glyph, style in ((holds[row["id"]][0], TOP["used"], "mem"), (holds[row["id"]][1], TOP["held"], "held"))]
    parts += [(other, TOP["other"], "other"), (floor, TOP["floor"], "faint"), (max(room, 0), TOP["room"], "room")]
    bar_cells = ledger_cells(parts, total, w)
    lines = [[(f"builds {client.fmt_size(builds_used)} ", "mem"), (f"+{client.fmt_size(held_total)} held", "held"),
              (f" {sep} other {client.fmt_size(other)}", "other"), (f" {sep} floor {client.fmt_size(floor)}", "dim"),
              (f" {sep} room {client.fmt_size(room)}", "room") if room >= 0 else (f" {sep} over by {client.fmt_size(-room)}", "bad")],
             [(f"{TOP['used']} used  ", "mem"), (f"{TOP['held']} held for growth  ", "held"),
              (f"{TOP['other']} other work  ", "other"), (f"{TOP['floor']} floor  ", "dim"), (f"{TOP['room']} room", "room")]]
    if queued:
        line = [(f"{len(queued)} waiting", "warn")]
        for_mem = [r for r in queued if r.get("waiting_for") == "memory"]
        if for_mem:
            closest = min((r.get("est_mem") or 0) - max(room, 0) for r in for_mem)  # (over by: said beside the bar)
            line.append((f" {sep} {len(for_mem)} for memory"
                         + (f", the closest {client.fmt_size(closest)} short" if closest > 0 else ""), "dim"))
        in_line = sum(1 for r in queued if r.get("held"))
        if in_line:
            line.append((f" {sep} {in_line} in line, with room kept for them", "dim"))
        lines.append(line)
    pm = m.get("psi_memory", {})
    swap = (f"swap {client.fmt_size(m['swap_used'])} of {client.fmt_size(m['swap_total'])} {sep} " if m.get("swap_total") else "")
    lines.append([(f"{swap}pressure {pct(pm, 'some')} some, {pct(pm, 'full')} full", "dim")])
    groups, who = snap["groups"], []
    named = ("builds", "ci", "containers", "apps")
    for name, label in zip(named, ("builds", "CI", "containers", "apps")):
        g = groups.get(name)
        if g:
            who.append((f"{label} {g['cores']:.1f}c {client.fmt_size(g['mem'])}" if g.get("known") else f"{label} - {client.fmt_size(g['mem'])}")
                       + (f" +{client.fmt_size(g['swap'])} swap" if g["swap"] else ""))
    if m.get("busy") is not None and all(groups.get(n, {}).get("known") for n in named):
        who.append(f"other {max(0.0, m['busy'] - sum(groups[n]['cores'] for n in named if n in groups)):.1f}c")
    lines.append([("who: " + f" {sep} ".join(who + [f"pressure: io {pct(m.get('psi_io', {}), 'some')}"]), "dim")])
    b = groups.get("builds")
    if b and b.get("known"):
        bp = b.get("psi_mem") or {}
        lines.append([(f"builds together: memory pressure {pct(bp, 'some')} some, {pct(bp, 'full')} full {sep} read "
                       f"{rate_text(b.get('read_rate'))} {sep} write {rate_text(b.get('write_rate'))}", "dim")])
    for fs in m.get("filesystems") or []:
        bad = fs["floor"] and fs["free"] < fs["floor"]
        lines.append([(f"DISK {fs['mount']}: {client.fmt_size(fs['free'])} free of {client.fmt_size(fs['size'])}"
                       + (f" {sep} floor {client.fmt_size(fs['floor'])}" if fs["floor"] else "")
                       + f" {sep} read {rate_text(fs.get('read'))} {sep} write {rate_text(fs.get('write'))}",
                       "bad" if bad else "dim")])
    return title, bar_cells, [wrapped for line in lines for wrapped in wrap_spans(line, w)]


def draw_memory(c: Canvas, y: int, x: int, h: int, w: int, panel) -> None:
    title, bar_cells, lines = panel
    c.box(y, x, h, w, title, "box_mem")
    if h < 3 or w < 12:
        return
    c.spans(y + 1, x + 1, bar_cells, w - 2)
    for i, parts in enumerate(lines[:h - 3]):
        c.spans(y + 2 + i, x + 1, parts, w - 2)


def detail_lines(snap: dict, row: dict, shared: set) -> list[str]:
    """What the detail pane says about one running build."""
    s, sep = snap.get("builds", {}).get(row["id"]) or {}, GLYPHS["sep"]
    out = [f"command   {row.get('label') or ''}", f"in        {row.get('cwd')}",
           f"plan      {client.fmt_size(row.get('est_mem') or 0)} ({row.get('est_src') or '?'}); peak "
           f"{client.fmt_size(row.get('mem_peak') or 0)} at {client.fmt_age(row.get('peak_at') or 0)} in",
           f"cores     {row.get('granted')} given (wants {row.get('want')}, at least {row.get('need')}); "
           f"{'-' if s.get('cores') is None else format(s['cores'], '.1f')} in use, {row.get('peak_cores') or 0:.1f} at most"]
    if s:
        ev = s.get("events") or {}
        out.append(f"pressure  memory {pct(s.get('psi_mem') or {}, 'some')} some, {pct(s.get('psi_mem') or {}, 'full')} "
                   f"full {sep} cpu {pct(s.get('psi_cpu') or {}, 'some')} {sep} io {pct(s.get('psi_io') or {}, 'some')} "
                   f"{sep} over MemoryHigh {ev.get('high', 0)}x {sep} OOM kills {ev.get('oom_kill', 0)}")
    else:
        out.append("pressure  - (no cgroup of its own to read, or not visible from here)")
    waits = ", ".join(f"{k} {client.fmt_age(v)}" for k, v in sorted((row.get("waited_by") or {}).items(), key=lambda kv: -kv[1]))
    out.append(f"waited    {client.fmt_age(row.get('waited') or 0)}{f' ({waits})' if waits else ''}")
    for folder in folders_in(snap, row):
        found = snap.get("dir_sizes", {}).get(folder)
        said = ("measuring" if found is None else "couldn't be measured" if found[0] is None else
                ("at least " if not found[1] else "") + client.fmt_size(found[0]) + f", {client.fmt_age(found[2])} ago")
        out.append(f"writes    {folder}: {said}{' (shared with another build)' if folder in shared else ''}")
    if row.get("release_at") is not None:
        out.append(f"past its peak at {client.fmt_age(row['release_at'])} in: would keep {client.fmt_size(row.get('release_hold') or 0)}")
    return out


def draw_running(c: Canvas, y: int, x: int, h: int, w: int, snap: dict, running: list, selected: str | None,
                 narrow: bool, holds: dict) -> None:
    ov, sep = snap.get("overview") or {}, GLYPHS["sep"]
    jobs = (ov.get("config") or {}).get("max_jobs")
    c.box(y, x, h, w, [(f"{len(running)} running" + (f" of {jobs}" if jobs else ""), "title")], "box_proc")
    iy, ix, ih, iw = y + 1, x + 1, h - 2, w - 2
    if ih < 1:
        return
    builds, shared = snap.get("builds", {}), shared_folders(snap)
    core = [("TICKET", 9)] + ([] if narrow else [("LANE", 6), ("AGE", 6)]) + [("CORES used/given", 16),
                                                                            ("MEMORY now/peak/plan", 26)]
    optional = [("DISK", 8), ("MEMP", 6), ("READ/s", 8), ("WRITE/s", 8), ("SWAP", 7)]
    used_w = 2 + sum(cw + 1 for _, cw in core)
    shown = []
    for name, cw in optional:  # in order: the last ones go first
        if used_w + cw + 1 + 12 > iw:
            break
        shown.append((name, cw))
        used_w += cw + 1
    cmdw = max(0, iw - used_w - 1)

    def cell(text, cw, right=False):
        text = fit(clean(text), cw)
        pad = " " * (cw - text_width(text))
        return pad + text if right else text + pad

    header = "  " + " ".join(cell(n, cw, n == "AGE") for n, cw in core)
    header += "".join(" " + cell(n, cw, True) for n, cw in shown) + "  COMMAND"
    c.put(iy, ix, header, "dim", iw)
    order = [r["id"] for r in running]
    room = ih - 1
    if room <= 0:
        return
    start, slots = 0, len(running)
    if len(running) > room:
        slots = room - 1 if room >= 2 else room  # one line says what is above and below
        index = order.index(selected) if selected in order else 0
        start = min(max(0, index - slots + 1), len(running) - slots)
    for n, row in enumerate(running[start:start + slots]):
        ry = iy + 1 + n
        s = builds.get(row["id"]) or {}
        given = row.get("granted") or 0
        used = s.get("cores")
        filled = 0 if used is None else round(min(1.0, used / max(given, 1)) * 6)
        mem_now, growth = holds.get(row["id"], (s.get("mem", row.get("mem_now") or 0) or 0, 0))
        if row.get("released") is False:  # (nothing let go: the rest of its plan is held, whatever the samples'
            # timing; an older daemon doesn't say, and then its total, shared out, stands)
            growth = max(growth, (row.get("est_mem") or 0) - mem_now)
        peak, plan = max(row.get("mem_peak") or 0, mem_now), row.get("est_mem") or 0
        cells_now = round(min(1.0, mem_now / plan) * 8) if plan else 0  # its plan: ▓ used, ▒ held, · let go
        cells_held = max(0, round(min(1.0, (mem_now + growth) / plan) * 8) - cells_now) if plan else 0
        size, mark = build_disk(snap, row)
        disk = mark if size is None else (mark if mark == GLYPHS["atleast"] else "") + client.fmt_size(size) + (
            "*" if any(f in shared for f in folders_in(snap, row)) else "")
        psi = s.get("psi_mem") or {}
        values = {"DISK": disk, "MEMP": pct(psi, "some") if s else "-", "READ/s": rate_text(s.get("read_rate")),
                  "WRITE/s": rate_text(s.get("write_rate")), "SWAP": client.fmt_size(s.get("swap", 0)) if s else "-"}
        some = psi.get("some", 0.0)
        tone = "bad" if some >= 10 or row.get("frozen") else "warn" if some >= 1 or (plan and mem_now > plan) else ""
        c.line_style[ry] = tone
        chosen = row["id"] == selected
        parts = [(GLYPHS["mark"] if chosen else " ", ""), (" ", ""), (cell(row["short"], 9), tone)]
        if not narrow:
            parts += [(" " + cell(row["lane"], 6) + " " + cell(client.fmt_age(row.get("elapsed") or 0), 6, True), "")]
        parts += [(" ", ""), (TOP["core"] * filled, "cpu3"), (TOP["nocore"] * (6 - filled), "faint"),
                  (cell(f" {'-' if used is None else format(used, '4.1f')}/{given}", 10), ""),
                  (" ", ""), (TOP["used"] * cells_now, "mem"), (TOP["held"] * cells_held, "held"),
                  (TOP["dot"] * (8 - cells_now - cells_held), "faint"),
                  (cell(f" {client.fmt_size(mem_now)}/{client.fmt_size(peak)}/{client.fmt_size(plan)}", 18), "warn" if tone == "warn" else "")]
        parts += [(" " + cell(values[n], cw, True), "") for n, cw in shown]
        label = row.get("label") or ""
        parts += [("  ", ""), ("CI " if row.get("from_ci") else "", "ci"), (fit(clean(label), cmdw), "")]
        c.spans(ry, ix, parts, iw)
        if chosen:
            c.select(ry, ix, ix + iw)
    if len(running) > slots:
        c.put(iy + 1 + slots, ix, f"  {GLYPHS['more']} {start} above, {len(running) - start - slots} below (up/down)",
              "dim", iw)


def draw_waiting(c: Canvas, y: int, x: int, h: int, w: int, snap: dict, queued: list) -> None:
    """The waiting builds, in the order they get a turn. Each says what it needs and why it waits:
    what the free room lacks, or how many builds in line hold room that binds it (the daemon's
    count; an older daemon's: every one ahead)."""
    sep = GLYPHS["sep"]
    budget = ((snap.get("overview") or {}).get("budget") or {})
    room = budget.get("free_mem") or 0
    reasons = {row.get("waiting_for") or "" for row in queued}
    in_line = sum(1 for row in queued if row.get("held"))
    title = [(f"{len(queued)} waiting", "warn" if queued else "title")]
    if len(reasons) == 1 and queued and next(iter(reasons)):
        title.append((f" {sep} all for {next(iter(reasons))}", "dim"))
    if in_line:
        title.append((f" {sep} {in_line} in line", "dim"))
    c.box(y, x, h, w, title, "box_proc")
    iy, ix, ih, iw = y + 1, x + 1, h - 2, w - 2
    if ih < 1:
        return
    if not queued:
        c.put(iy, ix, "nothing waiting", "dim", iw)
        return
    longest = max((row.get("waited") or 0) for row in queued) or 1
    shown = queued[:ih]
    lane_w = min(12, max(text_width(clean(row["lane"])) for row in shown))
    ahead = 0  # builds in line before this one
    for n, row in enumerate(queued):
        if n >= len(shown):
            break
        waited, need = row.get("waited") or 0, row.get("est_mem") or 0
        cells = max(1, round(waited / longest * 10))
        parts = [(TOP["wait"] * cells + " " * (10 - cells), "warn" if row.get("held") else "dim"),
                 (" " + client.fmt_age(waited).rjust(6), ""), (" " + row["short"], "title"),
                 (" " + fit(row["lane"], lane_w).ljust(lane_w), "dim"), (f" needs {client.fmt_size(need):>6}", "")]
        if row.get("held"):  # room is kept for it: builds behind it wait
            parts.append((f" {TOP['flag']} in line", "warn"))
        if row.get("waiting_for") == "memory" and need > max(room, 0):
            parts.append((f" {client.fmt_size(need - max(room, 0))} short", "dim"))  # (what the free room lacks)
        elif row.get("waiting_for") == "held" and (row["behind"] if "behind" in row else ahead):
            # (the daemon says how many in line bind it, #62; an older one doesn't: every one ahead)
            parts.append((f" behind {row['behind'] if 'behind' in row else ahead} in line", "dim"))
        reason = clean(row.get("reason") or "")
        said = f"needs {client.fmt_size(need)} of memory; "  # (the column says that much already)
        reason = reason[len(said):] if reason.startswith(said) else reason
        if reason:
            parts.append(("  " + reason, "dim"))
        c.spans(iy + n, ix, parts, iw)
        ahead += 1 if row.get("held") else 0
    if len(shown) < len(queued):  # (in the border: every line inside shows a build)
        c.put(y + h - 1, x + 2, f" {GLYPHS['more']} {len(queued) - len(shown)} more waiting ", "dim", max(0, w - 4))


def draw_lanes(c: Canvas, y: int, x: int, h: int, w: int, snap: dict, running: list, queued: list) -> None:
    c.box(y, x, h, w, "LANES", "box_net")
    lanes: dict[str, list] = {}
    for row in running:
        lanes.setdefault(row["lane"], [0, 0, 0.0])[0] += 1
        lanes[row["lane"]][2] += (snap.get("builds", {}).get(row["id"]) or {}).get("cores") or 0
    for row in queued:
        lanes.setdefault(row["lane"], [0, 0, 0.0])[1] += 1
    for n, (name, (run, wait, cores)) in enumerate(sorted(lanes.items(), key=lambda kv: (-kv[1][0], -kv[1][1], kv[0]))):
        if n >= h - 2:
            break
        c.spans(y + 1 + n, x + 1, [(fit(name, 12).ljust(12), "title"), (f" {GLYPHS['mark']} {run:<2}", "ok" if run else "dim"),
                                    (f" {TOP['dot']} {wait} waiting", "warn" if wait else "dim"),
                                    (f" {GLYPHS['sep']} {cores:.1f} cores" if run else "", "dim")], w - 2)
    if not lanes:
        c.put(y + 1, x + 1, "nothing running", "dim", w - 2)


def draw_timeline(c: Canvas, y: int, x: int, h: int, w: int, snap: dict, hist: TopHistory, finished: list,
                  now: float) -> None:
    ov, sep = snap.get("overview") or {}, GLYPHS["sep"]
    c.box(y, x, h, w, [("TIMELINE", "title"), (f" {sep} the last 30 minutes", "")], "box_cpu")
    iy, ix, ih, iw = y + 1, x + 1, h - 2, w - 2
    label = 26
    gw = iw - label - 1
    if gw < 10 or ih < 6:
        c.put(iy, ix, "too small for the timeline", "dim", iw)
        return
    total = (snap["machine"].get("mem_total") or 1)
    for r, line in enumerate(graph_rows(hist, 3, gw, 3, total, now)):
        c.put(iy + r, ix, ("memory" if r == 1 else "").ljust(label), "dim")
        c.put(iy + r, ix + label + 1, line, "mem")
    c.put(iy + 3, ix + label + 1, time_axis(gw), "dim")
    live = []
    for row in ov.get("running") or []:
        start = row.get("started") or now - (row.get("elapsed") or 0)
        live.append({"short": row["short"], "lane": row["lane"], "submitted": row.get("submitted") or start,
                     "started": start, "ended": None, "peak": row.get("mem_peak") or 0, "from_ci": row.get("from_ci")})
    for row in ov.get("queued") or []:
        live.append({"short": row["short"], "lane": row["lane"], "submitted": row.get("submitted") or now - (row.get("waited") or 0),
                     "started": None, "ended": None, "peak": 0, "from_ci": row.get("from_ci"), "held": row.get("held")})
    room = ih - 5  # (the graph, its axis and the legend)
    done = sorted(finished, key=lambda r: r["submitted"])
    if len(live) > room:  # the running and waiting builds first; the most recently submitted of them
        live = sorted(live, key=lambda r: r["submitted"])[len(live) - room:]
    keep = done[max(0, len(done) - (room - len(live))):] if room > len(live) else []
    more = len(done) - len(keep) + (len(ov.get("running") or []) + len(ov.get("queued") or []) - len(live))
    rows = sorted(live + keep, key=lambda r: r["submitted"])
    col = lambda t: min(gw - 1, max(0, int((t - (now - TOP_SPAN)) / TOP_SPAN * gw)))  # noqa: E731
    big, mid = total / 8, total / 20  # (a darker bar for more memory: 16G and 6.4G on a 128G machine)
    for n, r in enumerate(rows):
        ry = iy + 4 + n
        c.spans(ry, ix, [("CI " if r.get("from_ci") else "   ", "ci"), (r["short"], ""), (" " + fit(r["lane"], 8), "dim")], label)
        line, styles = [" "] * gw, [""] * gw
        until = r["started"] if r["started"] is not None else now
        for i in range(col(r["submitted"]), col(until) + (0 if r["started"] is not None else 1)):
            line[i], styles[i] = TOP["dot"], "warn" if r.get("held") else "dim"
        if r["started"] is not None:
            peak = r.get("peak") or 0
            glyph = TOP["big"] if peak >= big else TOP["mid"] if peak >= mid else TOP["small"]
            state = r.get("state")
            tone = ("mem" if r["ended"] is None else "ok" if state == "succeeded" else "dim" if state == "cancelled"
                    else "bad")
            for i in range(col(r["started"]), col(r["ended"] if r["ended"] is not None else now) + 1):
                line[i], styles[i] = glyph, tone
        for i, (ch, st) in enumerate(zip(line, styles)):
            if ch != " ":
                c.put(ry, ix + label + 1 + i, ch, st)
    legend = [(f"{TOP['dot']} waiting  ", "dim"), (f"{TOP['big']} running  ", "mem"), (f"{TOP['big']} done  ", "ok"),
              (f"{TOP['big']} failed or lost  ", "bad"), (f"(darker {TOP['big']} {TOP['mid']} {TOP['small']}: more memory)", "dim")]
    if more:
        legend.insert(0, (f"{GLYPHS['more']} {more} not shown  ", "dim"))
    c.spans(iy + ih - 1, ix, legend, iw)


def draw_help(c: Canvas) -> None:
    lines = [("q  ?  t", "quit · this help (any key closes it) · the timeline"),
             ("up/down j/k", "select a running build; enter or d: its detail"),
             ("l  s", "the end of its output · sort: age, cpu, mem, io, disk"), ("", ""),
             (TOP["used"] + " " + TOP["held"], "memory a build uses · held for it to grow into"),
             (TOP["dot"], "in a build's row: its plan, let go after its peak"),
             (TOP["other"] + " " + TOP["floor"] + " " + TOP["room"], "other work · the floor · room for a new build"),
             (TOP["core"] + TOP["nocore"], "cores in use / given"),
             (TOP["flag"] + " in line", "room is kept for it (a long wait, or a CI job's build)"),
             ("short", "what the free room lacks for it"),
             ("behind N", "its room is free but kept for N in line ahead")]
    w = min(c.width - 4, 78)
    h = min(c.height - 2, len(lines) + 2)
    y, x = max(0, (c.height - h) // 2), max(0, (c.width - w) // 2)
    for row in range(y, y + h):
        c.put(row, x, " " * w)
    c.box(y, x, h, w, "HELP", "title")
    for n, (key, text) in enumerate(lines[:h - 2]):
        c.spans(y + 1 + n, x + 2, [(key.ljust(14), "title"), (text, "")], w - 4)


def render_screen(snap: dict, width: int, height: int, *, view: str = "main", selected: str | None = None,
                  sort: str = "age", detail: bool = False, log_lines: list[str] | None = None,
                  hist: TopHistory | None = None, finished: list | None = None, show_help: bool = False,
                  stale: bool = False, answered_at: float | None = None) -> tuple[Canvas, list[str]]:
    """The screen (height 0: as tall as everything needs) and the running builds' ids in screen order.
    On a fixed height, room goes first to one running build and the waiting panel's box, then to an
    open detail pane (up to half of what is left), the machine's panels, the rest of the pane, the
    rest of the running builds, and the waiting list."""
    hist = hist if hist is not None else TopHistory()
    now = snap["time"]
    ov, m, sep = snap.get("overview"), snap["machine"], GLYPHS["sep"]
    running = running_order(snap, sort)
    order = [row["id"] for row in running]
    if selected not in order:
        selected = order[0] if order else None
    queued = list((ov or {}).get("queued") or [])
    holds = build_holds(snap, running)
    info: list[str] = []
    if detail and selected:
        row = next(r for r in running if r["id"] == selected)
        info = detail_lines(snap, row, shared_folders(snap))
    log = [f"the end of {selected[:8]}'s output:"] + list(log_lines) if log_lines is not None and selected else []
    pane = info + log
    natural = not height
    wide, narrow = width >= 110, width < 80
    cpu_w = width * 2 // 5 if wide else 0
    panel = memory_panel(snap, running, queued, width - cpu_w - 2, holds)
    top_need = max(7, len(panel[2]) + 3)
    run_need = 3 + len(running)
    wait_need = len(queued) + 2 if queued else 3
    pane_want = len(pane) + 2 if pane else 0
    if natural:
        top_h, pane_h, run_h, bottom_h = top_need, pane_want, run_need, wait_need
        height = 1 + top_h + run_h + pane_h + bottom_h + 1
    else:
        run_min = 3 if not running else 4 if len(running) == 1 else 5  # (a build, and what is above and below)
        rest = height - 2 - run_min - 3
        pane_h = min(pane_want, max(0, rest) // 2)
        rest -= pane_h
        top_h = min(top_need, rest) if height >= 18 else 0
        top_h = top_h if top_h >= 7 else 0  # (a panel too short to say anything)
        rest -= top_h
        more = min(pane_want - pane_h, rest)
        pane_h, rest = pane_h + more, rest - more
        if pane_h < 3:  # (no room for a line inside its box)
            rest, pane_h = rest + pane_h, 0
        run_h = run_min + min(rest, max(0, run_need - run_min))
        rest -= run_h - run_min
        bottom_h = 3 + max(0, rest)  # the waiting list, and anything left over
        if pane and len(pane) > pane_h - 2 and log:  # keep the newest log lines, and as much detail as fits
            inside = pane_h - 2
            keep = max(0, inside - min(len(log), max(2, inside // 2)))
            pane = info[:keep] + log[:1] + log[1:][-(inside - keep - 1):] if inside - keep > 1 else info[:inside]
    c = Canvas(width, height)
    stamp = time.strftime("%H:%M:%S", time.localtime(now))
    state = ("daemon not answering" if ov is None else
             f"PAUSED: {(ov.get('paused') or {}).get('reason') or ''}" if ov.get("paused") else f"buildqd {ov.get('version', '')}")
    head = [("buildq top", "title"), (f"  {stamp}  ", ""), (state, "bad" if ov is None or ov.get("paused") else "dim")]
    if not (wide and top_h) and m.get("busy") is not None:
        cores = m.get("cores") or 1
        head += [(f"   CPU {bar(m['busy'] / cores, 10)} {m['busy']:.1f} of {cores} busy", level(m["busy"] / cores))]
    if not top_h and m.get("mem_total"):
        room = ((ov or {}).get("budget") or {}).get("free_mem") or 0
        head += [(f"   MEM {client.fmt_size(m['mem_total'] - (m.get('mem_available') or 0))} used {sep} "
                  + (f"room {client.fmt_size(room)}" if room >= 0 else f"over by {client.fmt_size(-room)}"), "")]
    c.spans(0, 0, head, width)
    if width < 50 or height < 12:
        c.put(1, 0, f"buildq top needs at least 50x12 (this is {width}x{height})", "warn")
        for n, row in enumerate(running[:max(0, height - 3)]):
            c.put(2 + n, 0, f"{row['short']} {row['lane']} {client.fmt_size(row.get('mem_now') or 0)}/{client.fmt_size(row.get('est_mem') or 0)}")
        return c, order
    if view == "timeline":
        draw_timeline(c, 1, 0, height - 2, width, snap, hist, finished or [], now)
    else:
        if top_h and wide:
            draw_cpu(c, 1, 0, top_h, cpu_w, snap, hist, now)
        if top_h:
            draw_memory(c, 1, cpu_w, top_h, width - cpu_w, panel)
        run_y = 1 + top_h
        draw_running(c, run_y, 0, run_h, width, snap, running, selected, narrow, holds)
        if pane_h:
            py = run_y + run_h
            row = next((r for r in running if r["id"] == selected), {})
            c.box(py, 0, pane_h, width, [(f"{selected} ", "title"), (fit(clean(row.get("name") or ""), width - 50), "dim")],
                  "box_proc")
            for n, text in enumerate(pane[:pane_h - 2]):
                c.put(py + 1 + n, 1, "  " + text, "", width - 2)
        by = 1 + top_h + run_h + pane_h
        if wide:
            lanes_w = 46
            draw_waiting(c, by, 0, bottom_h, width - lanes_w, snap, queued)
            draw_lanes(c, by, width - lanes_w, bottom_h, lanes_w, snap, running, queued)
        else:
            draw_waiting(c, by, 0, bottom_h, width, snap, queued)
    age = max(0.0, time.time() - (answered_at if answered_at is not None else now))
    when = f"updated {client.fmt_age(age)} ago" if age >= 1 else "updated now"
    items = ["q quit", "? help", "t timeline", "up/down select", "enter detail", f"s sort ({sort})", "l log"]
    while len(items) > 2 and len(f" {sep} ".join(items)) > width - len(when) - 3:
        items.pop()
    keys = f" {sep} ".join(items)
    c.put(height - 1, 0, keys, "dim", max(0, width - len(when) - 2))
    c.put(height - 1, max(0, width - len(when) - 1), when, "warn" if stale else "dim")
    if show_help:
        draw_help(c)
    return c, order


def render_top(snap: dict, width: int, height: int, selected: str | None = None, sort: str = "age",
               detail: bool = False, log_lines: list[str] | None = None, **kw) -> tuple[list[tuple[str, str]], list[str]]:
    """The screen as lines of (text, colour) and the running builds' ids in screen order (height 0:
    as tall as everything needs). A line's colour is what it says as a whole: "bad" for a build under
    memory pressure, "warn" for one past its plan."""
    c, order = render_screen(snap, width, height, selected=selected, sort=sort, detail=detail, log_lines=log_lines, **kw)
    lines = [(text, c.line_style[i]) for i, text in enumerate(c.lines())]
    if not height:
        while lines and not lines[-1][0]:
            lines.pop()
    return lines, order


def tail_lines(path, count: int = 12) -> list[str]:
    if not path:
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 16384))
            return [clean(line) for line in f.read().decode(errors="replace").splitlines()[-count:]]
    except OSError:
        return ["(its output can't be read)"]




def print_top_once(sampler: TopSampler) -> int:
    """One snapshot as text: two looks a second apart (rates need two), folder sizes measured
    first for up to 20 seconds in all (a folder not done by then shows as not measured yet)."""
    overview = client.request({"op": "status"}, 3)
    if sampler.sizes is not None and overview is not None:
        deadline = time.monotonic() + 20
        for row in overview.get("running") or []:
            for folder in folders_of(row):
                left = deadline - time.monotonic()
                if left > 1 and sampler.sizes.get(folder) is None:
                    sampler.sizes.measure_into(folder, left, keep_cut=False)
    sampler.snapshot(client.request({"op": "status"}, 3))
    time.sleep(1.0)
    snap = sampler.snapshot(client.request({"op": "status"}, 3))
    if snap["overview"] is None:
        client.unreachable()
    width = shutil.get_terminal_size((220, 24)).columns if sys.stdout.isatty() else 220
    encoding = sys.stdout.encoding or "utf-8"
    hist = TopHistory()  # the CPU graph's half hour, from the analytics
    hist.backfill(os.path.join(client.state_dir(), "metrics.db"), snap["time"])
    hist.add(snap)
    for text, _ in render_top(snap, width, 0, hist=hist)[0]:
        print(text.encode(encoding, "replace").decode(encoding, "replace"))
    return 0 if snap["overview"] is not None else 75


def wants_ascii(encoding: str | None) -> bool:
    """Whether to draw with ASCII: the terminal's encoding (the curses window's own, or the locale's
    codeset) isn't UTF-8. Python's UTF-8 mode says utf-8 under LC_ALL=C while curses writes ASCII,
    and ncurses then draws block characters as spaces without an error."""
    return "utf8" not in (encoding or "ascii").lower().replace("-", "").replace("_", "")


def colour_wanted(env=None) -> bool:
    """Colour unless NO_COLOR is set to anything (no-color.org)."""
    return not (env if env is not None else os.environ).get("NO_COLOR")


# btop's Default theme (aristocratos/btop, src/btop_theme.cpp, Apache-2.0), as xterm-256 colours:
# indexes 16 to 255 are the same on every terminal, unlike 0 to 15, which its theme sets. Readable text
# is a light grey (btop's main_fg); the dim greys are for decoration only (graph labels, empty cells),
# never for something to read. Built for a dark background, as btop is. Each style: (colour, bold).
TOP_COLOURS_256 = {
    "head": (255, True), "title": (255, True), "ok": (48, False), "warn": (220, False), "bad": (167, True),
    "dim": (245, False), "faint": (241, False), "box": (102, False), "box_cpu": (102, False),
    "box_mem": (101, False), "box_proc": (138, False), "box_net": (103, False),
    "mem": (193, False), "held": (222, False), "other": (182, False), "room": (123, True), "ci": (146, True),
    "cpu0": (151, False), "cpu1": (151, False), "cpu2": (186, False), "cpu3": (186, False),
    "cpu4": (180, False), "cpu5": (180, False), "cpu6": (174, False), "cpu7": (167, False),
}
SELECTED_BG_256 = 52  # btop's selected_bg (#6a2f2f), darker for contrast: white on it reads at 12:1


def init_colours(curses, wanted: bool) -> dict:
    """The colour (and bold) for each kind of text. The terminal's own background stays
    (use_default_colors): curses.wrapper has started colour, and without that call the screen would
    turn white on black. On a 256-colour terminal, btop's Default theme; on 8 colours, its TTY theme's
    choices. With NO_COLOR, only bold, and the selected row in reverse video."""
    rev, bold = curses.A_REVERSE, curses.A_BOLD
    plain = {"": 0, "head": bold, "title": bold, "sel": rev, "sel_bad": rev | bold, "sel_warn": rev | bold}
    colours = dict(plain)
    try:
        if not curses.has_colors():
            return colours
        curses.use_default_colors()
        if not wanted:
            return colours
        pairs: dict = {}
        limit = getattr(curses, "COLOR_PAIRS", 64) - 1  # (pair 0 is the terminal's own)

        def pair(fg: int, bg: int = -1) -> int:
            if (fg, bg) not in pairs:
                n = len(pairs) + 1
                curses.init_pair(n, fg, bg)
                pairs[(fg, bg)] = curses.color_pair(n)
            return pairs[(fg, bg)]
        rich = len({fg for fg, _ in TOP_COLOURS_256.values()}) + 3  # the pairs btop's theme takes
        if limit < 9:  # not even the eight colours' pairs: plain text
            return plain
        palette = getattr(curses, "COLORS", 8)
        try:
            direct = getattr(curses, "tigetflag", lambda name: -1)("RGB") > 0
        except curses.error:
            direct = False
        if 256 <= palette < 0x1000000 and not direct and limit >= rich:  # (direct colour: numbers are RGB)
            for style, (fg, strong) in TOP_COLOURS_256.items():
                colours[style] = pair(fg) | (bold if strong else 0)
            colours["sel"] = pair(255, SELECTED_BG_256) | bold
            colours["sel_bad"] = pair(210, SELECTED_BG_256) | bold
            colours["sel_warn"] = pair(222, SELECTED_BG_256) | bold
        else:  # btop's TTY theme: the terminal's own eight, nothing read in blue
            white, green, yellow, red, magenta, cyan = (curses.COLOR_WHITE, curses.COLOR_GREEN, curses.COLOR_YELLOW,
                                                        curses.COLOR_RED, curses.COLOR_MAGENTA, curses.COLOR_CYAN)
            dim = curses.A_DIM
            colours.update(head=pair(white) | bold, title=pair(white) | bold, ok=pair(green), warn=pair(yellow),
                           bad=pair(red) | bold, dim=0, faint=dim, box=dim, box_cpu=pair(green) | dim,
                           box_mem=pair(yellow) | dim, box_proc=pair(red) | dim, box_net=pair(magenta) | dim,
                           mem=pair(green), held=pair(yellow), other=pair(magenta), room=pair(cyan) | bold,
                           ci=pair(magenta) | bold, sel=pair(white, curses.COLOR_BLUE) | bold,
                           # (red or yellow on blue reads at 1:1 on some palettes: the warning is the cell's
                           # background instead)
                           sel_bad=pair(white, red) | bold, sel_warn=pair(curses.COLOR_BLACK, yellow))
            for i in range(8):
                colours[f"cpu{i}"] = pair(green if i < 3 else yellow if i < 6 else red)
    except (curses.error, ValueError):  # (ValueError: a pair number past what the terminal has)
        return plain  # no default colours on this terminal: plain text
    return colours


TERM_FALLBACK = "xterm-256color"


def curses_with_fallback(run, wrapper, env=None) -> tuple[int | None, str | None]:
    """Run a curses program; if this machine has no terminfo for the terminal's TERM (a newer
    terminal, such as Ghostty or kitty, over SSH), try once more as TERM_FALLBACK. Returns (the
    program's exit code, or None if no terminal works) and a note for after the screen is gone."""
    import curses
    env = os.environ if env is None else env
    term = env.get("TERM", "")
    started = []

    def guarded(screen):
        started.append(True)  # past initscr: a curses.error from here on is the program's, not the terminal's
        return run(screen)
    try:
        return wrapper(guarded), None
    except curses.error as e:
        if isinstance(e.__context__, (SystemExit, KeyboardInterrupt)):
            raise e.__context__ from None  # the terminal went away (SIGHUP): curses' own teardown failed on it
        if started:
            raise
        if not term or term in ("dumb", TERM_FALLBACK):
            return None, f"no usable terminal (TERM={term or 'unset'}: {e}); one snapshot instead"
    env["TERM"] = TERM_FALLBACK
    why = (f"this machine has no terminfo for TERM={term}, so `buildq top` used {TERM_FALLBACK}. To use "
           f"{term} itself, install its entry here (Fedora's ncurses-term package has many; or, on the "
           f"machine you connect from, `infocmp -x {term} | ssh <this machine> -- tic -x -`), or set "
           f"TERM={TERM_FALLBACK} for SSH to this machine")
    try:
        return wrapper(guarded), why
    except curses.error as e:
        if isinstance(e.__context__, (SystemExit, KeyboardInterrupt)):
            raise e.__context__ from None
        if started:
            raise
        return None, f"no usable terminal (TERM={term}, and {TERM_FALLBACK}: {e}); one snapshot instead"


class StatusFetcher:
    """Asks the daemon for its status every interval on a thread of its own, so the screen never
    waits on it: keys and redraws go on while the daemon is slow or down."""

    def __init__(self, interval: float, ask=None):
        import threading
        self.interval = interval
        self.ask = ask or (lambda: client.request({"op": "status"}, 1.5, connect_timeout=0.5))
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stopping = False
        self.status, self.version = None, 0
        self.answered = threading.Event()  # the first request has come back (with an answer or not)
        self.thread = threading.Thread(target=self.loop, name="status", daemon=True)

    def start(self) -> "StatusFetcher":
        self.thread.start()
        return self

    def loop(self) -> None:
        while not self.stopping:
            try:
                status = self.ask()
            except Exception:  # (request() catches what it expects; anything else mustn't freeze the screen)
                status = None
            with self.lock:
                self.status, self.version = status, self.version + 1
            self.answered.set()
            self.wake.wait(self.interval)  # (after the request: a slow daemon isn't asked again at once)
            self.wake.clear()

    def latest(self) -> tuple[int, dict | None]:
        with self.lock:
            return self.version, self.status

    def stop(self) -> None:
        self.stopping = True
        self.wake.set()


def exit_on_signals() -> None:
    """SIGTERM and SIGHUP (the terminal or SSH session gone) end the program as an exception, so the
    screen is restored and what it started is stopped; ncurses' own handler would exit at once."""
    def stop(signum, frame):
        raise SystemExit(128 + signum)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, stop)


def cmd_top(args) -> int:
    """A live view of the machine and each build, refreshed every --interval seconds."""
    import locale
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:  # a locale named in the environment but not installed
        pass
    # One snapshot: when asked, into a pipe, or with no terminal to draw on (TERM unset, empty or dumb,
    # where curses would start and fill the screen with spaces).
    if args.once or not sys.stdout.isatty() or os.environ.get("TERM", "") in ("", "dumb"):
        if wants_ascii(locale.nl_langinfo(locale.CODESET)):
            use_ascii()
        sampler = TopSampler(DirSizes(background=False))
        try:
            return print_top_once(sampler)
        finally:
            sampler.sizes.stop()
    import curses
    exit_on_signals()
    sizes = DirSizes()
    fetcher = StatusFetcher(args.interval)

    def run(screen) -> int:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        encoding = getattr(screen, "encoding", None) or "ascii"  # what curses really writes

        def sync(begin: bool) -> None:
            """Synchronized output (DEC mode 2026, as btop wraps each frame): the terminal shows the
            frame whole; one that doesn't know the mode ignores it."""
            try:
                sys.stdout.write("\x1b[?2026h" if begin else "\x1b[?2026l")
                sys.stdout.flush()
            except (OSError, ValueError):
                pass
        if wants_ascii(encoding):
            use_ascii()
        screen.timeout(100)
        colours = init_colours(curses, colour_wanted())
        sampler = TopSampler(sizes)
        sorts = ["age", "cpu", "mem", "io", "disk"]
        sort, selected, detail, logs, view, show_help = "age", None, False, False, "main", False
        snap, seen, order, dirty = None, -1, [], True
        hist, finished, finished_at, answered_at, drawn_at = TopHistory(), [], 0.0, None, 0.0
        hist.backfill(os.path.join(client.state_dir(), "metrics.db"), time.time())
        if not fetcher.thread.is_alive():
            fetcher.start()
        fetcher.answered.wait(2.0)  # the first answer, if it comes soon (else "not answering" until it does)
        while True:
            version, status = fetcher.latest()
            if snap is None or version != seen:
                snap, seen, dirty = sampler.snapshot(status), version, True
                hist.add(snap)
                if snap["overview"] is not None:
                    answered_at = snap["time"]
            if time.monotonic() - drawn_at >= 1.0:
                dirty = True  # ("updated … ago" counts on, and turns yellow once the daemon is late)
            if view == "timeline" and time.monotonic() - finished_at > 10:
                live = {r["id"] for r in ((snap["overview"] or {}).get("running") or []) + ((snap["overview"] or {}).get("queued") or [])}
                finished, finished_at, dirty = finished_builds(os.path.join(client.state_dir(), "metrics.db"), snap["time"], live), time.monotonic(), True
            if dirty:
                height, width = screen.getmaxyx()
                row = next((r for r in (snap["overview"] or {}).get("running") or [] if r["id"] == selected), None)
                log = tail_lines(row.get("log")) if logs and row else None
                canvas, order = render_screen(snap, max(width, 20), max(height, 5), view=view, selected=selected, sort=sort,
                                              detail=detail, log_lines=log, hist=hist, finished=finished,
                                              show_help=show_help, answered_at=answered_at,
                                              stale=answered_at is not None and time.time() - answered_at > 3 * args.interval + 2)
                if order and selected not in order:
                    selected = order[0]  # (kept while the daemon doesn't answer: it comes back to the same build)
                screen.erase()
                for y in range(min(canvas.height, height)):
                    x = 0
                    for text, style in canvas.runs(y):
                        if y == height - 1 and x + text_width(text) >= width:
                            text = fit(text, max(0, width - 1 - x))  # (curses can't write the bottom-right cell)
                        try:
                            screen.addstr(y, x, text.encode(encoding, "replace").decode(encoding, "replace"),
                                          colours.get(style, 0))
                        except (curses.error, UnicodeError):
                            pass
                        x += text_width(text)
                screen.noutrefresh()
                sync(True)
                try:
                    curses.doupdate()
                finally:
                    sync(False)  # (or the terminal waits out its timeout for the frame's end)
                dirty, drawn_at = False, time.monotonic()
            key = screen.getch()
            if key == -1:
                continue
            dirty = True
            if key in (ord("q"), ord("Q")):
                return 0
            if show_help and key != curses.KEY_RESIZE:
                show_help = False  # any key closes the help
                continue
            if key == ord("s"):
                sort = sorts[(sorts.index(sort) + 1) % len(sorts)]
            elif key in (curses.KEY_DOWN, ord("j")) and order:
                selected = order[min(order.index(selected) + 1 if selected in order else 0, len(order) - 1)]
            elif key in (curses.KEY_UP, ord("k")) and order:
                selected = order[max(order.index(selected) - 1 if selected in order else 0, 0)]
            elif key in (10, 13, curses.KEY_ENTER, ord("d")):
                detail = not detail
            elif key == ord("l"):
                logs = not logs
            elif key == ord("t"):
                view, finished_at = ("timeline" if view == "main" else "main"), 0.0
            elif key == ord("?"):
                show_help = True
            # KEY_RESIZE: redrawn at the new size, without taking a new sample

    why = None
    try:
        try:
            code, why = curses_with_fallback(run, curses.wrapper)
        finally:  # (synchronized output off, whatever ended the session: Ctrl-C and signals too)
            try:
                if sys.stdout.isatty():
                    sys.stdout.write("\x1b[?2026l")
                    sys.stdout.flush()
            except (OSError, ValueError):
                pass
        if code is not None:
            return code
        sizes.stop()
        if wants_ascii(locale.nl_langinfo(locale.CODESET)):
            use_ascii()
        once = TopSampler(DirSizes(background=False))  # no usable terminal: one snapshot instead
        try:
            return print_top_once(once)
        finally:
            once.sizes.stop()
    except KeyboardInterrupt:
        return 0
    finally:
        fetcher.stop()
        sizes.stop()
        if why:
            client.note(why)

