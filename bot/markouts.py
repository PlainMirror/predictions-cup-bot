"""Markout analysis of live market-making logs: are we being picked off?

    py markouts.py                      # all logs in data/mm_logs (excluding paper/dry)
    py markouts.py data/mm_logs/mm_20261011_090000.jsonl

For every fill: edge at fill = (FV - price) * direction, and markout at +1m / +5m / +30m = (FV later - price) * direction.
If the edge shrinks or turns negative by +5m/+30m, our fills are informed against us (adverse selection):
widen that market's spread or cut its size. Also writes data/fills_features.csv, the training table for the
fill / markout models described in mm/README.md.
"""
from __future__ import annotations

import bisect
import csv
import glob
import json
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    # py markouts.py live | shadow  -> only that kind of log (default: the shadow's); or pass log paths
    kind = sys.argv[1] if len(sys.argv) == 2 and sys.argv[1].isalpha() else None   # live | shadow | shadowfresh
    paths = [] if kind else sys.argv[1:]
    paths = paths or sorted(glob.glob(str(ROOT / "data" / "mm_logs" / f"{kind or 'shadow'}_*.jsonl")))
    fills, fv_series, exits, values = [], defaultdict(list), [], []
    for p in paths:
        for line in open(p, encoding="utf-8"):
            r = json.loads(line)
            if r["type"] == "exit_fill":
                exits.append(r)
            if r["type"] == "snap":
                if r.get("value") is not None:
                    values.append((r["t"], r["value"]))
                for row in r["rows"]:
                    if row.get("fv") is not None:
                        fv_series[row["ex"]].append((r["t"], row["fv"], row.get("bb"), row.get("ba"), row.get("pos")))
            elif r["type"] == "fill" and r.get("yes_price") is not None:
                fills.append(r)
    if values:
        hrs = (values[-1][0] - values[0][0]) / 3600
        print(f"Account value (marked to Kalshi FV): {values[0][1]:,.0f} -> {values[-1][1]:,.0f} "
              f"({values[-1][1] - values[0][1]:+,.0f}) over {hrs:.1f} h")
    if exits:
        print(f"Exits filled: {len(exits)} lots, {sum(e['qty'] for e in exits):,.0f} shares, realised {sum(e['pnl'] for e in exits):+,.0f}, "
              f"median hold {st.median(e['held_h'] for e in exits):.1f} h")
    if not fills:
        raise SystemExit("No fills logged yet.")
    for s in fv_series.values():
        s.sort()

    def fv_at(ex, t):
        s = fv_series.get(ex) or []
        i = bisect.bisect_right([x[0] for x in s], t) - 1
        return s[i] if i >= 0 else None

    by_mkt = defaultdict(lambda: defaultdict(float))
    rows = []
    for f in fills:
        ex, d, px, q, t = f["exchange_id"], f["yes_dir"], f["yes_price"], f["qty"], f["t"]
        base = fv_at(ex, t)
        if not base:
            continue
        rec = {"t": t, "exchange_id": ex, "title": f.get("title"), "kind": f.get("kind"), "dir": d, "price": px, "qty": q,
               "fv": base[1], "book_bid": base[2], "book_ask": base[3], "pos_before": base[4],
               "edge": (base[1] - px) * d}
        for lab, secs in (("m1", 60), ("m5", 300), ("m30", 1800)):
            later = fv_at(ex, t + secs)
            rec[lab] = None if not later or later[0] < t + secs * 0.8 else (later[1] - px) * d
        rows.append(rec)
        b = by_mkt[f.get("title") or ex]
        b["n"] += 1
        b["shares"] += q
        for k in ("edge", "m1", "m5", "m30"):
            if rec[k] is not None:
                b[k] += rec[k] * q
                b[k + "_sh"] += q
    print(f"{len(rows)} fills from {len(paths)} log(s)\n")
    print(f"{'market':52} {'fills':>5} {'shares':>8} {'edge c':>7} {'+1m c':>7} {'+5m c':>7} {'+30m c':>7}")
    tot = defaultdict(float)
    for name, b in sorted(by_mkt.items(), key=lambda kv: -kv[1]["shares"]):
        cents = lambda k: f"{100 * b[k] / b[k + '_sh']:+.2f}" if b.get(k + "_sh") else "-"
        print(f"{name[:52]:52} {int(b['n']):5} {b['shares']:8,.0f} {cents('edge'):>7} {cents('m1'):>7} {cents('m5'):>7} {cents('m30'):>7}")
        for k, v in b.items():
            tot[k] += v
    c = lambda k: f"{100 * tot[k] / tot[k + '_sh']:+.2f}c" if tot.get(k + "_sh") else "-"
    print(f"\nALL: edge {c('edge')}  +1m {c('m1')}  +5m {c('m5')}  +30m {c('m30')} per share  (falling = adverse selection)")
    out = ROOT / "data" / "fills_features.csv"
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"Training table: {out}")


if __name__ == "__main__":
    main()
