"""Paper exchange + simulated fair values, with the same interface as the live client.

It is a test harness and an intuition pump, NOT a forecast: the flow assumptions below are guesses until
the live logs (data/mm_logs) tell us the real fill rates and markouts.

World per contract:
  * true FV: random walk in logit space with the contract's hourly sigma, plus Poisson news jumps
  * "house" market maker (Susquehanna-like): quotes FV from `house_lag_s` ago +- house_half, size house_size
  * noise traders (students): Poisson market orders, random side, lognormal size
  * arbitrage bots: take any resting price more than arb_edge through the true FV
"""
from __future__ import annotations

import math
import random
from collections import deque
from dataclasses import dataclass

from mm.fv import FVState


@dataclass
class WorldParams:
    noise_per_hour: float = 20.0
    noise_median_shares: float = 200.0
    house: bool = True
    house_half: float = 0.0025       # 1 tick wide market, like the featured markets
    house_size: float = 3000.0
    house_lag_s: float = 60.0
    house_refresh_s: float = 15.0
    arb_edge: float = 0.012
    arb_size: float = 800.0
    jumps_per_day: float = 1.0
    jump_sd: float = 0.04
    substep_s: float = 2.0


def _logit(p):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def _sig(x):
    return 1 / (1 + math.exp(-x))


