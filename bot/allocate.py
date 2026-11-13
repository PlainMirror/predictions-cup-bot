"""Depth-aware allocation of cash across mispriced contracts (READ-ONLY: proposes, never trades).

    py allocate.py [--budget 95000] [--min-edge 0.03] [--cap 15000] [--skip Montana,"South Dakota",Idaho,Nebraska]

For every contest contract with a Kalshi fair value, walks the real order book level by level and keeps every
level whose edge vs FV is >= min-edge (buying YES below FV, or buying NO = selling YES above FV). Each level is a
"slice" with return-on-cash = edge / cash-per-share. Slices are taken greedily, best return first, within the budget,
a per-contract cash cap, and one direction per race (no D-vs-R double exposure). Writes reports/alloc_<stamp>.csv
in the same format trade.py reads (approve=0 on every row).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
from pathlib import Path

import kalshi
from common import parse_contest_title
from scan import fair_value, load_overrides
from sm_client import SuperMarket

ROOT = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=float, default=95_000)
    ap.add_argument("--min-edge", type=float, default=0.03)
    ap.add_argument("--cap", type=float, default=15_000, help="max cash per race (D and R contracts together)")
    ap.add_argument("--skip", default="Montana,South Dakota,Idaho,Nebraska",
                    help="states whose D/R contracts depend on how independents resolve")
    a = ap.parse_args()
    skip = [s.strip() for s in a.skip.split(",") if s.strip()]

    sm = SuperMarket()
    t = sm.use_tournament()
    budget = min(a.budget, float(t["myBalance"]) - 1000)
    lookup = kalshi.as_lookup(kalshi.fetch_reference(ROOT / "data" / "kalshi_reference.json"))
    overrides = load_overrides()
    markets = sm.markets()
    ex_ids = [m["exchanges"][0]["id"] for m in markets if len(m.get("exchanges") or []) == 1]
    tops = sm.prices(ex_ids)

    cands = []
    for m in markets:
        exs = m.get("exchanges") or []
        parsed = parse_contest_title(m["title"])
        if len(exs) != 1 or not parsed or any(s in m["title"] for s in skip):
            continue
        fv, hs, src = fair_value(lookup, overrides, *parsed)
        if fv is None:
            continue
        top = tops.get(exs[0]["id"], {})
        bb, ba = top.get("bestBid"), top.get("bestAsk")
        if not ((ba is not None and fv - ba >= a.min_edge) or (bb is not None and bb - fv >= a.min_edge)):
            continue
        cands.append((m, exs[0]["id"], parsed, fv))

    print(f"Reading {len(cands)} order books...")
    slices = []
    for m, ex, (race, party), fv in cands:
        book = sm.orderbook(ex, depth=30)
        for lvl in book.get("asks", []):           # buy YES below FV
            px, q = float(lvl["price"]), float(lvl["quantity"])
            if fv - px < a.min_edge:
                break
            slices.append({"m": m, "ex": ex, "race": race, "backs": party, "action": "buy", "price": px, "qty": q,
                           "cash": px, "edge": fv - px, "fv": fv, "p_lose": 1 - fv})
        for lvl in book.get("bids", []):           # sell YES above FV == buy NO at 1 - px
            px, q = float(lvl["price"]), float(lvl["quantity"])
            if px - fv < a.min_edge:
                break
            slices.append({"m": m, "ex": ex, "race": race, "backs": {"D": "R", "R": "D"}.get(party, "not " + party),
                           "action": "sell", "price": px, "qty": q, "cash": 1 - px, "edge": px - fv, "fv": fv, "p_lose": fv})
    for s in slices:
        s["ret"] = s["edge"] / s["cash"]
    slices.sort(key=lambda s: -s["ret"])

    used, ev, per_race, race_dir, picks = 0.0, 0.0, {}, {}, []
    for s in slices:
        if used >= budget:
            break
        if race_dir.setdefault(s["race"], s["backs"]) != s["backs"]:
            continue  # never back both sides of the same race
        room = min(a.cap - per_race.get(s["race"], 0.0), budget - used)
        q = int(min(s["qty"], room / s["cash"]))
        if q < 1 or q * s["cash"] < 50:
            continue
        cost = q * s["cash"]
        used += cost
        ev += q * s["edge"]
        per_race[s["race"]] = per_race.get(s["race"], 0.0) + cost
        picks.append({**s, "q": q, "cost": cost})

    # merge slices per contract into one limit order at the worst accepted level
    orders = {}
    for p in picks:
        k = (p["ex"], p["action"])
        o = orders.setdefault(k, {"title": p["m"]["title"], "market_id": p["m"]["id"], "ex": p["ex"], "action": p["action"],
                                  "q": 0, "cost": 0.0, "ev": 0.0, "fv": p["fv"], "p_lose": p["p_lose"], "limit": p["price"]})
        o["q"] += p["q"]
        o["cost"] += p["cost"]
        o["ev"] += p["q"] * p["edge"]
        o["limit"] = max(o["limit"], p["price"]) if p["action"] == "buy" else min(o["limit"], p["price"])
    rows = sorted(orders.values(), key=lambda o: -o["ev"] / o["cost"])
    print(f"\n{'contract':58} {'order':22} {'FV':>6} {'shares':>7} {'cash':>8} {'exp. profit':>11} {'ret':>6} {'P(lose)':>7}")
    for o in rows:
        what = (f"buy YES <= {o['limit']:.3f}" if o["action"] == "buy" else f"buy NO <= {1 - o['limit']:.3f}")
        print(f"{o['title'][9:67]:58} {what:22} {o['fv']:6.3f} {o['q']:7,} {o['cost']:8,.0f} {o['ev']:11,.0f} "
              f"{o['ev'] / o['cost'] * 100:5.1f}% {o['p_lose']:7.1%}")
    print(f"\nTOTAL cash {used:,.0f} of {budget:,.0f} | expected profit {ev:,.0f} ({ev / max(used, 1) * 100:.1f}% on cash) "
          f"| worst single-race loss {max(per_race.values(), default=0):,.0f}")

    path = ROOT / "reports" / f"alloc_{dt.datetime.now():%Y%m%d_%H%M}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["approve", "kind", "group", "relationship_id", "exchange_id", "market_id", "title", "side", "action",
                    "price", "quantity", "fv", "note"])
        for o in rows:
            w.writerow([0, "EDGE", "", "", o["ex"], o["market_id"], o["title"], "yes", o["action"], f"{o['limit']:.3f}",
                        o["q"], f"{o['fv']:.4f}", f"exp {o['ev']:.0f} on {o['cost']:.0f}"])
    print(f"Order sheet (all approve=0): {path}")


if __name__ == "__main__":
    main()
