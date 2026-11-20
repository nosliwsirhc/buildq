"""#54's trial, day by day (private data, local only): builds that started under each
scale_mem_with_cores setting, real breaches (breached_at), memory stops, OOM kills, waits for memory,
held memory, and the lowest memory available. Revert rules (owner, 2026-11-16): any OOM kill, or a
memory stop of a build that breached its plan; free memory under the 16G floor; more than 5% of
builds past their start plan (outgrown_at)."""
import os, sqlite3, json, statistics as st, datetime, collections, sys
GIB = 2**30
db = sqlite3.connect(f"file:{os.path.expanduser('~/.local/state/buildq/metrics.db')}?mode=ro", uri=True)
db.row_factory = sqlite3.Row
cols = {r[1] for r in db.execute("PRAGMA table_info(build)")}
if "start_rule" not in cols:
    sys.exit("no trial columns yet (#54 not installed)")
days = collections.defaultdict(list)
for r in db.execute("SELECT * FROM build WHERE start_rule IS NOT NULL ORDER BY ended"):
    day = datetime.datetime.fromtimestamp(r["ended"]).strftime("%m-%d")
    days[(day, r["start_rule"])].append(r)
def pct(xs, p):
    xs = sorted(xs); return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else 0
print(f"{'day':6} {'rule':7} {'builds':>6} {'heavy':>5} {'outgrew':>7} {'breach':>6} {'oom':>4} {'mem stops':>9} "
      f"{'agent mem wait p50/p90 (heavy)':>31} {'plan/peak (heavy)':>18}")
for (day, rule), rows in sorted(days.items()):
    heavy = [r for r in rows if r["lane"] == "heavy"]
    outgrew = sum(1 for r in heavy if r["outgrown_at"] is not None)
    breach = sum(1 for r in heavy if (r["breaches"] or 0) > 0)
    oom = sum(1 for r in rows if r["oom"])
    stops = sum(max(0, (r["requeues"] or 0) - (r["ci_stops"] or 0)) for r in rows)
    waits = [json.loads(r["waited_by"] or "{}").get("memory", 0) / 60 for r in heavy if not r["from_ci"]]
    pp = [(r["start_plan"] or 0) / max(r["mem_peak"] or 1, 1) for r in heavy if r["start_plan"] and r["mem_peak"]]
    print(f"{day:6} {rule:7} {len(rows):6} {len(heavy):5} {outgrew/max(len(heavy),1):7.1%} {breach/max(len(heavy),1):6.1%} "
          f"{oom:4} {stops:9} {pct(waits,.5):14.1f}m / {pct(waits,.9):5.1f}m {st.median(pp) if pp else 0:15.1f}x")
for row in db.execute("""SELECT strftime('%m-%d', t, 'unixepoch', 'localtime') AS day, min(mem_available_min) AS low,
                         avg(growth) AS held, sum(mem_wait_seconds) AS waits FROM minute
                         WHERE t >= (SELECT min(ended) FROM build WHERE start_rule IS NOT NULL) GROUP BY day"""):
    print(f"{row['day']}: lowest available {(row['low'] or 0)/GIB:.1f}G, held for growth avg {(row['held'] or 0)/GIB:.1f}G, "
          f"seconds with a build waiting on memory {row['waits'] or 0:.0f}")
