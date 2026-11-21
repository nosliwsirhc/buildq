# SPDX-License-Identifier: MIT
"""Build history persistence (history.jsonl) and resource estimation."""

from __future__ import annotations

import collections
import json
import math
import os

from .common import MIB
from .config import Config, Lane

class Estimate:
    def __init__(self, want: int, need: int, mem: int, source: str, rigid: bool, wall: float | None = None,
                 mem_cores: int = 1):
        self.want, self.need, self.mem, self.source, self.rigid, self.wall = want, need, mem, source, rigid, wall
        self.mem_cores = mem_cores  # the cores the build had when it needed `mem`


class History:
    KEEP = 100  # recent records per command
    PEAK_DAYS = 400  # each day's biggest run is kept this long, so a full rebuild isn't forgotten

    def __init__(self, path: str, max_keys: int = 2000):
        self.path = path
        self.max_keys = max_keys
        self.by_key: collections.OrderedDict[str, collections.deque] = collections.OrderedDict()
        self.peaks: dict[str, dict[int, dict]] = {}  # key -> day -> that day's biggest run
        self.lines = 0
        try:
            with open(path) as f:
                for line in f:
                    record = self.valid(line)
                    if record:
                        self._remember(record)
                    self.lines += 1
        except FileNotFoundError:
            pass
        if self.lines > self._bound():
            self.compact()

    @staticmethod
    def valid(line: str) -> dict | None:
        try:
            r = json.loads(line)
            if not isinstance(r, dict) or not isinstance(r.get("key"), str):
                return None
            r["peak_mem"] = int(r["peak_mem"])
            r["peak_cores"] = float(r["peak_cores"])
            r["t"] = float(r.get("t", 0))
            for name in ("wall", "granted", "avg_cores", "mem_cores", "peak_at", "tail", "peak_cpu"):
                if r.get(name) is not None:
                    r[name] = float(r[name])
                    if not math.isfinite(r[name]) or r[name] < 0:
                        return None
            if r["peak_mem"] < 0 or not math.isfinite(r["peak_cores"]):
                return None
            return r
        except (ValueError, KeyError, TypeError, OverflowError):
            return None

    def _remember(self, record: dict) -> None:
        key = record["key"]
        records = self.by_key.pop(key, None) or collections.deque(maxlen=self.KEEP)
        records.append(record)
        self.by_key[key] = records  # most recently used last
        day = int(record["t"] // 86400)
        peaks = self.peaks.setdefault(key, {})
        if day not in peaks or record["peak_mem"] > peaks[day]["peak_mem"]:
            peaks[day] = record
        for old in [d for d in peaks if d < day - self.PEAK_DAYS]:
            del peaks[old]
        while len(self.by_key) > self.max_keys:
            gone, _ = self.by_key.popitem(last=False)
            self.peaks.pop(gone, None)

    def _retained(self, key: str) -> list[dict]:
        recent = list(self.by_key.get(key, ()))
        seen = {id(r) for r in recent}
        return [r for r in self.peaks.get(key, {}).values() if id(r) not in seen] + recent

    def _bound(self) -> int:
        return 1000 + 2 * sum(len(self._retained(k)) for k in self.by_key)

    def add(self, record: dict) -> None:
        self._remember(record)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "a") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
        self.lines += 1
        if self.lines > self._bound():
            self.compact()

    def compact(self) -> None:
        tmp = f"{self.path}.tmp"
        with open(tmp, "w") as f:
            for key in self.by_key:
                for record in sorted(self._retained(key), key=lambda r: r["t"]):
                    f.write(json.dumps(record, separators=(",", ":")) + "\n")
        os.replace(tmp, self.path)
        self.lines = sum(len(self._retained(k)) for k in self.by_key)

    @staticmethod
    def shaped(r: dict) -> bool:
        """A run that says when it peaked and what it used after: it succeeded, wasn't killed for
        memory, ran a minute or more, and its peak came before its last minute and last tenth."""
        return (r.get("exit") == 0 and not r.get("oom") and not r.get("mem_high") and r.get("peak_at") is not None
                and r.get("tail") is not None and r.get("peak_cpu") is not None and (r.get("wall") or 0) >= 60
                and r["wall"] - r["peak_at"] >= max(60.0, 0.1 * r["wall"]))

    def after_peak(self, key: str, cfg: Config, now: float, need: int = 3) -> tuple[float, float, int, int, float] | None:
        """For "past the peak": from the same runs the estimate reads (the last history_runs, and
        the biggest within history_days), the latest any of them peaked (seconds in), the most any
        used after its peak, the peak behind the estimate, and the cores that peak was measured
        with, and the most CPU-seconds any had used by its peak. None unless the run behind the
        estimate and `need` recent runs are `shaped`, no recent successful run peaked at its end
        (such a command's runs say nothing about after a peak), and no run in the window was killed
        for memory (the clearest sign that a later phase can be bigger). Failed runs teach nothing
        here either: a failure may have skipped a later, bigger phase."""
        cutoff = now - cfg.history_days * 86400
        recent = [r for r in self.by_key.get(key, ()) if r["t"] >= cutoff][-cfg.history_runs:]
        window = [r for r in self._retained(key) if r["t"] >= cutoff] or recent
        if not window:
            return None
        top = max(window, key=lambda r: r["peak_mem"])
        known = [r for r in recent if self.shaped(r)]
        ended_at_peak = [r for r in recent if r.get("exit") == 0 and not r.get("oom") and not self.shaped(r)
                         and r.get("peak_at") is not None and (r.get("wall") or 0) >= 60]
        if len(known) < need or not self.shaped(top) or ended_at_peak or any(r.get("oom") for r in window):
            return None
        runs = known + ([top] if all(r is not top for r in known) else [])
        cores = int(top.get("mem_cores") or top.get("granted") or 0)
        return (max(r["peak_at"] for r in runs), max(r["tail"] for r in runs), int(top["peak_mem"]), cores,
                max(r["peak_cpu"] for r in runs))

    def estimate(self, key: str, lane: Lane, cfg: Config, ncpu: int, cap_enforced: bool, cold: bool,
                 now: float) -> Estimate:
        """What the next run of a command needs.

        Memory: the largest peak of its recent runs (the last history_runs within history_days),
        with a margin. It is never lowered below a seen peak, except down to an enforced MemoryMax,
        which the build can't exceed anyway. A cold build counts at least as the lane default.

        Cores: a run that used nearly all the cores it was given ("saturated") could have used
        more, so the build wants half as many again; a run that left cores idle wants a little
        more than it used. A run that used far more cores than it was given doesn't follow the job
        count, so the build is "rigid": it needs what it uses, however many cores it is given.
        """
        cutoff = now - cfg.history_days * 86400
        recent = [r for r in self.by_key.get(key, ()) if r["t"] >= cutoff][-cfg.history_runs:]
        cap = lane.mem_cap if cap_enforced else None
        rigid = False
        walls = sorted(float(r["wall"]) for r in recent if r.get("wall") and r.get("exit") == 0) or \
            sorted(float(r["wall"]) for r in recent if r.get("wall"))
        wall = walls[len(walls) // 2] if walls else None  # the median time of its successful runs
        if not recent:
            want, mem, source = lane.cores, lane.mem, f"{lane.name} lane default"
            mem_cores = lane.cores
        else:
            # Memory: the biggest run within history_days, however many smaller ones came since.
            window = [r for r in self._retained(key) if r["t"] >= cutoff] or recent
            top = max(window, key=lambda r: r["peak_mem"])
            mem_cores = int(top.get("mem_cores") or top.get("granted") or lane.cores) or lane.cores
            peak = top["peak_mem"]
            if any(r.get("oom") for r in window):
                peak = max(peak, cap or int(peak * 1.5), lane.mem)
            mem = int(peak * cfg.mem_margin) + 256 * MIB
            wants = []
            for r in recent:
                used, given = r["peak_cores"], float(r.get("granted") or 0)
                sustained = float(r.get("avg_cores") or 0)
                if given and sustained > given * 1.2 + 0.5:  # not a burst: it ran wider than told
                    rigid = True
                    wants.append(used * 1.1)
                elif given and used >= given * 0.85:
                    wants.append(given * 1.5)
                else:
                    wants.append(used * 1.2)
            want = max(wants)
            source = f"largest of the last {len(recent)} run{'s' if len(recent) > 1 else ''}"
        if cold:
            if lane.mem > mem:
                mem, mem_cores = lane.mem, lane.cores
            want = max(want, lane.max_cores)
            source += ", cold build"
        mem = max(mem, lane.min_mem)
        if cap:
            mem = min(mem, cap)
        if rigid:
            # It uses what it uses; plan for that, but never for more than the lane allows or the
            # CPU target could ever cover, or it could never start.
            ceiling = max(1, math.floor(cfg.cpu_target * ncpu)) if lane.waits_for_cpu else ncpu
            want = min(max(1, math.ceil(want)), lane.max_cores, ceiling)
            need = want
            source += ", ignores the job count"
        else:
            want = min(max(lane.min_cores, math.ceil(want)), lane.max_cores)
            need = lane.min_cores
        return Estimate(want, need, mem, source, rigid, wall, max(1, mem_cores))

