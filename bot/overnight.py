"""Which contracts will move, how much, and can that be traded? (READ-ONLY research + forecasts; places NO orders)

    py overnight.py study      # evidence: vol by window, walk-forward predictability, direction, catch-net back-test
    py overnight.py tonight    # forecast for the next 12h window (8pm-8am or 8am-8pm ET) + score finished forecasts
    py overnight.py watch      # loop: forecast ~10 min before 8am/8pm, score each window after it ends (24/7)
                               #   stop: create data/STOP_OVERNIGHT   log: data/overnight_console.log

Data: every contest trade (15-minute VWAP buckets, collector DB) + Kalshi fair value (hourly candles cached in
data/kalshi_hourly/ until the collector's 15-second Kalshi rows start). Windows are aligned to 8:00 and 20:00 ET.
Features use only data before the window starts (no look-ahead); the model is refit on the last 4 days each time.

Forecast = predicted 12h trading range (high - low of 15-min VWAPs) per contract, from a ridge regression on
trailing activity (48h absolute movement, 24h volume, last ranges, gap to Kalshi, ...). Direction is NOT predicted
except the pull toward Kalshi FV. The "catch net" is a resting buy 3c below min(contest, FV) and a sell 3c above
max(contest, FV): it only fills on overshoots that are also good value vs Kalshi.
Files: data/vol_forecasts.csv (every forecast), data/vol_scores.csv (realised vs forecast), reports/overnight_*.md.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
import time
import traceback
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.linear_model import Ridge

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB = DATA / "market_data.sqlite"
CACHE = DATA / "kalshi_hourly"
FORECASTS = DATA / "vol_forecasts.csv"
SCORES = DATA / "vol_scores.csv"
STOP = DATA / "STOP_OVERNIGHT"
ET = dt.timezone(dt.timedelta(hours=-4))   # EDT until Nov 1 (switch to -5 after)
BUCKET = 900                               # 15 minutes
WIN = 12 * 3600
TICK = 0.005
NET_D = 0.03                               # catch-net distance beyond min/max(contest, FV)
FEATS = ["f_prev_rng", "f_same_rng", "f_absmove48", "f_rng24", "f_lv24", "f_absgap", "f_gapsd24", "f_kprev_rng",
         "f_pq", "f_comp", "night", "off_h", "off_s", "off_c"]


def et(s: str) -> float:
    return dt.datetime.fromisoformat(s).replace(tzinfo=ET).timestamp()


def fmt(ts: float, f: str = "%a %m-%d %H:%M") -> str:
    return dt.datetime.fromtimestamp(ts, ET).strftime(f)


def boundary_after(ts: float) -> float:
    d = dt.datetime.fromtimestamp(ts, ET)
    for h in (8, 20):
        b = d.replace(hour=h, minute=0, second=0, microsecond=0)
        if b.timestamp() > ts:
            return b.timestamp()
    return (d.replace(hour=8, minute=0, second=0, microsecond=0) + dt.timedelta(days=1)).timestamp()


def is_night(a: float) -> bool:
    return dt.datetime.fromtimestamp(a, ET).hour >= 20 or dt.datetime.fromtimestamp(a, ET).hour < 8


# ------------------------------------------------------------------------------------------------ data
class Data:
    """All contest trades and Kalshi FV on a 15-minute grid (columns = exchange ids)."""

    def __init__(self, end: float | None = None, first: str = "2026-10-01 12:00"):
        self.db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
        self.mk = self._markets()
        self.t_end = end or time.time()
        t0 = et(first)
        self.grid = np.arange(int(t0 // BUCKET * BUCKET), int(self.t_end // BUCKET * BUCKET) + BUCKET, BUCKET)
        tr = pd.read_sql_query("SELECT exchange_id AS ex, ts, price, quantity AS q FROM trades WHERE ts>=? AND ts<? "
                               "AND price IS NOT NULL AND quantity>0", self.db, params=(t0, self.t_end))
        tr["b"] = (tr.ts // BUCKET * BUCKET).astype(np.int64)
        self.trades = tr
        self._buckets(tr)
        self.fv = self._kalshi()
        self.fv_known = self.fv.shift(1)          # during bucket b we know the value at the end of bucket b-1
        self.T = {ex: (g.ts.values, g.price.values, g.q.values) for ex, g in tr.sort_values("ts").groupby("ex")}

    def _markets(self) -> pd.DataFrame:
        kmap = {}
        for t, o, p, pty in self.db.execute("SELECT ticker, office, place, party FROM kalshi_map"):
            kmap.setdefault((o, p, pty), t)
        rows = [{"ex": ex, "title": title, "office": o, "place": p, "party": pty, "ticker": kmap.get((o, p, pty))}
                for ex, title, o, p, pty in self.db.execute(
                    "SELECT exchange_id, title, office, place, party FROM markets WHERE office IS NOT NULL")]
        return pd.DataFrame(rows).set_index("ex")

    def _buckets(self, tr):
        tr = tr.assign(pq=tr.price * tr.q)
        agg = tr.groupby(["b", "ex"]).agg(pq=("pq", "sum"), q=("q", "sum"), hi=("price", "max"), lo=("price", "min"))
        cols = self.mk.index
        self.vwap = (agg.pq / agg.q).unstack().reindex(index=self.grid, columns=cols).ffill(limit=48)
        self.vol = agg.q.unstack().reindex(index=self.grid, columns=cols).fillna(0.0)
        self.hi = agg.hi.unstack().reindex(index=self.grid, columns=cols)
        self.lo = agg.lo.unstack().reindex(index=self.grid, columns=cols)

    def _kalshi(self) -> pd.DataFrame:
        """fv.loc[b] = Kalshi mid known at the END of bucket b (last point before b+15m), forward-filled <= 3h."""
        first_live = self.db.execute("SELECT MIN(ts) FROM kalshi").fetchone()[0] or float("inf")
        out = {}
        for ex, m in self.mk.iterrows():
            if not m.ticker:
                continue
            pts = {}
            f = CACHE / f"{m.ticker}.json"
            if f.exists():
                pts = {int(k): v for k, v in json.loads(f.read_text()).items() if int(k) < first_live}
            for ts, bi, ak in self.db.execute("SELECT ts, bid, ask FROM kalshi WHERE ticker=?", (m.ticker,)):
                if bi is not None and ak is not None and 0 < ak - bi <= 0.10:
                    pts[ts] = (bi + ak) / 2
            if not pts:
                continue
            s = pd.Series(pts).sort_index()
            idx = np.searchsorted(s.index.values, self.grid + BUCKET, side="left") - 1
            ok = idx >= 0
            vals = np.where(ok, s.values[np.clip(idx, 0, None)], np.nan)
            age = self.grid + BUCKET - np.where(ok, s.index.values[np.clip(idx, 0, None)], -1e18)
            vals[age > 3 * 3600] = np.nan
            out[ex] = vals
        return pd.DataFrame(out, index=self.grid).reindex(columns=self.mk.index)

    # ---- helpers
    @staticmethod
    def at(df: pd.DataFrame, ts: float) -> pd.Series:
        """Row for the last bucket that ENDED at or before ts."""
        b = int(ts // BUCKET * BUCKET) - BUCKET
        return df.loc[b] if b in df.index else pd.Series(np.nan, index=df.columns)

    @staticmethod
    def span(df: pd.DataFrame, a: float, b: float) -> pd.DataFrame:
        return df.loc[(df.index >= a) & (df.index < b)]


# ------------------------------------------------------------------------------------------------ features / outcomes
def features(D: Data, a: float, night: bool) -> pd.DataFrame:
    """Per contract, known at time a."""
    sp, at = D.span, D.at
    c0, k0 = at(D.vwap, a), at(D.fv, a)
    prev, same, h24, h48 = sp(D.vwap, a - WIN, a), sp(D.vwap, a - 2 * WIN, a - WIN), sp(D.vwap, a - 86400, a), sp(D.vwap, a - 172800, a)
    kprev = sp(D.fv, a - WIN, a)
    gap24 = sp(D.vwap, a - 86400, a) - sp(D.fv_known, a - 86400, a)
    f = pd.DataFrame({
        "c0": c0, "k0": k0,
        "f_prev_rng": prev.max() - prev.min(), "f_same_rng": same.max() - same.min(),
        "f_absmove48": h48.diff().abs().sum(), "f_rng24": h24.max() - h24.min(),
        "f_lv24": np.log1p(sp(D.vol, a - 86400, a).sum()),
        "f_gap": c0 - k0, "f_absgap": (c0 - k0).abs(), "f_gapsd24": gap24.std(),
        "f_kprev_rng": kprev.max() - kprev.min(), "f_kprev_d": kprev.ffill().iloc[-1] - kprev.bfill().iloc[0] if len(kprev) else np.nan,
        "f_cprev_d": prev.ffill().iloc[-1] - prev.bfill().iloc[0] if len(prev) else np.nan,
        "f_pq": (k0.fillna(c0) * (1 - k0.fillna(c0))), "f_comp": ((k0 >= 0.1) & (k0 <= 0.9)).astype(int),
        "night": int(night),
    })
    f = f.join(D.mk[["title", "office", "place", "party"]])
    f["off_h"], f["off_s"], f["off_c"] = [(f.office == o).astype(int) for o in ("house", "senate", "control")]
    return f[f.c0.notna()]


def outcomes(D: Data, a: float, b: float) -> pd.DataFrame:
    sp, at = D.span, D.at
    c0, c1, k0, k1 = at(D.vwap, a), at(D.vwap, b), at(D.fv, a), at(D.fv, b)
    w = pd.concat([sp(D.vwap, a, b), c0.to_frame().T])
    kw = sp(D.fv, a, b)
    return pd.DataFrame({"c1": c1, "dc": c1 - c0, "rng": w.max() - w.min(), "k1": k1, "dk": k1 - k0,
                         "krng": kw.max() - kw.min(), "exc_dn": (k0 - sp(D.lo, a, b).min()).clip(lower=0),
                         "exc_up": (sp(D.hi, a, b).max() - k0).clip(lower=0), "vol": sp(D.vol, a, b).sum()})


def panel(D: Data, start: float, end: float) -> pd.DataFrame:
    rows = []
    a = start
    while a + WIN <= end:
        f = features(D, a, is_night(a)).join(outcomes(D, a, a + WIN))
        f["t0"] = a
        rows.append(f[f.c1.notna()].reset_index())
        a += WIN
    return pd.concat(rows, ignore_index=True)


def fit_predict(train: pd.DataFrame, test: pd.DataFrame) -> np.ndarray:
    X = train[FEATS].astype(float)
    med = X.median()
    m = Ridge(alpha=1.0).fit(X.fillna(med), np.log(train.rng + 0.005))
    return np.clip(np.exp(m.predict(test[FEATS].astype(float).fillna(med))) - 0.005, 0, None)


# ------------------------------------------------------------------------------------------------ catch net
def net_levels(c0: float, k0: float, d: float = NET_D) -> tuple[float, float]:
    xb = np.floor((min(c0, k0) - d) / TICK + 1e-9) * TICK
    xs = np.ceil((max(c0, k0) + d) / TICK - 1e-9) * TICK
    return xb, xs


def sim_net(D: Data, rows: pd.DataFrame, a: float, b: float, *, d: float = NET_D, anchor: str = "both",
            qty: float = 5000, cancel: float = 0.015, exit_frac: float = 0.6, hold_h: float = 48,
            exit_cost: float = 0.005, budget: float = 100_000) -> pd.DataFrame:
    """Rest a buy below / a sell above each contract from a to b (cancelled once Kalshi moves `cancel` from its value
    at a). A print strictly through our price would have hit us first: fill = min(remaining, printed qty). Each fill
    rests an exit at fill + exit_frac * edge; it fills on later prints through it (half the printed size). Unexited
    quantity is sold at the contest VWAP hold_h after the fill, paying exit_cost (pnl_real; NaN until that time).
    Also marked at window end to Kalshi (pnl_fv) and to the contest VWAP minus exit_cost (pnl_c)."""
    events = []
    for ex, r in rows.iterrows():
        if ex not in D.T or pd.isna(r.k0) or pd.isna(r.c0):
            continue
        ref_b, ref_s = (min(r.c0, r.k0), max(r.c0, r.k0)) if anchor == "both" else (r.k0, r.k0)
        xb, xs = net_levels(r.c0, r.k0, d) if anchor == "both" else net_levels(r.k0, r.k0, d)
        ts, px, qs = D.T[ex]
        i, j = np.searchsorted(ts, a), np.searchsorted(ts, b)
        kw = D.span(D.fv_known[[ex]], a, b)[ex]
        moved = kw[(kw - r.k0).abs() >= cancel]
        t_cancel = float(moved.index[0]) if len(moved) else b
        for side, x, ref in ((1, xb, ref_b), (-1, xs, ref_s)):
            if not TICK <= x <= 1 - TICK:
                continue
            rem = qty
            for k in range(i, j):
                if ts[k] >= t_cancel or rem <= 0:
                    break
                if side * (x - px[k]) > 1e-9:
                    f = min(rem, qs[k])
                    rem -= f
                    events.append((ts[k], ex, side, x, f, side * (ref - x), r.title))
    out, spent = [], 0.0
    k_end, c_end = D.at(D.fv, b), D.at(D.vwap, b)
    for tf, ex, side, x, f, edge, title in sorted(events):
        cost = (x if side == 1 else 1 - x) * f
        if spent + cost > budget:
            continue
        spent += cost
        ts, px, qs = D.T[ex]
        tgt = x + side * max(TICK, exit_frac * edge)
        rem, pnl = f, 0.0
        for m in range(np.searchsorted(ts, tf, side="right"), np.searchsorted(ts, tf + hold_h * 3600)):
            if rem <= 0:
                break
            if side * (px[m] - tgt) > 1e-9:
                g = min(rem, qs[m] * 0.5)
                rem -= g
                pnl += side * (tgt - x) * g
        exited = 1 - rem / f
        bail = D.at(D.vwap, tf + hold_h * 3600)[ex] if tf + hold_h * 3600 <= D.t_end else np.nan
        pnl_real = pnl + (side * (bail - x) - exit_cost) * rem if rem > 0 and pd.notna(bail) else (pnl if rem <= 0 else np.nan)
        out.append({"t": tf, "ex": ex, "title": title, "side": side, "px": x, "qty": f, "edge": edge, "cost": cost,
                    "pnl_fv": side * (k_end[ex] - x) * f, "pnl_c": (side * (c_end[ex] - x) - exit_cost) * f,
                    "pnl_real": pnl_real, "exited": exited})
    return pd.DataFrame(out)


# ------------------------------------------------------------------------------------------------ study
def study(a_start: str = "2026-10-01 20:00"):
    D = Data()
    p = panel(D, et(a_start), D.t_end)
    p["win"] = p.t0.map(lambda t: fmt(t, "%m-%d %H"))
    L = [f"# Which contracts move, and can it be traded? ({fmt(D.t_end)} ET)", ""]

    # 1. regime: volatility by window
    g = p.groupby("win").agg(night=("night", "first"), rng_med=("rng", "median"), absdc=("dc", lambda x: x.abs().mean()),
                             n3=("rng", lambda x: (x >= 0.03).sum()), n5=("rng", lambda x: (x >= 0.05).sum()),
                             krng=("krng", "mean"), vol=("vol", "sum"))
    L += ["## 1. Volatility by 12h window (contest VWAP range; Kalshi range)", "",
          "| window | night | median range c | mean abs net move c | contracts range ≥3c | ≥5c | Kalshi mean range c | volume M |",
          "|---|---|---|---|---|---|---|---|"]
    for w, r in g.iterrows():
        L.append(f"| {w} | {'Y' if r.night else ''} | {r.rng_med*100:.1f} | {r.absdc*100:.2f} | {int(r.n3)} | {int(r.n5)} | {r.krng*100:.2f} | {r.vol/1e6:.1f} |")

    # 2. walk-forward predictability
    wins = sorted(p.win.unique())
    res = []
    p["absdc"] = p.dc.abs()
    for i, w in enumerate(wins):
        if i < 4:
            continue
        tr = p[(p.win < w) & (p.win >= wins[max(0, i - 8)])]
        te = p[p.win == w]
        pred = fit_predict(tr, te)
        for tgt in ("rng", "absdc"):
            act = te[tgt].values
            for name, s in (("model", pred), ("baseline 48h movement", te.f_absmove48.values)):
                top = np.argsort(-s)[:20]
                res.append({"win": w, "target": tgt, "pred": name, "rho": spearmanr(s, act)[0],
                            "top20": len(set(top) & set(np.argsort(-act)[:20])) / 20, "lift": act[top].mean() / act.mean()})
    r = pd.DataFrame(res)
    s = r.groupby(["target", "pred"])[["rho", "top20", "lift"]].mean()
    s6 = r[r.win.isin(wins[-6:])].groupby(["target", "pred"])[["rho", "top20", "lift"]].mean()
    L += ["", "## 2. Can we predict WHICH contracts move? (walk-forward, refit on prior 4 days)", "",
          "rho = rank correlation of predicted vs realised; top20 = share of the realised top-20 movers we named in advance; "
          "lift = realised move of our top-20 / average contract.", "",
          "| target | predictor | rho (all) | top20 (all) | lift (all) | rho (last 6) | top20 (last 6) | lift (last 6) |", "|---|---|---|---|---|---|---|---|"]
    for (t, pr), x in s.iterrows():
        y = s6.loc[(t, pr)]
        lab = "12h range (high-low)" if t == "rng" else "abs net move (close-close)"
        L.append(f"| {lab} | {pr} | {x.rho:.2f} | {x.top20:.0%} | {x.lift:.2f}× | {y.rho:.2f} | {y.top20:.0%} | {y.lift:.2f}× |")

    # 3. direction
    q = p[p.t0 >= et("2026-10-04 08:00")].dropna(subset=["f_gap", "dc"])
    L += ["", "## 3. Can we predict the DIRECTION?", "",
          "| signal (12h ahead) | n | hit rate | avg move in predicted direction c |", "|---|---|---|---|"]
    for thr in (0.02, 0.03, 0.05):
        x = q[(q.f_absgap >= thr) & (q.dc.abs() >= 0.0025)]
        L.append(f"| contest {thr*100:.0f}c+ away from Kalshi → moves toward it | {len(x)} | {(np.sign(-x.f_gap) == np.sign(x.dc)).mean():.0%} | {(-np.sign(x.f_gap) * x.dc).mean()*100:+.2f} (gap avg {x.f_absgap.mean()*100:.1f}) |")
    x = q[q.f_kprev_d.abs() >= 0.01]
    L.append(f"| Kalshi moved 1c+ in prior 12h → contest follows | {len(x)} | {(np.sign(x.f_kprev_d) == np.sign(x.dc)).mean():.0%} | {(np.sign(x.f_kprev_d) * x.dc).mean()*100:+.2f} |")
    x = q[(q.f_cprev_d.abs() >= 0.02) & (q.f_kprev_d.abs() < 0.005)]
    L.append(f"| contest-only 2c+ move in prior 12h → reverts | {len(x)} | {(np.sign(-x.f_cprev_d) == np.sign(x.dc)).mean():.0%} | {(-np.sign(x.f_cprev_d) * x.dc).mean()*100:+.2f} |")

    # 4. catch net
    L += ["", "## 4. Catch net back-test (rest buy 3c below min(contest,FV), sell 3c above max; 5k sh/side; cancel on 1.5c Kalshi move; exit 60% of edge, else bail after 48h)", "",
          "| window | night | fills | capital | P&L vs Kalshi at window end | P&L vs contest at end | realised (48h) |", "|---|---|---|---|---|---|---|"]
    for a in sorted(p.t0.unique()):
        if a < et("2026-10-04 08:00"):
            continue
        rows = p[p.t0 == a].set_index("ex")
        f = sim_net(D, rows, a, a + WIN)
        if f.empty:
            L.append(f"| {fmt(a, '%m-%d %H')} | {'Y' if is_night(a) else ''} | 0 | 0 | 0 | 0 | 0 |")
            continue
        real = f"{f.pnl_real.sum():+,.0f}" if f.pnl_real.notna().all() else "pending"
        L.append(f"| {fmt(a, '%m-%d %H')} | {'Y' if is_night(a) else ''} | {len(f)} | {f.cost.sum():,.0f} | {f.pnl_fv.sum():+,.0f} | {f.pnl_c.sum():+,.0f} | {real} |")
    out = ROOT / "reports" / f"overnight_study_{fmt(D.t_end, '%m%d_%H%M')}.md"
    out.write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))
    print(f"\nReport: {out}")


# ------------------------------------------------------------------------------------------------ forecast + scoring
def forecast(D: Data, a: float) -> pd.DataFrame:
    """Forecast the window [a, a+12h) with data up to min(a, now)."""
    t_feat = min(a, D.t_end)
    p = panel(D, et("2026-10-01 20:00") + ((t_feat - 4 * 86400 - et("2026-10-01 20:00")) // WIN) * WIN, t_feat)
    if p.empty or p.t0.nunique() < 4:
        p = panel(D, et("2026-10-01 20:00"), t_feat)
    f = features(D, t_feat, is_night(a))
    f["pred_rng"] = fit_predict(p, f)
    lv = [net_levels(c, k) if pd.notna(k) else (np.nan, np.nan) for c, k in zip(f.c0, f.k0)]
    f["net_buy"], f["net_sell"] = [x[0] for x in lv], [x[1] for x in lv]
    f["win_start"], f["made"] = a, D.t_end
    return f.sort_values("pred_rng", ascending=False)


def write_forecast(f: pd.DataFrame, a: float, md: bool = True) -> Path | None:
    keep = ["win_start", "made", "title", "pred_rng", "f_prev_rng", "f_lv24", "c0", "k0", "f_gap", "net_buy", "net_sell"]
    out = f[keep].reset_index().rename(columns={"index": "ex"})
    old = pd.read_csv(FORECASTS, dtype={"ex": str}) if FORECASTS.exists() else pd.DataFrame()
    if not old.empty:
        old = old[old.win_start != a]
    pd.concat([old, out], ignore_index=True).to_csv(FORECASTS, index=False)
    if not md:
        return None
    L = [f"# Movers forecast for {fmt(a)} → {fmt(a + WIN, '%H:%M')} ET ({'night' if is_night(a) else 'day'}; made {fmt(f.made.iloc[0])})", "",
         "Predicted 12h range = expected high-low of the contest price. 'Lean' = the only direction signal that works: "
         "toward Kalshi when the gap is ≥3c (~70-80% hit, but only ~10-20% of the gap closes per 12h). "
         "Catch net = resting orders that fill only on overshoots that are also good value vs Kalshi (paper only).", "",
         "| # | contract | pred range c | last 12h range c | 24h vol | contest | Kalshi | gap c | lean | net buy ≤ | net sell ≥ |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, (ex, r) in enumerate(f.head(40).iterrows(), 1):
        lean = "-" if pd.isna(r.f_gap) or abs(r.f_gap) < 0.03 else ("BUY" if r.f_gap < 0 else "SELL")
        k = "-" if pd.isna(r.k0) else f"{r.k0:.3f}"
        g = "-" if pd.isna(r.f_gap) else f"{r.f_gap*100:+.1f}"
        nb = f"{r.net_buy:.3f}" if TICK <= r.net_buy <= 1 - TICK else "-"
        ns = f"{r.net_sell:.3f}" if TICK <= r.net_sell <= 1 - TICK else "-"
        L.append(f"| {i} | {r.title} | {r.pred_rng*100:.1f} | {r.f_prev_rng*100:.1f} | {np.expm1(r.f_lv24)/1e3:,.0f}k | {r.c0:.3f} | {k} | {g} | {lean} | {nb} | {ns} |")
    out_md = ROOT / "reports" / f"overnight_forecast_{fmt(a, '%m%d_%H%M')}.md"
    out_md.write_text("\n".join(L), encoding="utf-8")
    return out_md


def score(D: Data) -> list[dict]:
    """Score every logged forecast window that has ended and is not yet scored."""
    if not FORECASTS.exists():
        return []
    fc = pd.read_csv(FORECASTS, dtype={"ex": str})
    done = set(pd.read_csv(SCORES).win_start) if SCORES.exists() else set()
    new = []
    for a, g in fc.groupby("win_start"):
        b = a + WIN
        if a in done or b > D.t_end - BUCKET:
            continue
        g = g.set_index("ex")
        o = outcomes(D, a, b).reindex(g.index)
        ok = o.rng.notna()
        pred, act = g.pred_rng[ok].values, o.rng[ok].values
        top = np.argsort(-pred)[:20]
        rows = pd.DataFrame({"c0": g.c0, "k0": g.k0, "title": g.title})
        net = sim_net(D, rows, a, b)
        s = {"win_start": a, "window": fmt(a), "n": int(ok.sum()), "rho": round(spearmanr(pred, act)[0], 3),
             "top20_hit": len(set(top) & set(np.argsort(-act)[:20])) / 20,
             "lift": round(act[top].mean() / act.mean(), 2), "median_range_c": round(np.median(act) * 100, 2),
             "n_range_ge3c": int((act >= 0.03).sum()),
             "lean_hits": None, "lean_n": 0,
             "net_fills": len(net), "net_capital": round(net.cost.sum(), 0) if len(net) else 0,
             "net_pnl_fv": round(net.pnl_fv.sum(), 0) if len(net) else 0,
             "net_pnl_contest": round(net.pnl_c.sum(), 0) if len(net) else 0}
        lean = g[g.f_gap.abs() >= 0.03].join(o.dc)
        lean = lean[lean.dc.abs() >= 0.0025]
        if len(lean):
            s["lean_n"] = len(lean)
            s["lean_hits"] = round(float((np.sign(-lean.f_gap) == np.sign(lean.dc)).mean()), 2)
        new.append(s)
    if new:
        old = pd.read_csv(SCORES) if SCORES.exists() else pd.DataFrame()
        pd.concat([old, pd.DataFrame(new)], ignore_index=True).to_csv(SCORES, index=False)
    return new


def tonight(a: float | None = None):
    D = Data()
    for s in score(D):
        print(f"SCORED {s['window']}: rho {s['rho']:+.2f}, top-20 hit {s['top20_hit']:.0%}, lift {s['lift']}×, "
              f"median range {s['median_range_c']}c, {s['n_range_ge3c']} contracts ≥3c | lean {s['lean_hits']} of {s['lean_n']} | "
              f"net {s['net_fills']} fills, capital {s['net_capital']:,.0f}, P&L vs Kalshi {s['net_pnl_fv']:+,.0f}, vs contest {s['net_pnl_contest']:+,.0f}")
    a = a or boundary_after(D.t_end - 15 * 60)       # the window starting next (or that just started)
    f = forecast(D, a)
    out = write_forecast(f, a)
    print(f"\nForecast {fmt(a)} ET ({'night' if is_night(a) else 'day'}): top 15 predicted movers")
    for ex, r in f.head(15).iterrows():
        g = "" if pd.isna(r.f_gap) else f"gap {r.f_gap*100:+.1f}c"
        print(f"  {r.pred_rng*100:4.1f}c  {r.title[9:70]:62} {r.c0:.3f}  {g}")
    print(f"Report: {out}")


def watch():
    log = DATA / "overnight_console.log"
    def say(msg):
        line = f"{fmt(time.time(), '%m-%d %H:%M:%S')} {msg}"
        print(line, flush=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    say("watch started")
    made = set()                                      # windows forecast by this loop (refreshes earlier manual ones)
    last_score = 0.0
    while not STOP.exists():
        now = time.time()
        try:
            nxt = boundary_after(now)
            if nxt - now <= 10 * 60 and nxt not in made:
                D = Data()
                out = write_forecast(forecast(D, nxt), nxt)
                made.add(nxt)
                say(f"forecast {fmt(nxt)} -> {out.name}")
            if now - last_score > 30 * 60:
                D = Data()
                for s in score(D):
                    say(f"scored {s['window']}: rho {s['rho']:+.2f} top20 {s['top20_hit']:.0%} lift {s['lift']}x "
                        f"median {s['median_range_c']}c n>=3c {s['n_range_ge3c']} lean {s['lean_hits']}/{s['lean_n']} "
                        f"net {s['net_fills']} fills P&L vs Kalshi {s['net_pnl_fv']:+,.0f} vs contest {s['net_pnl_contest']:+,.0f}")
                last_score = now
        except Exception:
            say("ERROR " + traceback.format_exc().replace("\n", " | "))
        time.sleep(60)
    say("stopped (STOP_OVERNIGHT)")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["study", "tonight", "watch"])
    ap.add_argument("--at", help="window start for 'tonight' (YYYY-MM-DD HH:MM ET); default = next 8am/8pm")
    ap.add_argument("--backfill", help="'tonight' only: also log walk-forward forecasts for every window since this "
                                       "date (YYYY-MM-DD) and score them, to build a track record")
    a = ap.parse_args()
    if a.cmd == "study":
        study()
    elif a.cmd == "tonight":
        if a.backfill:
            D = Data()
            w = et(f"{a.backfill} 08:00")
            while w + WIN <= D.t_end:
                write_forecast(forecast(D, w), w, md=False)
                w += WIN
        tonight(et(a.at) if a.at else None)
    else:
        watch()


if __name__ == "__main__":
    sys.exit(main())
