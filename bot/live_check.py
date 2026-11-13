"""First live step: verify the API conventions the engine relies on with ONE-SHARE orders (costs about one tick).

    py live_check.py            # PLACES REAL ORDERS (1 share each); only run when trading is approved
    py live_check.py --plan     # read-only: show the market and prices it would use, place nothing

Checks, on one liquid competitive contract (default: Democratic Party wins the U.S. Senate):
  1. resting buy YES 1 @ bid-5c   -> open, nothing traded; open-orders shows it as ('bid', price) in YES terms
  2. resting sell YES 1 @ ask+5c  -> open; canonicalised (buy NO @ 1-p) and mapped back to ('ask', price)
  3. cancel one order by id, the other with the tournament-wide cancel-all (the engine's shutdown path)
  4. expiration: a resting order with a 20 s expiry is gone after 35 s (the engine's dead-man switch)
  5-8. two marketable round trips, 1 share each: buy YES then sell it (long), sell YES (= buy NO) then buy YES (short).
     Each fill is found by order id (fills lag a few seconds), its YES price is no worse than the book, and the
     position moves +1, 0, -1, 0. Fills do NOT say buy or sell (closing a long YES came back side=yes, quantity=+1).
  9. balance moved by about two spreads only
Writes data/live_check_<time>.json. Exit code 0 only if every check passed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
import uuid
from pathlib import Path

from mm.engine import yes_terms
from sm_client import SuperMarket

ROOT = Path(__file__).resolve().parent.parent
TICK = 0.005
DEFAULT_TITLE = "Will the Democratic Party win the U.S. Senate?"


def exp_in(secs: int) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=secs)).strftime("%Y-%m-%dT%H:%M:%SZ")


def snap(p: float) -> float:
    return round(min(1 - TICK, max(TICK, round(p / TICK) * TICK)), 4)


def yes_price(f: dict) -> float:
    """Same as MMEngine.refresh_fills: a fill's price is in its own outcome; convert to YES terms."""
    px = float(f.get("price"))
    return round(px, 4) if f.get("side", "yes").lower() == "yes" else round(1 - px, 4)


