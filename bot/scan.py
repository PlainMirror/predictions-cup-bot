"""Daily opportunity scanner for the Predictions Cup.

    py scan.py                       # live: needs SM_API_KEY (read scope is enough)
    py scan.py --snapshot FILE       # offline: run on a saved/synthetic contest snapshot

Writes reports/scan_<timestamp>.md (read this) and reports/orders_<timestamp>.csv (proposed orders,
all with approve=0; flip the ones you want to 1 and hand the file to trade.py).

Fair value = your override in data/fv_overrides.csv if present, else Kalshi's mid (normalised so a
race's outcomes sum to 1). Opportunity types, best first:
  ENGINE   cross-market violations the platform itself detects (/relationships/constraints)
  PAIR     D and R contracts on the same race whose bids sum > 1 (sell both: riskless) or asks < 1
  EDGE     contest price far from fair value (directional, sized by fractional Kelly)
  CHAMBER  chamber-control price inconsistent with the contest's own race prices (Senate model)
  QUOTE    empty / wide books where resting quotes around fair value can earn the spread
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
from pathlib import Path

import kalshi
import senate_model
from common import parse_contest_title, round_tick

ROOT = Path(__file__).resolve().parent.parent
DATA, REPORTS = ROOT / "data", ROOT / "reports"


# ---------------------------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------------------------
def load_live() -> dict:
    from sm_client import SuperMarket
    sm = SuperMarket()
    t = sm.use_tournament()
    markets = sm.markets()
    ex_ids = [ex["id"] for m in markets for ex in m.get("exchanges", [])]
    prices = sm.prices(ex_ids)
    try:
        cons = sm.constraints(violations_only=True)
    except Exception as e:  # the scan is still useful without it
        cons = {"data": [], "error": str(e)}
    try:
        board = sm.leaderboard(period="all", limit=10)
    except Exception as e:
        board = {"error": str(e)}
    snap = {"source": "live", "taken_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "tournament": t, "markets": markets, "prices": prices, "constraints": cons, "books": {},
            "leaderboard": board}
    snap["_client"] = sm
    return snap


def load_snapshot(path: Path) -> dict:
    snap = json.loads(path.read_text(encoding="utf-8"))
    snap.setdefault("books", {})
    snap.setdefault("constraints", {"data": []})
    return snap


def load_overrides() -> dict[tuple, float]:
    p = DATA / "fv_overrides.csv"
    out = {}
    if p.exists():
        lines = [l for l in p.open(encoding="utf-8") if l.strip() and not l.lstrip().startswith("#")]
        for row in csv.DictReader(lines):
            office = (row.get("office") or "").strip().lower()
            if not office or not (row.get("fv") or "").strip():
                continue
            place = (row.get("place") or "").strip()
            place = place.lower() if office == "control" else place.upper()
            out[(office, place, row["party"].strip().upper())] = float(row["fv"])
    return out


# ---------------------------------------------------------------------------------------------
# Fair values
# ---------------------------------------------------------------------------------------------
def fair_value(lookup: dict, overrides: dict, race: tuple, party: str):
    """Returns (fv, kalshi_half_spread, source) or (None, None, None)."""
    if (race[0], race[1], party) in overrides:
        return overrides[(race[0], race[1], party)], 0.0, "override"
    r = lookup.get(race)
    if not r or party not in r or r[party].get("mid") is None:
        return None, None, None
    total = sum((r.get(p) or {}).get("mid") or 0 for p in ("D", "R")) + r.get("other_mid", 0)
    mid = r[party]["mid"]
    fv = mid / total if 0.85 <= total <= 1.15 else mid
    return fv, (r[party].get("spread") or 0) / 2, "kalshi"


def kelly_buy(p: float, q: float) -> float:
    """Kelly fraction for buying a contract paying 1 at price q when P(win)=p."""
    return max(0.0, (p - q) / (1 - q)) if q < 1 else 0.0


# ---------------------------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------------------------
def scan(snap: dict, args):
    lookup = kalshi.as_lookup(kalshi.fetch_reference(DATA / "kalshi_reference.json"))
    overrides = load_overrides()
    bankroll = args.bankroll or float((snap.get("tournament") or {}).get("myBalance") or 100000)
    prices = snap["prices"]

    rows, unparsed = [], []
    for m in snap["markets"]:
        if m.get("status", "open") != "open":
            continue
        exs = m.get("exchanges") or []
        if m.get("isMultiOutcome") or len(exs) != 1:
            unparsed.append(f"{m['title']} (multi-outcome, {len(exs)} exchanges)")
            continue
        parsed = parse_contest_title(m["title"])
        ex = exs[0]
        px = prices.get(ex["id"], {})
        row = {"market_id": m["id"], "exchange_id": ex["id"], "title": m["title"],
               "bid": px.get("bestBid"), "ask": px.get("bestAsk"), "last": px.get("latestPrice") or ex.get("latestPrice"),
               "race": None, "party": None, "fv": None, "hs": None, "fv_src": None}
        if parsed:
            row["race"], row["party"] = parsed
            row["fv"], row["hs"], row["fv_src"] = fair_value(lookup, overrides, *parsed)
        else:
            unparsed.append(m["title"])
        rows.append(row)

    opps: list[dict] = []
    gid = 0

    # ---- ENGINE: the platform's own relationship-violation detector --------------------------
    for c in (snap.get("constraints") or {}).get("data", []):
        if c.get("evaluationStatus") != "violated":
            continue
        gid += 1
        trades = c.get("suggestedCorrectiveTrades") or []
        opps.append({"kind": "ENGINE", "score": c.get("violationAmount", 0) * 10, "group": f"g{gid}",
                     "title": f"{c.get('type')} violation {c.get('violationAmount'):.3f}: {c.get('reason', '')[:120]}",
                     "legs": [{"exchange_id": t["exchangeId"], "side": t.get("outcomeSide", "yes").lower(),
                               "action": t.get("action", "buy").lower(), "price": t.get("currentPrice"),
                               "title": t.get("marketTitle") or "", "note": t.get("rationale", "")}
                              for t in trades],
                     "relationship_id": c.get("relationshipId")})

    # ---- PAIR: D and R on the same race ------------------------------------------------------
    by_race: dict[tuple, dict] = {}
    for r in rows:
        if r["race"]:
            by_race.setdefault(r["race"], {})[r["party"]] = r
    for race, pr in by_race.items():
        d, rr = pr.get("D"), pr.get("R")
        if not d or not rr:
            continue
        if d["bid"] is not None and rr["bid"] is not None and d["bid"] + rr["bid"] > 1 + args.min_arb:
            gid += 1
            lock = d["bid"] + rr["bid"] - 1
            opps.append({"kind": "PAIR", "score": 5 + lock * 10, "group": f"g{gid}",
                         "title": f"{race[0]} {race[1]}: bids sum {d['bid'] + rr['bid']:.3f} -> sell both, lock {lock:.3f}/pair (riskless)",
                         "legs": [_leg(d, "yes", "sell", d["bid"], "sell YES D at bid"),
                                  _leg(rr, "yes", "sell", rr["bid"], "sell YES R at bid")], "lock": lock})
        if d["ask"] is not None and rr["ask"] is not None and d["ask"] + rr["ask"] < 1 - args.min_arb:
            p_other = (lookup.get(race) or {}).get("other_mid", 0.0)
            ev = 1 - p_other - d["ask"] - rr["ask"]
            if ev > args.min_arb:
                gid += 1
                opps.append({"kind": "PAIR", "score": 3 + ev * 10, "group": f"g{gid}",
                             "title": f"{race[0]} {race[1]}: asks sum {d['ask'] + rr['ask']:.3f} -> buy both, EV {ev:.3f}/pair "
                                      f"(loses if a 3rd party wins, P~{p_other:.2f})",
                             "legs": [_leg(d, "yes", "buy", d["ask"], "buy YES D at ask"),
                                      _leg(rr, "yes", "buy", rr["ask"], "buy YES R at ask")], "lock": ev})

    # ---- EDGE: contest vs fair value -----------------------------------------------------------
    # "Buy YES D" and "sell YES R" (= buy NO R) are the same bet on a two-party race, so keep only the
    # better route per (race, direction) and mention the sibling, instead of doubling the exposure.
    best: dict[tuple, dict] = {}
    for r in rows:
        if r["fv"] is None:
            continue
        thr = max(args.min_edge, (r["hs"] or 0) + 0.01)
        p = r["fv"]
        cands = []
        if r["ask"] is not None and p - r["ask"] >= thr:
            cands.append(("buy", r["ask"], p - r["ask"], kelly_buy(p, r["ask"]), r["ask"]))
        if r["bid"] is not None and r["bid"] - p >= thr:
            # selling YES at b == buying NO at 1-b
            cands.append(("sell", r["bid"], r["bid"] - p, kelly_buy(1 - p, 1 - r["bid"]), 1 - r["bid"]))
        for action, px, edge, f, cost in cands:
            backs = r["party"] if action == "buy" else {"D": "R", "R": "D"}.get(r["party"], f"not {r['party']}")
            key = (r["race"] or r["title"], backs)
            o = _edge(r, action, px, edge, f, bankroll, args)
            o["ret"] = edge / max(cost, 0.005)  # expected return per SUSQie spent
            if key in best:
                keep, alt = (o, best[key]) if o["ret"] > best[key]["ret"] else (best[key], o)
                keep["title"] += f" [better route than: {alt['legs'][0]['action']} YES '{alt['legs'][0]['title']}' @ {alt['legs'][0]['price']:.3f}]"
                best[key] = keep
            else:
                best[key] = o
    opps += best.values()

    # ---- QUOTE: books with no / wide quotes, where we can make the market ----------------------
    for r in rows:
        if r["fv"] is None:
            continue
        spread = (r["ask"] - r["bid"]) if (r["ask"] is not None and r["bid"] is not None) else None
        if spread is None or spread > args.quote_if_spread_over:
            half = max(args.quote_half_width, (r["hs"] or 0) + 0.01)
            bid_px = round_tick(r["fv"] - half, mode="down")
            ask_px = round_tick(r["fv"] + half, mode="up")
            if r["bid"] is not None:
                bid_px = max(bid_px, min(round_tick(r["bid"] + 0.005), round_tick(r["fv"] - 0.01, mode="down")))
            if r["ask"] is not None:
                ask_px = min(ask_px, max(round_tick(r["ask"] - 0.005), round_tick(r["fv"] + 0.01, mode="up")))
            if not (0.005 <= bid_px < r["fv"] < ask_px <= 0.995):
                continue
            qty = max(1, int(args.quote_notional / max(r["fv"], 0.05)))
            gid += 1
            opps.append({"kind": "QUOTE", "score": 1 + (spread or 0.2), "group": f"g{gid}",
                         "title": f"{r['title']} | book {_fmt(r['bid'])}/{_fmt(r['ask'])} vs FV {r['fv']:.3f} -> quote {bid_px:.3f}/{ask_px:.3f}",
                         "legs": [_leg(r, "yes", "buy", bid_px, "rest bid", qty), _leg(r, "yes", "sell", ask_px, "rest offer", qty)]})

    # ---- CHAMBER: Senate control vs the contest's own race prices ------------------------------
    notes = chamber_check(rows, lookup, by_race, opps)

    opps.sort(key=lambda o: -o["score"])
    return opps, rows, unparsed, notes, bankroll


def _leg(r: dict, side: str, action: str, price, note: str, qty: int | None = None) -> dict:
    return {"exchange_id": r["exchange_id"], "market_id": r["market_id"], "title": r["title"], "side": side,
            "action": action, "price": price, "quantity": qty, "fv": r["fv"], "note": note}


def _fmt(x):
    return "-" if x is None else f"{x:.3f}"


def _edge(r, action, px, edge, kelly_f, bankroll, args) -> dict:
    stake = min(args.kelly * kelly_f * bankroll, args.max_frac * bankroll)
    cost_per = px if action == "buy" else 1 - px
    qty = int(stake / max(cost_per, 0.005))
    return {"kind": "EDGE", "score": 2 + edge * 10, "group": None,
            "title": f"{r['title']} | {action.upper()} YES @ {px:.3f} vs FV {r['fv']:.3f} ({r['fv_src']}) edge {edge:.3f}, Kelly {kelly_f:.2f}",
            "legs": [_leg(r, "yes", action, px, f"edge {edge:.3f}", qty)], "edge": edge}


def chamber_check(rows, lookup, by_race, opps) -> list[str]:
    notes = []
    ks = lookup.get(("control", "senate"), {})
    if not ks.get("R"):
        return ["No Kalshi Senate-control price; skipped chamber check."]
    k_races = senate_model.races_from_kalshi(lookup)
    rho, _ = senate_model.implied_rho(k_races, ks["R"]["mid"], sims=8000)
    # Replace Kalshi race probabilities with the contest's own mid prices where the contest has a book.
    c_races = {s: dict(v) for s, v in k_races.items()}
    replaced = []
    for (office, place), pr in by_race.items():
        if office != "senate" or place not in c_races:
            continue
        mids = {}
        for party, r in pr.items():
            if r["bid"] is not None and r["ask"] is not None:
                mids[party] = (r["bid"] + r["ask"]) / 2
            elif r["last"] is not None:
                mids[party] = r["last"]
        if "R" in mids:
            c_races[place]["R"] = mids["R"]
            c_races[place]["D"] = mids.get("D", 1 - mids["R"])
            replaced.append(place)
        elif "D" in mids:
            c_races[place]["D"] = mids["D"]
            c_races[place]["R"] = 1 - mids["D"] - c_races[place].get("OTHER", 0)
            replaced.append(place)
    res = senate_model.simulate(c_races, rho, sims=20000)
    p_model = res["p"]["R"]
    notes.append(f"Senate model: Kalshi-implied race correlation rho={rho:.2f}. Using contest prices for "
                 f"{len(replaced)} races ({', '.join(sorted(replaced))}) and Kalshi for the rest, "
                 f"P(R control)={p_model:.3f}, P(D)={res['p']['D']:.3f}, P(neither/independent pivot)={res['p']['NEITHER']:.3f}. "
                 f"Kalshi control: R {ks['R']['mid']:.3f}.")
    for r in rows:
        if r["race"] == ("control", "senate"):
            p = p_model if r["party"] == "R" else res["p"]["D"]
            for side_px, action in ((r["ask"], "buy"), (r["bid"], "sell")):
                if side_px is None:
                    continue
                edge = (p - side_px) if action == "buy" else (side_px - p)
                if edge > 0.03:
                    opps.append({"kind": "CHAMBER", "score": 2 + edge * 10, "group": None,
                                 "title": f"{r['title']} {action.upper()} @ {side_px:.3f}: contest's own race prices imply {p:.3f}",
                                 "legs": [_leg(r, "yes", action, side_px, f"chamber edge {edge:.3f}")]})
    return notes


# ---------------------------------------------------------------------------------------------
# Order-book sizing (live only) and output
# ---------------------------------------------------------------------------------------------
def size_from_books(snap, opps, max_books: int):
    sm = snap.get("_client")
    if not sm:
        return
    wanted = []
    for o in opps:
        if o["kind"] in ("PAIR", "EDGE", "CHAMBER", "ENGINE"):
            wanted += [l["exchange_id"] for l in o["legs"]]
    for ex_id in list(dict.fromkeys(wanted))[:max_books]:
        snap["books"][ex_id] = sm.orderbook(ex_id, depth=10)
    for o in opps:
        top = []
        for l in o["legs"]:
            b = snap["books"].get(l["exchange_id"])
            if not b or l["price"] is None:
                continue
            levels = b["asks"] if l["action"] == "buy" else b["bids"]
            avail = sum(x["quantity"] for x in levels
                        if (x["price"] <= l["price"] + 1e-9 if l["action"] == "buy" else x["price"] >= l["price"] - 1e-9))
            l["book_qty"] = avail
            top.append(avail)
        if o["kind"] in ("PAIR", "ENGINE") and top:
            q = int(min(top))
            for l in o["legs"]:
                l["quantity"] = q
        else:
            for l in o["legs"]:
                if "book_qty" in l and l["quantity"]:
                    l["quantity"] = int(min(l["quantity"], l["book_qty"]))


def write_outputs(snap, opps, rows, unparsed, notes, bankroll, args):
    REPORTS.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    md, csv_path = REPORTS / f"scan_{stamp}.md", REPORTS / f"orders_{stamp}.csv"
    t = snap.get("tournament") or {}
    out = [f"# Predictions Cup scan {stamp}",
           f"Source: {snap.get('source')} | taken {snap.get('taken_at', '?')} | bankroll used for sizing: {bankroll:,.0f}",
           f"Tournament: {t.get('name', '?')} | members {t.get('memberCount', '?')} | markets {len(snap['markets'])}", ""]
    out += [f"> {n}" for n in notes] + [""]
    board = snap.get("leaderboard") or {}
    entries = board.get("leaderboard") or []
    if entries:
        out.append("## Leaderboard top 10 (P&L; tells you how high the winning line is)")
        for e in entries[:10]:
            out.append(f"- #{e.get('rank')} {e.get('username') or e.get('profileId')}: P&L {e.get('pnl', 0):,.0f}, "
                       f"trades {e.get('tradesCount')}, volume {e.get('volume', 0):,.0f}")
        my = board.get("myRank")
        out += [f"- you: rank {my}" if my is not None else "- you: unranked (make at least one trade)", ""]
    for kind in ("ENGINE", "PAIR", "CHAMBER", "EDGE", "QUOTE"):
        sel = [o for o in opps if o["kind"] == kind]
        out.append(f"## {kind} ({len(sel)})")
        for o in sel[: args.top]:
            out.append(f"- {o['title']}")
            for l in o["legs"]:
                bq = f", book {l['book_qty']:.0f}" if "book_qty" in l else ""
                out.append(f"    - {l['action']} {l['side']} ex{l['exchange_id']} @ {_fmt(l['price'])} x {l.get('quantity') or '?'}{bq} — {l.get('note', '')}")
        out.append("")
    out.append("## Fair-value table (contest vs reference)")
    out.append("| market | bid | ask | last | FV | src |")
    out.append("|---|---|---|---|---|---|")
    for r in sorted(rows, key=lambda r: (r["race"] or ("~", ""), r["party"] or "")):
        out.append(f"| {r['title']} | {_fmt(r['bid'])} | {_fmt(r['ask'])} | {_fmt(r['last'])} | {_fmt(r['fv'])} | {r['fv_src'] or '-'} |")
    if unparsed:
        out += ["", "## Markets needing a manual fair value (non-standard titles)"] + [f"- {u}" for u in unparsed]
    md.write_text("\n".join(out), encoding="utf-8")

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["approve", "kind", "group", "relationship_id", "exchange_id", "market_id", "title", "side", "action",
                    "price", "quantity", "fv", "note"])
        for o in opps:
            for l in o["legs"]:
                if l["price"] is None:
                    continue
                w.writerow([0, o["kind"], o.get("group") or "", o.get("relationship_id") or "", l["exchange_id"],
                            l.get("market_id", ""), l.get("title", ""), l["side"], l["action"], f"{l['price']:.3f}",
                            l.get("quantity") or "", "" if l.get("fv") is None else f"{l['fv']:.4f}", l.get("note", "")])
    # keep a raw snapshot for history / backtesting
    raw = {k: v for k, v in snap.items() if not k.startswith("_")}
    if snap.get("source") == "live":
        (DATA / "snapshots").mkdir(parents=True, exist_ok=True)
        (DATA / "snapshots" / f"contest_{stamp}.json").write_text(json.dumps(raw), encoding="utf-8")
    return md, csv_path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot", type=Path, help="offline contest snapshot JSON instead of the live API")
    ap.add_argument("--bankroll", type=float, help="override bankroll for sizing (default: your live balance)")
    ap.add_argument("--min-edge", type=float, default=0.03, help="min |price - FV| to flag an EDGE")
    ap.add_argument("--min-arb", type=float, default=0.004, help="min locked profit per pair to flag a PAIR")
    ap.add_argument("--kelly", type=float, default=0.25, help="fraction of full Kelly for EDGE sizing")
    ap.add_argument("--max-frac", type=float, default=0.10, help="max bankroll fraction per EDGE trade")
    ap.add_argument("--quote-if-spread-over", type=float, default=0.04)
    ap.add_argument("--quote-half-width", type=float, default=0.02)
    ap.add_argument("--quote-notional", type=float, default=2000, help="SUSQies per resting quote")
    ap.add_argument("--max-books", type=int, default=45, help="order books to read for sizing (100 reads/min budget)")
    ap.add_argument("--top", type=int, default=40, help="rows per section in the report")
    args = ap.parse_args()

    snap = load_snapshot(args.snapshot) if args.snapshot else load_live()
    opps, rows, unparsed, notes, bankroll = scan(snap, args)
    size_from_books(snap, opps, args.max_books)
    md, csv_path = write_outputs(snap, opps, rows, unparsed, notes, bankroll, args)
    counts = {k: sum(o["kind"] == k for o in opps) for k in ("ENGINE", "PAIR", "CHAMBER", "EDGE", "QUOTE")}
    print(f"{len(rows)} markets scanned, {sum(r['fv'] is not None for r in rows)} with fair values. Opportunities: {counts}")
    print(f"Report: {md}\nOrders: {csv_path}  (set approve=1 on rows you want, then: py trade.py {csv_path.name})")


if __name__ == "__main__":
    main()
