"""Intraday scalping back-test: a few high-movement contracts, fade short-term swings around the contest's own average.
(READ-ONLY research on the collector DB; places nothing.)

    py scalp.py                       # parameter grid on Oct 4-8 (train), best settings re-run untouched on Oct 9-now (test)
    py scalp.py --one N,X,H,T         # one setting over the whole period with daily P&L (N markets, X c, H h anchor, T h)

Universe: every day at 00:00 ET, the N contracts with the largest trailing-48h movement (sum of |15-min VWAP changes|),
price 0.05-0.95, 24h volume >= 50k shares. Each gets capital/N.
Anchor: volume-weighted EWMA of the contest's own trade prices (half-life H hours).
Entry (passive, conservative fills): a resting buy at anchor - X and a resting sell at anchor + X, re-placed every 5 min.
  A print STRICTLY through our price fills min(remaining, printed size): that seller/buyer would have hit us first.
  News filter: no fading when Kalshi moved >= 1.5c the same way in the last 2 h (that move is information).
Exit: take-profit resting at entry + 60% of X (passive, strictly-through fills); stop-loss at 2X against (aggressive:
  next print -/+ slip); time stop after T hours (aggressive). One position per contract at a time.
P&L is in CONTEST prices (what the platform pays), open positions marked at the last print minus slip.
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import sys

import numpy as np
import pandas as pd

import overnight as o

TICK = 0.005


def snap(x, up):
    return round((math.ceil(x / TICK - 1e-9) if up else math.floor(x / TICK + 1e-9)) * TICK, 4)


def universe(D: o.Data, day_start: float, n: int) -> list[str]:
    h48 = D.span(D.vwap, day_start - 172800, day_start)
    move = h48.diff().abs().sum()
    vol24 = D.span(D.vol, day_start - 86400, day_start).sum()
    last = D.at(D.vwap, day_start)
    ok = (last >= 0.05) & (last <= 0.95) & (vol24 >= 50_000)
    return move[ok].sort_values(ascending=False).head(n).index.tolist()


def simulate(D: o.Data, t0: float, t1: float, n: int, X: float, H: float, T: float, capital: float = 70_000,
             slip: float = 0.005, tp_frac: float = 0.6, sl_mult: float = 2.0, news: float = 0.015, verbose=False):
    per = capital / n
    trades, daily = [], {}
    day = t0
    open_pos = {}                # ex -> dict(d, qty, px, t, tp, sl)
    cap = [0.0, 0.0]             # capital in positions now, peak
    while day < t1:
        uni = universe(D, day, n)
        end = min(day + 86400, t1)
        active = set(uni) | set(open_pos)
        for ex in active:
            if ex not in D.T:
                continue
            ts, px, qs = D.T[ex]
            # anchor warm-up from the previous 24h
            i0, i1 = np.searchsorted(ts, day - 86400), np.searchsorted(ts, end)
            lam = math.log(2) / (H * 3600)
            num = den = 0.0
            t_prev = None
            resting = None        # (side, price, remaining, placed_t)
            last_place = -1e18
            pos = open_pos.get(ex)
            for k in range(i0, i1):
                t, p, q = ts[k], px[k], qs[k]
                if t_prev is not None:
                    w = math.exp(-lam * (t - t_prev))
                    num, den = num * w, den * w
                t_prev = t
                if t >= day:
                    # ---- manage the open position on this print
                    if pos is not None:
                        d = pos["d"]
                        exit_px = None
                        # entry order still resting (5 min): further prints through it add to the position up to budget
                        if t - pos["t"] < 300 and d * (pos["px"] - p) > 1e-9 and pos["qty"] < pos["target"]:
                            add = min(q, pos["target"] - pos["qty"])
                            pos["qty"] += add
                            cap[0] += add * pos["unit"]
                            cap[1] = max(cap[1], cap[0])
                            continue_exit = False
                        if d * (p - pos["tp"]) > 1e-9:                       # print through our resting take-profit
                            exit_px = pos["tp"]
                        elif d * (p - pos["px"]) <= -pos["sl"] or t - pos["t"] >= T * 3600:
                            exit_px = p - d * slip                           # stop / time: cross at the next print
                        if exit_px is not None:
                            fill = min(pos["qty"], q) if exit_px == pos["tp"] else pos["qty"]
                            pnl = d * (exit_px - pos["px"]) * fill
                            trades.append({"t": t, "ex": ex, "pnl": pnl, "qty": fill, "hold_h": (t - pos["t"]) / 3600,
                                           "why": "tp" if exit_px == pos["tp"] else "stop/time"})
                            daily[int((t - t0) // 86400)] = daily.get(int((t - t0) // 86400), 0.0) + pnl
                            pos["qty"] -= fill
                            cap[0] -= fill * pos["unit"]
                            if pos["qty"] < 1:
                                pos = None
                                open_pos.pop(ex, None)
                            resting = None
                    # ---- entries (only for today's universe, flat, anchor warmed up)
                    elif ex in uni and den > 0:
                        anchor = num / den
                        if t - last_place >= 300:
                            # news filter: Kalshi move over the last 2 h
                            kb = D.fv_known[ex] if ex in D.fv_known.columns else None
                            kmove = 0.0
                            if kb is not None:
                                b_now = int(t // o.BUCKET * o.BUCKET)
                                a_, b_ = kb.get(b_now - 8 * o.BUCKET), kb.get(b_now)
                                if a_ is not None and b_ is not None and a_ == a_ and b_ == b_:
                                    kmove = b_ - a_
                            resting = [(+1, snap(anchor - X, False)), (-1, snap(anchor + X, True)), kmove]
                            last_place = t
                        if resting is not None:
                            kmove = resting[2]
                            for d, lim in resting[:2]:
                                if d * (lim - p) > 1e-9:                      # print strictly through our resting order
                                    if news and d * kmove <= -news:           # Kalshi moved the same way: real news, skip
                                        continue
                                    unit = lim if d > 0 else 1 - lim
                                    target = per / max(unit, 0.01)
                                    qty = min(q, target)
                                    pos = {"d": d, "qty": qty, "px": lim, "t": t, "tp": snap(lim + d * tp_frac * X, d > 0),
                                           "sl": sl_mult * X, "unit": unit, "target": target}
                                    cap[0] += qty * unit
                                    cap[1] = max(cap[1], cap[0])
                                    open_pos[ex] = pos
                                    resting = None
                                    break
                num += p * q
                den += q
            if pos is not None:
                open_pos[ex] = pos
        day = end
    # mark open positions at the last print minus slip
    for ex, pos in open_pos.items():
        ts, px, qs = D.T[ex]
        j = np.searchsorted(ts, t1) - 1
        if j >= 0:
            pnl = pos["d"] * (px[j] - pos["d"] * slip - pos["px"]) * pos["qty"]
            trades.append({"t": t1, "ex": ex, "pnl": pnl, "qty": pos["qty"], "hold_h": (t1 - pos["t"]) / 3600, "why": "open"})
            daily[int((t1 - 1 - t0) // 86400)] = daily.get(int((t1 - 1 - t0) // 86400), 0.0) + pnl
    tr = pd.DataFrame(trades)
    tr.attrs["peak_capital"] = cap[1]
    return tr, daily


def summary(tr, daily, days):
    if tr.empty:
        return {"pnl": 0, "per_day": 0, "trades": 0, "win": np.nan, "worst_day": 0}
    d = [daily.get(i, 0.0) for i in range(int(math.ceil(days)))]
    return {"pnl": tr.pnl.sum(), "per_day": tr.pnl.sum() / days, "trades": len(tr), "win": (tr.pnl > 0).mean(),
            "worst_day": min(d), "pos_days": sum(1 for v in d if v > 0) / max(len(d), 1),
            "tp_share": (tr.why == "tp").mean(), "peak_capital": tr.attrs.get("peak_capital", 0),
            "shares": tr.qty.sum()}


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--one", help="N,X,H,T")
    ap.add_argument("--capital", type=float, default=70_000)
    a = ap.parse_args()
    D = o.Data(first="2026-10-01 12:00")
    train = (o.et("2026-10-04 00:00"), o.et("2026-10-09 00:00"))
    test = (o.et("2026-10-09 00:00"), D.t_end - 600)
    if a.one:
        n, X, H, T = (float(x) for x in a.one.split(","))
        tr, daily = simulate(D, train[0], test[1], int(n), X, H, T, a.capital)
        days = (test[1] - train[0]) / 86400
        print(summary(tr, daily, days))
        for i in sorted(daily):
            print(f"  {o.fmt(train[0] + i * 86400, '%a %m-%d')}  {daily[i]:+8,.0f}")
        print(tr.groupby("ex").pnl.agg(["count", "sum"]).join(D.mk[["title"]]).sort_values("sum").to_string())
        return
    rows = []
    for n in (4, 6, 8):
        for X in (0.015, 0.02, 0.03):
            for H in (2.0, 6.0):
                for T in (6.0, 24.0):
                    tr, daily = simulate(D, *train, n, X, H, T, a.capital)
                    s = summary(tr, daily, (train[1] - train[0]) / 86400)
                    rows.append({"N": n, "X": X, "H": H, "T": T, **s})
    g = pd.DataFrame(rows).sort_values("per_day", ascending=False)
    print("TRAIN Oct 4-8 (70k, contest prices):")
    print(g.head(10).round(3).to_string(index=False))
    print("worst settings:")
    print(g.tail(3).round(3).to_string(index=False))
    print("\nTEST Oct 9-now, the 3 best train settings, untouched:")
    for _, r in g.head(3).iterrows():
        tr, daily = simulate(D, *test, int(r["N"]), r["X"], r["H"], r["T"], a.capital)
        s = summary(tr, daily, (test[1] - test[0]) / 86400)
        print(f"  N={int(r['N'])} X={r['X']} H={r['H']} T={r['T']}: " + ", ".join(f"{k} {v:,.2f}" if isinstance(v, float) else f"{k} {v}" for k, v in s.items()))


if __name__ == "__main__":
    main()
