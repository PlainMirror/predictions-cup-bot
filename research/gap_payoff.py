"""Gap-trade payoff at CONTEST prices by gap bucket and holding time, recent regime (read-only).
Entry = a contract-hour whose contest VWAP is >= gap through Kalshi FV (FV .10-.90); direction toward Kalshi.
Payoff = contest VWAP move in our favour after h hours minus a 1c round trip. Overlapping hourly samples."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))
import numpy as np
import pandas as pd
import overnight as o

D = o.Data()
H = 3600
grid = D.grid[D.grid % H == 0]
cv = D.vwap.shift(1).reindex(grid)
kv = D.fv.shift(1).reindex(grid)
start, horizons = o.et("2026-10-10 00:00"), (24, 48, 72, 96, 120, 168)
rows = []
for ex in D.mk.index:
    if ex not in kv.columns:
        continue
    c, k = cv[ex].values, kv[ex].values
    for i, t in enumerate(grid):
        if t < start or not np.isfinite(c[i]) or not np.isfinite(k[i]) or not 0.10 <= k[i] <= 0.90:
            continue
        gap = c[i] - k[i]
        if abs(gap) < 0.04:
            continue
        d = -np.sign(gap)
        r = {"ex": ex, "t": t, "gap": abs(gap), "party": D.mk.at[ex, "party"], "k": k[i]}
        for h in horizons:
            j = i + h
            r[f"r{h}"] = d * (c[j] - c[i]) - 0.01 if j < len(c) and np.isfinite(c[j]) else np.nan
            r[f"g{h}"] = abs(c[j] - k[j]) if j < len(c) and np.isfinite(c[j]) and np.isfinite(k[j]) else np.nan
        rows.append(r)
p = pd.DataFrame(rows)
p["b"] = pd.cut(p.gap * 100, [4, 6, 8, 10, 100], labels=["4-6", "6-8", "8-10", "10+"], right=False)
print(f"entries {len(p):,} contract-hours on {p.ex.nunique()} contracts since Oct 10 (FV .10-.90)\n")
print("mean payoff c/share at contest prices after h hours (1c round trip), hit %, n:")
for b, g in p.groupby("b", observed=True):
    cells = []
    for h in horizons:
        x = g[f"r{h}"].dropna()
        cells.append(f"{h:>3}h {100 * x.mean():+5.2f} {100 * (x > 0).mean():3.0f}% n{len(x):5}" if len(x) else f"{h:>3}h   -")
    print(f"  gap {b:>4}c | " + " | ".join(cells))
print("\nshare of the entry gap still open after h hours (median):")
for b, g in p.groupby("b", observed=True):
    print(f"  gap {b:>4}c | " + " | ".join(f"{h}h {np.nanmedian(g[f'g{h}'] / g.gap):.2f}" for h in horizons))
s = p[p.gap >= 0.06]
late = s[s.g120 >= 0.04]
print(f"\n6c+ entries whose gap is still >= 4c after 120 h: {len(late) / s.g120.notna().sum():.0%}; their move 120h->168h: "
      f"{100 * (late.r168 - late.r120).mean():+.2f}c (n {late.r168.notna().sum()})")