class PaperWorld:
    def __init__(self, contracts: list[dict], wp: WorldParams, seed: int = 1, t0: float = 1_791_000_000.0):
        self.rng, self.wp, self.t = random.Random(seed), wp, t0
        self.c = {c["exchange_id"]: dict(c) for c in contracts}
        self.races: dict[tuple, dict] = {}
        for c in contracts:
            r = self.races.setdefault(c["race"], {"x": _logit(c["fv0"] if c["party"] != "R" else 1 - c["fv0"]),
                                                   "sigma": c["sigma_hourly"], "hist": deque(maxlen=4000)})
        self.book: dict[str, list[dict]] = {ex: [] for ex in self.c}
        self.next_id = 1
        self.pos: dict[str, float] = {ex: 0.0 for ex in self.c}
        self.cash = 100_000.0
        self.my_fills: list[dict] = []
        self.last_px: dict[str, float] = {}
        self.house_t = -1e9
        self.stats = {"noise_trades": 0, "arb_trades": 0, "my_passive_fills": 0, "my_take_fills": 0}
        for race in self.races.values():
            race["hist"].append((self.t, _sig(race["x"])))

    # ---- fair values ----
    def fv(self, ex: str, t: float | None = None) -> float:
        c = self.c[ex]
        race = self.races[c["race"]]
        if t is None:
            pd = _sig(race["x"])
        else:
            pd = race["hist"][0][1]
            for ts, v in race["hist"]:
                if ts <= t:
                    pd = v
                else:
                    break
        return pd if c["party"] != "R" else 1 - pd

    # ---- matching ----
    def _rest(self, ex, owner, side, price, qty, exp=None):
        o = {"id": self.next_id, "owner": owner, "side": side, "price": round(price, 4), "qty": qty, "t": self.t, "exp": exp}
        self.next_id += 1
        self.book[ex].append(o)
        return o

    def _match(self, ex, taker_owner, side, qty, limit=None, taker_order_id=None):
        """Taker buys (side='bid') from asks or sells (side='ask') into bids. Returns (filled, cost, last_px)."""
        opp = "ask" if side == "bid" else "bid"
        levels = sorted([o for o in self.book[ex] if o["side"] == opp and o["owner"] != taker_owner],
                        key=lambda o: (o["price"] if opp == "ask" else -o["price"], o["t"]))
        filled = cost = 0.0
        last = None
        for o in levels:
            if qty <= 0:
                break
            if limit is not None and ((side == "bid" and o["price"] > limit + 1e-9) or (side == "ask" and o["price"] < limit - 1e-9)):
                break
            q = min(qty, o["qty"])
            o["qty"] -= q
            qty -= q
            filled += q
            cost += q * o["price"]
            last = o["price"]
            self.last_px[ex] = o["price"]
            if o["owner"] == "me":
                self._my_fill(ex, o["id"], +1 if o["side"] == "bid" else -1, o["price"], q)
                self.stats["my_passive_fills"] += 1
            if taker_owner == "me":
                self._my_fill(ex, taker_order_id, +1 if side == "bid" else -1, o["price"], q)
                self.stats["my_take_fills"] += 1
        self.book[ex] = [o for o in self.book[ex] if o["qty"] > 1e-9]
        return filled, cost, last

    def _my_fill(self, ex, oid, d, p, q):
        x = self.pos[ex]
        if d > 0:
            n = min(q, max(0.0, -x))
            self.cash += (1 - p) * n - p * (q - n)
        else:
            n = min(q, max(0.0, x))
            self.cash += p * n - (1 - p) * (q - n)
        self.pos[ex] = x + d * q
        self.my_fills.append({"id": len(self.my_fills) + 1, "orderId": oid, "exchangeId": ex, "marketId": ex,
                              "price": p if d > 0 else round(1 - p, 4), "side": "yes" if d > 0 else "no",
                              "quantity": q if d > 0 else -q, "filledAt": self.t, "fv": self.fv(ex)})

    # ---- simulation ----
    def advance(self, seconds: float):
        wp, end = self.wp, self.t + seconds
        while self.t < end - 1e-9:
            dt = min(wp.substep_s, end - self.t)
            self.t += dt
            for race in self.races.values():
                p = _sig(race["x"])
                sig_x = race["sigma"] / max(p * (1 - p), 0.03)
                race["x"] += self.rng.gauss(0, sig_x * math.sqrt(dt / 3600))
                if self.rng.random() < wp.jumps_per_day * dt / 86400:
                    race["x"] = _logit(min(0.99, max(0.01, p + self.rng.gauss(0, wp.jump_sd))))
                race["hist"].append((self.t, _sig(race["x"])))
            for ex in self.book:
                self.book[ex] = [o for o in self.book[ex] if not (o["exp"] and o["exp"] <= self.t)]
            if wp.house and self.t - self.house_t >= wp.house_refresh_s:
                self.house_t = self.t
                for ex in self.book:
                    self.book[ex] = [o for o in self.book[ex] if o["owner"] != "house"]
                    f = self.fv(ex, self.t - wp.house_lag_s)
                    b = math.floor((f - wp.house_half) / 0.005 + 1e-9) * 0.005
                    a = math.ceil((f + wp.house_half) / 0.005 - 1e-9) * 0.005
                    if 0.005 <= b < a <= 0.995:
                        self._rest(ex, "house", "bid", b, wp.house_size)
                        self._rest(ex, "house", "ask", a, wp.house_size)
            for ex in self.book:
                n = self._poisson(wp.noise_per_hour * dt / 3600)
                for _ in range(n):
                    q = round(math.exp(math.log(wp.noise_median_shares) + self.rng.gauss(0, 1.0)))
                    self._match(ex, "noise", "bid" if self.rng.random() < 0.5 else "ask", q)
                    self.stats["noise_trades"] += 1
                f = self.fv(ex)
                f_, _, _ = self._match(ex, "arb", "bid", wp.arb_size, limit=f - wp.arb_edge)
                g_, _, _ = self._match(ex, "arb", "ask", wp.arb_size, limit=f + wp.arb_edge)
                self.stats["arb_trades"] += (f_ > 0) + (g_ > 0)

    def _poisson(self, lam):
        n, p, L = 0, 1.0, math.exp(-lam)
        while True:
            p *= self.rng.random()
            if p <= L:
                return n
            n += 1

    def value(self) -> float:
        v = self.cash
        for ex, q in self.pos.items():
            f = self.fv(ex)
            v += q * f if q > 0 else -q * (1 - f)
        return v


