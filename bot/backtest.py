"""Capital-efficiency backtest of the mispricing strategy on REAL contest trades (READ-ONLY research).

    py backtest.py                      # default grid of exit policies, full history and from Oct 3
    py backtest.py --start 2026-10-03 --take-edge 0.03 --fill-frac 0.5

Data: every contest trade the collector has back-filled (data/market_data.sqlite) + Kalshi hourly candles (fair value,
cached in data/kalshi_hourly/). No look-ahead: a trade at time t is judged against the last Kalshi candle COMPLETED before t.

Simulation per exit policy, with a cash budget (default 100k):
  ENTRY  a real trade printed at p with |p - FV| >= take_edge proves liquidity existed at p. We take at p worsened by `slip`
         (1 tick), for fill_frac (default 50%) of the printed size (other bots compete), capped per entry and per race,
         one direction per race, only if cash is available.
  EXIT   each lot rests a closing order at the exit-policy target (mm/exits.py). It fills when a LATER real trade prints
         through the target, again for fill_frac of the printed size. Exits return cash, which funds new entries.
  END    open lots are marked to Kalshi FV (= their expected value if held to settlement).
Metrics: P&L, peak and average capital tied up, capital-days, P&L per 1k of capital per day, turnover, % of entry
quantity exited and the median holding time. Capital efficiency = P&L / capital-days.
Caveats: no historical order books before Oct 10 (liquidity inferred from prints); fill_frac and slip are assumptions.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import json
import math
import sqlite3
import statistics as st
import time
from dataclasses import dataclass, field
from pathlib import Path

import kalshi
from common import http_json, qs
from mm.exits import ExitParams, Lot, exit_price

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "market_data.sqlite"
CACHE = ROOT / "data" / "kalshi_hourly"


# ------------------------------------------------------------------------------------------------ data
def kalshi_hourly(ticker: str, start: int, end: int) -> dict[int, float]:
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"{ticker}.json"
    if f.exists() and time.time() - f.stat().st_mtime < 3600:
        return {int(k): v for k, v in json.loads(f.read_text()).items()}
    series = ticker.split("-")[0]
    d = http_json("GET", f"{kalshi.BASE}/series/{series}/markets/{ticker}/candlesticks" +
                  qs({"start_ts": start, "end_ts": end, "period_interval": 60}))
    out = {}
    for c in d.get("candlesticks", []):
        b, a = c["yes_bid"].get("close_dollars"), c["yes_ask"].get("close_dollars")
        if b is not None and a is not None and 0 < float(a) and float(a) - float(b) <= 0.08:
            out[int(c["end_period_ts"])] = (float(a) + float(b)) / 2
    f.write_text(json.dumps(out))
    time.sleep(0.1)
    return out


def load(start_ts: float, end_ts: float):
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    kmap = {}
    for t, o, p, pty in db.execute("SELECT ticker, office, place, party FROM kalshi_map"):
        kmap.setdefault((o, p, pty), t)
    markets = {}
    for ex, title, o, p, pty in db.execute("SELECT exchange_id, title, office, place, party FROM markets WHERE office IS NOT NULL"):
        if (o, p, pty) in kmap:
            markets[ex] = {"title": title, "race": (o, p), "party": pty, "ticker": kmap[(o, p, pty)]}
    print(f"Fetching Kalshi hourly fair values for {len(markets)} contracts (cached after the first run)...")
    k_start = int(start_ts) - 3 * 86400
    fv = {}
    for ex, m in markets.items():
        try:
            h = kalshi_hourly(m["ticker"], k_start, int(end_ts) + 3600)
        except Exception:
            continue
        if h:
            keys = sorted(h)
            fv[ex] = (keys, [h[k] for k in keys])
    trades = db.execute("SELECT ts, exchange_id, price, quantity FROM trades WHERE ts>=? AND ts<=? AND price IS NOT NULL "
                        "AND quantity>0 ORDER BY ts", (start_ts, end_ts)).fetchall()
    trades = [t for t in trades if t[1] in fv]
    done = db.execute("SELECT COUNT(*) FROM backfill WHERE done=1").fetchone()[0]
    return markets, fv, trades, done


def vwap_history(trades, bucket=900):
    """ex -> (bucket END times, 15-min VWAP), for 'what was the contest price H hours ago'."""
    acc = {}
    for ts, ex, p, q in trades:
        b = int(ts // bucket * bucket) + bucket
        d = acc.setdefault(ex, {})
        pq, qq = d.get(b, (0.0, 0.0))
        d[b] = (pq + p * q, qq + q)
    out = {}
    for ex, d in acc.items():
        keys = sorted(d)
        out[ex] = (keys, [d[k][0] / d[k][1] for k in keys])
    return out


def price_at(hist, ex, ts):
    h = hist.get(ex)
    if not h:
        return None
    i = bisect.bisect_right(h[0], ts) - 1
    return h[1][i] if i >= 0 and ts - h[0][i] <= 6 * 3600 else None


def fv_jumped(fv, ex, ts, size):
    """Did the last completed Kalshi candle move >= size vs the one before (news or thin-market noise)?"""
    keys, vals = fv[ex]
    i = bisect.bisect_right(keys, ts) - 1
    return i >= 1 and keys[i] - keys[i - 1] <= 2 * 3600 and abs(vals[i] - vals[i - 1]) >= size


def fv_at(fv, ex, ts):
    keys, vals = fv[ex]
    i = bisect.bisect_right(keys, ts) - 1          # last candle whose END is at or before ts (completed)
    if i < 0 or ts - keys[i] > 3 * 3600:
        return None
    return vals[i]


# ------------------------------------------------------------------------------------------------ simulation
@dataclass
class Policy:
    name: str
    exits: bool = True
    X: ExitParams = field(default_factory=ExitParams)


def simulate(pol: Policy, markets, fv, trades, fvs, a, end_ts):
    """trades: [(ts, ex, p, q)], fvs: precomputed FV per trade (None if unknown)."""
    cash = a.budget
    lots: dict[str, list[Lot]] = {}
    race_cost: dict[tuple, float] = {}
    race_dir: dict[tuple, int] = {}
    open_cost, t_prev, cap_days, peak = 0.0, None, 0.0, 0.0
    n_entries = entry_notional = entry_qty = 0.0
    realized, exited_qty, holds = 0.0, 0.0, []
    edge = a.take_edge
    daily, next_mark = [], (math.floor(trades[0][0] / 86400) + 1) * 86400 if trades else None
    last_px = {}
    for (ts, ex, p, q), f in zip(trades, fvs):
        last_px[ex] = p
        while next_mark is not None and ts >= next_mark:
            val = cash
            for ex2, ls2 in lots.items():
                f2 = fv_at(fv, ex2, next_mark)
                if f2 is None:
                    f2 = fv[ex2][1][-1]
                val += sum(l.qty * (f2 if l.d > 0 else 1 - f2) for l in ls2)
            daily.append((next_mark, val, open_cost))
            next_mark += 86400
        if t_prev is not None and open_cost:
            cap_days += open_cost * (ts - t_prev) / 86400
        t_prev = ts
        if f is None:
            continue
        avail = q * a.fill_frac
        # ---- exits on this contract (fast reject: price must be at least min_profit better than entry)
        ls = lots.get(ex)
        if pol.exits and ls:
            for lot in ls:
                if avail <= 0:
                    break
                tgt, _ = exit_price(lot, f, ts, pol.X, 0.005)
                if tgt is None:        # time stop / liquidation: exit at market = this print, worsened by slip
                    tgt = p - a.slip if lot.d > 0 else p + a.slip
                elif (lot.d > 0 and p < lot.p0 + 0.005) or (lot.d < 0 and p > lot.p0 - 0.005):
                    continue
                if (lot.d > 0 and p >= tgt - 1e-9) or (lot.d < 0 and p <= tgt + 1e-9):
                    fq = min(lot.qty, avail)
                    avail -= fq
                    lot.qty -= fq
                    unit0 = lot.p0 if lot.d > 0 else 1 - lot.p0
                    back = fq * (tgt if lot.d > 0 else 1 - tgt)
                    cash += back
                    realized += back - fq * unit0
                    open_cost -= fq * unit0
                    race_cost[markets[ex]["race"]] -= fq * unit0
                    exited_qty += fq
                    holds.append(((ts - lot.t0) / 3600, fq))
            lots[ex] = [l for l in ls if l.qty > 1e-9]
        # ---- entry
        if avail <= 0 or ts < a.entry_from or (f - edge < p < f + edge) or not (a.fv_lo <= f <= a.fv_hi):
            continue
        if a.jump_skip and fv_jumped(fv, ex, ts, a.jump_skip):
            continue      # Kalshi just jumped: wait for it to be confirmed before trusting it
        if a.fresh_h:
            # only gaps that OPENED recently: H hours ago the contest was within fresh_max of Kalshi (in our direction)
            tb = ts - a.fresh_h * 3600
            c_then, f_then = price_at(a.hist, ex, tb), fv_at(fv, ex, tb)
            if c_then is None or f_then is None:
                continue
            want = 1 if p < f else -1           # +1: contest below FV (we buy)
            if want * (f_then - c_then) >= a.fresh_max:
                continue
        race, party = markets[ex]["race"], markets[ex]["party"]
        if p <= f - edge:
            d, px = 1, min(p + a.slip, f - edge)
        else:
            d, px = -1, max(p - a.slip, f + edge)
        backs = party if d > 0 else {"D": "R", "R": "D"}.get(party, "x" + party)
        race_side = 1 if backs == "D" else -1 if backs == "R" else 0
        if race_side and race_dir.get(race, race_side) != race_side and race_cost.get(race, 0) > 1:
            continue  # never back both sides of a race
        unit = px if d > 0 else 1 - px
        # optional tiered sizing (research, Oct 22): gaps >= big_edge get big_max_entry / big_race_cap
        big = getattr(a, "big_edge", None) is not None and ((f - p) if d > 0 else (p - f)) >= a.big_edge
        room = min(a.big_max_entry if big else a.max_entry, (a.big_race_cap if big else a.race_cap) - race_cost.get(race, 0.0), cash)
        nq = math.floor(min(avail, room / max(unit, 0.005)))
        if nq < 1:
            continue
        cash -= nq * unit
        open_cost += nq * unit
        peak = max(peak, open_cost)
        race_cost[race] = race_cost.get(race, 0.0) + nq * unit
        if race_side:
            race_dir[race] = race_side
        lots.setdefault(ex, []).append(Lot(ex, d, nq, px, f, ts))
        n_entries += 1
        entry_notional += nq * unit
        entry_qty += nq
    if t_prev is not None:
        cap_days += open_cost * max(0.0, end_ts - t_prev) / 86400
    unreal = unreal_c = 0.0
    for ex, ls in lots.items():
        f = fv_at(fv, ex, end_ts) or fv[ex][1][-1]
        c = last_px.get(ex, f)
        for l in ls:
            unreal += l.qty * ((f - l.p0) if l.d > 0 else (l.p0 - f))
            # marked to the CONTEST: last print, minus the cost of crossing to get out
            unreal_c += l.qty * ((c - a.slip - l.p0) if l.d > 0 else (l.p0 - c - a.slip))
    pnl = realized + unreal
    days = (end_ts - a.entry_from) / 86400
    avg_cap = cap_days / days if days > 0 else 0.0
    med_hold = None
    if holds:
        holds.sort()
        half, acc = sum(w for _, w in holds) / 2, 0.0
        for h, w in holds:
            acc += w
            if acc >= half:
                med_hold = h
                break
    return {"policy": pol.name, "pnl": pnl, "pnl_contest": realized + unreal_c, "realized": realized, "unrealized": unreal,
            "open_cost_end": open_cost,
            "peak_cap": peak, "avg_cap": avg_cap, "cap_days": cap_days,
            "pnl_per_1k_day": 1000 * pnl / cap_days if cap_days else 0.0,
            "roc_peak": pnl / peak if peak else 0.0, "entries": int(n_entries),
            "turnover": entry_notional / avg_cap if avg_cap else 0.0,
            "exited_pct": exited_qty / entry_qty if entry_qty else 0.0, "median_hold_h": med_hold, "_daily": daily}


def grid() -> list[Policy]:
    pols = [Policy("hold to settlement (no exits)", exits=False)]
    for sc in (1.0, 0.9, 0.7):
        for ec in (0.6, 0.3, 0.0):
            if ec >= sc:
                continue
            for gh in (6, 24, 72):
                pols.append(Policy(f"start {sc:.0%} -> end {ec:.0%} over {gh}h", True,
                                   ExitParams(start_capture=sc, end_capture=ec, glide_hours=gh, final_exit_et="2026-12-31T00:00")))
    return pols


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2026-10-01", help="first day trades are replayed (UTC date)")
    ap.add_argument("--entry-from", default=None, help="first day entries are allowed (default = --start)")
    ap.add_argument("--budget", type=float, default=100_000)
    ap.add_argument("--take-edge", type=float, default=0.03)
    ap.add_argument("--slip", type=float, default=0.005)
    ap.add_argument("--fill-frac", type=float, default=0.5)
    ap.add_argument("--max-entry", type=float, default=5_000)
    ap.add_argument("--race-cap", type=float, default=15_000)
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--fv-band", default="0,1", help="only enter when Kalshi FV is inside lo,hi (e.g. 0.1,0.9 = competitive races)")
    ap.add_argument("--fresh-h", type=float, default=0.0, help="only enter gaps that opened within this many hours (0 = off)")
    ap.add_argument("--fresh-max", type=float, default=0.01, help="'opened' = the gap H hours ago was below this")
    ap.add_argument("--jump-skip", type=float, default=0.0, help="skip entries right after a Kalshi candle move >= this (0 = off)")
    ap.add_argument("--quiet-daily", action="store_true", help="with --only: print the summary table only")
    ap.add_argument("--policy", default="", help="custom exit policy start,end,glide_h[,time_stop_h] (replaces the grid)")
    ap.add_argument("--only", default="", help="comma-separated substrings: run only matching policies (and show daily P&L)")
    a = ap.parse_args()
    a.fv_lo, a.fv_hi = (float(x) for x in a.fv_band.split(","))
    start_ts = dt.datetime.fromisoformat(a.start).replace(tzinfo=dt.timezone.utc).timestamp()
    a.entry_from = dt.datetime.fromisoformat(a.entry_from).replace(tzinfo=dt.timezone.utc).timestamp() if a.entry_from else start_ts
    end_ts = time.time()
    markets, fv, trades, done = load(start_ts, end_ts)
    print(f"{len(trades):,} trades on {len({t[1] for t in trades})} contracts with Kalshi FV "
          f"(trade-history back-fill complete for {done} of 237 contracts; results firm up as it finishes)")
    print(f"budget {a.budget:,.0f} | take edge {a.take_edge} | slip {a.slip} | fill {a.fill_frac:.0%} of printed size | "
          f"max {a.max_entry:,.0f}/entry, {a.race_cap:,.0f}/race | FV band {a.fv_lo}-{a.fv_hi} | entries from {dt.datetime.fromtimestamp(a.entry_from, dt.timezone.utc):%b %d}")
    fvs = [fv_at(fv, ex, ts) for ts, ex, _, _ in trades]
    a.hist = vwap_history(trades) if a.fresh_h else None
    t0 = time.time()
    res = []
    pols = grid()
    if a.policy:
        v = [float(x) for x in a.policy.split(",")]
        X = ExitParams(start_capture=v[0], end_capture=v[1], glide_hours=v[2], time_stop_h=v[3] if len(v) > 3 else None,
                       final_exit_et="2026-12-31T00:00", liquidate_et="2026-12-31T00:00")
        pols = [Policy(f"custom {a.policy}", True, X)]
    if a.only:
        keys = [k.strip() for k in a.only.split(",") if k.strip()]
        pols = [p_ for p_ in pols if any(k in p_.name for k in keys)]
    for i, pol in enumerate(pols):
        res.append(simulate(pol, markets, fv, trades, fvs, a, end_ts))
        if i == 0:
            print(f"(one policy takes {time.time() - t0:.0f} s; {len(pols)} policies)")
    res.sort(key=lambda r: -r["pnl"])
    hdr = (f"{'policy':34} {'P&L':>8} {'@contest':>9} {'realised':>9} {'open@end':>8} {'peak cap':>9} {'avg cap':>8} {'P&L/1k/day':>10} "
           f"{'turnover':>8} {'exited':>6} {'hold h':>6} {'entries':>7}")
    print("\n" + hdr)
    show = res[:a.top] + [r for r in res[a.top:] if r["policy"].startswith("hold")]
    for r in show:
        hold = "-" if r["median_hold_h"] is None else f"{r['median_hold_h']:.1f}"
        print(f"{r['policy']:34} {r['pnl']:8,.0f} {r['pnl_contest']:9,.0f} {r['realized']:9,.0f} {r['open_cost_end']:8,.0f} {r['peak_cap']:9,.0f} {r['avg_cap']:8,.0f} "
              f"{r['pnl_per_1k_day']:10.1f} {r['turnover']:8.1f} {r['exited_pct']:6.0%} {hold:>6} {r['entries']:7}")
    if a.only and not a.quiet_daily:
        for r in res:
            print()
            print(f"Daily account value (cash + positions at Kalshi FV) - {r['policy']}:")
            prev = a.budget
            for t, v, oc in r["_daily"]:
                print(f"  {dt.datetime.fromtimestamp(t, dt.timezone.utc):%b %d}  value {v:10,.0f}  day {v - prev:+8,.0f}  capital in positions {oc:9,.0f}")
                prev = v
    for r in res:
        r.pop("_daily", None)
    out = ROOT / "reports" / f"backtest_{dt.datetime.now():%Y%m%d_%H%M}_{a.start}.csv"
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(res[0].keys()))
        w.writeheader()
        w.writerows(res)
    print(f"\nAll {len(res)} policies: {out}")


if __name__ == "__main__":
    main()
