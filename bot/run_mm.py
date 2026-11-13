"""Market maker for the Predictions Cup.

  py run_mm.py --paper                 simulate 48h on a paper exchange (no key needed)
  py run_mm.py --select                rank live markets by 24h volume, save data/mm_markets.json (key: read)
  py run_mm.py --dry                   live data, compute and log takes/exits, send nothing (key: read)
  py run_mm.py --live                  trade (key: read + trade)
  py run_mm.py --shadow                virtual orders on live data (collector DB)
Options: --params my.json (override mm/params.py), --exit 0.7,0.0,24, --mm (also make markets in --markets /
data/mm_markets.json; off by default: never shadow-tested)

--live and --dry run exactly the shadow's strategy: takes + exits on every contract with a Kalshi fair value, market
data from the collector's realtime feed (collector.py must be running), exit lots saved to data/live_lots.json.

Stop with Ctrl+C or by creating data/STOP (every engine), data/STOP_LIVE (live only) or data/STOP_SHADOW (shadows only);
every resting order is cancelled on the way out.
Do not trade by hand in the markets the bot is making: it treats every open order there as its own.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from pathlib import Path

from common import keep_awake, parse_contest_title
from mm.engine import MMEngine
from mm.events import Calendar
from mm.params import Params

ROOT = Path(__file__).resolve().parent.parent
SEL = ROOT / "data" / "mm_markets.json"

# Paper universe: the contest's featured markets, with Kalshi FV and 30-day hourly sigma measured 2026-10-10.
PAPER_CONTRACTS = [
    ("Will the Democratic Party win the U.S. House?", 0.895, 0.0013),
    ("Will the Republican Party win the U.S. Senate?", 0.395, 0.0036),
    ("Will the Democratic Party win the Texas Senate?", 0.625, 0.0033),
    ("Will the Democratic Party win the Maine Senate?", 0.575, 0.0035),
    ("Will the Democratic Party win the NY-17 House race?", 0.585, 0.0051),
    ("Will the Democratic Party win the PA-07 House race?", 0.865, 0.0061),
    ("Will the Republican Party win the Arizona Governor?", 0.125, 0.0023),
    ("Will the Democratic Party win the New Hampshire Senate?", 0.890, 0.0026),
    ("Will the Republican Party win the New Hampshire Senate?", 0.105, 0.0026),
]


def contest_universe(client, fv) -> list[dict]:
    out = []
    for m in client.markets():
        exs = m.get("exchanges") or []
        parsed = parse_contest_title(m["title"])
        if len(exs) != 1 or not parsed:
            continue
        race, party = parsed
        out.append({"market_id": m["id"], "exchange_id": exs[0]["id"], "title": m["title"], "race": race, "party": party})
    fv.track([(u["race"], u["party"]) for u in out])
    return [u for u in out if fv.has(u["race"], u["party"])]


def select(client, fv, P, top: int):
    import time as _t
    uni = contest_universe(client, fv)
    fv.poll()
    prices = client.prices([u["exchange_id"] for u in uni])
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = []
    print(f"Scoring {len(uni)} markets with a Kalshi fair value (one trade-tape read each, ~{len(uni) * 0.7 / 60:.0f} min)...")
    for u in uni:
        s = fv.get(u["race"], u["party"])
        if not s or not (P.min_fv <= s.fv <= P.max_fv):
            continue
        tr = client._get(f"/exchanges/{u['exchange_id']}/trades", tournamentId=client.tournament_id, limit=200, **{"from": since})
        trades = tr.get("data") or []
        vol = sum(float(t.get("volume") or 0) for t in trades)
        px = prices.get(u["exchange_id"], {})
        spread = (px["bestAsk"] - px["bestBid"]) if px.get("bestAsk") is not None and px.get("bestBid") is not None else None
        # Expected capture ~ flow x spread we can earn. A 1-tick book means Susquehanna already quotes it:
        # we would sit behind its queue and rarely fill, so those markets score low.
        capture = 0.03 if spread is None else (0.002 if spread <= P.tick + 1e-9 else min(spread, 0.06))
        rows.append({**u, "trades_24h": len(trades), "volume_24h": round(vol), "spread": spread, "fv": round(s.fv, 3),
                     "sigma_hourly": round(s.sigma_hourly, 4), "score": round((vol + 500) * capture, 1)})
    rows.sort(key=lambda r: -r["score"])
    print(f"\n{'market':58} {'trades':>6} {'volume':>9} {'spread':>7} {'FV':>6} {'score':>7}")
    for r in rows[:60]:
        sp = "-" if r["spread"] is None else f"{r['spread']:.3f}"
        print(f"{r['title'][:58]:58} {r['trades_24h']:6} {r['volume_24h']:9,} {sp:>7} {r['fv']:6.3f} {r['score']:7.0f}")
    chosen = rows[:top]
    SEL.write_text(json.dumps(chosen, indent=1), encoding="utf-8")
    print(f"\nSaved top {len(chosen)} to {SEL}. Edit that file to add/remove markets.")


def run_paper(P, hours: float, house: bool, seed: int, noise: float = 20.0, fv_lag: float = 15.0):
    from mm.paper import PaperExchange, PaperFV, PaperWorld, WorldParams
    contracts = []
    for i, (title, fv0, sig) in enumerate(PAPER_CONTRACTS):
        race, party = parse_contest_title(title)
        contracts.append({"market_id": str(100 + i), "exchange_id": str(900 + i), "title": title, "race": race,
                          "party": party, "fv0": fv0, "sigma_hourly": sig})
    world = PaperWorld(contracts, WorldParams(house=house, noise_per_hour=noise), seed=seed)
    client, fvsrc = PaperExchange(world), None
    markets = [{k: c[k] for k in ("market_id", "exchange_id", "title", "race", "party")} for c in contracts]
    fvsrc = PaperFV(world, P, markets, lag_s=fv_lag)
    eng = MMEngine(client, fvsrc, P, markets, calendar=Calendar(Path("nonexistent")), clock=lambda: world.t,
                   sleep=world.advance, log_name=f"paper_{'house' if house else 'nohouse'}_{seed}.jsonl", verbose=False)
    start = world.value()
    eng.run(max_cycles=int(hours * 3600 / P.cycle_s))
    # flatten-free summary: mark to true FV
    fills = world.my_fills
    def markout(f, secs):
        c = world.c[f["exchangeId"]]
        later = world.fv(f["exchangeId"], f["filledAt"] + secs)
        yes_px = f["price"] if f["side"] == "yes" else 1 - f["price"]
        d = 1 if f["quantity"] > 0 else -1
        return (later - yes_px) * d * abs(f["quantity"])
    edge0 = sum(((f["fv"] - (f["price"] if f["side"] == "yes" else 1 - f["price"])) * (1 if f["quantity"] > 0 else -1)) * abs(f["quantity"]) for f in fills)
    m5 = sum(markout(f, 300) for f in fills)
    m60 = sum(markout(f, 3600) for f in fills)
    shares = sum(abs(f["quantity"]) for f in fills)
    recs = [json.loads(l) for l in open(eng.log_path, encoding="utf-8")]
    writes = sum(r["op"]["cost"] if r["type"] == "requote" else 1 for r in recs if r["type"] in ("requote", "take"))
    print(f"\n=== PAPER {hours:.0f}h, {'WITH' if house else 'WITHOUT'} a tight house MM, {noise:g} student orders/h/market, "
          f"FV lag {fv_lag:g}s (seed {seed}) ===")
    print(f"writes/min {writes / (hours * 60):.1f}")
    print(f"P&L marked to true FV: {world.value() - start:+,.0f} SUSQies")
    print(f"fills: {len(fills)} ({shares:,.0f} shares)  passive {world.stats['my_passive_fills']}  takes {world.stats['my_take_fills']}")
    if shares:
        print(f"edge at fill vs FV: {edge0:+,.0f} ({edge0 / shares * 100:+.2f}c/share)   "
              f"markout +5m: {m5:+,.0f}   +60m: {m60:+,.0f}  (negative drift = adverse selection)")
    print(f"open inventory: " + ", ".join(f"{world.c[ex]['title'][9:40]} {q:+.0f}" for ex, q in world.pos.items() if abs(q) > 0.5))
    print(f"log: {eng.log_path}")


def parse_exit(spec: str):
    """start_capture,end_capture,glide_hours[,time_stop_hours]"""
    from mm.exits import ExitParams
    v = [float(x) for x in spec.split(",")]
    return ExitParams(start_capture=v[0], end_capture=v[1], glide_hours=v[2], time_stop_h=v[3] if len(v) > 3 else None)


def describe_election(P) -> str:
    if not P.election_mode:
        return f"Election mode OFF: every order is pulled at {P.election_night_cutoff_et} ET."
    return (f"ELECTION MODE armed: take-only vs Kalshi from {P.en_start_et} to {P.en_end_et} ET; per-race caps "
            f"{P.en_cap_called:.0%}/{P.en_cap_lean:.0%}/{P.en_cap_open:.0%} of V0 (called/leaning/open), leaning+open "
            f"<= {P.en_cap_uncalled:.0%}; poll-close gate {'on' if P.en_gate else 'OFF'}. Freeze new risk: data/EN_FREEZE.")


def describe_exit(X) -> str:
    ts = f", market exit after {X.time_stop_h:g} h" if X.time_stop_h else ""
    return (f"gap lots: start {X.start_capture:.0%} of gap -> {X.end_capture:.0%} over {X.glide_hours:g} h{ts}; "
            f"everything exits at market from {X.liquidate_et} ET")


def run_shadow(P, fv, uni, exit_spec="0.7,0.0,24", tag=""):
    """Every contract with a fair value is eligible for takes + exits; market-making quotes only where selected."""
    from mm.shadow import ShadowExchange
    quote_ids = set()
    if SEL.exists():
        quote_ids = {r["market_id"] for r in json.loads(SEL.read_text(encoding="utf-8"))}
    markets = [dict(u, quote=u["market_id"] in quote_ids) for u in uni.values()]
    fv.tracked = {(m["race"], m["party"]) for m in markets}
    print(f"SHADOW: {len(markets)} contracts (takes + exits), market making in {sum(m['quote'] for m in markets)}. "
          "Orders are virtual; nothing is sent to the exchange.")
    print("Calibrating volatility from Kalshi history...")
    fv.calibrate(days=3)
    fv.poll()
    P.cycle_s = max(P.cycle_s, 6.0)
    from mm.exits import ExitParams
    X = parse_exit(exit_spec)
    print(f"Exit policy: {describe_exit(X)}; entries: {P.entry_mode}")
    print(describe_election(P))
    if P.fresh_h:
        print(f"Fresh-gap rule ON: only gaps that opened within {P.fresh_h:g} h")
    eng = MMEngine(ShadowExchange(), fv, P, markets, books_per_cycle=10_000, exit_params=X, stop_name="STOP_SHADOW",
                   log_name=f"shadow{tag}_{dt.datetime.now():%Y%m%d_%H%M%S}.jsonl")
    print(f"Logging to {eng.log_path}. Ctrl+C or create data/STOP to stop.")
    eng.run()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--paper", action="store_true")
    g.add_argument("--select", action="store_true")
    g.add_argument("--dry", action="store_true")
    g.add_argument("--live", action="store_true")
    g.add_argument("--shadow", action="store_true", help="real engine + live data (collector DB), VIRTUAL orders only")
    ap.add_argument("--params")
    ap.add_argument("--markets", help="comma-separated contest market ids (default: data/mm_markets.json)")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--hours", type=float, default=48)
    ap.add_argument("--no-house", action="store_true", help="paper: no Susquehanna-style market maker in the book")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--exit", default="0.7,0.0,24", help="exit policy start_capture,end_capture,glide_hours (backtest best: 0.7,0.0,24)")
    ap.add_argument("--noise", type=float, default=20.0, help="paper: student market orders per hour per market")
    ap.add_argument("--fv-lag", type=float, default=15.0, help="paper: seconds our Kalshi FV lags the truth")
    ap.add_argument("--entry", choices=["fv", "model", "both", "none"], help="entry strategy (default: params file, else fv)")
    ap.add_argument("--tag", default="", help="shadow: log name suffix, e.g. 'fresh' -> data/mm_logs/shadowfresh_*.jsonl")
    ap.add_argument("--mm", action="store_true", help="live/dry: also quote two-sided markets (not shadow-tested)")
    a = ap.parse_args()
    if not a.paper:
        keep_awake()
    P = Params.load(a.params)
    if a.entry:
        P.entry_mode = a.entry

    if a.paper:
        return run_paper(P, a.hours, house=not a.no_house, seed=a.seed, noise=a.noise, fv_lag=a.fv_lag)

    from mm.fv import KalshiFV
    from sm_client import SuperMarket
    client = SuperMarket(reads_per_min=60, writes_per_min=29)  # the collector uses ~25 reads/min of the same 100
    t = client.use_tournament()
    print(f"Tournament: {t['name']} | members {t.get('memberCount')} | balance {t.get('myBalance')}")
    fv = KalshiFV(P)
    if a.select:
        return select(client, fv, P, a.top)

    uni = {u["market_id"]: u for u in contest_universe(client, fv)}
    if a.shadow:
        return run_shadow(P, fv, uni, a.exit, a.tag)
    run_live(P, fv, uni, client, a)


def run_live(P, fv, uni, client, a):
    """Same strategy as the shadow (takes + exits everywhere a fair value exists), real orders unless --dry."""
    from mm.exits import ExitParams
    from mm.livefeed import LiveFeedClient
    quote_ids = set()
    if a.mm:
        if a.markets:
            quote_ids = {x.strip() for x in a.markets.split(",")}
        elif SEL.exists():
            quote_ids = {r["market_id"] for r in json.loads(SEL.read_text(encoding="utf-8"))}
    markets = [dict(u, quote=u["market_id"] in quote_ids) for u in uni.values()]
    fv.tracked = {(m["race"], m["party"]) for m in markets}
    feed = LiveFeedClient(client)
    age = feed.feed_age()
    if age > feed.max_feed_age:
        raise SystemExit(f"Collector feed is {age:.0f}s old: start collector.py first.")
    X = parse_exit(a.exit)
    P.cycle_s = max(P.cycle_s, 6.0)
    mode = "DRY (nothing sent)" if a.dry else "LIVE"
    print(f"{mode}: {len(markets)} contracts (takes + exits), market making in {sum(m['quote'] for m in markets)}. "
          f"Market data: collector feed ({age:.0f}s old).")
    print(f"Exit policy: {describe_exit(X)}. Entries: {P.entry_mode}"
          f"{f' (model >= {P.model_thr*100:.1f}c, hold {P.model_hold_h:g} h)' if P.entry_mode in ('model', 'both') else ''}"
          f"{f' (gap >= {P.take_edge}, {P.take_min_fv} <= FV <= {P.take_max_fv})' if P.entry_mode in ('fv', 'both') else ''}; "
          f"max {P.take_max_notional:,.0f}/take, {P.max_risk_per_race:,.0f}/race, "
          f"{P.max_total_risk:,.0f} total; stop after {P.max_daily_loss:,.0f} loss")
    if P.big_edge:
        late = (f"; when a full {X.time_stop_h:g} h hold no longer fits before liquidation, big-tier entries only, until "
                f"{P.late_min_h:g} h before it" if P.late_min_h and X.time_stop_h else "")
        print(f"Size tiers: gaps >= {P.big_edge * 100:.0f}c (+{P.lean_extra * 100:.0f}c against {P.lean_party or '-'}) get "
              f"{P.big_take_notional:,.0f}/take and {P.big_risk_per_race:,.0f}/race{late}.")
    print(describe_election(P))
    print("Calibrating volatility from Kalshi history...")
    fv.calibrate(days=3)
    fv.poll()
    lots = None if a.dry else ROOT / "data" / "live_lots.json"
    from mm.engine import BOOK_FILE
    eng = MMEngine(feed, fv, P, markets, dry=a.dry, exit_params=X, books_per_cycle=10_000, lots_path=lots,
                   prime_fills=True, book_path=BOOK_FILE, stop_name="STOP_LIVE",
                   log_name=f"{'dry' if a.dry else 'live'}_{dt.datetime.now():%Y%m%d_%H%M%S}.jsonl")
    if eng.lots:
        print(f"Resumed {sum(len(v) for v in eng.lots.values())} exit lots from {lots}")
    if not a.dry:
        # orders left by a previous run could fill unseen (fills don't say buy/sell): start from a clean book
        print("Cancelling any open orders left from a previous run...", client.cancel_all())
    print(f"Logging to {eng.log_path}. Ctrl+C or create data/STOP to stop (cancels all resting orders).")
    eng.run()


if __name__ == "__main__":
    main()