def run(title: str = DEFAULT_TITLE, plan_only: bool = False) -> bool:
    c = SuperMarket(reads_per_min=40, writes_per_min=10)
    t = c.use_tournament()
    bal0 = float(t.get("myBalance") or 0)
    m = next((m for m in c.markets() if m["title"] == title), None)
    if not m or len(m.get("exchanges") or []) != 1:
        raise SystemExit(f"market not found: {title}")
    ex = str(m["exchanges"][0]["id"])
    book = c.orderbook(ex, depth=5)
    bb, ba = float(book["bids"][0]["price"]), float(book["asks"][0]["price"])
    print(f"{title} (exchange {ex}): book {bb}/{ba}, balance {bal0:,.2f}")
    if plan_only:
        print(f"Would rest buy 1 @ {snap(bb - 0.05)}, sell 1 @ {snap(ba + 0.05)}, test expiry, then round trips: buy 1 @ ask + sell 1 @ bid (long), sell 1 @ bid + buy 1 @ ask (short).")
        return True

    report = {"started": dt.datetime.now().isoformat(), "exchange_id": ex, "title": title, "book": [bb, ba],
              "balance_before": bal0, "checks": []}
    ok_all = True

    def check(name: str, ok: bool, **detail):
        nonlocal ok_all
        ok_all &= bool(ok)
        report["checks"].append({"check": name, "ok": bool(ok), **detail})
        print(f"  [{'PASS' if ok else 'FAIL'}] {name} {json.dumps(detail, default=str)[:300]}")

    def mine(oid):
        return next((o for o in (c.open_orders().get("data") or []) if o["id"] == oid), None)

    def fills_for(order_id, wait_s=20):
        """Fills appear a few seconds after the trade (live check Oct 11): poll by order id."""
        t_end = time.time() + wait_s
        while order_id is not None and time.time() < t_end:
            fs = [f for f in (c.fills(limit=20).get("data") or []) if f.get("orderId") == order_id]
            if fs:
                return fs
            time.sleep(2)
        return []

    def place(action, px, secs):
        return c.place(ex, "yes", action, 1, px, idempotency_key=str(uuid.uuid4()), expiration=exp_in(secs))

    try:
        # 1-2 resting orders and their canonical form
        pb, ps = snap(bb - 0.05), snap(ba + 0.05)
        r1 = place("buy", pb, 300)
        check("1 resting buy accepted, nothing traded", r1.get("orderId") is not None and r1.get("open") and
              not float(r1.get("quantityTraded") or 0), res=r1)
        o1 = mine(r1.get("orderId"))
        check("1 open order reads back as ('bid', price)", o1 is not None and yes_terms(o1) == ("bid", pb), order=o1)
        r2 = place("sell", ps, 300)
        check("2 resting sell accepted, nothing traded", r2.get("orderId") is not None and r2.get("open") and
              not float(r2.get("quantityTraded") or 0), res=r2)
        o2 = mine(r2.get("orderId"))
        check("2 open order reads back as ('ask', price)", o2 is not None and yes_terms(o2) == ("ask", ps), order=o2)
        # 3 cancels
        c.cancel(r1["orderId"])
        check("3a cancel by id removes the order", mine(r1["orderId"]) is None)
        c.cancel_all()
        check("3b tournament-wide cancel-all removes the other", mine(r2["orderId"]) is None)
        # 4 expiry
        r4 = place("buy", pb, 20)
        time.sleep(35)
        check("4 order with a 20 s expiry is gone after 35 s", r4.get("orderId") is not None and mine(r4["orderId"]) is None, res=r4)
        # 5-8 two round trips: long YES (buy, then sell to close) and short (sell YES = buy NO, then buy YES to close).
        # The engine takes direction from its own order record; from a fill it only needs the YES price.
        def round_leg(label, action, want_pos):
            book = c.orderbook(ex, depth=5)
            b, a_ = float(book["bids"][0]["price"]), float(book["asks"][0]["price"])
            px = a_ if action == "buy" else b
            r = place(action, px, 30)
            traded = abs(float(r.get("quantityTraded") or 0))
            if not traded and r.get("orderId") is not None:
                c.cancel(r["orderId"])
            check(f"{label} marketable {action} traded 1", traded == 1, res=r)
            if traded != 1:      # book moved away: stop here rather than run the next legs on a wrong position
                raise RuntimeError(f"{label} did not trade; remaining legs skipped")
            fs = fills_for(r.get("orderId"))
            ok_px = bool(fs) and all((yes_price(f) <= a_ + 1e-9) if action == "buy" else (yes_price(f) >= b - 1e-9) for f in fs)
            check(f"{label} fill found by order id; YES price {'<= ask' if action == 'buy' else '>= bid'} ({b}/{a_})", ok_px,
                  fills=fs, yes_prices=[yes_price(f) for f in fs])
            pos = {str(p["exchangeId"]): float(p["quantity"]) for p in (c.positions().get("positions") or []) if not p.get("settled")}
            check(f"{label} position is {want_pos:+d}", abs(pos.get(ex, 0.0) - want_pos) < 1e-9, position=pos.get(ex))

        round_leg("5 open long:", "buy", +1)
        round_leg("6 close long:", "sell", 0)
        round_leg("7 open short (sell YES = buy NO):", "sell", -1)
        round_leg("8 close short:", "buy", 0)
        bal1 = float(c.tournament_info().get("myBalance") or 0)
        check("9 balance moved by about two spreads only", -0.05 <= bal1 - bal0 <= 0.0001, before=bal0, after=bal1)
        report["balance_after"] = bal1
    except Exception as e:
        check("no API errors", False, error=str(e)[:500])
        try:
            c.cancel_all()
        except Exception:
            pass
    report["passed"] = ok_all
    out = ROOT / "data" / f"live_check_{dt.datetime.now():%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(f"{'ALL CHECKS PASSED' if ok_all else 'CHECKS FAILED'} -> {out}")
    return ok_all


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true", help="read-only dry plan")
    ap.add_argument("--title", default=DEFAULT_TITLE)
    a = ap.parse_args()
    sys.exit(0 if run(a.title, a.plan) else 1)


if __name__ == "__main__":
    main()
