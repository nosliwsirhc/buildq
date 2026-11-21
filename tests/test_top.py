"""Unit tests for `buildq top` rendering, sampling, colours, and fetcher."""

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import GIB, MIB  # noqa: E402, F401
from buildq import client, top  # noqa: E402


class Top(unittest.TestCase):
    """buildq top: what it reads, and what it draws."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(top.GLYPHS.update, dict(top.GLYPHS))
        self.addCleanup(top.BOX.update, dict(top.BOX))
        self.addCleanup(top.TOP.update, dict(top.TOP))

    def cgroup(self, name, usec, anon, swap=0, rbytes=0, wbytes=0, some=0.0):
        path = os.path.join(self.tmp.name, name)
        os.makedirs(path, exist_ok=True)
        files = {"cpu.stat": f"usage_usec {usec}\n", "memory.stat": f"anon {anon}\nkernel 0\nshmem 0\nslab_reclaimable 0\n",
                 "memory.swap.current": f"{swap}\n",
                 "io.stat": f"253:0 rbytes={rbytes} wbytes={wbytes}\n259:0 rbytes={rbytes} wbytes={wbytes}\n",
                 "memory.pressure": f"some avg10={some} avg60=0 avg300=0 total=0\nfull avg10=0.00 avg60=0 avg300=0 total=0\n",
                 "memory.events": "low 0\nhigh 3\nmax 0\noom 0\noom_kill 1\n"}
        for file, text in files.items():
            with open(os.path.join(path, file), "w") as f:
                f.write(text)
        return path

    def test_a_cgroup_sample_and_its_rates_counting_stacked_devices_once(self):
        path = self.cgroup("b", 0, 2 * GIB, swap=GIB, some=4.5)
        top._HOLDERS.clear()
        self.addCleanup(top._HOLDERS.clear)
        now = time.monotonic()
        top._HOLDERS.update({"253:0": ([], now), "259:0": (["253:0"], now)})  # dm-crypt on a partition of it
        sample = top.cgroup_sample(path)
        self.assertEqual((sample["mem"], sample["swap"], sample["psi_mem"]["some"]), (2 * GIB, GIB, 4.5))
        self.assertEqual((sample["events"]["high"], sample["events"]["oom_kill"]), (3, 1))
        sampler = top.TopSampler()
        self.assertIsNone(sampler.cgroup(path, 100.0)["cores"])  # nothing to compare with yet
        self.cgroup("b", 4_000_000, 2 * GIB, rbytes=20 * MIB, wbytes=2 * MIB)
        second = sampler.cgroup(path, 102.0)
        self.assertAlmostEqual(second["cores"], 2.0)
        self.assertAlmostEqual(second["read_rate"], 10 * MIB)  # not 20: what went through dm-crypt counts once
        self.assertIsNone(top.cgroup_sample(os.path.join(self.tmp.name, "gone")))
        os.remove(os.path.join(path, "memory.stat"))  # going away mid-read
        self.assertIsNone(top.cgroup_sample(path))

    def test_stacked_io_is_counted_once_per_interval(self):
        top._HOLDERS.clear()
        self.addCleanup(top._HOLDERS.clear)
        now = time.monotonic()
        zero = {d: (0, 0) for d in ("253:0", "253:1", "259:0", "8:0", "8:16")}

        def rate(holders, after, before=zero):
            top._HOLDERS.clear()
            top._HOLDERS.update({d: (h, now) for d, h in holders.items()})
            return top.io_rate(before, after, 1.0)[0]
        self.assertEqual(rate({"259:0": ["253:0"]}, {"253:0": (10, 0), "259:0": (30, 0)}), 30)  # 20 to a plain partition
        self.assertEqual(rate({"259:0": ["253:0", "253:0"]}, {"253:0": (10, 0), "259:0": (30, 0)}), 30)  # an LV on 2 parts
        self.assertEqual(rate({"8:0": ["253:1"], "8:16": ["253:1"]},  # striped over two disks, 20 plain on one
                              {"253:1": (100, 0), "8:0": (70, 0), "8:16": (50, 0)}), 120)
        self.assertEqual(rate({"259:0": []}, {"259:0": (8 * GIB + 5, 0)},  # the dm device went away: no burst
                              before={"253:0": (8 * GIB, 0), "259:0": (8 * GIB, 0)}), 5)
        self.assertEqual(rate({"8:0": ["253:1"], "8:16": ["253:1"]},  # a stripe split 100/0 in the interval
                              {"253:1": (100, 0), "8:0": (100, 0), "8:16": (0, 0)}), 100)
        self.assertEqual(rate({"8:0": ["9:0"], "8:16": ["9:0"]},  # RAID1 reads from one mirror
                              {"9:0": (100, 0), "8:0": (100, 0), "8:16": (0, 0)}, before={**zero, "9:0": (0, 0)}), 100)
        self.assertEqual(rate({}, {"259:0": (7, 0)}), 7)  # nothing stacked: itself, once
        self.assertEqual(top.text_width("e\u0301\u200dx\ufe0f"), 2)

    def snap(self, running=None):
        running = running or [
            {"id": "a" * 36, "short": "aaaaaaaa", "lane": "heavy", "elapsed": 600, "granted": 8, "want": 8, "need": 2,
             "est_mem": 20 * GIB, "mem_now": 4 * GIB, "mem_peak": 18 * GIB, "label": "cargo test --locked\n\x1b[31mred",
             "write_dirs": ["/h/t/target"], "waited_by": {"memory": 120.0}, "waited": 120, "cwd": "/src",
             "est_src": "largest of the last 3 runs", "peak_at": 90.0, "release_at": 300.0, "release_hold": 2 * GIB,
             "log": None, "peak_cores": 7.5},
            {"id": "b" * 36, "short": "bbbbbbbb", "lane": "web", "elapsed": 30, "granted": 2, "est_mem": 6 * GIB,
             "mem_now": GIB, "mem_peak": GIB, "label": "pnpm test", "write_dirs": ["/h/t/target/tmp"]}]
        queued = [{"short": "cccccccc", "lane": "heavy", "waited": 400, "est_mem": 20 * GIB, "held": False,
                   "reason": "needs 20G of memory; 2.0G is free after the 16G floor and what running builds may still grow into"}]
        return {"time": 1794600000.0,
                "machine": {"cores": 32, "busy": 20.0, "mem_total": 128 * GIB, "mem_available": 60 * GIB,
                            "swap_total": 8 * GIB, "swap_used": 6 * GIB, "psi_cpu": {"some": 3.0},
                            "psi_memory": {"some": 0.5, "full": 0.1}, "psi_io": {},
                            "filesystems": [{"mount": "/", "device": "nvme0n1p3", "free": 1000 * GIB, "size": 1860 * GIB,
                                             "floor": 372 * GIB, "read": 18 * MIB, "write": 45 * MIB}]},
                "groups": {"builds": {"cores": 9.0, "mem": 5 * GIB, "swap": 0, "read_rate": MIB, "write_rate": 2 * MIB,
                                      "known": True, "psi_mem": {"some": 0.2}},
                           "ci": {"cores": 6.0, "mem": 8 * GIB, "swap": 0, "known": True},
                           "containers": {"cores": 0.0, "mem": 0, "swap": 0, "known": True},
                           "apps": {"cores": 2.0, "mem": 14 * GIB, "swap": GIB, "known": True}},
                "builds": {"a" * 36: {"cores": 1.5, "mem": 4 * GIB, "swap": 0, "read_rate": 3 * MIB, "write_rate": 0,
                                      "psi_mem": {"some": 12.0}, "events": {"high": 2, "oom_kill": 0}},
                           "b" * 36: {"cores": 1.9, "mem": GIB, "swap": 0, "psi_mem": {}}},
                "dir_sizes": {"/h/t/target": (41 * GIB, False, 60.0)},
                "overview": {"version": "0.2.0", "paused": None, "running": running, "queued": queued,
                             "budget": {"cpu_cap": 28.8, "growth_reserved": 30 * GIB, "free_mem": 2 * GIB},
                             "config": {"mem_floor": 16 * GIB, "disk_floor": "20%"}}}

    def draw(self, snap, width=220, height=0, **kw):
        with mock.patch.object(top, "safe_dir", side_effect=lambda p: p if isinstance(p, str) and p.startswith("/h/") else None):
            return top.render_top(snap, width, height, **kw)

    @staticmethod
    def marked(lines):
        """The running build the screen marks as selected."""
        return next(t for t, _ in lines if re.search(r"(▶|>) [0-9a-f]{8}", t))

    def test_the_screen(self):
        lines, order = self.draw(self.snap(), detail=True, selected="a" * 36)
        text = "\n".join(t for t, _ in lines)
        for expected in ("20.0 of 32 busy · target 28.8", "60G free", "+30G held", "6.0G of 8.0G",
                         "/: 1000G free of 1860G · floor 372G · read 18M/s · write 45M/s", "CI 6.0c 8.0G",
                         "other 3.0c", "pressure: io -", "2 running", "1 waiting", "aaaaaaaa", "1.5/8", "4.0G/18G/20G",
                         "≥41G", "cargo test --locked·red", "memory 2m00s", "would keep 2.0G", "cccccccc",
                         "MEMORY", "CPU", "LANES", "▓ used", "▒ held for growth", "builds together",
                         "2.0G is free after the 16G floor"):
            self.assertIn(expected, text)
        self.assertNotIn("\x1b", text)
        self.assertEqual(order, ["a" * 36, "b" * 36])
        colour = next(c for t, c in lines if "aaaaaaaa" in t and "1.5/8" in t)
        self.assertEqual(colour, "bad")  # 12% memory pressure: it is being slowed

    def test_the_selection_follows_the_build_not_the_row(self):
        for sort in ("age", "cpu"):
            lines, order = self.draw(self.snap(), selected="b" * 36, sort=sort)
            self.assertIn("bbbbbbbb", self.marked(lines), sort)
        self.assertEqual(self.draw(self.snap(), sort="cpu")[1][0], "b" * 36)  # 1.9 cores before 1.5

    def test_a_small_screen_scrolls_to_the_selected_build_and_keeps_its_detail(self):
        rows = [{"id": f"{i:036d}", "short": f"{i:08d}", "lane": "heavy", "elapsed": 1000 - i, "granted": 2,
                 "est_mem": GIB, "mem_now": 0, "mem_peak": 0, "label": f"job {i}", "write_dirs": []} for i in range(20)]
        for pick in (0, 4, 8, 10, 18, 19):
            lines, order = self.draw(self.snap(rows), width=80, height=24, selected=f"{pick:036d}", detail=True)
            self.assertLessEqual(len(lines), 24)
            self.assertTrue(all(len(t) <= 80 for t, _ in lines))
            self.assertIn(f"{pick:08d}", self.marked(lines), pick)
            self.assertTrue(any(f"{pick:036d}" in t for t, _ in lines), pick)  # its detail's title
            self.assertTrue(any("above," in t and "below" in t for t, _ in lines), pick)
        log = [f"LOG{i:02d}" for i in range(12)]
        lines = self.draw(self.snap(rows), width=80, height=24, selected=f"{3:036d}", detail=True, log_lines=log)[0]
        text = "\n".join(t for t, _ in lines)
        self.assertIn("LOG11", text)  # the newest lines are the ones kept
        narrow = self.draw(self.snap(rows), width=70, height=24)[0]
        row = next(t for t, _ in narrow if "00000000" in t)
        self.assertIn("job 0", row)  # the command keeps its room
        self.assertNotIn("LANE", next(t for t, _ in narrow if "TICKET" in t))
        short = self.draw(self.snap(rows), width=100, height=12, selected=f"{9:036d}")[0]
        self.assertIn("00000009", self.marked(short))  # the machine's panel gave way
        header = next(t for t, _ in lines if "TICKET" in t)
        self.assertIn("COMMAND", header)
        self.assertNotIn("SWAP", header)  # 80 columns: the last optional columns go first
        self.assertIn("LANE", header)

    def test_shared_folders_are_marked_and_unknown_sizes_say_so(self):
        snap = self.snap()
        snap["dir_sizes"]["/h/t/target/tmp"] = (GIB, True, 5.0)
        lines = self.draw(snap)[0]  # one build's folder lies inside the other's: both are marked
        self.assertTrue(any("41G*" in t and "aaaaaaaa" in t for t, _ in lines))
        self.assertTrue(any("1.0G*" in t and "bbbbbbbb" in t for t, _ in lines))
        snap["overview"]["running"][1]["write_dirs"] = ["/h/t/target"]  # the same folder
        self.assertTrue(any("41G*" in t and "bbbbbbbb" in t for t, _ in self.draw(snap)[0]))
        snap["dir_sizes"] = {}
        self.assertTrue(any("…" in t and "aaaaaaaa" in t for t, _ in self.draw(snap)[0]))

    def test_ascii_when_the_terminal_isnt_utf8(self):
        top.use_ascii()
        snap = self.snap()
        snap["overview"]["running"][1]["label"] = "pnpm test \u00e9t\u00e9\ttab\x9bx"
        text = "\n".join(t for t, _ in self.draw(snap, selected="a" * 36, detail=True)[0])
        text.encode("ascii")  # nothing a non-UTF-8 terminal can't show
        self.assertIn("> aaaaaaaa", text)
        self.assertIn("tab", text)

    def test_the_memory_ledger_fills_what_its_parts_add_up_to(self):
        cells = top.ledger_cells([(3, "u", "mem"), (3, "h", "held"), (0, "x", "other"), (4, "o", "other")], 10, 7)
        self.assertEqual("".join(t for t, _ in cells), "uuhhooo")  # rounded as they add up, none lost or doubled
        self.assertEqual(len("".join(t for t, _ in top.ledger_cells([(20, "u", "")], 10, 7))), 7)  # never past the bar
        lines, _ = self.draw(self.snap(), width=150)
        bar = next(t for t, _ in lines if "▓" in t and "▒" in t and "■" in t)
        self.assertLess(bar.index("▓"), bar.index("█"))  # the builds, then other work, the floor, the room
        self.assertLess(bar.index("█"), bar.index("░"))
        self.assertLess(bar.index("░"), bar.index("■"))

    def test_the_cpu_graph_starts_from_the_analytics(self):
        path = os.path.join(self.enterContext(tempfile.TemporaryDirectory()), "metrics.db")
        import sqlite3
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE minute (t REAL, cpu_busy REAL, cpu_busy_max REAL, cores INTEGER, mem_total INTEGER,"
                   " mem_available INTEGER, builds_mem INTEGER, growth INTEGER)")
        now = 1794600000.0
        db.executemany("INSERT INTO minute VALUES (?,?,?,?,?,?,?,?)",
                       [(now - 3600, 31.0, 32.0, 32, 128 * GIB, 64 * GIB, GIB, 0),  # older than the graph
                        (now - 600, 16.0, 30.0, 32, 128 * GIB, 64 * GIB, GIB, 0),
                        (now - 540, 24.0, 30.0, 32, 128 * GIB, 64 * GIB, GIB, 0),
                        (now - 480, 32.0, 32.0, 32, 128 * GIB, 64 * GIB, GIB, 0)])
        db.commit()
        db.close()
        hist = top.TopHistory()
        hist.backfill(path, now)
        self.assertEqual((len(hist.minutes), len(hist.samples)), (3, 0))
        steps = hist.buckets(1, 30, now)  # minute steps; each minute's row stands at its middle
        self.assertEqual(steps[:19], [None] * 19)  # nothing before the first minute
        self.assertEqual(steps[19:22], [16.0, 24.0, 32.0])  # each minute's average (not its busiest moment)
        self.assertEqual(steps[22:], [None] * 8)  # and no plateau carried on after the last
        half = hist.buckets(1, 60, now)  # half-minute steps: a line between the minutes, not flat steps
        self.assertEqual((half[40], half[41]), (18.0, 22.0))  # a straight line between the minutes
        lone = top.TopHistory()
        lone.minutes = [(now - 870, 12.0, 32, 64 * GIB, GIB, 0, 128 * GIB)]  # one minute, no neighbours
        self.assertIn(12.0, lone.buckets(1, 30, now))
        hist.backfill(path, now)
        self.assertEqual(len(hist.minutes), 3)  # only once
        top.TopHistory().backfill(os.path.join(os.path.dirname(path), "missing.db"), now)  # no analytics: empty
        lines, _ = self.draw(self.snap(), width=150, hist=hist)
        graph = [t for t, _ in lines if "│32" in t.replace(" ", "")]
        self.assertTrue(graph and any("\u2801" <= ch <= "\u28ff" for ch in graph[0]))  # braille, as btop draws

    def test_steps_are_fixed_to_the_clock_and_averaged(self):
        hist = top.TopHistory()
        snap = self.snap()
        for t, busy in ((0, 10.0), (20, 30.0), (70, 5.0)):
            s2 = dict(snap, time=1794600000.0 + t)
            s2["machine"] = dict(snap["machine"], busy=busy)
            hist.add(s2)
        before = hist.buckets(1, 30, 1794600075.0)
        self.assertEqual(before[-2:], [20.0, 5.0])  # the average in each minute; the newest still filling
        self.assertEqual(hist.buckets(1, 30, 1794600079.0), before)  # nothing moves within a step
        self.assertEqual(hist.buckets(1, 30, 1794600121.0)[-3:], [20.0, 5.0, None])  # a new step: one along
        sparse = top.TopHistory()  # a sample every 40 s, steps of 20 s: the line between, not gaps
        for t, busy in ((0, 8.0), (40, 16.0)):
            s3 = dict(snap, time=1794600000.0 + t)
            s3["machine"] = dict(snap["machine"], busy=busy)
            sparse.add(s3)
        steps = sparse.buckets(1, 90, 1794600040.0)
        self.assertEqual(steps[-3:], [8.0, 14.0, 16.0])  # the middle step is 30 s along a 40 s line

    def test_braille_has_two_steps_a_cell_and_a_baseline(self):
        self.assertEqual(top.braille_rows([0, 4], 1, 4, baseline=False), ["\u28b8"])  # right column full
        self.assertEqual(top.braille_rows([4, 4], 1, 4, baseline=False), ["\u28ff"])
        self.assertEqual(top.braille_rows([0, 0], 2, 4), ["\u2800", "\u28c0"])  # a baseline dot each
        self.assertEqual(top.braille_rows([None, None], 2, 4), ["\u2800", "\u2800"])  # no data: blank, as btop
        self.assertEqual(top.braille_rows([2, 6], 2, 8, baseline=False), ["\u28a0", "\u28fc"])  # 2 and 4 dots low, 0 and 2 high
        self.assertEqual(top.gradient_style(0, 8), "cpu0")
        self.assertEqual(top.gradient_style(7, 8), "cpu7")  # green at the bottom, red at the top
        top.use_ascii()
        hist = top.TopHistory()
        hist.add(self.snap())
        rows = top.graph_rows(hist, 1, 10, 3, 32, self.snap()["time"])
        "".join(rows).encode("ascii")  # one step a cell, in ASCII, without braille

    def test_the_timeline_and_the_help(self):
        snap = self.snap()
        snap["overview"]["running"][0]["from_ci"] = True
        finished = [{"short": "dddddddd", "lane": "check", "submitted": snap["time"] - 1500, "started": snap["time"] - 1400,
                     "ended": snap["time"] - 900, "peak": 20 * GIB, "from_ci": False, "state": "done"}]
        lines, _ = self.draw(snap, width=120, height=30, view="timeline", finished=finished)
        text = "\n".join(t for t, _ in lines)
        self.assertIn("TIMELINE", text)
        rows = {m: next(t for t, _ in lines if m in t) for m in ("aaaaaaaa", "bbbbbbbb", "cccccccc", "dddddddd")}
        self.assertTrue(rows["aaaaaaaa"].lstrip("│ ").startswith("CI aaaaaaaa"))
        self.assertIn("█", rows["dddddddd"])  # it ran, with a lot of memory
        self.assertIn("·", rows["cccccccc"])  # it waits
        self.assertNotIn("█", rows["cccccccc"].split("heavy", 1)[1])
        self.assertLess(text.index("dddddddd"), text.index("cccccccc"))  # oldest first, newest at the bottom
        lines, _ = self.draw(snap, width=120, height=30, show_help=True)
        text = "\n".join(t for t, _ in lines)
        for expected in ("HELP", "the timeline", "held for it to grow into", "behind N", "a long wait, or a CI job's build"):
            self.assertIn(expected, text)

    def test_the_layout_follows_the_width(self):
        wide = "\n".join(t for t, _ in self.draw(self.snap(), width=150, height=40)[0])
        self.assertIn("LANES", wide)
        self.assertIn("╭─ CPU", wide)
        narrow = "\n".join(t for t, _ in self.draw(self.snap(), width=90, height=40)[0])
        self.assertNotIn("LANES", narrow)
        self.assertNotIn("╭─ CPU", narrow)
        self.assertIn("CPU ", narrow.splitlines()[0])  # a bar in the title line instead
        self.assertIn("MEMORY", narrow)
        many = self.snap()
        many["overview"]["running"] = [dict(many["overview"]["running"][1], id=f"{i:036d}", short=f"{i:08d}",
                                            elapsed=i) for i in range(40)]
        for snap, pick, short in ((self.snap(), "a" * 36, "aaaaaaaa"), (many, f"{7:036d}", "00000007")):
            for width in (50, 79, 80, 90, 100, 110, 150, 300):
                for height in range(12, 41):
                    for detail, log in ((False, None), (True, None), (True, [f"line {i}" for i in range(30)])):
                        lines, _ = self.draw(snap, width=width, height=height, selected=pick, detail=detail, log_lines=log)
                        where = (width, height, detail, bool(log), len(snap["overview"]["running"]))
                        self.assertEqual(len(lines), height, where)
                        self.assertTrue(all(top.text_width(t) <= width for t, _ in lines), where)
                        self.assertIn(short, self.marked(lines), where)  # the selected build is in view
                        if log and any("the end of" in t for t, _ in lines):
                            self.assertIn("line 29", "\n".join(t for t, _ in lines), where)  # the newest kept
                        borders = [i for i, (t, _) in enumerate(lines) if t.startswith("╰")]
                        tops = [i for i, (t, _) in enumerate(lines) if t.startswith("╭")]
                        self.assertEqual(len(borders), len(tops), where)  # no box drawn over another

    def queue_in_a_jam(self, snap, free=-6 * GIB):
        snap["overview"]["budget"].update(free_mem=free, held_mem=223 * GIB)  # (summed over all in line: not shown)
        snap["overview"]["queued"] = [
            {"short": "cccccccc", "lane": "heavy", "waited": 400, "est_mem": 20 * GIB, "held": True, "waiting_for": "memory",
             "reason": "needs 20G of memory; 0M is free after the 16G floor and what running builds may still grow into"},
            {"short": "dddddddd", "lane": "heavy", "waited": 390, "est_mem": 2 * GIB, "held": True, "waiting_for": "held",
             "held_for": "memory", "reason": "the room it needs is held for a build that has waited longer"},
            {"short": "eeeeeeee", "lane": "heavy", "waited": 300, "est_mem": 2 * GIB, "held": False, "waiting_for": "cpu",
             "reason": "needs 8 cores; 2.0 of 28.8 are free (20.0 busy, 0.0 expected from builds just started)"},
            {"short": "ffffffff", "lane": "heavy", "waited": 200, "est_mem": 300 * GIB, "held": False, "waiting_for": "too-big",
             "reason": "needs 300G of memory, more than this machine has; cancel it or give its lane a smaller MemoryMax"},
            {"short": "gggggggg", "lane": "web", "waited": 100, "est_mem": GIB, "held": False, "waiting_for": "held",
             "held_for": "memory", "reason": "the room it needs is held for a build that has waited longer"}]
        return snap

    def test_every_wait_says_why_and_what_it_lacks(self):
        snap = self.queue_in_a_jam(self.snap())
        lines, _ = self.draw(snap, width=260)
        rows = {m: next(t for t, _ in lines if m in t and "needs" in t) for m in ("cccccccc", "dddddddd", "eeeeeeee", "ffffffff", "gggggggg")}
        self.assertIn("⚑ in line", rows["cccccccc"])
        self.assertIn("20G short", rows["cccccccc"])  # what the free room lacks, never more than it needs
        self.assertIn("0M is free after the 16G floor", rows["cccccccc"])
        self.assertNotIn("needs 20G of memory;", rows["cccccccc"])  # (said in its column)
        self.assertIn("behind 1 in line", rows["dddddddd"])  # its room is free, kept for the one ahead
        self.assertIn("behind 2 in line", rows["gggggggg"])
        self.assertIn("needs 8 cores; 2.0 of 28.8 are free", rows["eeeeeeee"])  # not hidden for starting "needs"
        self.assertNotIn("short", rows["eeeeeeee"])
        self.assertIn("cancel it or give its lane a smaller MemoryMax", rows["ffffffff"])
        text = "\n".join(t for t, _ in lines)
        self.assertIn("over by 6.0G", text)  # more held than free: not "room 0M"
        self.assertIn("1 for memory, the closest 20G short", text)
        self.assertIn("2 in line, with room kept for them", text)
        self.assertNotIn("223G", text)  # room kept, summed over everyone in line, would read as more than the machine
        self.assertNotIn("next", text)

    def test_behind_is_the_daemons_count_when_it_gives_one(self):
        snap = self.queue_in_a_jam(self.snap())
        snap["overview"]["queued"][4]["behind"] = 1  # two in line ahead, but only one binds it (#62)
        row = next(t for t, _ in self.draw(snap, width=260)[0] if "gggggggg" in t)
        self.assertIn("behind 1 in line", row)

    def test_a_jam_on_a_normal_terminal_still_lists_waiting_builds(self):
        snap = self.queue_in_a_jam(self.snap())
        snap["overview"]["queued"] *= 5  # 25 waiting
        snap["overview"]["running"] = [dict(snap["overview"]["running"][1], id=f"{i:036d}", short=f"{i:08d}")
                                       for i in range(8)]
        for width, height in ((80, 24), (100, 30), (150, 24), (80, 18), (80, 12)):
            lines, _ = self.draw(snap, width=width, height=height)
            text = "\n".join(t for t, _ in lines)
            self.assertIn("cccccccc", text, (width, height))  # a waiting build, not just a count
            self.assertIn("more waiting", text, (width, height))  # (the count, in the box's border)

    def test_each_build_is_drawn_holding_what_the_daemon_holds_for_it(self):
        snap = self.snap()
        a, b = snap["overview"]["running"]
        snap["builds"]["b" * 36]["mem"] = GIB
        a.update(est_mem=20 * GIB, growth=3 * GIB, released=True)  # let go of after its peak: 3G kept, not 16G
        b.update(est_mem=6 * GIB, growth=5 * GIB)
        snap["overview"]["budget"]["growth_reserved"] = 8 * GIB
        holds = top.build_holds(snap, top.running_order(snap, "age"))
        self.assertEqual(holds, {"a" * 36: (4 * GIB, 3 * GIB), "b" * 36: (GIB, 5 * GIB)})
        lines, _ = self.draw(snap, width=200, height=40)
        row = next(t for t, _ in lines if "aaaaaaaa" in t and "/" in t)
        self.assertIn("▓▓▒····· 4.0G", row)  # 4G used and 3G held of 20G; the rest let go
        text = "\n".join(t for t, _ in lines)
        self.assertIn("builds 5.0G +8.0G held", text)
        a["released"] = b["released"] = False  # nothing let go: each row shows the rest of its plan held
        row = next(t for t, _ in self.draw(snap, width=200, height=40)[0] if "bbbbbbbb" in t and "/" in t)
        self.assertIn("▓▒▒▒▒▒▒▒", row)
        del a["growth"], b["growth"], a["released"], b["released"]  # an older daemon: its total shared out by the plans
        lines, _ = self.draw(snap, width=200, height=40)
        row = next(t for t, _ in lines if "aaaaaaaa" in t and "/" in t)
        self.assertIn("·", row)  # (its share of the 8G held, not the whole rest of its 20G plan)
        holds = top.build_holds(snap, top.running_order(snap, "age"))
        self.assertAlmostEqual(sum(h for _, h in holds.values()), 8 * GIB, delta=2)  # (rounded down per build)

    def test_the_selected_build_keeps_its_warning(self):
        c, _ = top.render_screen(self.snap(), 200, 40, selected="a" * 36)
        y = next(i for i, t in enumerate(c.lines()) if "▶ aaaaaaaa" in t)
        x = c.lines()[y].index("aaaaaaaa")
        self.assertEqual(c.styles[y][x], "sel_bad")  # under memory pressure on the selection bar
        self.assertEqual(c.styles[y][x - 2], "sel")  # the bar runs across the row, as btop's
        self.assertEqual(c.styles[y][c.lines()[y].index("cargo test")], "sel")
        other = next(i for i, t in enumerate(c.lines()) if "bbbbbbbb" in t and "/" in t)
        self.assertNotIn("sel", c.styles[other])

    def test_every_filesystem_has_its_line(self):
        snap = self.snap()
        snap["machine"]["filesystems"].append({"mount": "/var/lib/containers", "device": "sdb1", "free": 10 * GIB,
                                               "size": 500 * GIB, "floor": 100 * GIB, "read": 0, "write": 0})
        for width, height in ((150, 0), (150, 40), (90, 40), (90, 0)):
            text = "\n".join(t for t, _ in self.draw(snap, width=width, height=height)[0])
            self.assertIn("DISK /:", text, (width, height))
            self.assertIn("DISK /var/lib/containers: 10G free of 500G", text, (width, height))

    def test_a_wide_character_cut_in_half_leaves_no_half(self):
        c = top.Canvas(10, 1)
        c.put(0, 0, "日本語")
        c.put(0, 1, "x")  # over the right half of 日
        c.put(0, 4, "y")  # over the left half of 語
        self.assertEqual(c.lines()[0], " x本y")
        self.assertEqual(sum(top.text_width(t) for t, _ in c.runs(0)), 10)
        c = top.Canvas(6, 1)
        c.put(0, 0, "a日b")
        c.put(0, 0, "語")  # a wide one over a narrow one and the left half of another
        self.assertEqual(sum(top.text_width(t) for t, _ in c.runs(0)), 6)

    def test_older_analytics_and_finished_builds(self):
        import sqlite3
        path = os.path.join(self.enterContext(tempfile.TemporaryDirectory()), "metrics.db")
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE minute (t REAL, cpu_busy REAL, cores INTEGER, mem_total INTEGER, mem_available INTEGER)")
        now = 1794600000.0
        db.execute("INSERT INTO minute VALUES (?,?,?,?,?)", (now - 300, 12.0, 32, 128 * GIB, 64 * GIB))
        db.execute("CREATE TABLE build (id TEXT, lane TEXT, submitted INTEGER, ended INTEGER, attempt_wall REAL, wall REAL,"
                   " mem_peak INTEGER, from_ci INTEGER, state TEXT)")
        db.executemany("INSERT INTO build VALUES (?,?,?,?,?,?,?,?,?)", [
            ("1" * 36, "heavy", now - 1200, now - 600, 300.0, 400.0, 20 * GIB, 1, "failed"),
            ("2" * 36, "web", now - 7200, now - 7000, 100.0, 100.0, GIB, 0, "succeeded"),  # ended before the half hour
            ("3" * 36, "web", now - 900, now - 100, None, 50.0, GIB, 0, "succeeded")])
        db.commit()
        db.close()
        hist = top.TopHistory()
        hist.backfill(path, now)  # no growth, no builds_mem: what there is still fills the graph
        self.assertEqual([m[1] for m in hist.minutes], [12.0])
        done = top.finished_builds(path, now, skip={"3" * 36})
        self.assertEqual([(d["short"], d["started"], d["from_ci"]) for d in done], [("11111111", now - 900, True)])

    def test_the_timeline_keeps_the_running_builds(self):
        snap = self.snap()
        finished = [{"short": f"{i:08d}", "lane": "check", "submitted": snap["time"] - 1700 + i, "started": snap["time"] - 1600 + i,
                     "ended": snap["time"] - 1000 + i, "peak": GIB, "from_ci": False, "state": "failed" if i == 59 else "succeeded"}
                    for i in range(60)]
        lines, _ = self.draw(snap, width=120, height=20, view="timeline", finished=finished)
        text = "\n".join(t for t, _ in lines)
        for short in ("aaaaaaaa", "bbbbbbbb", "cccccccc", "00000059"):
            self.assertIn(short, text)  # the running and waiting builds, then the newest finished ones
        self.assertIn("not shown", text)
        c, _ = top.render_screen(snap, 120, 20, view="timeline", finished=finished)
        y = next(i for i, t in enumerate(c.lines()) if "00000059" in t)
        self.assertIn("bad", c.styles[y][27:])  # a failed build isn't drawn as a success

    def test_with_no_daemon_it_still_shows_the_machine(self):
        snap = self.snap()
        snap["overview"] = None
        lines, order = self.draw(snap)
        text = "\n".join(t for t, _ in lines)
        self.assertIn("daemon not answering", text)
        self.assertEqual(order, [])

    def test_only_folders_inside_home_are_measured(self):
        home = os.path.join(self.tmp.name, "home")
        os.makedirs(os.path.join(home, "target", "deep"))
        os.symlink("/", os.path.join(home, "root"))
        with mock.patch.dict(os.environ, HOME=home):
            self.assertEqual(top.safe_dir(os.path.join(home, "target")), os.path.realpath(os.path.join(home, "target")))
            for bad in ("/", "//", home, os.path.join(home, "root"), self.tmp.name, os.path.join(home, "missing"), None):
                self.assertIsNone(top.safe_dir(bad), bad)
            row = {"write_dirs": [os.path.join(home, "target", "deep"), os.path.join(home, "target"), "/"]}
            self.assertEqual(top.folders_of(row), [os.path.realpath(os.path.join(home, "target"))])

    def test_folder_sizes_count_hard_links_once(self):
        folder = os.path.join(self.tmp.name, "target")
        os.makedirs(os.path.join(folder, "deep"))
        with open(os.path.join(folder, "deep", "a"), "wb") as f:
            f.write(os.urandom(200_000))
        os.link(os.path.join(folder, "deep", "a"), os.path.join(folder, "b"))
        size, complete, cut = top.DirSizes.measure(folder, 30)
        self.assertEqual((complete, cut), (True, False))
        self.assertLess(size, 2 * 200_000)  # the linked file once
        self.assertGreaterEqual(size, 200_000)
        self.assertEqual(top.DirSizes.measure(os.path.join(self.tmp.name, "missing"), 30)[:2], (None, False))

    def test_du_vanishing_entries_are_fine_unreadable_ones_are_partial(self):
        class Done:
            def __init__(self, out, err, code):
                self.out, self.err, self.returncode = out, err, code
            def communicate(self, timeout=None):
                return self.out, self.err
        for err, complete in ((b"du: cannot access 'x/y': No such file or directory\n", True),
                              (b"du: cannot read directory 'x/z': Permission denied\n", False)):
            with mock.patch.object(top.subprocess, "Popen", return_value=Done(b"4096\tx\n", err, 1)):
                self.assertEqual(top.DirSizes.measure("x", 5), (4096, complete, False))

    def test_a_new_stacked_device_makes_the_holders_be_read_again(self):
        top._HOLDERS.clear()
        self.addCleanup(top._HOLDERS.clear)
        top._HOLDERS["259:0"] = ([], time.monotonic())  # read before dm-0 existed
        holders = {"/sys/dev/block/259:0/holders": [], "/sys/dev/block/259:0/p3/holders": ["dm-0"],
                   "/sys/dev/block/253:0/holders": []}
        with mock.patch.object(top.glob, "glob", side_effect=lambda p: ["/sys/dev/block/259:0/p3/holders"]
                               if p.startswith("/sys/dev/block/259:0/") else []), \
                mock.patch.object(top.os, "listdir", side_effect=lambda p: holders.get(p, [])), \
                mock.patch.object(top, "read_text", side_effect=lambda p: "253:0\n" if p.endswith("/dm-0/dev") else None):
            first = {"259:0": (30, 0), "253:0": (10, 0)}
            top.io_rate({"259:0": (0, 0)}, first, 1.0)  # dm-0 is new: what is stacked is read afresh
            self.assertEqual(top.io_rate(first, {"259:0": (60, 0), "253:0": (20, 0)}, 1.0)[0], 30)  # not 40

    def test_the_holders_of_a_disk_include_its_partitions(self):
        top._HOLDERS.clear()
        self.addCleanup(top._HOLDERS.clear)
        holders = {"/sys/dev/block/259:0/holders": [], "/sys/dev/block/259:0/nvme0n1p3/holders": ["dm-0"]}
        with mock.patch.object(top.glob, "glob", return_value=["/sys/dev/block/259:0/nvme0n1p3/holders"]), \
                mock.patch.object(top.os, "listdir", side_effect=lambda p: holders[p]), \
                mock.patch.object(top, "read_text", side_effect=lambda p: "253:0\n" if p == "/sys/class/block/dm-0/dev" else None):
            self.assertEqual(top.holders_of("259:0"), ["253:0"])

    def test_du_runs_in_the_c_locale_and_wide_text_fits(self):
        seen = {}

        class Done:
            returncode = 0
            def communicate(self, timeout=None):
                return b"1\tx\n", b""
        def popen(command, **kw):
            seen.update(kw.get("env") or {})
            return Done()
        with mock.patch.object(top.subprocess, "Popen", side_effect=popen):
            top.DirSizes.measure("x", 5)
        self.assertEqual(seen.get("LC_ALL"), "C")
        self.assertEqual(top.text_width("日本x"), 5)
        for odd in ("\u202e", "\u061c", "\ufeff", "\u0600", "\u2028", "\U000e0041"):
            self.assertNotIn(odd, top.clean("ls " + odd + "x"))
        self.assertIn("\u200c", top.clean("\u0645\u06cc\u200c\u062e"))  # Persian keeps its non-joiner
        self.assertEqual(top.fit("日本語", 5), "日本")

    def test_the_interval_is_checked(self):
        self.assertEqual(client.interval_arg("2"), 2.0)
        for bad in ("-1", "0", "0.1", "nan", "inf", "x"):
            with self.assertRaises(argparse.ArgumentTypeError, msg=bad):
                client.interval_arg(bad)

    def test_which_encodings_want_ascii(self):
        self.assertTrue(top.wants_ascii("ANSI_X3.4-1968"))  # LC_ALL=C, as nl_langinfo and curses say
        self.assertTrue(top.wants_ascii("ascii"))
        self.assertTrue(top.wants_ascii(None))
        self.assertFalse(top.wants_ascii("UTF-8"))
        self.assertFalse(top.wants_ascii("utf_8"))

    def test_no_color_turns_colour_off(self):
        self.assertTrue(top.colour_wanted({}))
        self.assertFalse(top.colour_wanted({"NO_COLOR": "1"}))
        self.assertTrue(top.colour_wanted({"NO_COLOR": ""}))  # set but empty: no-color.org says ignore it

    def test_a_terminal_this_machine_has_no_terminfo_for_falls_back(self):
        import curses
        env, tried = {"TERM": "xterm-ghostty"}, []

        def wrapper(run):
            tried.append(env["TERM"])
            if env["TERM"] == "xterm-ghostty":
                raise curses.error("setupterm: could not find terminal")
            return run(None)
        code, why = top.curses_with_fallback(lambda screen: 7, wrapper, env)
        self.assertEqual((code, tried), (7, ["xterm-ghostty", "xterm-256color"]))
        self.assertIn("no terminfo for TERM=xterm-ghostty", why)
        self.assertIn("infocmp -x xterm-ghostty", why)

    def test_no_terminal_at_all_is_one_snapshot_and_says_why(self):
        import curses

        def wrapper(run):
            raise curses.error("setupterm: could not find terminal")
        code, why = top.curses_with_fallback(lambda screen: 0, wrapper, {"TERM": "dumb"})
        self.assertIsNone(code)
        self.assertIn("one snapshot instead", why)

    def test_a_curses_error_once_running_isnt_taken_for_the_terminal(self):
        import curses

        def run(screen):
            raise curses.error("addwstr() returned ERR")
        with self.assertRaises(curses.error):
            top.curses_with_fallback(run, lambda r: r(None), {"TERM": "xterm-ghostty"})

    def test_the_screen_never_waits_on_the_daemon(self):
        import threading
        release = threading.Event()

        def slow():
            release.wait(5)
            return {"overview": {}}
        fetcher = top.StatusFetcher(0.05, ask=slow).start()
        self.addCleanup(fetcher.stop)
        self.addCleanup(release.set)
        started = time.monotonic()
        self.assertEqual(fetcher.latest(), (0, None))  # at once, while the daemon is still answering
        self.assertLess(time.monotonic() - started, 0.5)
        release.set()
        self.assertTrue(fetcher.answered.wait(5))
        version, status = fetcher.latest()
        self.assertEqual((version >= 1, status), (True, {"overview": {}}))

    def test_sigterm_and_sighup_end_it_as_an_exception(self):
        import signal as signals
        saved = {s: signals.getsignal(s) for s in (signals.SIGTERM, signals.SIGHUP)}
        self.addCleanup(lambda: [signals.signal(s, h) for s, h in saved.items()])
        top.exit_on_signals()
        for sig in (signals.SIGTERM, signals.SIGHUP):
            with self.assertRaises(SystemExit) as caught:
                os.kill(os.getpid(), sig)
                time.sleep(1)  # (the handler runs at the next bytecode)
            self.assertEqual(caught.exception.code, 128 + sig)

    def test_no_color_keeps_the_terminal_s_own_colours(self):
        calls = []

        class Curses:
            A_BOLD, A_REVERSE, A_DIM, COLOR_CYAN, COLOR_GREEN, COLOR_YELLOW, COLOR_RED, COLOR_BLUE, COLOR_MAGENTA = \
                1 << 21, 1 << 18, 1 << 20, 6, 2, 3, 1, 4, 5
            COLOR_WHITE, COLOR_BLACK, COLORS = 7, 0, 8
            error = Exception

            def has_colors(self):
                return True

            def use_default_colors(self):
                calls.append("default")

            def init_pair(self, *a):
                calls.append("pair")

            def color_pair(self, i):
                return i << 8
        plain = top.init_colours(Curses(), wanted=False)
        self.assertEqual(calls, ["default"])  # the terminal's own colours, not white on black
        self.assertEqual((plain[""], plain["head"]), (0, Curses.A_BOLD))  # headings keep their bold
        calls.clear()
        coloured = top.init_colours(Curses(), wanted=True)  # 8 colours: btop's TTY theme
        self.assertEqual(calls.count("pair"), 9)
        self.assertNotEqual(coloured["ok"], 0)
        self.assertEqual(coloured["dim"], 0)  # nothing to read in blue
        self.assertNotEqual(coloured["sel_bad"], coloured["sel"])  # a warning on the bar: its own background
        rich = Curses()
        rich.COLORS = 256
        calls.clear()
        coloured = top.init_colours(rich, wanted=True)  # btop's Default theme
        self.assertEqual(calls.count("pair"), len({fg for fg, _ in top.TOP_COLOURS_256.values()}) + 3)
        self.assertTrue(coloured["sel"] & Curses.A_BOLD)
        self.assertNotEqual(coloured["dim"], coloured["faint"])
        class Exhausted(Curses):  # real curses raises ValueError past the terminal's pairs
            COLORS, COLOR_PAIRS = 256, 16

            def init_pair(self, n, *a):
                if n >= self.COLOR_PAIRS:
                    raise ValueError("Color pair is greater than COLOR_PAIRS-1")
                calls.append("pair")
        few = top.init_colours(Exhausted(), wanted=True)  # too few pairs for btop's theme: its TTY theme
        self.assertEqual(few["dim"], 0)
        self.assertNotEqual(few["ok"], 0)
        Exhausted.COLOR_PAIRS = 8
        self.assertEqual(top.init_colours(Exhausted(), wanted=True).get("ok", 0), 0)  # too few for that too: plain
        direct = Curses()
        direct.COLORS, direct.COLOR_PAIRS = 0x1000000, 0x10000  # xterm-direct: numbers are RGB, not indexes
        calls.clear()
        self.assertEqual(top.init_colours(direct, wanted=True)["dim"], 0)  # the eight colours' theme
        self.assertEqual(calls.count("pair"), 9)
        capped = Curses()
        capped.COLORS, capped.COLOR_PAIRS = 32767, 0x10000  # direct colour capped by an older ncurses
        capped.tigetflag = lambda name: 1 if name == "RGB" else -1
        self.assertEqual(top.init_colours(capped, wanted=True)["dim"], 0)

    def test_a_closed_terminal_ends_with_its_signal_s_code_not_a_traceback(self):
        import curses

        def wrapper(run):
            try:
                run(None)
            finally:  # as curses.wrapper's teardown fails on a hung-up terminal
                raise curses.error("nocbreak() returned ERR")

        def run(screen):
            raise SystemExit(129)
        with self.assertRaises(SystemExit) as caught:
            top.curses_with_fallback(run, wrapper, {"TERM": "xterm-256color"})
        self.assertEqual(caught.exception.code, 129)
        env, calls = {"TERM": "xterm-ghostty"}, []

        def fallback_wrapper(run):  # the first try fails setupterm; the fallback runs, then hangs up
            calls.append(env["TERM"])
            if len(calls) == 1:
                raise curses.error("setupterm: could not find terminal")
            return wrapper(run)
        with self.assertRaises(SystemExit) as caught:
            top.curses_with_fallback(run, fallback_wrapper, env)
        self.assertEqual((caught.exception.code, calls), (129, ["xterm-ghostty", "xterm-256color"]))

        def interrupted(screen):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            top.curses_with_fallback(interrupted, wrapper, {"TERM": "xterm-256color"})

    def test_the_fetcher_survives_an_unexpected_error(self):
        answers = iter([{"overview": {}}, OSError(23, "too many open files"), {"overview": {"x": 1}}])

        def ask():
            a = next(answers, {"overview": {"x": 1}})
            if isinstance(a, Exception):
                raise a
            return a
        fetcher = top.StatusFetcher(0.01, ask=ask).start()
        self.addCleanup(fetcher.stop)
        deadline = time.monotonic() + 5
        while fetcher.latest()[0] < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertGreaterEqual(fetcher.latest()[0], 3)  # it went on asking
        self.assertTrue(fetcher.thread.is_alive())



if __name__ == "__main__":
    unittest.main()
