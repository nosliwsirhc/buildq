#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Generate dark and light SVG benchmark charts from `bench/sim.py baselines --json`.

Run:
    python3 bench/charts.py                        # runs sim.py baselines and writes docs/images/
    python3 bench/charts.py --input results.json   # renders from a saved `baselines --json` file
"""

from __future__ import annotations

import argparse
from html import escape
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIM_PY = os.path.join(ROOT, "bench", "sim.py")
DEFAULT_OUT = os.path.join(ROOT, "docs", "images")

FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, monospace"

THEMES = {
    "dark": {
        "bg": "#0d1117",
        "panel": "#161b22",
        "border": "#30363d",
        "grid": "#21262d",
        "text": "#e6edf3",
        "muted": "#8b949e",
        "subtle": "#6e7681",
        "base_bar": "#4493f8",
        "base_muted": "#3b5370",
        "buildq": "#3fb950",
        "buildq_soft": "#193822",
        "oom": "#f85149",
        "timeout": "#d29922",
        "floor": "#f85149",
        "row_hi": "#13231a",
    },
    "light": {
        "bg": "#ffffff",
        "panel": "#f6f8fa",
        "border": "#d0d7de",
        "grid": "#e5e9ee",
        "text": "#1f2328",
        "muted": "#59636e",
        "subtle": "#656d76",
        "base_bar": "#218bff",
        "base_muted": "#8094aa",
        "buildq": "#1a7f37",
        "buildq_soft": "#dafbe1",
        "oom": "#cf222e",
        "timeout": "#bf8700",
        "floor": "#cf222e",
        "row_hi": "#eafbee",
    },
}

POLICY_ORDER = [
    ("none", "No queue"),
    ("slots:2", "2 fixed slots"),
    ("slots:4", "4 fixed slots"),
    ("slots:6", "6 fixed slots"),
    ("buildq", "buildq"),
]


def fmt_dur_short(seconds: float) -> str:
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    if s < 7200:
        return f"{s / 60:.1f}m"
    return f"{s / 3600:.1f}h"


def _svg_open(width: int, height: int, theme: dict[str, str], title: str) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">',
        f'  <title>{escape(title)}</title>',
        "  <style>",
        f"    .t {{ font-family: {FONT}; fill: {theme['text']}; }}",
        f"    .m {{ font-family: {FONT}; fill: {theme['muted']}; }}",
        f"    .s {{ font-family: {FONT}; fill: {theme['subtle']}; }}",
        f"    .mono {{ font-family: {MONO}; fill: {theme['muted']}; }}",
        "  </style>",
        f'  <rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="12" '
        f'fill="{theme["bg"]}" stroke="{theme["border"]}"/>',
    ]


def render_overview(data: dict, mode: str) -> str:
    """Two-panel summary: (1) outcomes across all 132 builds, (2) lowest free RAM vs 16 GB floor."""
    th = THEMES[mode]
    totals = data["totals"]
    total_builds = totals["buildq"]["builds"]
    n_scenarios = len(data["scenarios"])
    w, h = 920, 358
    out = _svg_open(w, h, th, "buildq vs. no queue and fixed slots: build outcomes and RAM safety margin")

    # Header
    out.append(
        f'  <text x="24" y="34" class="t" font-size="16" font-weight="700">'
        f'buildq vs. no queue &amp; fixed slots ({total_builds} builds across {n_scenarios} workloads)</text>'
    )
    out.append(
        f'  <text x="24" y="54" class="m" font-size="12">'
        f'32-core, 125 GB Linux workstation replay (python3 bench/sim.py baselines) · 16 GB RAM safety floor</text>'
    )

    # Panel containers
    p1_x, p1_y, p1_w, p1_h = 20, 72, 434, 264
    p2_x, p2_y, p2_w, p2_h = 466, 72, 434, 264
    for px, py, pw, ph in ((p1_x, p1_y, p1_w, p1_h), (p2_x, p2_y, p2_w, p2_h)):
        out.append(
            f'  <rect x="{px}" y="{py}" width="{pw}" height="{ph}" rx="8" '
            f'fill="{th["panel"]}" stroke="{th["border"]}"/>'
        )

    # ---- Panel 1: Build outcomes (stacked horizontal bar) ----
    out.append(
        f'  <text x="{p1_x + 16}" y="{p1_y + 26}" class="t" font-size="13" font-weight="600">'
        f'Build outcomes (all {total_builds} builds)</text>'
    )
    # Legend
    lx = p1_x + 16
    ly = p1_y + 45
    legend_items = [
        (th["base_bar"], "Baseline ok"),
        (th["buildq"], "buildq ok"),
        (th["oom"], "OOM killed"),
        (th["timeout"], "CI timed out"),
    ]
    for color, label in legend_items:
        out.append(f'  <rect x="{lx:.1f}" y="{ly - 9}" width="10" height="10" rx="2" fill="{color}"/>')
        out.append(f'  <text x="{lx + 14:.1f}" y="{ly}" class="m" font-size="10.5">{escape(label)}</text>')
        lx += 14 + len(label) * 5.7 + 14

    bar_x = p1_x + 102
    bar_w = 170
    row_y0 = p1_y + 70
    row_step = 35
    for i, (key, label) in enumerate(POLICY_ORDER):
        t = totals[key]
        ry = row_y0 + i * row_step
        is_bq = key == "buildq"
        if is_bq:
            out.append(
                f'  <rect x="{p1_x + 8}" y="{ry - 6}" width="{p1_w - 16}" height="30" rx="6" '
                f'fill="{th["row_hi"]}" stroke="{th["buildq"]}" stroke-opacity="0.45"/>'
            )
        out.append(
            f'  <text x="{bar_x - 10}" y="{ry + 13}" text-anchor="end" class="t" '
            f'font-size="12" font-weight="{"700" if is_bq else "500"}">{escape(label)}</text>'
        )
        # Track background
        out.append(
            f'  <rect x="{bar_x}" y="{ry}" width="{bar_w}" height="18" rx="4" fill="{th["grid"]}"/>'
        )
        ok_w = bar_w * (t["succeeded"] / total_builds)
        oom_w = bar_w * (t["oom_kills"] / total_builds)
        to_w = bar_w * (t["ci_timed_out"] / total_builds)
        cur_x = bar_x
        if ok_w > 0:
            ok_col = th["buildq"] if is_bq else th["base_bar"]
            out.append(
                f'  <rect x="{cur_x:.1f}" y="{ry}" width="{ok_w:.1f}" height="18" rx="3" fill="{ok_col}"/>'
            )
            cur_x += ok_w
        if oom_w > 0:
            w_draw = max(oom_w, 3.0)
            out.append(
                f'  <rect x="{cur_x:.1f}" y="{ry}" width="{w_draw:.1f}" height="18" rx="2" fill="{th["oom"]}"/>'
            )
            cur_x += w_draw
        if to_w > 0:
            w_draw = max(to_w, 3.0)
            out.append(
                f'  <rect x="{cur_x:.1f}" y="{ry}" width="{w_draw:.1f}" height="18" rx="2" fill="{th["timeout"]}"/>'
            )

        # Right-side annotation
        parts = [f'{t["succeeded"]}/{total_builds}']
        if t["oom_kills"] and t["ci_timed_out"]:
            parts.append(f'{t["oom_kills"]} OOM, {t["ci_timed_out"]} T/O')
        elif t["oom_kills"]:
            parts.append(f'{t["oom_kills"]} OOM')
        elif t["ci_timed_out"]:
            parts.append(f'{t["ci_timed_out"]} T/O')
        else:
            parts.append("100%")
        note = " · ".join(parts)
        txt_col = th["buildq"] if is_bq else (th["oom"] if t["oom_kills"] else th["text"])
        out.append(
            f'  <text x="{bar_x + bar_w + 10}" y="{ry + 13}" font-family="{FONT}" '
            f'fill="{txt_col}" font-size="11.5" font-weight="{"700" if is_bq else "500"}">{escape(note)}</text>'
        )

    # ---- Panel 2: Lowest free RAM vs 16 GB safety floor ----
    out.append(
        f'  <text x="{p2_x + 16}" y="{p2_y + 26}" class="t" font-size="13" font-weight="600">'
        f'Lowest free RAM (16 GB safety floor)</text>'
    )
    out.append(
        f'  <text x="{p2_x + 16}" y="{p2_y + 45}" class="m" font-size="10.5">'
        f'Right of dashed line = safe · left = starved of RAM</text>'
    )

    r_bar_x = p2_x + 102
    r_bar_w = 180
    max_gb = 64.0
    floor_gb = 16.0
    floor_x = r_bar_x + r_bar_w * (floor_gb / max_gb)

    # Vertical grid ticks
    for tick in (0, 16, 32, 48, 64):
        tx = r_bar_x + r_bar_w * (tick / max_gb)
        if tick == 16:
            out.append(
                f'  <line x1="{tx:.1f}" y1="{p2_y + 60}" x2="{tx:.1f}" y2="{p2_y + 238}" '
                f'stroke="{th["floor"]}" stroke-width="1.5" stroke-dasharray="4,3"/>'
            )
            out.append(
                f'  <text x="{tx:.1f}" y="{p2_y + 252}" text-anchor="middle" font-family="{FONT}" '
                f'fill="{th["floor"]}" font-size="10" font-weight="600">16G floor</text>'
            )
        else:
            out.append(
                f'  <line x1="{tx:.1f}" y1="{p2_y + 64}" x2="{tx:.1f}" y2="{p2_y + 236}" '
                f'stroke="{th["grid"]}" stroke-width="1"/>'
            )
            out.append(
                f'  <text x="{tx:.1f}" y="{p2_y + 252}" text-anchor="middle" class="s" font-size="10">{tick}G</text>'
            )

    for i, (key, label) in enumerate(POLICY_ORDER):
        t = totals[key]
        ry = row_y0 + i * row_step
        is_bq = key == "buildq"
        if is_bq:
            out.append(
                f'  <rect x="{p2_x + 8}" y="{ry - 6}" width="{p2_w - 16}" height="30" rx="6" '
                f'fill="{th["row_hi"]}" stroke="{th["buildq"]}" stroke-opacity="0.45"/>'
            )
        out.append(
            f'  <text x="{r_bar_x - 10}" y="{ry + 13}" text-anchor="end" class="t" '
            f'font-size="12" font-weight="{"700" if is_bq else "500"}">{escape(label)}</text>'
        )
        free_gb = float(t["least_free_gib"])
        below_s = float(t["below_floor_s"])
        bw = max(r_bar_w * min(free_gb, max_gb) / max_gb, 4.0)
        if is_bq:
            bcol = th["buildq"]
        elif below_s > 0:
            bcol = th["oom"]
        else:
            bcol = th["base_muted"]
        out.append(
            f'  <rect x="{r_bar_x}" y="{ry}" width="{bw:.1f}" height="18" rx="3" fill="{bcol}"/>'
        )
        if below_s > 0:
            note = f'{free_gb:.1f}G ({fmt_dur_short(below_s)} below)'
            ncol = th["oom"]
        elif is_bq:
            note = f'{free_gb:.1f}G (0s below)'
            ncol = th["buildq"]
        else:
            note = f'{free_gb:.1f}G (0s below)'
            ncol = th["muted"]
        out.append(
            f'  <text x="{r_bar_x + r_bar_w + 10}" y="{ry + 13}" font-family="{FONT}" '
            f'fill="{ncol}" font-size="11.5" font-weight="{"700" if is_bq else "500"}">{escape(note)}</text>'
        )

    # Re-draw floor line on top of row highlight cleanly
    out.append(
        f'  <line x1="{floor_x:.1f}" y1="{p2_y + 60}" x2="{floor_x:.1f}" y2="{p2_y + 238}" '
        f'stroke="{th["floor"]}" stroke-width="1.5" stroke-dasharray="4,3"/>'
    )

    out.append("</svg>")
    return "\n".join(out) + "\n"


def render_scenarios(data: dict, mode: str) -> str:
    """Two-panel deep dive: (1) median CI wait across CI workloads, (2) outgrows-estimate makespan."""
    th = THEMES[mode]
    scenarios = data["scenarios"]
    w, h = 920, 408
    out = _svg_open(w, h, th, "buildq CI wait time and memory-spike throughput benchmarks")

    out.append(
        f'  <text x="24" y="34" class="t" font-size="16" font-weight="700">'
        f'CI wait time &amp; memory-spike completion time</text>'
    )
    out.append(
        f'  <text x="24" y="54" class="m" font-size="12">'
        f'Lower is better · deterministic replay against the real buildqd daemon</text>'
    )

    p1_x, p1_y, p1_w, p1_h = 20, 72, 466, 316
    p2_x, p2_y, p2_w, p2_h = 498, 72, 402, 316
    for px, py, pw, ph in ((p1_x, p1_y, p1_w, p1_h), (p2_x, p2_y, p2_w, p2_h)):
        out.append(
            f'  <rect x="{px}" y="{py}" width="{pw}" height="{ph}" rx="8" '
            f'fill="{th["panel"]}" stroke="{th["border"]}"/>'
        )

    # ---- Panel 1: Median CI wait (minutes) across the 3 CI scenarios ----
    out.append(
        f'  <text x="{p1_x + 16}" y="{p1_y + 26}" class="t" font-size="13" font-weight="600">'
        f'Median CI wait time (minutes, lower is better)</text>'
    )
    ci_policies = [
        ("slots:2", "2 slots", th["base_muted"]),
        ("slots:4", "4 slots", th["base_bar"]),
        ("slots:6", "6 slots", th["timeout"]),
        ("buildq", "buildq", th["buildq"]),
    ]
    lx = p1_x + 16
    ly = p1_y + 44
    for _, lbl, col in ci_policies:
        out.append(f'  <rect x="{lx}" y="{ly - 9}" width="10" height="10" rx="2" fill="{col}"/>')
        out.append(f'  <text x="{lx + 14}" y="{ly}" class="m" font-size="10.5">{escape(lbl)}</text>')
        lx += 14 + len(lbl) * 6.0 + 14

    ci_groups = [
        ("mixed-day", "mixed-day (75 builds)"),
        ("ci-behind-agents", "ci-behind-agents (12)"),
        ("ci-behind-overdue", "ci-behind-overdue (13)"),
    ]
    bx = p1_x + 126
    bw_max = 205
    max_min = 50.0

    # Grid lines for Panel 1
    for tick in (0, 10, 25, 50):
        tx = bx + bw_max * (tick / max_min)
        out.append(
            f'  <line x1="{tx:.1f}" y1="{p1_y + 56}" x2="{tx:.1f}" y2="{p1_y + 276}" '
            f'stroke="{th["grid"]}" stroke-width="1"/>'
        )
        out.append(
            f'  <text x="{tx:.1f}" y="{p1_y + 291}" text-anchor="middle" class="s" font-size="10">{tick}m</text>'
        )

    gy = p1_y + 62
    for sc_key, sc_label in ci_groups:
        out.append(
            f'  <text x="{bx - 10}" y="{gy + 28}" text-anchor="end" class="t" '
            f'font-size="11" font-weight="600">{escape(sc_label.split(" ")[0])}</text>'
        )
        sub = sc_label[sc_label.index("("):]
        out.append(
            f'  <text x="{bx - 10}" y="{gy + 42}" text-anchor="end" class="s" font-size="10">{escape(sub)}</text>'
        )
        for j, (pkey, _, pcol) in enumerate(ci_policies):
            s = scenarios[sc_key][pkey]
            wait_s = float(s["wait"]["ci"]["p50"])
            ci_to = int(s["ci_timed_out"])
            ry = gy + j * 15
            bar_len = max(bw_max * min(wait_s / 60.0, max_min) / max_min, 3.0)
            is_bq = pkey == "buildq"
            out.append(
                f'  <rect x="{bx}" y="{ry}" width="{bar_len:.1f}" height="11" rx="2.5" fill="{pcol}"/>'
            )
            lbl_txt = fmt_dur_short(wait_s)
            if ci_to > 0:
                lbl_txt += f" ({ci_to} T/O)"
            elif is_bq and sc_key == "mixed-day" and wait_s > 0:
                s4_wait = float(scenarios[sc_key]["slots:4"]["wait"]["ci"]["p50"])
                if s4_wait >= wait_s * 2:
                    lbl_txt += f" ({round(s4_wait / wait_s)}x vs 4 slots)"
            elif is_bq:
                lbl_txt += " (0 T/O)"
            txt_col = th["buildq"] if is_bq else (th["timeout"] if ci_to > 0 else th["muted"])
            out.append(
                f'  <text x="{bx + bar_len + 7:.1f}" y="{ry + 9.5}" font-family="{FONT}" '
                f'fill="{txt_col}" font-size="10.5" font-weight="{"700" if is_bq else "500"}">{escape(lbl_txt)}</text>'
            )
        gy += 71

    out.append(
        f'  <text x="{p1_x + 16}" y="{p1_y + 307}" class="s" font-size="10">'
        f'No queue omitted: OOM-kills 3/12 and 4/13 builds in ci-behind-*.</text>'
    )

    # ---- Panel 2: outgrows-estimate makespan ----
    out.append(
        f'  <text x="{p2_x + 16}" y="{p2_y + 26}" class="t" font-size="13" font-weight="600">'
        f'Memory spike: outgrows-estimate (22 builds)</text>'
    )
    out.append(
        f'  <text x="{p2_x + 16}" y="{p2_y + 44}" class="m" font-size="10.5">'
        f'Total completion time (minutes) · red = builds OOM-killed</text>'
    )

    og = scenarios["outgrows-estimate"]
    og_rows = [
        ("slots:2", "2 fixed slots"),
        ("slots:4", "4 fixed slots"),
        ("buildq", "buildq"),
        ("slots:6", "6 fixed slots"),
        ("none", "No queue"),
    ]
    ox = p2_x + 98
    ow_max = 172
    max_og_min = 140.0

    for tick in (0, 35, 70, 105, 140):
        tx = ox + ow_max * (tick / max_og_min)
        out.append(
            f'  <line x1="{tx:.1f}" y1="{p2_y + 60}" x2="{tx:.1f}" y2="{p2_y + 276}" '
            f'stroke="{th["grid"]}" stroke-width="1"/>'
        )
        out.append(
            f'  <text x="{tx:.1f}" y="{p2_y + 291}" text-anchor="middle" class="s" font-size="10">{tick}m</text>'
        )

    oy0 = p2_y + 74
    ostep = 40
    for i, (pkey, plabel) in enumerate(og_rows):
        s = og[pkey]
        ry = oy0 + i * ostep
        is_bq = pkey == "buildq"
        oom = int(s["oom_kills"]["own_cap"] + s["oom_kills"]["machine"])
        ok = int(s["outcomes"].get("succeeded", 0))
        builds = int(s["builds"])
        mins = float(s["makespan_s"]) / 60.0
        blen = max(ow_max * min(mins, max_og_min) / max_og_min, 4.0)

        if is_bq:
            out.append(
                f'  <rect x="{p2_x + 8}" y="{ry - 7}" width="{p2_w - 16}" height="32" rx="6" '
                f'fill="{th["row_hi"]}" stroke="{th["buildq"]}" stroke-opacity="0.45"/>'
            )
        out.append(
            f'  <text x="{ox - 10}" y="{ry + 13}" text-anchor="end" class="t" '
            f'font-size="11.5" font-weight="{"700" if is_bq else "500"}">{escape(plabel)}</text>'
        )
        if is_bq:
            col = th["buildq"]
            dash = ""
        elif oom > 0:
            col = th["oom"]
            dash = ' fill-opacity="0.72"' if pkey == "none" else ""
        else:
            col = th["base_bar"] if pkey == "slots:4" else th["base_muted"]
            dash = ""
        out.append(
            f'  <rect x="{ox}" y="{ry}" width="{blen:.1f}" height="18" rx="3" fill="{col}"{dash}/>'
        )

        dur = fmt_dur_short(s["makespan_s"])
        if is_bq:
            ann = f"{dur} (22/22 ok)"
            acol = th["buildq"]
        elif oom > 0:
            ann = f"{dur} ({ok}/{builds}, {oom} OOM)"
            acol = th["oom"]
        else:
            ann = f"{dur} ({ok}/{builds} ok)"
            acol = th["muted"]
        out.append(
            f'  <text x="{ox + blen + 8:.1f}" y="{ry + 13}" font-family="{FONT}" '
            f'fill="{acol}" font-size="11" font-weight="{"700" if is_bq else "500"}">{escape(ann)}</text>'
        )

    bq_ms = float(og["buildq"]["makespan_s"])
    s4_ms = float(og["slots:4"]["makespan_s"])
    s2_ms = float(og["slots:2"]["makespan_s"])
    if bq_ms > 0 and s4_ms > bq_ms and s2_ms > bq_ms:
        og_foot = f"buildq finishes {round(100 * (s4_ms - bq_ms) / s4_ms)}% faster than 4 slots & {s2_ms / bq_ms:.1f}x faster than 2 slots."
    else:
        og_foot = "buildq finishes all 22 builds with 0 OOM kills."
    out.append(
        f'  <text x="{p2_x + 16}" y="{p2_y + 307}" class="s" font-size="10">'
        f'{escape(og_foot)}</text>'
    )

    out.append("</svg>")
    return "\n".join(out) + "\n"


def load_data(path: str | None) -> dict:
    if path:
        with open(path) as f:
            return json.load(f)
    out = subprocess.check_output([sys.executable, SIM_PY, "baselines", "--json"], text=True)
    return json.loads(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate SVG benchmark charts from sim.py baselines.")
    parser.add_argument("--input", help="JSON file from `python3 bench/sim.py baselines --json`")
    parser.add_argument("--out-dir", default=DEFAULT_OUT, help="output directory for SVGs")
    args = parser.parse_args(argv)

    data = load_data(args.input)
    os.makedirs(args.out_dir, exist_ok=True)
    files = {
        "benchmark-overview-dark.svg": render_overview(data, "dark"),
        "benchmark-overview-light.svg": render_overview(data, "light"),
        "benchmark-scenarios-dark.svg": render_scenarios(data, "dark"),
        "benchmark-scenarios-light.svg": render_scenarios(data, "light"),
    }
    for name, content in files.items():
        dst = os.path.join(args.out_dir, name)
        with open(dst, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"wrote {dst} ({len(content)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
