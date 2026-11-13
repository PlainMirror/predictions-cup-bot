"""Exit/entry policy grid on the live period with backtest.py's simulator (read-only). One data load, many policies.
Marks: '@contest' = open lots at the contest price at the end (what an Oct 31 liquidation would get)."""
import sys
import time
import argparse
import datetime as dt
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))
import backtest as bt
from mm.exits import ExitParams

start = sys.argv[1] if len(sys.argv) > 1 else "2026-10-10"
base = dict(start=start, entry_from=None, budget=70_000, take_edge=0.06, slip=0.005, fill_frac=0.5, max_entry=3000,
            race_cap=8000, fv_lo=0.1, fv_hi=0.9, fresh_h=0.0, fresh_max=0.01, jump_skip=0.0, hist=None)
start_ts = dt.datetime.fromisoformat(start).replace(tzinfo=dt.timezone.utc).timestamp()
end_ts = time.time()
markets, fv, trades, done = bt.load(start_ts, end_ts)
fvs = [bt.fv_at(fv, ex, ts) for ts, ex, _, _ in trades]


def X(s, e, g, ts=None):
    return ExitParams(start_capture=s, end_capture=e, glide_hours=g, time_stop_h=ts, final_exit_et="2026-12-31T00:00",
                      liquidate_et="2026-12-31T00:00")


runs = [("CURRENT 0.7->0.3/120h, stop 120h", {}, X(0.7, 0.3, 120, 120)),
        ("no time stop 0.7->0.3/120h", {}, X(0.7, 0.3, 120)),
        ("0.7->0.0/120h, stop 120h", {}, X(0.7, 0.0, 120, 120)),
        ("0.7->0.0/120h, no stop", {}, X(0.7, 0.0, 120)),
        ("0.5->0.3/120h, stop 120h", {}, X(0.5, 0.3, 120, 120)),
        ("0.9->0.3/120h, stop 120h", {}, X(0.9, 0.3, 120, 120)),
        ("0.7->0.3/72h, stop 120h", {}, X(0.7, 0.3, 72, 120)),
        ("0.7->0.3/168h, stop 168h", {}, X(0.7, 0.3, 168, 168)),
        ("0.7->0.5/120h, stop 168h", {}, X(0.7, 0.5, 120, 168)),
        ("0.6->0.2/96h, stop 144h", {}, X(0.6, 0.2, 96, 144)),
        ("edge 5c, current exits", {"take_edge": 0.05}, X(0.7, 0.3, 120, 120)),
        ("edge 7c, current exits", {"take_edge": 0.07}, X(0.7, 0.3, 120, 120)),
        ("edge 8c, current exits", {"take_edge": 0.08}, X(0.7, 0.3, 120, 120)),
        ("FV band .05-.95, current", {"fv_lo": 0.05, "fv_hi": 0.95}, X(0.7, 0.3, 120, 120)),
        ("budget 100k, current", {"budget": 100_000}, X(0.7, 0.3, 120, 120)),
        ("race cap 12k, 5k/take, current", {"race_cap": 12_000, "max_entry": 5000}, X(0.7, 0.3, 120, 120))]
print(f"{len(trades):,} trades, entries from {start}; 70k budget, 3k/take, 8k/race, edge 6c, FV .1-.9 unless noted\n")
print(f"{'policy':34} {'P&L@K':>7} {'@contest':>8} {'realised':>8} {'open@end':>8} {'avg cap':>8} {'@c/1k/day':>9} {'exited':>6} {'hold h':>6} {'entries':>7}")
for name, over, x in runs:
    a = argparse.Namespace(**{**base, **over})
    a.entry_from = start_ts
    r = bt.simulate(bt.Policy(name, True, x), markets, fv, trades, fvs, a, end_ts)
    days = (end_ts - start_ts) / 86400
    per = r["pnl_contest"] / max(r["avg_cap"], 1) * 1000 / days
    hold = "-" if r["median_hold_h"] is None else f"{r['median_hold_h']:.0f}"
    print(f"{name:34} {r['pnl']:7,.0f} {r['pnl_contest']:8,.0f} {r['realized']:8,.0f} {r['open_cost_end']:8,.0f} {r['avg_cap']:8,.0f} "
          f"{per:9.1f} {r['exited_pct']:6.0%} {hold:>6} {r['entries']:7}", flush=True)
