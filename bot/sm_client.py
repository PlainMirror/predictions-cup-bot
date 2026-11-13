"""Client for The Super Market API (the Predictions Cup platform), built from its OpenAPI spec.

Key facts baked in (from the platform's API documentation, not included here):
  * Auth: "Authorization: Bearer <key>". Create the key yourself on the site:
    My Profile -> API Keys, scopes read (+ trade to place orders). Put it in env var SM_API_KEY.
  * Budget per ACCOUNT: 100 reads + 30 writes per minute. A batch / multi-leg order is ONE write.
  * Prices are YES-normalised in [0, 1]. Limit prices must sit on the 0.005 tick in [0.005, 0.995].
  * `sell yes q @ p` is canonicalised to `buy no q @ (1 - p)`.
  * Resting orders do NOT reserve cash; cash moves only on fills.
"""
from __future__ import annotations

import json
import os
import time
import uuid

from common import HttpError, http_json, qs

DEFAULT_BASE = "https://sig.thesuper.market/api/v1"


def _user_env(name: str) -> str | None:
    """Windows user environment (set with `setx`), for processes started before the variable existed."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return winreg.QueryValueEx(k, name)[0] or None
    except (ImportError, OSError):
        return None


class _Budget:
    """Client-side pacing so we never trip the per-account limits."""

    def __init__(self, per_minute: int):
        self.gap = 60.0 / per_minute
        self.next_ok = 0.0

    def wait(self):
        now = time.monotonic()
        if now < self.next_ok:
            time.sleep(self.next_ok - now)
        self.next_ok = max(now, self.next_ok) + self.gap


class SuperMarket:
    def __init__(self, api_key: str | None = None, base: str | None = None,
                 reads_per_min: int = 90, writes_per_min: int = 27):
        self.key = api_key or os.environ.get("SM_API_KEY") or _user_env("SM_API_KEY")
        if not self.key:
            raise SystemExit("Set SM_API_KEY (create it on the site: My Profile -> API Keys).")
        self.base = (base or os.environ.get("SM_BASE_URL") or DEFAULT_BASE).rstrip("/")
        self._reads, self._writes = _Budget(reads_per_min), _Budget(writes_per_min)
        self.tournament_id: str | None = None
        self.slug: str | None = None

    # ---- plumbing -------------------------------------------------------------------------
    def _get(self, path: str, **params):
        self._reads.wait()
        return http_json("GET", self.base + path + qs(params), headers={"Authorization": f"Bearer {self.key}"})

    def _write(self, method: str, path: str, body: dict | None = None):
        self._writes.wait()
        return http_json(method, self.base + path, headers={"Authorization": f"Bearer {self.key}"}, body=body)

    # ---- account / tournament ---------------------------------------------------------------
    def account(self):
        return self._get("/account")

    def tournaments(self):
        return self._get("/tournaments", limit=50)["data"]

    def use_tournament(self, slug: str | None = None) -> dict:
        """Pick the Midterms tournament (or the given slug) and remember its UUID for every call."""
        if not slug:
            ts = self.tournaments()
            mids = [t for t in ts if "midterm" in (t["name"] + t["slug"]).lower()] or ts
            if not mids:
                raise SystemExit("No tournaments visible to this key. Make one trade on the site first, or check the key.")
            slug = mids[0]["slug"]
        t = self._get(f"/tournaments/{slug}")
        self.slug, self.tournament_id = t["slug"], t["id"]
        return t

    # ---- market data ------------------------------------------------------------------------
    def markets(self, status: str | None = "open") -> list[dict]:
        out, cursor = [], None
        while True:
            d = self._get(f"/tournaments/{self.slug}/markets", limit=100, cursor=cursor, status=status)
            out += d["data"]
            cursor = d["pagination"].get("nextCursor")
            if not d["pagination"].get("hasMore") or not cursor:
                return out

    def prices(self, exchange_ids: list[str]) -> dict[str, dict]:
        """exchangeId -> {latestPrice, bestBid, bestAsk, spread} for up to N exchanges (100 per call)."""
        out = {}
        for i in range(0, len(exchange_ids), 100):
            d = self._get("/exchanges/prices", ids=",".join(exchange_ids[i:i + 100]), tournamentId=self.tournament_id)
            for row in d["data"]:
                out[row["exchangeId"]] = row
        return out

    def orderbook(self, exchange_id: str, depth: int = 20) -> dict:
        return self._get(f"/exchanges/{exchange_id}/orderbook", depth=depth, tournamentId=self.tournament_id)

    def trades(self, exchange_id: str, limit: int = 50) -> dict:
        return self._get(f"/exchanges/{exchange_id}/trades", limit=limit, tournamentId=self.tournament_id)

    def constraints(self, violations_only: bool = True, min_violation: float | None = None) -> dict:
        """Engine-evaluated cross-market price constraints (the platform's own arbitrage detector)."""
        return self._get("/relationships/constraints", violationsOnly=str(violations_only).lower(),
                         minViolation=min_violation, tournamentId=self.tournament_id)

    def relationships(self, market_id: str | None = None) -> list[dict]:
        out, cursor = [], None
        while True:
            d = self._get("/relationships", marketId=market_id, limit=200, cursor=cursor, tournamentId=self.tournament_id)
            out += d.get("data", [])
            cursor = (d.get("pagination") or {}).get("nextCursor")
            if not cursor:
                return out

    # ---- portfolio --------------------------------------------------------------------------
    def positions(self) -> dict:
        return self._get(f"/tournaments/{self.slug}/portfolio/positions")

    def pnl(self, period: str = "all") -> dict:
        return self._get(f"/tournaments/{self.slug}/portfolio/pnl", period=period)

    def collateral(self) -> dict:
        return self._get("/portfolio/collateral", tournamentId=self.tournament_id)

    def open_orders(self) -> dict:
        """All open orders in the tournament (paginates past 200 when quoting many markets)."""
        data, cursor = [], None
        while True:
            d = self._get("/orders", status="open", tournamentId=self.tournament_id, limit=200, cursor=cursor)
            data += d.get("data") or []
            cursor = (d.get("pagination") or {}).get("nextCursor")
            if not (d.get("pagination") or {}).get("hasMore") or not cursor:
                return {"data": data}

    def fills(self, limit: int = 50) -> dict:
        """Newest first. price is per-share in the traded outcome; quantity is negative for NO."""
        return self._get(f"/tournaments/{self.slug}/portfolio/fills", limit=limit)

    def tournament_info(self) -> dict:
        return self._get(f"/tournaments/{self.slug}")

    def leaderboard(self, period: str = "all", limit: int = 100, offset: int = 0, sort: str = "pnl") -> dict:
        return self._get(f"/tournaments/{self.slug}/leaderboard", period=period, limit=limit, offset=offset, sort=sort)

    # ---- trading (needs the `trade` scope) ----------------------------------------------------
    def _order_body(self, exchange_id: str, side: str, action: str, quantity: int, price: float | None,
                    expiration: str | None = None) -> dict:
        body = {"exchangeId": str(exchange_id), "side": side, "action": action, "quantity": int(quantity),
                "tournamentId": self.tournament_id}
        if price is not None:
            body["price"] = round(price, 3)
        if expiration:
            body["expirationDate"] = expiration
        return body

    def place(self, exchange_id: str, side: str, action: str, quantity: int, price: float | None,
              idempotency_key: str | None = None, expiration: str | None = None) -> dict:
        body = self._order_body(exchange_id, side, action, quantity, price, expiration)
        body["idempotencyKey"] = idempotency_key or str(uuid.uuid4())
        return self._write("POST", "/orders", body)

    def place_multi_leg(self, legs: list[dict], idempotency_key: str | None = None,
                        relationship_id: str | None = None) -> dict:
        """All-or-nothing (up to 10 legs). Each leg: dict(exchange_id, side, action, quantity, price)."""
        body = {"idempotencyKey": idempotency_key or str(uuid.uuid4()),
                "legs": [self._order_body(l["exchange_id"], l["side"], l["action"], l["quantity"], l.get("price"),
                                          l.get("expiration")) for l in legs]}
        if relationship_id:
            body["relationshipConstraint"] = relationship_id
        return self._write("POST", "/orders/multi-leg", body)

    def place_batch(self, orders: list[dict], idempotency_key: str | None = None, retries: int = 0) -> dict:
        """Up to 50 INDEPENDENT orders in ONE write (POST /orders/batch; partial success = HTTP 207). Each order:
        dict(exchange_id, side, action, quantity, price, expiration). Returns {"results": [{"index", "ok", "status",
        "data"}]}, data = the single-order response. No retries by default: election takes expire in seconds, so a
        429 must not stall the engine. A 422/429/502 that still carries per-order results is returned (not raised),
        with "http_status" and "retry_after" added."""
        body = {"idempotencyKey": idempotency_key or str(uuid.uuid4()),
                "orders": [self._order_body(o["exchange_id"], o["side"], o["action"], o["quantity"], o.get("price"),
                                            o.get("expiration")) for o in orders]}
        self._writes.wait()
        try:
            return http_json("POST", self.base + "/orders/batch", headers={"Authorization": f"Bearer {self.key}"},
                             body=body, retries=retries)
        except HttpError as e:
            try:
                j = json.loads(e.body)
            except ValueError:
                raise e
            if isinstance(j, dict) and isinstance(j.get("results"), list):
                j["http_status"], j["retry_after"] = e.status, e.headers.get("Retry-After")
                return j
            raise

    def cancel(self, order_id: int) -> dict:
        return self._write("DELETE", f"/orders/{order_id}")

    def cancel_all(self, exchange_id: str | None = None, market_id: str | None = None) -> dict:
        body = {"tournamentId": self.tournament_id}
        if exchange_id:
            body["exchangeId"] = str(exchange_id)
        if market_id:
            body["marketId"] = str(market_id)
        return self._write("POST", "/orders/cancel-all", body)
