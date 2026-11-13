"""Model of the CONTEST price (not Kalshi): predicted 24h move of each contract's contest price. (READ-ONLY)

    py contest_model.py now            # predictions for every contract + your live holdings (no orders)
    py contest_model.py eval           # walk-forward check of the 2c rule (P&L at contest prices, 1c round trip)

Why: Kalshi-gap trades do not pay at contest prices within a day (gap >= 2c, 24h hold: -0.6c per trade after a 1c round
trip); the contest's own dynamics matter. Features (hourly, known at t):
  gap        contest VWAP - Kalshi FV                     (closes ~19%/day)
  dev24/72   gap minus its own 24h / 72h average          (deviations from the contract's usual premium: ~36%/day)
  pair_gap   contest price minus (1 - other party's contest price - Kalshi independents)   (D/R mismatch)
  pair_dev24 pair_gap minus its 24h average
  mom1/6/24  contest move over the last 1 / 6 / 24 h       (reverses)
  kmom6      Kalshi move over the last 6 h                 (~no effect)
Ridge regression of the next-24h contest move, refit on all history with a known outcome each time it runs.
Walk-forward Oct 7-12: predictions >= 2c -> +0.5c per trade net, ~60% hit, 8-16 trades/day (all contracts with Kalshi).
"""
from __future__ import annotations

import sys
import time

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge

import overnight as o

H = 3600
FEATS = ["gap", "dev24", "dev72", "pair_gap", "pair_dev24", "mom1", "mom6", "mom24", "kmom6"]


