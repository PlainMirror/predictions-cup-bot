"""Settlement book: hold-to-settlement positions where the contest misprices long shots / heavy favourites vs Kalshi.

    py settle_book.py --size 50000                  # PLAN ONLY (default): orders, expected value, risk; sends nothing
    py settle_book.py --size 50000 --live           # PLACES ORDERS: review the dry run first
Options: --cap 0.15 (max share of the book in one race), --min-edge 0.05, --exclude senate:FL,senate:RI, --rho 0.5

Why: tournament players overpay for long shots (they need variance to win), so contest prices sit 5-14c away from Kalshi
in safe races and stay there (~200 h to close half). These gaps pay at settlement, not day to day: EV ~7-12% of the
capital by Nov 4, if Kalshi is right. The capital is locked until the races settle.

Rules: only contracts Kalshi prices outside 0.10-0.90 (the live engine never trades those), at least --min-edge through
Kalshi, best edge-per-SUSQie levels first (live books from the collector), at most --cap of the book per race, one
direction per race. Each order is a limit at the worst level we accept, expiring in 60 s (take only: nothing rests).
Holdings go to data/settlement_book.json (signed shares per exchange), which the live engine subtracts from its own
positions (caps, exits, loss stop), and every fill to data/settlement_book_ledger.jsonl. Re-running tops up to --size.
Before going live, check each market's Info tab for resolution wording (independent candidates, special elections).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
import uuid
from pathlib import Path

import numpy as np
from scipy.stats import norm

import overnight as o

ROOT = Path(__file__).resolve().parent.parent
BOOK = ROOT / "data" / "settlement_book.json"
LEDGER = ROOT / "data" / "settlement_book_ledger.jsonl"
TICK = 0.005


def load_book() -> dict[str, float]:
    return {str(k): float(v) for k, v in json.loads(BOOK.read_text(encoding="utf-8")).items()} if BOOK.exists() else {}


def ledger_cost() -> dict[str, float]:
    out = {}
    if LEDGER.exists():
        for line in LEDGER.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            out[r["ex"]] = out.get(r["ex"], 0.0) + r["cost"]
    return out


def candidates(D: o.Data, min_edge: float, exclude: set[str]) -> list[dict]:
    k = D.at(D.fv, time.time())
    out = []
    for ex in D.mk.index:
        fv = k.get(ex)
        m = D.mk.loc[ex]
        race = f"{m.office}:{m.place}"
        if fv is None or fv != fv or 0.1 <= fv <= 0.9 or race in exclude or m.party not in ("D", "R"):
            continue
        r = D.db.execute("SELECT bids, asks, recv_ts FROM books WHERE exchange_id=? ORDER BY recv_ts DESC LIMIT 1", (ex,)).fetchone()
        if not r or time.time() - r[2] > 600:
            continue
        for side, levels in (("sell", json.loads(r[0])), ("buy", json.loads(r[1]))):
            for px, q in levels:
                edge = (px - fv) if side == "sell" else (fv - px)
                if edge < min_edge:
                    break
                unit = (1 - px) if side == "sell" else px
                wins_if_d = (m.party == "D") == (side == "buy")
                out.append({"ex": ex, "race": race, "party": m.party, "title": m.title, "ticker": D.mk.loc[ex, "ticker"],
                            "side": side, "px": px, "fv": fv, "q": q, "edge": edge, "unit": unit, "ret": edge / unit,
                            "wins_if_d": wins_if_d, "p_d": fv if m.party == "D" else 1 - fv})
    return sorted(out, key=lambda x: -x["ret"])


def allocate(levels, size, cap, have_race: dict[str, float], have_dir: dict[str, bool]):
    used = sum(have_race.values())
    per, alloc = dict(have_race), []
    for lv in levels:
        if lv["race"] in have_dir and have_dir[lv["race"]] != lv["wins_if_d"]:
            continue                                   # one direction per race
        room = min(size - used, cap * size - per.get(lv["race"], 0.0))
        if room < 50:
            continue
        n = int(min(lv["q"], room / lv["unit"]))
        if n < 1:
            continue
        alloc.append({**lv, "n": n})
        used += n * lv["unit"]
        per[lv["race"]] = per.get(lv["race"], 0.0) + n * lv["unit"]
        have_dir.setdefault(lv["race"], lv["wins_if_d"])
    return alloc


def risk(positions, rho, sims=400_000, seed=11):
    """positions: (race, wins_if_d, p_d, shares, unit). Outcomes from Kalshi odds with a national swing (rho)."""
    rng = np.random.default_rng(seed)
    nat = rng.standard_normal(sims)
    dwin = {}
    pnl = np.zeros(sims)
    for race, wins_if_d, p_d, n, unit in positions:
        if race not in dwin:
            dwin[race] = (np.sqrt(rho) * nat + np.sqrt(1 - rho) * rng.standard_normal(sims)) < norm.ppf(min(max(p_d, 1e-4), 1 - 1e-4))
        win = dwin[race] if wins_if_d else ~dwin[race]
        pnl += np.where(win, n * (1 - unit), -n * unit)
    return pnl


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=float, required=True, help="total book capital (SUSQies), existing book included")
    ap.add_argument("--cap", type=float, default=0.15)
    ap.add_argument("--min-edge", type=float, default=0.05)
    ap.add_argument("--exclude", default="", help="comma-separated races, e.g. senate:FL")
    ap.add_argument("--rho", type=float, default=0.5)
    ap.add_argument("--live", action="store_true", help="PLACE the orders (needs explicit approval)")
    a = ap.parse_args()
    D = o.Data()
    book, cost = load_book(), ledger_cost()
    have_race, have_dir = {}, {}
    for ex, q in book.items():
        m = D.mk.loc[ex]
        race = f"{m.office}:{m.place}"
        have_race[race] = have_race.get(race, 0.0) + cost.get(ex, 0.0)
        have_dir[race] = (m.party == "D") == (q > 0)
    levels = candidates(D, a.min_edge, {x.strip() for x in a.exclude.split(",") if x.strip()})
    alloc = allocate(levels, a.size, a.cap, have_race, have_dir)

    # group levels into one order per (exchange, side): limit = worst level, quantity = sum
    orders = {}
    for x in alloc:
        g = orders.setdefault((x["ex"], x["side"]), {**x, "n": 0, "cost": 0.0, "ev": 0.0})
        g["n"] += x["n"]
        g["cost"] += x["n"] * x["unit"]
        g["ev"] += x["n"] * x["edge"]
        g["px"] = x["px"]
    print(f"Settlement book plan: size {a.size:,.0f} (existing {sum(have_race.values()):,.0f}), cap {a.cap:.0%}/race, "
          f"edge >= {a.min_edge * 100:.0f}c through Kalshi")
    print(f"{'race':14} {'contract':48} {'order':12} {'shares':>8} {'cost':>8} {'EV':>7} {'Kalshi':>6} {'P(win)':>6}")
    for (ex, side), g in orders.items():
        pw = g["p_d"] if g["wins_if_d"] else 1 - g["p_d"]
        print(f"{g['race']:14} {g['title'][9:57]:48} {side:4} @ {g['px']:.3f} {g['n']:8,} {g['cost']:8,.0f} {g['ev']:+7,.0f} "
              f"{g['fv']:6.3f} {pw:6.3f}")
    new_cost = sum(g["cost"] for g in orders.values())
    pos = [(g["race"], g["wins_if_d"], g["p_d"], g["n"], g["cost"] / g["n"]) for g in orders.values()]
    for ex, q in book.items():   # existing holdings in the risk run
        m = D.mk.loc[ex]
        k = D.at(D.fv, time.time()).get(ex)
        if k == k and k is not None and q:
            unit = cost.get(ex, 0.0) / abs(q)
            pos.append((f"{m.office}:{m.place}", (m.party == "D") == (q > 0), k if m.party == "D" else 1 - k, abs(q), unit))
    if pos:
        pnl = risk(pos, a.rho)
        tot = new_cost + sum(have_race.values())
        print(f"\nWhole book: capital {tot:,.0f}, EV at settlement {pnl.mean():+,.0f} ({pnl.mean() / max(tot, 1):+.1%}), "
              f"P(loss) {np.mean(pnl < 0):.1%}, 5th pct {np.percentile(pnl, 5):+,.0f}, 1st pct {np.percentile(pnl, 1):+,.0f}, "
              f"worst {pnl.min():+,.0f}  (Kalshi odds, national swing rho {a.rho})")
    if not a.live:
        print("\nPLAN ONLY: nothing sent. Add --live to place these orders (60 s limit orders, nothing rests).")
        return
    from sm_client import SuperMarket
    c = SuperMarket(reads_per_min=30, writes_per_min=10)
    c.use_tournament()
    exp = lambda: (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for (ex, side), g in orders.items():
        try:
            r = c.place(ex, "yes", side, g["n"], g["px"], idempotency_key=str(uuid.uuid4()), expiration=exp())
        except Exception as e:
            print(f"  FAILED {g['title'][9:57]} {side} {g['n']} @ {g['px']}: {str(e)[:160]}")
            continue
        traded = abs(float(r.get("quantityTraded") or 0))
        paid = float(r.get("totalCost") or 0)
        if r.get("open") and r.get("orderId") is not None:
            try:
                c.cancel(r["orderId"])           # take only: never leave a remainder resting
            except Exception:
                pass
        if traded:
            signed = traded if side == "buy" else -traded
            book[ex] = book.get(ex, 0.0) + signed
            with LEDGER.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"t": time.time(), "ex": ex, "title": g["title"], "side": side, "qty": traded,
                                     "limit": g["px"], "cost": paid, "fv": g["fv"]}) + "\n")
            tmp = BOOK.with_suffix(".tmp")
            tmp.write_text(json.dumps(book, indent=1), encoding="utf-8")
            tmp.replace(BOOK)
        print(f"  {g['title'][9:57]} {side} {g['n']} @ {g['px']}: traded {traded:,.0f}, cost {paid:,.2f}")
    print(f"Book now: {json.dumps(book)}  -> {BOOK}")


if __name__ == "__main__":
    main()
