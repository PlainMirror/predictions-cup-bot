"""Live trade ledger (read-only): every realised exit matched to the take that opened it.
Writes research/ledger.csv. Exit type: 'target' (resting exit or exit take before the time stop), 'time stop' (fv lots
held >= 119.5 h), 'model hold' (model lots held >= 23.9 h), 'restart' (lot adopted after a restart: entry unknown)."""
import glob
import json
from collections import defaultdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
KEEP = ('"type": "take"', '"type": "fill"', '"type": "exit_fill"', '"type": "adopt"', '"type": "start"')

takes = {}                      # order_id -> take record
fills = defaultdict(list)       # ex -> [(t, yes_price, qty, mode, fv, order_id, dir)]
exits = []
titles = {}
for p in sorted(glob.glob(str(ROOT / "data" / "mm_logs" / "live_*.jsonl"))):
    last_fill = {}
    for line in open(p, encoding="utf-8"):
        if not any(k in line[:40] for k in KEEP):
            continue
        r = json.loads(line)
        ty = r["type"]
        if ty == "start":
            for m in r.get("markets") or []:
                titles[m["exchange_id"]] = m["title"]
        elif ty == "take":
            oid = (r.get("res") or {}).get("orderId")
            if oid is not None:
                takes[oid] = r
        elif ty == "fill":
            last_fill[r["exchange_id"]] = r
            if r.get("kind") == "take":
                tk = takes.get(r.get("order_id")) or {}
                fills[r["exchange_id"]].append({"t": r["t"], "p0": r["yes_price"], "qty": r["qty"], "dir": r["yes_dir"],
                                                "mode": tk.get("mode") or "fv", "fv_take": tk.get("fv"), "used": 0.0})
        elif ty == "exit_fill":
            f = last_fill.get(r["exchange_id"]) or {}
            exits.append(dict(r, exit_kind=f.get("kind"), log=Path(p).name))

rows = []
for x in exits:
    ex, t0 = x["exchange_id"], x["t"] - x["held_h"] * 3600
    cand = [f for f in fills.get(ex, []) if abs(f["p0"] - x["entry"]) < 1e-6 and abs(f["t"] - t0) < 120 and f["used"] < f["qty"] - 1e-9]
    f = min(cand, key=lambda f: abs(f["t"] - t0)) if cand else None
    if f:
        f["used"] += x["qty"]
    d = (f["dir"] if f else (1 if x["pnl"] * (x["exit"] - x["entry"]) >= 0 else -1))
    mode = f["mode"] if f else "restart"
    gap = None if not f or f["fv_take"] is None else d * (f["fv_take"] - f["p0"])
    if mode == "restart":
        kind = "restart"
    elif mode == "model":
        kind = "model hold" if x["held_h"] >= 23.9 else "target"
    else:
        kind = "time stop" if x["held_h"] >= 119.5 else "target"
    rows.append({"t": x["t"], "ex": ex, "title": titles.get(ex, ex)[9:60], "mode": mode, "dir": d, "qty": x["qty"],
                 "entry": x["entry"], "exit": x["exit"], "pnl": x["pnl"], "ps": x["pnl"] / x["qty"] if x["qty"] else 0,
                 "held_h": x["held_h"], "gap": gap, "kind": kind, "fill_kind": x["exit_kind"], "log": x["log"]})
df = pd.DataFrame(rows)
df.to_csv(ROOT / "research" / "ledger.csv", index=False)
open_lots = [(ex, f) for ex, fs in fills.items() for f in fs if f["qty"] - f["used"] > 1]
print(f"exits {len(df)} ({df.qty.sum():,.0f} sh), realised {df.pnl.sum():+,.0f}; unmatched {sum(df['mode'] == 'restart')}")
print(f"take fills {sum(len(v) for v in fills.values())}, still open (by this matching) {len(open_lots)}")