def panel(D: o.Data | None = None) -> pd.DataFrame:
    D = D or o.Data()
    grid = D.grid[D.grid % H == 0]
    grid = np.unique(np.append(grid, int(D.t_end // o.BUCKET * o.BUCKET)))   # plus "now" (last complete 15-min bucket)
    cv = D.vwap.shift(1).reindex(grid)          # contest VWAP of the bucket ending at t
    kv = D.fv.shift(1).reindex(grid)            # Kalshi known at t
    mk = D.mk
    sib = {}
    for _, g in mk.groupby(["office", "place"]):
        d, r = g[g.party == "D"].index.tolist(), g[g.party == "R"].index.tolist()
        if d and r:
            sib[d[0]], sib[r[0]] = r[0], d[0]
    rows = []
    for ex in mk.index:
        if ex not in kv.columns or kv[ex].notna().sum() < 50:
            continue
        df = pd.DataFrame({"c": cv[ex].values, "k": kv[ex].values}, index=grid)
        df["gap"] = df.c - df.k
        df["dev24"] = df.gap - df.gap.rolling(24, min_periods=12).mean().shift(1)
        df["dev72"] = df.gap - df.gap.rolling(72, min_periods=24).mean().shift(1)
        for h in (1, 6, 24):
            df[f"mom{h}"] = df.c - df.c.shift(h)
        df["kmom6"] = df.k - df.k.shift(6)
        if ex in sib:
            oth = sib[ex]
            k_ind = (1 - kv[ex] - kv[oth]).clip(lower=0).values if oth in kv.columns else 0
            df["pair_gap"] = df.c - (1 - cv[oth].values - k_ind)
            df["pair_dev24"] = df.pair_gap - df.pair_gap.rolling(24, min_periods=12).mean().shift(1)
        else:
            df["pair_gap"] = np.nan
            df["pair_dev24"] = np.nan
        df["fwd24"] = df.c.shift(-24) - df.c
        df["ex"], df["t"] = ex, df.index
        rows.append(df)
    return pd.concat(rows, ignore_index=True)


def fit_predict(p: pd.DataFrame, now_t: float) -> pd.DataFrame:
    """Fit on rows whose 24h outcome is known; predict the latest row of each contract (features at now_t)."""
    tr = p[p.fwd24.notna() & p.k.notna() & (p.t >= o.et("2026-10-04 00:00"))]
    med = tr[FEATS].median()
    m = Ridge(alpha=1e-4).fit(tr[FEATS].fillna(med).fillna(0), tr.fwd24)
    last = p[p.t == p.t.max()].copy()
    last = last[last.c.notna() & last.k.notna()]
    if last.empty:            # no current Kalshi/contest data (e.g. right after the PC slept): no predictions at all
        last["pred"] = []
        return last.set_index("ex")
    last["pred"] = m.predict(last[FEATS].fillna(med).fillna(0))
    last = last[~last.ex.duplicated(keep="last")]
    last.attrs["coef"] = dict(zip(FEATS, m.coef_.round(4)))
    last.attrs["n_train"] = len(tr)
    return last.set_index("ex")


class ModelSignal:
    """For the engine: refreshed every `refresh_s`; pred(ex) = predicted 24h contest move (YES terms), ref(ex) = the
    contest VWAP the prediction starts from (entries must not pay more than ref + slip)."""

    def __init__(self, refresh_s: float = 600, clock=time.time):
        self.refresh_s, self.clock = refresh_s, clock
        self.t = 0.0
        self.tab: pd.DataFrame | None = None

    def refresh(self, force: bool = False):
        if not force and self.tab is not None and self.clock() - self.t < self.refresh_s:
            return
        try:
            D = o.Data()
            self.tab = fit_predict(panel(D), D.t_end)
            self.t = self.clock()
        except Exception:
            # Oct 14: after the PC slept 10 h every refresh failed and the engine kept trading on the pre-sleep table.
            # A table older than 2 refresh periods is dropped: no model entries until a refresh succeeds.
            if self.tab is not None and self.clock() - self.t > 2 * self.refresh_s:
                self.tab = None
            raise

    def pred(self, ex: str):
        if self.tab is None or ex not in self.tab.index or self.clock() - self.t > 2 * self.refresh_s:
            return None
        return float(self.tab.at[ex, "pred"])

    def ref(self, ex: str):
        if self.tab is None or ex not in self.tab.index:
            return None
        return float(self.tab.at[ex, "c"])


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "now"
    D = o.Data()
    if cmd == "now":
        last = fit_predict(panel(D), D.t_end)
        print(f"Model fitted on {last.attrs['n_train']:,} hourly rows; coefficients {last.attrs['coef']}")
        last = last.join(D.mk[["title"]])
        sig = last[last.pred.abs() >= 0.02].sort_values("pred")
        print(f"\nContracts with a predicted 24h contest move >= 2c now ({len(sig)}):")
        for ex, r in sig.iterrows():
            print(f"  {r.pred*100:+5.1f}c  {r.title[9:62]:54} contest {r.c:.3f}  Kalshi {r.k:.3f}")
        try:
            import json
            lots = json.load(open(o.ROOT / "data" / "live_lots.json"))
            pos = {}
            for l in lots:
                pos[l["exchange_id"]] = pos.get(l["exchange_id"], 0) + l["d"] * l["qty"]
            print("\nYour live holdings: predicted 24h contest move IN YOUR FAVOUR")
            for ex, q in pos.items():
                if ex in last.index:
                    r = last.loc[ex]
                    print(f"  {np.sign(q) * r.pred * 100:+5.1f}c  {r.title[9:62]:54} pos {q:+.0f}  contest {r.c:.3f}  Kalshi {r.k:.3f}")
        except FileNotFoundError:
            pass
    elif cmd == "eval":
        p = panel(D)
        p = p[p.k.notna() & (p.t >= o.et("2026-10-04 00:00"))]
        p["day"] = ((p.t - o.et("2026-10-04 00:00")) // 86400).astype(int)
        p["pred"] = np.nan
        for day in sorted(p.day.unique()):
            tr, te = p[(p.day < day - 1) & p.fwd24.notna()], p.day == day
            if len(tr) < 2000:
                continue
            med = tr[FEATS].median()
            p.loc[te, "pred"] = Ridge(1e-4).fit(tr[FEATS].fillna(med).fillna(0), tr.fwd24).predict(p.loc[te, FEATS].fillna(med).fillna(0))
        res = []
        for ex, g in p[p.pred.notna() & p.fwd24.notna()].sort_values("t").groupby("ex"):
            busy = -1
            for t, s, f in zip(g.t, g.pred, g.fwd24):
                if t >= busy and abs(s) >= 0.02:
                    res.append(np.sign(s) * f - 0.01)
                    busy = t + 24 * H
        r = np.array(res)
        print(f"walk-forward, predictions >= 2c, 24h hold, 1c round trip: {len(r)} trades, net {r.mean()*100:+.2f}c, hit {np.mean(r>0):.0%}")


if __name__ == "__main__":
    main()
