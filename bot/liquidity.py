"""Liquidity scanner from collected data (READ-ONLY, no API reads). Picks where market making can earn.

    py liquidity.py [--hours 24] [--top 30] [--save]

Per contract, from data/market_data.sqlite:
  trades/h, shares/h      student flow (what passive quotes can catch)
  median spread           from stored books; share of time the book is 1 tick wide (= Susquehanna-quoted, hard to beat)
  depth within 2c of mid  how much size sits near the price
  last vs Kalshi FV       gap now (mispricing) from the collector's Kalshi table
score = shares/h x capturable half-spread, discounted when the book is usually 1 tick wide.
--save writes data/mm_markets.json (the market-making set used by run_mm.py --shadow / --dry / --live).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics as st
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "market_data.sqlite"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--save", action="store_true")
    a = ap.parse_args()
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    since = time.time() - a.hours * 3600
    kmid = {}
    for t, o, p, pty in db.execute("SELECT ticker, office, place, party FROM kalshi_map"):
        row = db.execute("SELECT bid, ask FROM kalshi WHERE ticker=? ORDER BY ts DESC LIMIT 1", (t,)).fetchone()
        if row and row[0] is not None and row[1] is not None and row[1] - row[0] <= 0.1:
            kmid[(o, p, pty)] = (row[0] + row[1]) / 2
    rows = []
    for ex, mid_id, title, o, p, pty in db.execute("SELECT exchange_id, market_id, title, office, place, party FROM markets"):
        tr = db.execute("SELECT COUNT(*), COALESCE(SUM(quantity),0) FROM trades WHERE exchange_id=? AND ts>=?", (ex, since)).fetchone()
        books = db.execute("SELECT best_bid, best_ask, bids, asks FROM books WHERE exchange_id=? AND recv_ts>=? ORDER BY recv_ts",
                           (ex, time.time() - min(a.hours, 6) * 3600)).fetchall()
        spreads = [b[1] - b[0] for b in books if b[0] is not None and b[1] is not None]
        if not spreads:
            continue
        one_tick = sum(s <= 0.0051 for s in spreads) / len(spreads)
        last = books[-1]
        mid = (last[0] + last[1]) / 2 if last[0] is not None and last[1] is not None else None
        depth = 0.0
        if mid is not None:
            depth = sum(q for px, q in json.loads(last[2]) if px >= mid - 0.02) + sum(q for px, q in json.loads(last[3]) if px <= mid + 0.02)
        fv = kmid.get((o, p, pty))
        hours = max(a.hours, 1e-9)
        sh_per_h = tr[1] / hours
        capture = min(st.median(spreads) / 2, 0.03) * (0.15 if one_tick > 0.7 else 1.0)
        rows.append({"market_id": mid_id, "exchange_id": ex, "title": title, "trades_h": tr[0] / hours, "shares_h": sh_per_h,
                     "med_spread": st.median(spreads), "one_tick": one_tick, "depth2c": depth, "fv": fv,
                     "gap": None if fv is None or mid is None else mid - fv, "score": sh_per_h * capture})
    rows.sort(key=lambda r: -r["score"])
    print(f"{'contract':56} {'tr/h':>5} {'sh/h':>7} {'spread':>6} {'1tick':>5} {'depth2c':>8} {'gap':>6} {'score':>6}")
    for r in rows[:a.top]:
        g = "-" if r["gap"] is None else f"{r['gap']:+.3f}"
        print(f"{r['title'][9:65]:56} {r['trades_h']:5.1f} {r['shares_h']:7.0f} {r['med_spread']:6.3f} {r['one_tick']:5.0%} "
              f"{r['depth2c']:8.0f} {g:>6} {r['score']:6.1f}")
    if a.save:
        chosen = [r for r in rows[:a.top] if r["fv"] is not None]
        (ROOT / "data" / "mm_markets.json").write_text(json.dumps(chosen, indent=1), encoding="utf-8")
        print(f"saved {len(chosen)} markets to data/mm_markets.json")


if __name__ == "__main__":
    main()
