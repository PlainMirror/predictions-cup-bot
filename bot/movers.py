"""Market movements over a time window, contest vs Kalshi (READ-ONLY, uses the collector DB).

    py movers.py --from "2026-10-10 08:00" [--to "2026-10-10 13:40"]      (times are US Eastern)
    py movers.py --overnight 2026-10-10                                    (8pm the day before -> 8am that day)

For every contest contract: contest mid at start and end (order book if recorded, else trade VWAP), high/low, volume,
and Kalshi fair value at start and end. Each move is classified:
  NEWS     Kalshi moved >= 2c too (real information); lag = minutes the contest took to catch half of Kalshi's move
  CONTEST  only the contest moved (student flow, convergence, or a whale), Kalshi flat
  OPENED   Kalshi moved but the contest did not follow (a gap opened: tradable)
Also reports, per mover, the 15-minute bucket with the biggest Kalshi and contest jumps, to line up with news timing.
Writes reports/movers_<from>_<to>.md.
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "market_data.sqlite"
ET = dt.timezone(dt.timedelta(hours=-4))  # EDT until Nov 1


def et(s: str) -> float:
    return dt.datetime.fromisoformat(s).replace(tzinfo=ET).timestamp()


def fmt_t(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, ET).strftime("%a %H:%M")


class Series:
    """Piecewise-constant price series with 'value at or before t'."""

    def __init__(self, pts):
        pts = sorted(p for p in pts if p[1] is not None)
        self.t = [p[0] for p in pts]
        self.v = [p[1] for p in pts]

    def at(self, ts, max_age=6 * 3600):
        i = bisect.bisect_right(self.t, ts) - 1
        if i < 0 or ts - self.t[i] > max_age:
            return None
        return self.v[i]

    def window(self, a, b):
        i, j = bisect.bisect_left(self.t, a), bisect.bisect_right(self.t, b)
        return self.v[i:j]


def contest_series(db, ex, a, b):
    """Mid from stored books where available, otherwise 30-trade VWAP proxies from trades."""
    rows = db.execute("SELECT recv_ts, best_bid, best_ask FROM books WHERE exchange_id=? AND recv_ts BETWEEN ? AND ? "
                      "AND best_bid IS NOT NULL AND best_ask IS NOT NULL AND best_ask - best_bid <= 0.05",
                      (ex, a - 6 * 3600, b)).fetchall()
    pts = [(t, (bb + ba) / 2) for t, bb, ba in rows]
    tr = db.execute("SELECT ts, price, quantity FROM trades WHERE exchange_id=? AND ts BETWEEN ? AND ? ORDER BY ts",
                    (ex, a - 6 * 3600, b)).fetchall()
    if not pts or pts[0][0] > a:  # books do not cover the start: use a rolling VWAP of trades
        win, out = [], []
        for t, p, q in tr:
            win.append((p, q))
            win = win[-30:]
            qs = sum(x[1] for x in win)
            out.append((t, sum(x[0] * x[1] for x in win) / qs if qs else p))
        first_book = pts[0][0] if pts else float("inf")
        pts = [x for x in out if x[0] < first_book] + pts
    vol = sum(q for t, p, q in tr if a <= t <= b)
    ntr = sum(1 for t, p, q in tr if a <= t <= b)
    return Series(pts), vol, ntr


def kalshi_series(db, ticker, a, b):
    rows = db.execute("SELECT ts, bid, ask FROM kalshi WHERE ticker=? AND ts BETWEEN ? AND ?", (ticker, a - 6 * 3600, b)).fetchall()
    pts = [(t, (bi + ak) / 2) for t, bi, ak in rows if bi is not None and ak is not None and ak - bi <= 0.1]
    cache = ROOT / "data" / "kalshi_hourly" / f"{ticker}.json"
    if cache.exists() and (not pts or pts[0][0] > a):
        first = pts[0][0] if pts else float("inf")
        hourly = [(int(k), v) for k, v in json.loads(cache.read_text()).items() if int(k) < first]
        pts = hourly + pts
    return Series(pts)


def biggest_jump(s: Series, a, b, step=900):
    best, when = 0.0, None
    t = a
    while t < b:
        x, y = s.at(t), s.at(min(t + step, b))
        if x is not None and y is not None and abs(y - x) > abs(best):
            best, when = y - x, t
        t += step
    return best, when


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="frm")
    ap.add_argument("--to", default=None)
    ap.add_argument("--overnight", help="date (YYYY-MM-DD): 8pm the previous evening to 8am this date")
    ap.add_argument("--min-move", type=float, default=0.02)
    a = ap.parse_args()
    if a.overnight:
        d = dt.date.fromisoformat(a.overnight)
        t0 = et(f"{d - dt.timedelta(days=1)} 20:00")
        t1 = et(f"{d} 08:00")
    else:
        t0 = et(a.frm)
        t1 = et(a.to) if a.to else dt.datetime.now(ET).timestamp()
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    kmap = {}
    for t, o, p, pty in db.execute("SELECT ticker, office, place, party FROM kalshi_map"):
        kmap.setdefault((o, p, pty), t)
    rows = []
    for ex, title, o, p, pty in db.execute("SELECT exchange_id, title, office, place, party FROM markets"):
        cs, vol, ntr = contest_series(db, ex, t0, t1)
        c0, c1 = cs.at(t0), cs.at(t1)
        if c0 is None or c1 is None:
            continue
        ks = kalshi_series(db, kmap[(o, p, pty)], t0, t1) if (o, p, pty) in kmap else None
        k0, k1 = (ks.at(t0), ks.at(t1)) if ks else (None, None)
        w = cs.window(t0, t1)
        dc = c1 - c0
        dk = None if k0 is None or k1 is None else k1 - k0
        if abs(dc) < a.min_move and (dk is None or abs(dk) < a.min_move):
            continue
        if dk is not None and abs(dk) >= 0.02:
            cls = "NEWS" if abs(dc) >= 0.5 * abs(dk) and dc * dk > 0 else "OPENED"
        else:
            cls = "CONTEST"
        lag = None
        if cls == "NEWS" and ks:
            half_k = k0 + dk / 2
            tk = next((t for t in ks.t if t0 <= t <= t1 and (ks.at(t) - half_k) * dk >= 0), None)
            tc = next((t for t in cs.t if t0 <= t <= t1 and (cs.at(t) - (c0 + dk / 2)) * dk >= 0), None)
            if tk and tc:
                lag = (tc - tk) / 60
        jk, wk = biggest_jump(ks, t0, t1) if ks else (0.0, None)
        jc, wc = biggest_jump(cs, t0, t1)
        rows.append({"title": title, "ex": ex, "race": f"{o}:{p}", "party": pty, "c0": c0, "c1": c1, "dc": dc,
                     "hi": max(w) if w else None, "lo": min(w) if w else None, "vol": vol, "trades": ntr,
                     "k0": k0, "k1": k1, "dk": dk, "cls": cls, "lag_min": lag, "gap_end": None if k1 is None else c1 - k1,
                     "kjump": jk, "kjump_t": wk, "cjump": jc, "cjump_t": wc})
    rows.sort(key=lambda r: -max(abs(r["dc"]), abs(r["dk"] or 0)))
    f = lambda x, s="+.3f": "-" if x is None else format(x, s)
    lines = [f"# Market movements {fmt_t(t0)} -> {fmt_t(t1)} ET ({(t1 - t0) / 3600:.1f} h)", "",
             f"{len(rows)} contracts moved >= {a.min_move:.2f} (contest or Kalshi).", "",
             "| contract | class | contest start→end | Δ contest | Kalshi start→end | Δ Kalshi | gap at end | contest lag (min) | biggest Kalshi 15-min jump | biggest contest 15-min jump | shares traded |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['title']} | {r['cls']} | {r['c0']:.3f}→{r['c1']:.3f} | {r['dc']:+.3f} | "
                     f"{f(r['k0'], '.3f')}→{f(r['k1'], '.3f')} | {f(r['dk'])} | {f(r['gap_end'])} | {f(r['lag_min'], '.0f')} | "
                     f"{r['kjump']:+.3f} @ {fmt_t(r['kjump_t']) if r['kjump_t'] else '-'} | {r['cjump']:+.3f} @ {fmt_t(r['cjump_t']) if r['cjump_t'] else '-'} | {r['vol']:,.0f} |")
    tag = f"{dt.datetime.fromtimestamp(t0, ET):%m%d_%H%M}_{dt.datetime.fromtimestamp(t1, ET):%m%d_%H%M}"
    out = ROOT / "reports" / f"movers_{tag}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    for l in lines[:2]:
        print(l)
    print(f"{'contract':52} {'cls':7} {'contest':>13} {'Δc':>7} {'Kalshi':>13} {'Δk':>7} {'gap':>6} {'lag':>5} {'Kalshi jump @':>18} {'shares':>9}")
    for r in rows[:40]:
        print(f"{r['title'][9:61]:52} {r['cls']:7} {r['c0']:.3f}->{r['c1']:.3f} {r['dc']:+7.3f} "
              f"{f(r['k0'], '.3f'):>5}->{f(r['k1'], '.3f'):<5} {f(r['dk']):>7} {f(r['gap_end']):>6} {f(r['lag_min'], '.0f'):>5} "
              f"{(format(r['kjump'], '+.3f') + ' ' + fmt_t(r['kjump_t'])) if r['kjump_t'] else '-':>18} {r['vol']:9,.0f}")
    print(f"\nFull table: {out}")


if __name__ == "__main__":
    main()
