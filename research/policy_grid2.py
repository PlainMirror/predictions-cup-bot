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


T = dict(big_edge=0.08, big_max_entry=5000, big_race_cap=12_000)
runs = []
for ff in (0.5, 0.25):
    runs += [(f"[fill {ff:.0%}] CURRENT", {"fill_frac": ff}, X(0.7, 0.3, 120, 120)),
             (f"[fill {ff:.0%}] exits 0.7->0.0", {"fill_frac": ff}, X(0.7, 0.0, 120, 120)),
             (f"[fill {ff:.0%}] tiered 8c+ 5k/12k, exits ->0", {"fill_frac": ff, **T}, X(0.7, 0.0, 120, 120)),
             (f"[fill {ff:.0%}] tiered, exits ->0, budget 90k", {"fill_frac": ff, "budget": 90_000, **T}, X(0.7, 0.0, 120, 120)),
             (f"[fill {ff:.0%}] flat 5k/12k, exits ->0", {"fill_frac": ff, "max_entry": 5000, "race_cap": 12_000}, X(0.7, 0.0, 120, 120)),
             (f"[fill {ff:.0%}] tiered 8c+ 8k/16k, exits ->0", {"fill_frac": ff, "big_edge": 0.08, "big_max_entry": 8000, "big_race_cap": 16_000}, X(0.7, 0.0, 120, 120))]
print(f"{len(trades):,} trades, entries from {start}; 70k budget, 3k/take, 8k/race, edge 6c, FV .1-.9 unless noted\n")
print(f"{'policy':44} {'P&L@K':>7} {'@contest':>8} {'realised':>8} {'open@end':>8} {'avg cap':>8} {'@c/1k/day':>9} {'exited':>6} {'hold h':>6} {'entries':>7}")
for name, over, x in runs:
    a = argparse.Namespace(**{**base, **over})
    a.entry_from = start_ts
    r = bt.simulate(bt.Policy(name, True, x), markets, fv, trades, fvs, a, end_ts)
    days = (end_ts - start_ts) / 86400
    per = r["pnl_contest"] / max(r["avg_cap"], 1) * 1000 / days
    hold = "-" if r["median_hold_h"] is None else f"{r['median_hold_h']:.0f}"
    print(f"{name:44} {r['pnl']:7,.0f} {r['pnl_contest']:8,.0f} {r['realized']:8,.0f} {r['open_cost_end']:8,.0f} {r['avg_cap']:8,.0f} "
          f"{per:9.1f} {r['exited_pct']:6.0%} {hold:>6} {r['entries']:7}", flush=True)
