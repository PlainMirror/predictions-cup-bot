"""Live client whose MARKET DATA comes from the collector's realtime feed instead of paid API reads.

The shadow showed the strategy works on the collector's books (data/market_data.sqlite, updated within seconds over the
realtime channel). The plain live client could only afford ~2 order-book reads per cycle, so takes across 200 markets
would rarely fire. This wrapper gives the live engine the same data the shadow used:
  * prices() / orderbook(): latest stored book per exchange (top 8 levels; includes our own resting orders, which the
    engine subtracts itself).
  * After we send an order on an exchange, opposite-side levels at or through its limit are hidden until the collector
    stores a newer book, so we never fire twice at liquidity we may already have taken.
  * If the collector has stored no book for `max_feed_age` seconds the feed is treated as dead: every market-data read
    raises, the engine skips the cycle, and resting orders expire on their own (15 min TTL).
Everything else (orders, cancels, positions, fills, balance) goes to the real API client unchanged.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

DB = Path(__file__).resolve().parents[2] / "data" / "market_data.sqlite"


class FeedStale(RuntimeError):
    pass


class LiveFeedClient:
    def __init__(self, api, db_path: Path = DB, max_feed_age: float = 90.0, clock=time.time):
        self.api = api
        self.db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False, timeout=10)
        self.max_feed_age = max_feed_age
        self.clock = clock
        self.hide: dict[str, tuple[str, float, float]] = {}   # ex -> (side hidden 'ask'|'bid', limit YES price, sent at)
        self._age_t = -1e18
        self._age = float("inf")

    def __getattr__(self, name):          # orders, positions, fills, balance... -> real API
        return getattr(self.api, name)

    # ---- market data from the collector
    def feed_age(self) -> float:
        """Seconds since the collector stored its newest book. Newest row by rowid (indexed, instant): MAX(recv_ts) scanned
        the whole table and, called before each of 232 book reads, stretched live cycles to 40+ s (Oct 13: FV went stale,
        no exits were placed). Cached for 5 s."""
        now = self.clock()
        if now - self._age_t > 5:
            r = self.db.execute("SELECT recv_ts FROM books ORDER BY rowid DESC LIMIT 1").fetchone()
            self._age_t, self._last = now, (r[0] if r else None)
        return float("inf") if self._last is None else now - self._last

    def _check(self):
        age = self.feed_age()
        if age > self.max_feed_age:
            raise FeedStale(f"collector feed stale: newest book {age:.0f}s old (is collector.py running?)")

    def _book(self, ex: str):
        row = self.db.execute("SELECT bids, asks, recv_ts FROM books WHERE exchange_id=? ORDER BY recv_ts DESC LIMIT 1",
                              (ex,)).fetchone()
        if not row:
            return [], []
        bids, asks = json.loads(row[0]), json.loads(row[1])
        h = self.hide.get(ex)
        if h:
            side, lim, sent = h
            if row[2] > sent + 0.5:            # a newer book already reflects our order
                del self.hide[ex]
            elif side == "ask":
                asks = [l for l in asks if l[0] > lim + 1e-9]
            else:
                bids = [l for l in bids if l[0] < lim - 1e-9]
        return bids, asks

    def prices(self, ids):
        self._check()
        out = {}
        for ex in ids:
            bids, asks = self._book(ex)
            out[ex] = {"exchangeId": ex, "bestBid": bids[0][0] if bids else None, "bestAsk": asks[0][0] if asks else None}
        return out

    def orderbook(self, ex, depth=15):
        self._check()
        bids, asks = self._book(ex)
        return {"bids": [{"price": p, "quantity": q} for p, q in bids[:depth]],
                "asks": [{"price": p, "quantity": q} for p, q in asks[:depth]]}

    # ---- orders: pass through, remember what we may have consumed
    def _note(self, ex, side, action, price):
        if price is None:
            return
        yes_buy = (side == "yes") == (action == "buy")
        px = price if side == "yes" else round(1 - price, 4)
        self.hide[str(ex)] = ("ask" if yes_buy else "bid", px, self.clock())

    def place(self, exchange_id, side, action, quantity, price, idempotency_key=None, expiration=None):
        self._note(exchange_id, side, action, price)
        return self.api.place(exchange_id, side, action, quantity, price, idempotency_key=idempotency_key,
                              expiration=expiration)

    def place_multi_leg(self, legs, idempotency_key=None, relationship_id=None):
        for l in legs:
            self._note(l["exchange_id"], l["side"], l["action"], l.get("price"))
        return self.api.place_multi_leg(legs, idempotency_key=idempotency_key, relationship_id=relationship_id)

    def place_batch(self, orders, idempotency_key=None, retries=0):
        for o in orders:
            self._note(o["exchange_id"], o["side"], o["action"], o.get("price"))
        return self.api.place_batch(orders, idempotency_key=idempotency_key, retries=retries)