class PaperExchange:
    """Implements the subset of SuperMarket the engine uses."""

    def __init__(self, world: PaperWorld):
        self.w = world

    def use_tournament(self, slug=None):
        return {"name": "PAPER", "myBalance": self.w.cash, "memberCount": 0}

    def tournament_info(self):
        return {"myBalance": self.w.cash}

    def prices(self, ids):
        out = {}
        for ex in ids:
            bids = [o["price"] for o in self.w.book[ex] if o["side"] == "bid"]
            asks = [o["price"] for o in self.w.book[ex] if o["side"] == "ask"]
            out[ex] = {"exchangeId": ex, "bestBid": max(bids) if bids else None, "bestAsk": min(asks) if asks else None,
                       "latestPrice": self.w.last_px.get(ex)}
        return out

    def orderbook(self, ex, depth=15):
        agg = {"bid": {}, "ask": {}}
        for o in self.w.book[ex]:
            agg[o["side"]][o["price"]] = agg[o["side"]].get(o["price"], 0) + o["qty"]
        bids = [{"price": p, "quantity": q} for p, q in sorted(agg["bid"].items(), reverse=True)][:depth]
        asks = [{"price": p, "quantity": q} for p, q in sorted(agg["ask"].items())][:depth]
        return {"bids": bids, "asks": asks}

    def open_orders(self):
        data = []
        for ex, orders in self.w.book.items():
            for o in orders:
                if o["owner"] != "me":
                    continue
                if o["side"] == "bid":
                    data.append({"id": o["id"], "exchangeId": ex, "side": "yes", "action": "buy", "priceLimit": o["price"], "quantity": o["qty"], "open": True})
                else:
                    data.append({"id": o["id"], "exchangeId": ex, "side": "no", "action": "buy", "priceLimit": round(1 - o["price"], 4), "quantity": o["qty"], "open": True})
        return {"data": data}

    def positions(self):
        return {"positions": [{"exchangeId": ex, "quantity": q, "settled": False} for ex, q in self.w.pos.items() if abs(q) > 1e-9]}

    def fills(self, limit=50):
        return {"data": list(reversed(self.w.my_fills[-limit:]))}

    def place(self, ex, side, action, quantity, price, idempotency_key=None, expiration=None):
        yes_side = "bid" if (side == "yes") == (action == "buy") else "ask"
        px = price if side == "yes" else round(1 - price, 4)
        oid = self.w.next_id
        self.w.next_id += 1
        filled, cost, last = self.w._match(ex, "me", yes_side, quantity, limit=px, taker_order_id=oid)
        rem = quantity - filled
        exp_t = None
        if expiration:
            import datetime as _dt
            exp_t = _dt.datetime.strptime(expiration, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc).timestamp()
        if rem > 0:
            o = self.w._rest(ex, "me", yes_side, px, rem, exp_t)
            o["id"] = oid
        return {"orderId": oid, "open": rem > 0, "remainingQuantity": rem, "quantityTraded": filled,
                "totalCost": cost, "fillPrice": last}

    def place_multi_leg(self, legs, idempotency_key=None, relationship_id=None):
        return {"results": [{"index": i, "data": self.place(l["exchange_id"], l["side"], l["action"], l["quantity"], l.get("price"),
                                                            expiration=l.get("expiration"))}
                            for i, l in enumerate(legs)]}

    def cancel(self, order_id):
        for ex in self.w.book:
            self.w.book[ex] = [o for o in self.w.book[ex] if not (o["owner"] == "me" and o["id"] == order_id)]
        return {"ok": True}

    def cancel_all(self, exchange_id=None, market_id=None):
        n = 0
        for ex in self.w.book:
            if exchange_id and ex != exchange_id:
                continue
            before = len(self.w.book[ex])
            self.w.book[ex] = [o for o in self.w.book[ex] if o["owner"] != "me"]
            n += before - len(self.w.book[ex])
        return {"cancelled": n}


class PaperFV:
    """Fair value as Kalshi would show it: the true FV plus a little noise, a 1-tick reference spread."""

    def __init__(self, world: PaperWorld, params, markets: list[dict], lag_s: float = 15.0):
        self.w, self.p, self.lag = world, params, lag_s
        self.ex_of = {(m["race"], m["party"]): m["exchange_id"] for m in markets}
        self.obs: dict[tuple, deque] = {k: deque(maxlen=500) for k in self.ex_of}

    def track(self, keys):
        pass

    def has(self, race, party):
        return (race, party) in self.ex_of

    def poll(self):
        for k, ex in self.ex_of.items():
            # what Kalshi + our polling delay show: the true FV from `lag` seconds ago
            self.obs[k].append((self.w.t, self.w.fv(ex, self.w.t - self.lag) + self.w.rng.gauss(0, 0.001)))

    def get(self, race, party):
        h = self.obs.get((race, party))
        if not h:
            return None
        now, f = h[-1]
        recent = [v for t, v in h if now - t <= self.p.jump_window_s]
        c = self.w.c[self.ex_of[(race, party)]]
        return FVState(fv=f, ref_half_spread=0.0025, age_s=0.0, sigma_hourly=c["sigma_hourly"],
                       jumping=bool(recent) and max(recent) - min(recent) >= self.p.jump_threshold, ref_spread_ok=True)

    def fv_at(self, race, party, t):
        return self.w.fv(self.ex_of[(race, party)], t)
