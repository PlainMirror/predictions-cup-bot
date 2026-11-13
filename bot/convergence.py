"""How fast do contest prices converge to Kalshi? (READ-ONLY research; feeds the exit-policy choice.)

    py convergence.py [--min-gap 0.03] [--days 10]

Uses the collector's trade history (contest) and Kalshi's public hourly candles (fair value). For every hour in which a
contest market traded at least `min_gap` away from Kalshi's fair value, it follows the same market forward and records
how long until the contest price closed 50% / 90% of that gap (or whether it never did). Output, per market and overall:
  - median hours to close 50% and 90% of a gap, and the share of gaps that never closed within the window
  - gap persistence: correlation of the gap now with the gap 1h / 6h / 24h later (1.0 = gaps never close)
These numbers pick ExitParams: if gaps close 90% within a few hours, capture most of the gap (start_capture ~0.9, short
glide); if they linger for days, take less of the gap and recycle the cash (lower end_capture).
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics as st
import time
from pathlib import Path

from common import http_json, qs
import kalshi

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "market_data.sqlite"


def kalshi_hourly(ticker: str, start: int, end: int) -> dict[int, float]:
    series = ticker.split("-")[0]
    d = http_json("GET", f"{kalshi.BASE}/series/{series}/markets/{ticker}/candlesticks" +
                  qs({"start_ts": start, "end_ts": end, "period_interval": 60}))
    out = {}
    for c in d.get("candlesticks", []):
        b, a = c["yes_bid"].get("close_dollars"), c["yes_ask"].get("close_dollars")
        if b is not None and a is not None and 0 < float(a) and float(a) - float(b) <= 0.08:
            out[int(c["end_period_ts"]) // 3600] = (float(a) + float(b)) / 2
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-gap", type=float, default=0.03)
    ap.add_argument("--days", type=float, default=10)
    a = ap.parse_args()
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    now = int(time.time())
    start = now - int(a.days * 86400)
    mk = db.execute("SELECT exchange_id, title, office, place, party FROM markets WHERE office IS NOT NULL").fetchall()
    kmap = {}
    for t, o, p, pty in db.execute("SELECT ticker, office, place, party FROM kalshi_map"):
        kmap.setdefault((o, p, pty), t)
    events, persist, per_market = [], {1: [], 6: [], 24: []}, {}
    done_n = 0
    for ex, title, office, place, party in mk:
        tk = kmap.get((office, place, party))
        if not tk:
            continue
        trades = db.execute("SELECT ts, price FROM trades WHERE exchange_id=? AND ts>=? AND price IS NOT NULL ORDER BY ts",
                            (ex, start)).fetchall()
        if len(trades) < 5:
            continue
        try:
            fv = kalshi_hourly(tk, start, now)
        except Exception:
            continue
        time.sleep(0.1)
        # hourly contest price = last trade in the hour
        hp = {}
        for ts, px in trades:
            hp[int(ts) // 3600] = px
        hours = sorted(h for h in hp if h in fv or h - 1 in fv)
        gaps = {}
        for h in hours:
            f = fv.get(h, fv.get(h - 1))
            gaps[h] = hp[h] - f
        for lag in persist:
            for h, g in gaps.items():
                if h + lag in gaps:
                    persist[lag].append((g, gaps[h + lag]))
        mk_events = []
        hs = sorted(gaps)
        i = 0
        while i < len(hs):
            h0, g0 = hs[i], gaps[hs[i]]
            if abs(g0) < a.min_gap:
                i += 1
                continue
            t50 = t90 = None
            for h in hs[i + 1:]:
                g = gaps[h]
                if t50 is None and g * g0 <= 0.5 * g0 * g0:
                    t50 = h - h0
                if g * g0 <= 0.1 * g0 * g0:
                    t90 = h - h0
                    break
            f0 = fv.get(h0, fv.get(h0 - 1))
            vol24 = sum(1 for ts, _ in trades if h0 * 3600 - 86400 <= ts < h0 * 3600)
            ev = {"title": title, "gap0": g0, "t50": t50, "t90": t90, "horizon": hs[-1] - h0,
                  "competitive": 0.1 <= f0 <= 0.9, "busy": vol24 >= 50}
            mk_events.append(ev)
            # skip ahead to avoid counting the same episode every hour
            j = i + 1
            while j < len(hs) and abs(gaps[hs[j]]) >= a.min_gap and gaps[hs[j]] * g0 > 0:
                j += 1
            i = j
        if mk_events:
            per_market[title] = mk_events
            events += mk_events
        done_n += 1

    print(f"Markets analysed: {done_n}; gap episodes >= {a.min_gap:.2f}: {len(events)}\n")
    if not events:
        print("No data yet: let the collector back-fill trade history first.")
        return
    t50 = [e["t50"] for e in events if e["t50"] is not None]
    t90 = [e["t90"] for e in events if e["t90"] is not None]
    never50 = sum(e["t50"] is None for e in events)
    print(f"Closed 50% of the gap: {len(t50)}/{len(events)} episodes, median {st.median(t50) if t50 else '-'} h")
    print(f"Closed 90% of the gap: {len(t90)}/{len(events)} episodes, median {st.median(t90) if t90 else '-'} h")
    print(f"Never closed half within the data: {never50} ({never50 / len(events):.0%})")
    for lag, pairs in persist.items():
        if len(pairs) > 10:
            xs, ys = zip(*pairs)
            try:
                r = st.correlation(xs, ys)
                print(f"Gap persistence {lag:>2}h later: corr {r:.2f}  (1 = never closes, 0 = closes within {lag}h)")
            except st.StatisticsError:
                pass
    print("\nBy segment (competitive = Kalshi FV 0.10-0.90; busy = >=50 contest trades in the prior 24h):")
    segs = (("competitive & busy", lambda e: e["competitive"] and e["busy"]),
            ("competitive & quiet", lambda e: e["competitive"] and not e["busy"]),
            ("lopsided & busy", lambda e: not e["competitive"] and e["busy"]),
            ("lopsided & quiet", lambda e: not e["competitive"] and not e["busy"]))
    for name, sel in segs:
        ev = [e for e in events if sel(e)]
        if not ev:
            continue
        h50 = [e["t50"] for e in ev if e["t50"] is not None]
        h90 = [e["t90"] for e in ev if e["t90"] is not None]
        w6 = sum(1 for e in ev if e["t50"] is not None and e["t50"] <= 6) / len(ev)
        w24 = sum(1 for e in ev if e["t50"] is not None and e["t50"] <= 24) / len(ev)
        print(f"  {name:20} {len(ev):4} gaps | half closed {len(h50) / len(ev):4.0%} (median {st.median(h50) if h50 else '-'} h)"
              f" | half closed within 6h {w6:4.0%}, within 24h {w24:4.0%} | 90% closed median {st.median(h90) if h90 else '-'} h")
    print("\nSlowest-closing markets (median hours to close half, episodes):")
    rows = []
    for title, evs in per_market.items():
        h = [e["t50"] if e["t50"] is not None else e["horizon"] for e in evs]
        rows.append((st.median(h), len(evs), title, st.mean(abs(e["gap0"]) for e in evs)))
    for med, n, title, g in sorted(rows, reverse=True)[:15]:
        print(f"  {med:6.1f} h  {n:3} ep  avg gap {g:.3f}  {title}")


if __name__ == "__main__":
    main()
