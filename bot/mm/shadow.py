"""Shadow exchange: the real engine trades against LIVE market data with VIRTUAL orders. Nothing is ever sent.

Market data comes from the collector's SQLite (data/market_data.sqlite): latest order book per contract and every new
trade. Our orders exist only here. Fill model (conservative where it matters):
  * Taking: an order that crosses the real book fills immediately against the real levels (we assume we were first:
    optimistic, other bots may beat us; markouts will show it).
  * Resting: our virtual order joins the back of the queue at its price (queue_ahead = real size at that price when placed).
    A real trade at a price better than ours for the taker fills us first; a trade AT our price first eats queue_ahead.
    If the real book later crosses our price (someone posts through us), we fill at our price.
  * Cash/positions follow the platform's rules (buying the opposite side nets and credits 1 per pair).
The interface matches the subset of sm_client.SuperMarket the engine uses, so the engine code is identical to live.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import time
from pathlib import Path

DB = Path(__file__).resolve().parents[2] / "data" / "market_data.sqlite"


class ShadowExchange:
    def __init__(self, balance: float = 100_000.0, db_path: Path = DB, clock=time.time):
        self.db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
        self.clock = clock
        self.cash = balance
        self.pos: dict[str, float] = {}
        self.orders: dict[int, dict] = {}      # id -> {ex, side('bid'|'ask'), price, qty, ahead, exp}
        self.next_id = 1
        self.fills_log: list[dict] = []
        self.last_trade_recv = self.clock()     # only trades that happen after we start
        self.consumed: dict[tuple, tuple] = {}  # (ex, side, price) -> (qty we took, when): real book can't show it
        self.consume_ttl = 600.0

    # ------------------------------------------------------------------ market data
    def _book(self, ex: str):
        """Latest real book minus liquidity our virtual takes already consumed (kept for consume_ttl seconds)."""
        row = self.db.execute("SELECT bids, asks, recv_ts FROM books WHERE exchange_id=? ORDER BY recv_ts DESC LIMIT 1",
                              (ex,)).fetchone()
        if not row:
            return [], [], 0.0
        now = self.clock()
        out = []
        for side, levels in (("bid", json.loads(row[0])), ("ask", json.loads(row[1]))):
            kept = []
            for px, q in levels:
                c = self.consumed.get((ex, side, px))
                if c and now - c[1] < self.consume_ttl:
                    q -= c[0]
                if q > 0.5:
                    kept.append([px, q])
            out.append(kept)
        return out[0], out[1], row[2]

    def _advance(self):
        """Apply new real trades and book crossings to our resting virtual orders."""
        now = self.clock()
        for oid in [o for o, v in self.orders.items() if v["exp"] and v["exp"] <= now]:
            del self.orders[oid]
        if not self.orders:
            self.last_trade_recv = max(self.last_trade_recv, now - 1)
            return
        rows = self.db.execute("SELECT exchange_id, price, quantity, recv_ts FROM trades WHERE recv_ts > ? ORDER BY recv_ts",
                               (self.last_trade_recv,)).fetchall()
        for ex, px, qty, recv in rows:
            self.last_trade_recv = max(self.last_trade_recv, recv)
            if px is None or not qty:
                continue
            for oid, o in sorted(self.orders.items(), key=lambda kv: kv[0]):
                if o["ex"] != ex or qty <= 0:
                    continue
                if o["side"] == "ask" and px >= o["price"] - 1e-9 or o["side"] == "bid" and px <= o["price"] + 1e-9:
                    at_level = abs(px - o["price"]) < 1e-9
                    if at_level and o["ahead"] > 0:
                        eat = min(o["ahead"], qty)
                        o["ahead"] -= eat
                        qty -= eat
                    q = min(o["qty"], qty)
                    if q > 0:
                        self._fill(oid, o, q, o["price"], "passive")
                        qty -= q
        # crossings: real resting orders posted through our price trade with us at our price
        for oid, o in list(self.orders.items()):
            bids, asks, _ = self._book(o["ex"])
            if o["side"] == "bid" and asks and asks[0][0] <= o["price"] + 1e-9:
                self._fill(oid, o, min(o["qty"], asks[0][1]), o["price"], "crossed")
            elif o["side"] == "ask" and bids and bids[0][0] >= o["price"] - 1e-9:
                self._fill(oid, o, min(o["qty"], bids[0][1]), o["price"], "crossed")
        self.orders = {k: v for k, v in self.orders.items() if v["qty"] > 1e-9}

    def _fill(self, oid, o, q, p, how):
        ex, d = o["ex"], (1 if o["side"] == "bid" else -1)
        x = self.pos.get(ex, 0.0)
        if d > 0:
            n = min(q, max(0.0, -x))
            self.cash += (1 - p) * n - p * (q - n)
        else:
            n = min(q, max(0.0, x))
            self.cash += p * n - (1 - p) * (q - n)
        self.pos[ex] = x + d * q
        o["qty"] -= q
        self.fills_log.append({"id": f"s{len(self.fills_log) + 1}", "orderId": oid, "exchangeId": ex, "marketId": ex,
                               "price": p if d > 0 else round(1 - p, 4), "side": "yes" if d > 0 else "no",
                               "quantity": q if d > 0 else -q, "filledAt": self.clock(), "how": how})

    # ------------------------------------------------------------------ client interface used by the engine
    def use_tournament(self, slug=None):
        return {"name": "SHADOW (virtual orders on live data)", "myBalance": self.cash, "memberCount": None}

    def tournament_info(self):
        return {"myBalance": self.cash}

    def _with_ours(self, ex, bids, asks):
        b = {p: q for p, q in bids}
        a = {p: q for p, q in asks}
        for o in self.orders.values():
            if o["ex"] == ex:
                tgt = b if o["side"] == "bid" else a
                tgt[o["price"]] = tgt.get(o["price"], 0) + o["qty"]
        return sorted(b.items(), reverse=True), sorted(a.items())

    def prices(self, ids):
        self._advance()
        out = {}
        for ex in ids:
            bids, asks, _ = self._book(ex)
            bids, asks = self._with_ours(ex, bids, asks)
            out[ex] = {"exchangeId": ex, "bestBid": bids[0][0] if bids else None, "bestAsk": asks[0][0] if asks else None}
        return out

    def orderbook(self, ex, depth=15):
        bids, asks, _ = self._book(ex)
        bids, asks = self._with_ours(ex, bids, asks)
        return {"bids": [{"price": p, "quantity": q} for p, q in bids[:depth]],
                "asks": [{"price": p, "quantity": q} for p, q in asks[:depth]]}

    def open_orders(self):
        data = []
        for oid, o in self.orders.items():
            if o["side"] == "bid":
                data.append({"id": oid, "exchangeId": o["ex"], "side": "yes", "action": "buy", "priceLimit": o["price"], "quantity": o["qty"], "open": True})
            else:
                data.append({"id": oid, "exchangeId": o["ex"], "side": "no", "action": "buy", "priceLimit": round(1 - o["price"], 4), "quantity": o["qty"], "open": True})
        return {"data": data}

    def positions(self):
        self._advance()
        return {"positions": [{"exchangeId": ex, "quantity": q, "settled": False} for ex, q in self.pos.items() if abs(q) > 1e-9]}

    def fills(self, limit=50):
        return {"data": list(reversed(self.fills_log[-limit:]))}

    def place(self, ex, side, action, quantity, price, idempotency_key=None, expiration=None):
        yes_side = "bid" if (side == "yes") == (action == "buy") else "ask"
        px = price if side == "yes" else round(1 - price, 4)
        oid = self.next_id
        self.next_id += 1
        bids, asks, _ = self._book(ex)
        o = {"ex": ex, "side": yes_side, "price": px, "qty": float(quantity), "ahead": 0.0, "exp": None}
        traded, cost, last = 0.0, 0.0, None
        levels = asks if yes_side == "bid" else bids
        for lp, lq in levels:                      # take against the real book first
            if o["qty"] <= 0 or (yes_side == "bid" and lp > px + 1e-9) or (yes_side == "ask" and lp < px - 1e-9):
                break
            q = min(o["qty"], lq)
            key = (ex, "ask" if yes_side == "bid" else "bid", lp)
            prev = self.consumed.get(key)
            done = prev[0] if prev and self.clock() - prev[1] < self.consume_ttl else 0.0
            self.consumed[key] = (done + q, self.clock())
            self._fill(oid, o, q, lp, "take")
            traded += q
            cost += q * lp
            last = lp
        if o["qty"] > 1e-9:
            same = dict(bids if yes_side == "bid" else asks)
            o["ahead"] = float(same.get(px, 0.0))
            if expiration:
                o["exp"] = dt.datetime.strptime(expiration, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()
            self.orders[oid] = o
        return {"orderId": oid, "open": o["qty"] > 1e-9, "remainingQuantity": o["qty"], "quantityTraded": traded,
                "totalCost": cost, "fillPrice": last}

    def place_multi_leg(self, legs, idempotency_key=None, relationship_id=None):
        return {"results": [{"index": i, "data": self.place(l["exchange_id"], l["side"], l["action"], l["quantity"],
                                                             l.get("price"), expiration=l.get("expiration"))}
                            for i, l in enumerate(legs)]}

    def place_batch(self, orders, idempotency_key=None, retries=0):
        """Same response shape as the API's /orders/batch: independent orders, one result each."""
        out = []
        for i, o in enumerate(orders):
            try:
                out.append({"index": i, "ok": True, "status": 200,
                            "data": self.place(o["exchange_id"], o["side"], o["action"], o["quantity"], o.get("price"),
                                               expiration=o.get("expiration"))})
            except Exception as e:      # mirror a per-order failure instead of failing the whole batch
                out.append({"index": i, "ok": False, "status": 400, "data": {"code": "VALIDATION_ERROR", "error": str(e)}})
        return {"results": out}

    def cancel(self, order_id):
        self.orders.pop(order_id, None)
        return {"ok": True}

    def cancel_all(self, exchange_id=None, market_id=None):
        n = len(self.orders)
        self.orders = {k: v for k, v in self.orders.items() if exchange_id and v["ex"] != exchange_id}
        return {"cancelled": n - len(self.orders)}

    def value(self, fv_of) -> float:
        v = self.cash
        for ex, q in self.pos.items():
            f = fv_of(ex)
            if f is not None:
                v += q * f if q > 0 else -q * (1 - f)
        return v
