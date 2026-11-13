"""The market-making loop. Works against the live API (sm_client.SuperMarket) or the paper exchange.

Each cycle:
  1. refresh Kalshi fair values, contest top-of-book (one bulk read), our open orders / positions / fills
  2. per market: regime (events, jumps, staleness) -> target quote (quoter.make_quote) and stale-price takes
  3. spend the write budget by priority: dangerous quotes first, then takes, then requotes
  4. log everything (data/mm_logs/*.jsonl) for markout analysis
Stop it with Ctrl+C or by creating the file data/STOP: it cancels every resting order before exiting.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from mm import quoter
from mm.events import Calendar, Regime, et_to_utc
from mm.exits import ExitParams, Lot, exit_price

ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = ROOT / "data" / "mm_logs"
STOP_FILE = ROOT / "data" / "STOP"             # stops every engine (live and shadows)
BOOK_FILE = ROOT / "data" / "settlement_book.json"   # positions held to settlement by settle_book.py: not the engine's
DB_FILE = ROOT / "data" / "market_data.sqlite"


def epoch(x) -> float | None:
    """API timestamps (ISO, e.g. '2026-10-21T05:15:09.261Z') or the shadow's epoch floats -> epoch seconds."""
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    try:
        return dt.datetime.fromisoformat(str(x).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def yes_terms(order: dict) -> tuple[str, float]:
    """Canonical engine orders -> ('bid'|'ask', YES price)."""
    p = order.get("priceLimit")
    p = order.get("price") if p is None else p
    side, action = order["side"].lower(), order["action"].lower()
    if side == "yes":
        return ("bid", p) if action == "buy" else ("ask", p)
    return ("ask", round(1 - p, 4)) if action == "buy" else ("bid", round(1 - p, 4))


class MMEngine:
    def __init__(self, client, fv, P, markets: list[dict], calendar: Calendar | None = None,
                 clock=time.time, sleep=time.sleep, dry: bool = False, log_name: str | None = None, verbose: bool = True,
                 exit_params: ExitParams | None = None, books_per_cycle: int = 2, lots_path: Path | None = None,
                 prime_fills: bool = False, book_path: Path | None = None, stop_name: str | None = None):
        self.c, self.fv, self.P, self.cal = client, fv, P, calendar or Calendar()
        self.X = exit_params or ExitParams()
        self.books_per_cycle = books_per_cycle
        self.lots: dict[str, list[Lot]] = {}
        self.lots_path = lots_path        # live: lots survive restarts (else positions would lose their exit orders)
        self.prime_fills = prime_fills    # live: fills that exist at start-up are history, not new events
        self.adopt_pending = lots_path is not None   # live: adopt untracked positions at the first positions read
        self.book_path = book_path        # live/dry: settlement-book holdings are invisible to caps, exits and the loss stop
        self.book: dict[str, float] = {}
        self.book_ref: dict[str, tuple[float, float]] = {}
        self.stop_file = ROOT / "data" / stop_name if stop_name else None
        self.hist_db = None               # collector DB, for the fresh-gap rule
        self.fresh_cache: dict[tuple, bool] = {}
        if lots_path and lots_path.exists():
            try:
                saved = json.loads(lots_path.read_text(encoding="utf-8"))
            except (ValueError, UnicodeDecodeError):
                # Oct 26: the PC crashed and left this file as two NUL bytes; the next start would have died here. Set
                # it aside and start with no lots: adopt_orphans gives every real position (>= 10 sh) a fresh exit lot.
                bad = lots_path.with_name(f"{lots_path.stem}.corrupt_{dt.datetime.now():%Y%m%d_%H%M%S}.json")
                lots_path.replace(bad)
                print(f"WARNING: {lots_path.name} was unreadable (moved to {bad.name}); starting with no lots, "
                      "untracked positions will be adopted", flush=True)
                saved = []
            for d in saved:
                self.lots.setdefault(d["exchange_id"], []).append(Lot(**d))
        self.final_exit = et_to_utc(self.X.final_exit_et).timestamp()
        self.clock, self.sleep, self.dry, self.verbose = clock, sleep, dry, verbose
        self.markets = markets
        self.by_ex = {m["exchange_id"]: m for m in markets}
        self.races: dict[tuple, list[dict]] = {}
        for m in markets:
            self.races.setdefault(m["race"], []).append(m)
        self.live: dict[str, dict[str, list[dict]]] = {m["exchange_id"]: {"bid": [], "ask": []} for m in markets}
        self.pos: dict[str, float] = {}
        self.books: dict[str, dict] = {}
        self.tops: dict[str, dict] = {}
        self.registry: dict[int, dict] = {}
        self.seen_fills: set = set()
        self.tokens = 4.0
        self.last_t = clock()
        self.cycle = 0
        self.balance = None
        self.start_value = None
        self.force_orders = False
        self.dry_ids = -1
        self.cutoff = et_to_utc(P.election_night_cutoff_et)
        self.liquidate = et_to_utc(self.X.liquidate_et).timestamp()
        self.model = None
        self.model_last: dict[str, float] = {}
        self.exit_sent: dict[str, float] = {}
        self.last_fill_t: dict[str, float] = {}
        self.hold_place: dict[str, float] = {}
        self.loss_hits = 0
        self.take_tier: dict[tuple, str] = {}     # (ex, action) -> "big" | "std" of the last gap take planned
        self.pos_ok = False           # no trading until the saved lots were reconciled with the real position
        self.pos_reads = 0
        if P.entry_mode in ("model", "both"):
            from contest_model import ModelSignal
            self.model = ModelSignal(refresh_s=600, clock=clock)
        self.en = None                # election-night take mode (mm/election.py), only when the params file enables it
        if getattr(P, "election_mode", False):
            from mm.election import ElectionNight, STATE_FILE
            # live keeps data/en_state.json; a shadow running in parallel must never share it; dry runs keep nothing
            state = None if dry else STATE_FILE if lots_path is not None else STATE_FILE.with_name("en_state_shadow.json")
            self.en = ElectionNight(self, state_file=state)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        name = log_name or f"mm_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}{'_dry' if dry else ''}.jsonl"
        self.log_path = LOG_DIR / name
        self.logf = self.log_path.open("a", encoding="utf-8")
        self.log({"type": "start", "params": P.dump(), "markets": markets, "dry": dry})

    # ------------------------------------------------------------------------------------------
    def log(self, rec: dict):
        rec.setdefault("t", self.clock())
        self.logf.write(json.dumps(rec, default=str) + "\n")
        self.logf.flush()

    def say(self, msg: str):
        if self.verbose:
            print(f"{dt.datetime.fromtimestamp(self.clock()).strftime('%H:%M:%S')} {msg}", flush=True)

    # ---- reads -------------------------------------------------------------------------------
    def refresh_orders(self):
        live = {ex: {"bid": [], "ask": []} for ex in self.by_ex}
        data = self.c.open_orders().get("data") or []
        # Oct 15 live bug: a resting exit (DE Sen D, 75 @0.915) filled and dropped off the open orders before its fill was
        # readable (fills lag ~5-9 s); the engine saw "no exit order", re-placed 75 and 17 filled before the next cycle ->
        # short 17. An own resting order that vanishes before its expiry (we did not cancel it: cancels clear self.live)
        # most likely filled: place nothing more in that contract for 20 s, until the fills feed has caught up.
        open_ids = {o["id"] for o in data if o.get("open", True)}
        now, ttl = self.clock(), self.P.quote_ttl_s
        for ex, sides in self.live.items():
            for side, orders in sides.items():
                for o in orders:
                    t = self.registry.get(o["id"], {}).get("t")
                    if o["id"] in open_ids or (ttl and t is not None and now - t >= ttl - 2):
                        continue
                    if self.hold_place.get(ex, 0) < now:
                        self.log({"type": "vanished", "exchange_id": ex, "order_id": o["id"], "side": side, "qty": o["qty"]})
                    self.hold_place[ex] = now + 20
        for o in data:
            ex = str(o["exchangeId"])
            if ex not in live or not o.get("open", True):
                continue
            kind = self.registry.get(o["id"], {}).get("kind", "quote")
            if kind in ("take", "exit") or self.expiring(o["id"]):
                continue
            side, px = yes_terms(o)
            live[ex][side].append({"id": o["id"], "price": px, "qty": float(o["quantity"])})
        self.live = live

    def refresh_positions(self):
        self.pos_read_t = self.clock()        # fills that happened before this are in the read (see refresh_fills)
        d = self.c.positions()
        prev = getattr(self, "pos_prev", None)
        self.pos = {str(p["exchangeId"]): float(p["quantity"]) for p in (d.get("positions") or []) if not p.get("settled")}
        self.pos_prev = dict(self.pos)
        for p in d.get("positions") or []:
            ex = str(p["exchangeId"])
            if p.get("settled") and self.lots.get(ex):      # the market settled: its lots are done, not orphaned or lost
                self.log({"type": "settle_close", "exchange_id": ex, "lots": sum(l.d * l.qty for l in self.lots[ex]),
                          "position": {k: p.get(k) for k in ("quantity", "avgCost", "settledWith", "payout") if k in p}})
                self.lots[ex] = []
        if self.book_path and self.book_path.exists():
            try:
                self.book = {str(k): float(v) for k, v in json.loads(self.book_path.read_text(encoding="utf-8")).items()}
            except (ValueError, OSError):
                pass          # half-written file: keep the previous book
        if self.adopt_pending:          # after the book is loaded, so settlement-book shares are never adopted
            self.adopt_orphans(d.get("positions") or [])
        # lots can never exceed the engine's share of the position (oldest trimmed first, like the platform's FIFO)
        for ex, lots in self.lots.items():
            if prev is None or abs(prev.get(ex, 0.0) - self.pos.get(ex, 0.0)) > 1e-9:
                continue      # trim only on two identical reads in a row (a glitchy read once wiped a live lot, Oct 12)
            q = self.epos(ex)
            for d in (1, -1):
                held = max(0.0, d * q)
                total = sum(l.qty for l in lots if l.d == d)
                excess = total - held
                for l in [l for l in lots if l.d == d]:
                    if excess <= 1e-9:
                        break
                    cut = min(l.qty, excess)
                    l.qty -= cut
                    excess -= cut
            self.lots[ex] = [l for l in lots if l.qty > 1e-9]
        self.save_lots()
        # Oct 16 restart: data/live_lots.json said FL-09 R 719 but the position was 43 (a fill landed as the old engine
        # stopped). Lots are trimmed only on two identical reads, and before that the engine bought 2,374 and then sold
        # 719 of them as the "time-stopped" old lot. Trade only once a trim has run (or after 4 reads if they keep moving).
        self.pos_reads += 1
        if not self.pos_ok and ((prev is not None and prev == self.pos) or self.pos_reads >= 4):
            self.pos_ok = True
            self.log({"type": "positions_confirmed", "reads": self.pos_reads})

    def adopt_orphans(self, raw: list[dict]):
        """Live start-up: give every untracked position (>= 10 shares, not the settlement book) an exit lot, so nothing
        is left without an exit. Entry = the platform's average cost; clock starts now."""
        self.adopt_pending = False
        for p in raw:
            ex, q = str(p["exchangeId"]), float(p.get("quantity") or 0)
            if p.get("settled") or ex not in self.by_ex:
                continue
            q -= self.book.get(ex, 0.0)
            tracked = sum(l.d * l.qty for l in self.lots.get(ex, []))
            orphan = q - tracked
            if abs(orphan) < 10 or (tracked and (orphan > 0) != (tracked > 0)):
                continue
            d = 1 if orphan > 0 else -1
            cost = float(p.get("avgCost") or 0)
            p0 = cost if d > 0 else round(1 - cost, 4)
            s = self.fv.get(*self.key(self.by_ex[ex]))
            self.lots.setdefault(ex, []).append(Lot(ex, d, abs(orphan), p0, s.fv if s else p0, self.clock()))
            self.log({"type": "adopt", "exchange_id": ex, "qty": abs(orphan), "dir": d, "entry": p0})
            self.say(f"ADOPT untracked position {d * abs(orphan):+.0f} @ {p0} {self.by_ex[ex]['title'][:50]}")
        self.save_lots()

    def save_lots(self):
        if not self.lots_path:
            return
        tmp = self.lots_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:     # fsync: a crash must not leave NUL bytes (Oct 26)
            fh.write(json.dumps([asdict(l) for ls in self.lots.values() for l in ls], indent=0))
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(self.lots_path)

    def book_lots(self, ex, kind, yes_dir, qty, yes_px, fvs, reg: dict | None = None):
        """A filled take opens a lot (entry for the exit policy); a fill in the other direction closes lots FIFO."""
        if yes_px is None:
            return
        reg = reg or {}
        lots = self.lots.setdefault(ex, [])
        if kind == "take":
            mode, pred = reg.get("mode") or "fv", reg.get("pred")
            tp = yes_px + pred if (mode == "model" and pred is not None) else None
            # a gap lot opened when a full time-stop hold no longer fits (late window) glides and stops at the liquidation
            # (time left counted from when the take was placed)
            left = (self.liquidate - reg.get("t", self.clock())) / 3600
            hz = max(left, 0.0) if (mode == "fv" and self.X.time_stop_h and left < self.X.time_stop_h) else None
            lots.append(Lot(ex, yes_dir, qty, yes_px, fvs.fv if fvs else yes_px, self.clock(), mode=mode, tp=tp,
                            hold_h=self.P.model_hold_h if mode == "model" else None, horizon_h=hz))
            self.save_lots()
            return
        rest = qty
        for l in sorted(lots, key=lambda l: l.t0):       # oldest first (= the lots a time stop is exiting)
            if l.d == -yes_dir and rest > 0:
                cut = min(l.qty, rest)
                l.qty -= cut
                rest -= cut
                self.log({"type": "exit_fill", "exchange_id": ex, "qty": cut, "entry": l.p0, "exit": yes_px,
                          "pnl": round(cut * l.d * (yes_px - l.p0), 2), "held_h": round((self.clock() - l.t0) / 3600, 2)})
        self.lots[ex] = [l for l in lots if l.qty > 1e-9]
        self.save_lots()

    def refresh_balance(self):
        t = self.c.tournament_info()
        self.balance = float(t.get("myBalance") or 0)

    def refresh_fills(self):
        d = self.c.fills(limit=200)
        if self.prime_fills:
            self.seen_fills |= {(f["id"], f.get("orderId")) for f in (d.get("data") or [])}
            self.prime_fills = False
            self.log({"type": "fills_primed", "n": len(d.get("data") or [])})
            return
        for f in reversed(d.get("data") or []):
            key = (f["id"], f.get("orderId"))
            if key in self.seen_fills:
                continue
            self.seen_fills.add(key)
            ex = str(f["exchangeId"])
            m = self.by_ex.get(ex)
            reg = self.registry.get(f.get("orderId"), {})
            yes_dir = reg.get("yes_dir")
            if yes_dir is None:
                # Not an order this process placed. A fill does NOT say buy or sell (live check Oct 11: closing a long
                # YES came back side=yes, quantity=+1; the sign is the outcome, not the direction), so never guess:
                # log it and let refresh_positions reconcile the lots with the real position.
                self.log({"type": "foreign_fill", "fill": f})
                continue
            px = f.get("price")
            yes_px = px if f.get("side", "yes").lower() == "yes" else (None if px is None else round(1 - px, 4))
            fvs = self.fv.get(*self.key(m)) if m else None
            rec = {"type": "fill", "exchange_id": ex, "order_id": f.get("orderId"), "trade_id": f["id"],
                   "yes_dir": yes_dir, "yes_price": yes_px, "qty": abs(float(f["quantity"])), "kind": reg.get("kind", "?"),
                   "filled_at": f.get("filledAt"), "fv_now": None if not fvs else round(fvs.fv, 4),
                   "title": m["title"] if m else None, "race": m["race"] if m else None, "party": m["party"] if m else None}
            self.log(rec)
            self.last_fill_t[ex] = self.clock()
            # resting order filled: the position changed now, not at placement. But the fills feed runs 2-8 s behind the
            # positions read (Oct 21 live: AK Gov D's exit filled at 01:15:09, the 01:15:15 positions read already showed
            # it, the fill arrived at 01:15:17 and was subtracted AGAIN -> D -4,105 on paper; that phantom hedge let a
            # second AK Gov R short through the race cap, 10.1k at stake vs 8k). Apply it only when it happened after the
            # last positions read was requested (+2 s clock skew); otherwise that read has it, or the next one will.
            ft = epoch(f.get("filledAt"))
            if reg.get("kind") == "quote" and (ft is None or ft > getattr(self, "pos_read_t", -1e18) + 2.0):
                self.pos[ex] = self.pos.get(ex, 0.0) + yes_dir * abs(float(f["quantity"]))
            self.book_lots(ex, reg.get("kind", "?"), yes_dir, abs(float(f["quantity"])), yes_px, fvs, reg)
            if self.cycle > 0:
                edge = None if (fvs is None or yes_px is None) else (fvs.fv - yes_px) * yes_dir
                self.say(f"FILL {('BUY' if yes_dir > 0 else 'SELL')} {rec['qty']:.0f} @ {yes_px} {rec['title'][:50] if rec['title'] else ex}"
                         f"  edge vs FV {edge:+.3f}" if edge is not None else f"FILL {rec}")

    def refresh_book(self, ex: str):
        b = self.c.orderbook(ex, depth=15)
        self.books[ex] = {"t": self.clock(), "bids": [(float(x["price"]), float(x["quantity"])) for x in b.get("bids", [])],
                          "asks": [(float(x["price"]), float(x["quantity"])) for x in b.get("asks", [])]}

    # ---- helpers -------------------------------------------------------------------------------
    @staticmethod
    def key(m):
        return (m["race"], m["party"])

    def epos(self, ex: str) -> float:
        """The engine's own position: the real one minus what the settlement book holds there."""
        return self.pos.get(ex, 0.0) - self.book.get(ex, 0.0)

    def exposure(self, m) -> float:
        """Shares long this contract, netting the sibling party's contract in the same race (engine positions only)."""
        x = self.epos(m["exchange_id"])
        for sib in self.races.get(m["race"], []):
            if sib is not m and sib["party"] in ("D", "R") and m["party"] in ("D", "R") and sib["party"] != m["party"]:
                x -= self.epos(sib["exchange_id"])
        return x

    def race_risk(self, race) -> float:
        ms = self.races.get(race, [])
        if not ms:
            return 0.0
        m = ms[0]
        s = self.fv.get(*self.key(m))
        fv = s.fv if s else 0.5
        x = self.exposure(m)
        return abs(x) * (fv if x > 0 else 1 - fv)

    def others(self, ex: str, side: str):
        """Other people's levels on one side (our own resting size removed). Best first."""
        book = self.books.get(ex)
        levels = list(book["bids" if side == "bid" else "asks"]) if book else []
        mine = {}
        for o in self.live.get(ex, {}).get(side, []):
            mine[o["price"]] = mine.get(o["price"], 0) + o["qty"]
        out = []
        for px, q in levels:
            q2 = q - mine.get(px, 0)
            if q2 > 0.5:
                out.append((px, q2))
        top = self.tops.get(ex, {})
        t = top.get("bestBid" if side == "bid" else "bestAsk")
        my_prices = set(mine)
        if t is not None and t not in my_prices:
            better = (t > out[0][0]) if (out and side == "bid") else (t < out[0][0]) if out else True
            if better:  # bulk top is fresher than the book and shows someone else at a better price
                out.insert(0, (t, 1.0))
        return out

    def value(self) -> float | None:
        if self.balance is None:
            return None
        v = self.balance
        for ex, q in self.pos.items():
            m = self.by_ex.get(ex)
            s = self.fv.get(*self.key(m)) if m else None
            if s is None:
                continue
            v += q * s.fv if q > 0 else (-q) * (1 - s.fv)
            # settlement-book shares: their fair-value drift is not the engine's P&L (keeps the loss stop honest)
            b = self.book.get(ex)
            if b:
                ref = self.book_ref.get(ex)
                if ref is None or ref[0] != b:
                    self.book_ref[ex] = (b, s.fv)
                else:
                    v -= b * (s.fv - ref[1])
        return v

    def entry_ok(self, worst: float, levels) -> bool:
        """Rule (Oct 16): new entries only at contest prices inside [entry_min_px, entry_max_px]. A sweep trades
        every price between the best level and `worst`, so both ends must be inside."""
        best = levels[0][0] if levels else worst
        return self.P.entry_min_px - 1e-9 <= min(best, worst) and max(best, worst) <= self.P.entry_max_px + 1e-9

    def against_lean(self, m, action: str) -> bool:
        """True when this entry bets against P.lean_party: buying the other major party's YES or selling lean_party's."""
        lean = self.P.lean_party
        if not lean or m["party"] not in ("D", "R"):
            return False
        return (m["party"] == lean) != (action == "buy")

    def _tier_takes(self, m, inp, bids_o, asks_o, edge: float, notional: float, race_cap: float) -> dict:
        """{action: (action, worst, qty)} for one size tier, with the entry rules: lean_extra more edge against the lean
        party, and the entry price band."""
        P2 = copy.copy(self.P)
        P2.take_edge, P2.take_max_notional, P2.max_risk_per_race = edge, notional, race_cap
        tks = quoter.take_orders(inp, P2, bids_o, asks_o)
        if P2.lean_party and P2.lean_extra and any(self.against_lean(m, t[0]) for t in tks):
            P2.take_edge = edge + P2.lean_extra
            strict = {t[0]: t for t in quoter.take_orders(inp, P2, bids_o, asks_o)}
            tks = [strict.get(t[0]) if self.against_lean(m, t[0]) else t for t in tks]
        return {t[0]: t for t in tks if t and self.entry_ok(t[1], asks_o if t[0] == "buy" else bids_o)}

    def fv_takes(self, m, inp, bids_o, asks_o, big_only: bool = False) -> list:
        """Gap entries vs Kalshi. Size tiers (Oct 22): when levels sit >= big_edge through FV they are taken at the big
        sizes (only those levels); otherwise the normal tier (take_edge, take_max_notional, max_risk_per_race), which
        therefore cannot add to a race already above the normal cap. big_only (late window): big tier only. The tier of
        each action goes to self.take_tier for the log."""
        P = self.P
        big = self._tier_takes(m, inp, bids_o, asks_o, P.big_edge, P.big_take_notional, P.big_risk_per_race) if P.big_edge > 0 else {}
        std = {} if big_only else self._tier_takes(m, inp, bids_o, asks_o, P.take_edge, P.take_max_notional, P.max_risk_per_race)
        out = []
        for action in ("buy", "sell"):
            t = big.get(action) or std.get(action)
            if t:
                self.take_tier[(m["exchange_id"], action)] = "big" if action in big else "std"
                out.append(t)
        return out

    def model_take(self, m, inp, bids_o, asks_o):
        """Model mode: enter when the contest-price model predicts a 24h move >= model_thr, paying at most the model's
        reference contest price + model_slip. Returns (action, limit, qty, pred) or None."""
        P = self.P
        pred, ref = self.model.pred(m["exchange_id"]), self.model.ref(m["exchange_id"])
        if pred is None or ref is None:
            return None
        action = "buy" if pred > 0 else "sell"
        if abs(pred) < P.model_thr + (P.lean_extra if self.against_lean(m, action) else 0.0):
            return None
        can_buy, can_sell = quoter.capacities(inp, P)
        if pred > 0:
            lim = quoter.snap(ref + P.model_slip, P.tick, "down")
            lv = [(px, q) for px, q in asks_o if px <= lim + 1e-9]
            if not lv or not self.entry_ok(lv[-1][0], lv):
                return None
            worst = lv[-1][0]
            qty = min(sum(q for _, q in lv), P.take_max_notional / max(worst, 0.01), can_buy)
            return ("buy", worst, int(qty), pred) if qty >= 1 else None
        lim = quoter.snap(ref - P.model_slip, P.tick, "up")
        lv = [(px, q) for px, q in bids_o if px >= lim - 1e-9]
        if not lv or not self.entry_ok(lv[-1][0], lv):
            return None
        worst = lv[-1][0]
        qty = min(sum(q for _, q in lv), P.take_max_notional / max(1 - worst, 0.01), can_sell)
        return ("sell", worst, int(qty), pred) if qty >= 1 else None

    def fresh_ok(self, m, action: str) -> bool:
        """Fresh-gap rule (P.fresh_h > 0): only take a gap that OPENED within the last fresh_h hours, i.e. back then the
        contest was within fresh_max of Kalshi in our direction. Back-test Oct 5-12: same P&L as taking every gap with ~30%
        less capital tied up; much better while gaps were opening fast (Oct 5-8). Data: collector DB."""
        if not self.P.fresh_h:
            return True
        t = self.clock() - self.P.fresh_h * 3600
        key = (m["exchange_id"], action, int(t // 300))
        if key in self.fresh_cache:
            return self.fresh_cache[key]
        if self.hist_db is None:
            import sqlite3
            self.hist_db = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True, check_same_thread=False, timeout=10)
        tickers = (getattr(self.fv, "race_tickers", {}).get(m["race"]) or {}).get(m["party"]) or []
        c = self.hist_db.execute("SELECT SUM(price*quantity)/SUM(quantity) FROM trades WHERE exchange_id=? AND ts BETWEEN ? AND ?",
                                 (m["exchange_id"], t - 1800, t)).fetchone()[0]
        k = None
        if tickers:
            r = self.hist_db.execute("SELECT bid, ask FROM kalshi WHERE ticker=? AND ts<=? AND ts>? ORDER BY ts DESC LIMIT 1",
                                     (tickers[0], t, t - 3 * 3600)).fetchone()
            if r and r[0] is not None and r[1] is not None and 0 < r[1] - r[0] <= 0.1:
                k = (r[0] + r[1]) / 2
        if c is None or k is None:
            ok = False            # no history: not provably fresh
        else:
            want = 1 if action == "buy" else -1   # buy = contest below FV now
            ok = want * (k - c) < self.P.fresh_max
        self.fresh_cache[key] = ok
        return ok

    def regime_for(self, m, s) -> Regime:
        now = dt.datetime.fromtimestamp(self.clock(), dt.timezone.utc)
        r = self.cal.regime(now, m["race"], self.P)
        if now >= self.cutoff:
            r.pull, r.label = True, "election-night cutoff"
        if s is None or s.age_s > self.P.fv_max_age_s or not s.ref_spread_ok:
            r.pull, r.label = True, "no fresh fair value"
        elif s.jumping:
            r.spread_mult, r.size_mult, r.label = max(r.spread_mult, self.P.jump_mult), min(r.size_mult, 0.5), "jump"
        return r

    def en_on(self) -> bool:
        return self.en is not None and self.en.started(self.clock())

    def expiry(self, secs: float) -> str:
        return (dt.datetime.fromtimestamp(self.clock(), dt.timezone.utc) + dt.timedelta(seconds=secs)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def expiring(self, oid) -> bool:
        """Quote at (or within 2 s of) its exchange-side expiry: treat it as gone and re-place."""
        ttl = self.P.quote_ttl_s
        t = self.registry.get(oid, {}).get("t")
        return bool(ttl) and t is not None and ttl - 2 <= self.clock() - t < ttl + 30

    def overdue(self, oid) -> bool:
        """Still reported open 30 s after it should have expired: never trust it, cancel it."""
        ttl = self.P.quote_ttl_s
        t = self.registry.get(oid, {}).get("t")
        return bool(ttl) and t is not None and self.clock() - t >= ttl + 30

    # ---- writes ---------------------------------------------------------------------------------
    def spend(self, n: int) -> bool:
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False

    def _register(self, res: dict, ex: str, yes_dir: int, price: float, kind: str, extra: dict | None = None):
        oid = res.get("orderId")
        if oid is not None:
            self.registry[oid] = {"exchange_id": ex, "yes_dir": yes_dir, "price": price, "kind": kind, "t": self.clock(),
                                  **(extra or {})}
        traded = abs(float(res.get("quantityTraded") or 0))       # NO-side orders report negative quantities
        if traded:
            x = self.pos.get(ex, 0.0)
            if self.balance is not None:
                # Oct 16 live: time-stop exits closed ~12k of positions in one cycle; the position moved at once but the
                # cash only at the next balance read, so value() dropped ~12k and the 3k loss stop shut the engine down.
                # Book the cash now (platform rules: closing against an opposite position credits 1 per pair; limit
                # price, so never better than the real fill). The next balance read overwrites this estimate.
                n = min(traded, max(0.0, -yes_dir * x))
                if yes_dir > 0:
                    self.balance += (1 - price) * n - price * (traded - n)
                else:
                    self.balance += price * n - (1 - price) * (traded - n)
            self.pos[ex] = x + yes_dir * traded
        if oid is not None and res.get("open") and kind == "quote":
            rem = abs(float(res.get("remainingQuantity") or res.get("quantity") or 0))
            self.live[ex]["bid" if yes_dir > 0 else "ask"].append({"id": oid, "price": price, "qty": rem})

    def _legs(self, ex, q: quoter.Quote, which: list[str]):
        legs = []
        exp = self.expiry(self.P.quote_ttl_s) if self.P.quote_ttl_s else None
        for side in which:
            if side == "bid" and q.bid is not None:
                legs.append(({"exchange_id": ex, "side": "yes", "action": "buy", "quantity": q.bid_qty, "price": q.bid, "expiration": exp}, +1, q.bid))
            if side == "ask" and q.ask is not None:
                legs.append(({"exchange_id": ex, "side": "yes", "action": "sell", "quantity": q.ask_qty, "price": q.ask, "expiration": exp}, -1, q.ask))
        return legs

    def execute(self, act: dict):
        ex, kind = act["exchange_id"], act["op"]
        if kind.startswith("en_"):          # election-night batch / cancel (handles dry itself)
            return self.en.execute(act)
        if self.dry:
            self.log({"type": "dry_action", **{k: v for k, v in act.items() if k != "quote"},
                      "quote": asdict(act["quote"]) if act.get("quote") else None})
            if kind == "requote":  # pretend it happened so we only log changes
                q = act["quote"]
                if act.get("cancel_all"):
                    self.live[ex] = {"bid": [], "ask": []}
                for side in act.get("cancel", []):
                    self.live[ex][side] = []
                for side in act.get("place", []):
                    px, qty = (q.bid, q.bid_qty) if side == "bid" else (q.ask, q.ask_qty)
                    self.live[ex][side] = [{"id": self.dry_ids, "price": px, "qty": qty}]
                    self.registry[self.dry_ids] = {"t": self.clock(), "kind": "quote"}
                    self.dry_ids -= 1
                for side in act.get("place", []):
                    for o in self.live[ex][side]:
                        self.registry[o["id"]] = {"t": self.clock(), "kind": "quote"}
                self.say(f"DRY quote {self.by_ex[ex]['title'][:50]}: {q.bid}x{q.bid_qty} / {q.ask}x{q.ask_qty} "
                         f"(FV {act['fv']:.3f}, h {q.half:.3f}, {q.note})" if act.get("fv") is not None else
                         f"DRY pull {self.by_ex[ex]['title'][:50]} ({q.note})")
            else:
                self.say(f"DRY take {act['take']} {self.by_ex[ex]['title'][:50]} vs FV {act['fv']:.3f}")
            return
        try:
            if kind == "take":
                exp = (dt.datetime.fromtimestamp(self.clock(), dt.timezone.utc) + dt.timedelta(seconds=self.P.take_ttl_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
                action, px, qty = act["take"]
                kind_t = act.get("kind", "take")
                res = self.c.place(ex, "yes", action, qty, px, idempotency_key=str(uuid.uuid4()), expiration=exp)
                self._register(res, ex, +1 if action == "buy" else -1, px, kind_t,
                               {"mode": act.get("mode") or "fv", "pred": act.get("pred")})
                self.log({"type": kind_t, "exchange_id": ex, "action": action, "price": px, "qty": qty, "res": res, "fv": act["fv"],
                          "mode": act.get("mode"), "pred": act.get("pred"), "tier": act.get("tier"), "late": act.get("late")})
                self.say(f"{kind_t.upper()} {action} {qty} @ {px} vs FV {act['fv']:.3f} {self.by_ex[ex]['title'][:50]}")
                return
            # cancels
            if act.get("cancel_all"):
                self.c.cancel_all(exchange_id=ex)
                self.live[ex] = {"bid": [], "ask": []}
            for side in act.get("cancel", []):
                for o in self.live[ex][side]:
                    self.c.cancel(o["id"])
                self.live[ex][side] = []
            # placements
            legs = self._legs(ex, act["quote"], act.get("place", [])) if act.get("quote") else []
            if len(legs) == 1:
                (leg, d, px), = legs
                res = self.c.place(ex, leg["side"], leg["action"], leg["quantity"], leg["price"], idempotency_key=str(uuid.uuid4()),
                                   expiration=leg.get("expiration"))
                self._register(res, ex, d, px, "quote")
            elif len(legs) == 2:
                res = self.c.place_multi_leg([l for l, _, _ in legs], idempotency_key=str(uuid.uuid4()))
                for r_, (leg, d, px) in zip(res.get("results", []), legs):
                    self._register(r_.get("data") or {}, ex, d, px, "quote")
            self.log({"type": "requote", "exchange_id": ex, "op": act, "fv": act.get("fv")})
        except Exception as e:  # never let one failed call kill the loop; resync orders next cycle
            self.log({"type": "error", "exchange_id": ex, "op": kind, "error": str(e)[:500]})
            self.say(f"ERROR {kind} {ex}: {str(e)[:160]}")
            self.live[ex] = {"bid": [], "ask": []}
            self.force_orders = True

    def apply_exit(self, ex, s, q: quoter.Quote, bids_o, asks_o, acts) -> str | None:
        """Fold the exit policy for this contract's open lots into its quote. Returns the closing side, if any."""
        lots = [l for l in self.lots.get(ex, []) if l.qty >= 1]
        if not lots:
            return None
        now = self.clock()
        past_final = now >= self.final_exit
        past_liq = now >= self.liquidate
        d = 1 if sum(l.d * l.qty for l in lots) > 0 else -1
        lots = [l for l in lots if l.d == d]
        held = max(0.0, d * self.epos(ex))
        # Oct 13 live bug: with held == 0 the old code exited the full lot size again (fills are read later than the
        # take updates the position), flipping MI Sen R from -300 to +600 and VA-05 R from +1350 to -1350. Never exit
        # more than the engine's current position in the lots' direction.
        qty = int(min(sum(l.qty for l in lots), held))
        if qty < 1:
            return None
        if self.clock() - self.exit_sent.get(ex, -1e18) < 20:
            return None           # an exit take is already in flight for this contract
        prices = [(exit_price(l, s.fv, now, self.X, self.P.tick, past_final, past_liquidate=past_liq)[0], l.qty) for l in lots]
        due = [l for l, (p, _) in zip(lots, prices) if p is None]
        if due:
            # exit at market ONLY the lots whose time is up (Oct 16 live: AK Sen R lots from Oct 11 hit the 120 h time
            # stop and the whole 8,822-share position was bought back, including 4,255 sold 30 s earlier; NH Sen R
            # 70 h-old lots were dumped with the 120 h ones). The other lots get their resting target back once these
            # fills are read (after the 20 s lock); fills close lots oldest first, i.e. the due ones.
            # Cross to the best opposing price, but never worse than BOTH FV -/+ market_floor and entry -/+ 5c (Oct 14:
            # with the FV-only cap a short long-shot sold at 0.12 vs FV 0.01 could never be bought back: cap 0.11 <
            # ask 0.12+). No order is sent when nothing is available inside the limit.
            qty = int(min(sum(l.qty for l in due), held))
            if qty < 1:
                return None
            p0 = sum(l.p0 * l.qty for l in due) / sum(l.qty for l in due)
            self.exit_sent[ex] = self.clock()
            if d > 0:
                floor = quoter.snap(min(s.fv - self.X.market_floor, p0 - 0.05), self.P.tick, "up")
                if bids_o and bids_o[0][0] >= floor:
                    acts.append({"op": "take", "prio": 1, "exchange_id": ex, "take": ("sell", bids_o[0][0], qty), "fv": s.fv,
                                 "kind": "exit"})
            else:
                cap = quoter.snap(max(s.fv + self.X.market_floor, p0 + 0.05), self.P.tick, "down")
                if asks_o and asks_o[0][0] <= cap:
                    acts.append({"op": "take", "prio": 1, "exchange_id": ex, "take": ("buy", asks_o[0][0], qty), "fv": s.fv,
                                 "kind": "exit"})
            q.note += " exit@market"
            return None
        tgt = sum(p * w for p, w in prices) / sum(w for _, w in prices)
        tgt = quoter.snap(tgt, self.P.tick, "up" if d > 0 else "down")
        if d > 0:   # long YES: close by selling YES
            if bids_o and bids_o[0][0] >= tgt:  # students already pay our target: take it now
                acts.append({"op": "take", "prio": 1, "exchange_id": ex, "take": ("sell", tgt, qty), "fv": s.fv, "kind": "exit"})
                self.exit_sent[ex] = self.clock()
                return None
            if q.ask is None or tgt < q.ask:
                q.ask, q.ask_qty = tgt, max(qty, q.ask_qty if q.ask is not None else 0)
            if q.bid is not None and q.bid >= q.ask:
                q.bid = None
            q.note += f" exit@{tgt}"
            return "ask"
        if asks_o and asks_o[0][0] <= tgt:      # long NO: close by buying YES
            acts.append({"op": "take", "prio": 1, "exchange_id": ex, "take": ("buy", tgt, qty), "fv": s.fv, "kind": "exit"})
            self.exit_sent[ex] = self.clock()
            return None
        if q.bid is None or tgt > q.bid:
            q.bid, q.bid_qty = tgt, max(qty, q.bid_qty if q.bid is not None else 0)
        if q.ask is not None and q.ask <= q.bid:
            q.ask = None
        q.note += f" exit@{tgt}"
        return "bid"

    # ---- the cycle ------------------------------------------------------------------------------
    def plan(self) -> list[dict]:
        if self.en_on():                    # from Nov 3 17:00 ET: election-night take mode replaces everything below
            return self.en.plan()
        P, acts = self.P, []
        total_risk = sum(self.race_risk(r) for r in self.races)
        pending_race: dict[tuple, float] = {}   # risk of takes planned earlier in this cycle (not yet in positions)
        for m in self.markets:
            ex = m["exchange_id"]
            s = self.fv.get(*self.key(m))
            reg = self.regime_for(m, s)
            bids_o, asks_o = self.others(ex, "bid"), self.others(ex, "ask")
            live = self.live[ex]
            quoting = m.get("quote", True)
            exit_side = None
            if reg.pull or s is None:
                q = quoter.Quote(None, 0, None, 0, 0.0, 0.0, reg.label)
            else:
                inp = quoter.Inputs(fv=s.fv, ref_half_spread=s.ref_half_spread, sigma_hourly=s.sigma_hourly,
                                    exposure=self.exposure(m),
                                    risk_used_race=self.race_risk(m["race"]) + pending_race.get(m["race"], 0.0),
                                    risk_used_total=total_risk + sum(pending_race.values()),
                                    best_bid_others=bids_o[0][0] if bids_o else None,
                                    best_ask_others=asks_o[0][0] if asks_o else None,
                                    spread_mult=reg.spread_mult, size_mult=reg.size_mult, skew_mult=reg.skew_mult)
                q = quoter.make_quote(inp, P) if quoting else quoter.Quote(None, 0, None, 0, 0.0, s.fv, "no MM here")
                q.note = f"{q.note} {reg.label}".strip()
                book_age = self.clock() - self.books.get(ex, {}).get("t", 0)
                now_ = self.clock()
                model_open = now_ + P.model_hold_h * 3600 <= self.liquidate      # must be able to exit before liquidation
                fv_open = now_ + (self.X.time_stop_h or 0) * 3600 <= self.liquidate
                # late window (Oct 22): a full hold no longer fits before the liquidation -> big-tier gaps only, while
                # late_min_h hours remain; such lots glide and time-stop over the hours left (book_lots: horizon_h)
                late_open = (not fv_open and P.late_min_h > 0 and P.big_edge > 0
                             and now_ + P.late_min_h * 3600 <= self.liquidate)
                if (P.entry_mode in ("model", "both") and model_open and P.take_enabled and not reg.pull and s.age_s < 20
                        and book_age < 12 and not self.lots.get(ex) and abs(self.epos(ex)) < 1
                        and self.clock() - self.model_last.get(ex, 0) > 300):   # one position per contract
                    try:
                        tk = self.model_take(m, inp, bids_o, asks_o)
                    except Exception as e:      # a model glitch must never stop the engine (exits keep running)
                        tk = None
                        self.log({"type": "error", "op": "model_take", "error": str(e)[:300]})
                    if tk:
                        self.model_last[ex] = self.clock()
                        acts.append({"op": "take", "prio": 1, "exchange_id": ex, "take": tk[:3], "fv": s.fv, "kind": "take",
                                     "mode": "model", "pred": tk[3]})
                        pending_race[m["race"]] = pending_race.get(m["race"], 0.0) + tk[2] * (s.fv if tk[0] == "buy" else 1 - s.fv)
                if (P.entry_mode in ("fv", "both") and (fv_open or late_open) and P.take_enabled and not reg.pull and s.age_s < 20
                        and book_age < 12 and P.take_min_fv <= s.fv <= P.take_max_fv
                        and not any(l.mode == "model" for l in self.lots.get(ex, []))
                        and total_risk + sum(pending_race.values()) < P.max_total_risk - (P.model_reserve if P.entry_mode == "both" else 0)):
                    for tk in self.fv_takes(m, inp, bids_o, asks_o, big_only=not fv_open):
                        if not self.fresh_ok(m, tk[0]):
                            continue
                        acts.append({"op": "take", "prio": 1, "exchange_id": ex, "take": tk, "fv": s.fv, "kind": "take",
                                     "tier": self.take_tier.get((ex, tk[0])), "late": not fv_open})
                        action, _, tq = tk
                        pending_race[m["race"]] = pending_race.get(m["race"], 0.0) + tq * (s.fv if action == "buy" else 1 - s.fv)
                exit_side = self.apply_exit(ex, s, q, bids_o, asks_o, acts)
            fv_now = s.fv if s else None
            on_hold = self.hold_place.get(ex, 0) > self.clock()     # an own order just vanished (see refresh_orders)
            need_cancel, need_place, prio = [], [], 9.0
            for side, tgt, tq in (("bid", q.bid, q.bid_qty), ("ask", q.ask, q.ask_qty)):
                live[side] = [o for o in live[side] if not self.expiring(o["id"])]
                cur = live[side]
                if any(self.overdue(o["id"]) for o in cur):
                    need_cancel.append(side)
                    if tgt is not None:
                        need_place.append(side)
                    prio = min(prio, 0)
                    continue
                if side == exit_side and tgt is not None:
                    # an exit order rests below FV on purpose: it is only "dangerous" if it is cheaper than the target
                    danger = bool(cur) and ((side == "bid" and max(o["price"] for o in cur) > tgt + P.tick / 2) or
                                            (side == "ask" and min(o["price"] for o in cur) < tgt - P.tick / 2))
                else:
                    danger = bool(cur) and fv_now is not None and (
                        (side == "bid" and max(o["price"] for o in cur) > fv_now - P.min_edge_vs_fv / 2) or
                        (side == "ask" and min(o["price"] for o in cur) < fv_now + P.min_edge_vs_fv / 2))
                if tgt is None:
                    if cur:
                        need_cancel.append(side)
                        prio = min(prio, 0 if danger or reg.pull else 3)
                    continue
                if not cur:
                    if not on_hold:
                        need_place.append(side)
                        prio = min(prio, 4)
                    continue
                best = cur[0]
                age = self.clock() - self.registry.get(best["id"], {}).get("t", 0)
                settled_in = age >= P.min_quote_life_s
                moved = settled_in and abs(best["price"] - tgt) >= P.requote_ticks * P.tick - 1e-9
                thin = settled_in and best["qty"] < 0.4 * tq
                if danger or moved or thin or len(cur) > 1:
                    need_cancel.append(side)
                    need_place.append(side)
                    prio = min(prio, 0 if danger else 2 + max(0.0, 0.05 - abs(best["price"] - tgt)) * 10)
            if need_cancel or need_place:
                n_orders = sum(len(live[sd]) for sd in need_cancel)
                if n_orders > 1:
                    # one cancel-all write is cheaper than several single cancels; re-place every side we want
                    cancel_all, cancel = True, []
                    place = [sd for sd, tgt in (("bid", q.bid), ("ask", q.ask))
                             if tgt is not None and (live[sd] or not on_hold)]
                else:
                    cancel_all, cancel, place = False, need_cancel, need_place
                cost = (1 if cancel_all else len(cancel)) + (1 if place else 0)
                acts.append({"op": "requote", "prio": prio, "exchange_id": ex, "quote": q, "fv": fv_now,
                             "cancel_all": cancel_all, "cancel": cancel, "place": place, "cost": cost})
        return sorted(acts, key=lambda a: a["prio"])

    def step(self):
        P = self.P
        now = self.clock()
        self.tokens = min(8.0, self.tokens + (now - self.last_t) * P.writes_per_min / 60)
        self.last_t = now
        self.fv.poll()
        if self.model is not None:
            try:
                self.model.refresh()
            except Exception as e:
                self.log({"type": "error", "op": "model", "error": str(e)[:300]})
        ids = [m["exchange_id"] for m in self.markets]
        en_on = self.en_on()
        try:
            self.tops = self.c.prices(ids)
            # election night: nothing rests (takes expire in seconds), so open orders every 3rd cycle; cash binds, so
            # the balance every cycle
            if not self.dry and (self.cycle % (3 if en_on else P.orders_every) == 0 or self.force_orders):
                self.refresh_orders()
                self.force_orders = False
            if self.cycle % P.positions_every == 0:
                self.refresh_positions()
                self.refresh_balance()
            elif en_on:
                self.refresh_balance()
            if self.cycle % P.fills_every == 0:
                self.refresh_fills()
            # refresh up to two order books per cycle, stalest first
            stale = sorted(ids, key=lambda e: self.books.get(e, {}).get("t", 0))
            for ex in stale[:self.books_per_cycle]:
                if self.books_per_cycle > 2 or now - self.books.get(ex, {}).get("t", 0) > min(P.book_refresh_s, 12):
                    self.refresh_book(ex)
        except Exception as e:
            self.log({"type": "error", "op": "read", "error": str(e)[:500]})
            self.say(f"read error: {str(e)[:160]}")
            self.cycle += 1
            return
        v = self.value()
        if v is not None:
            if self.start_value is None:
                self.start_value = v
            # loss stop: judged only right after positions AND balance were read together, and only when two such
            # reads in a row agree (a fill between the two reads makes one inconsistent snapshot). Off on election
            # night: a correct take at 0.70 is marked at 0.99 at once and settlements move cash and positions apart;
            # the election guards (caps, reversal breaker) replace it.
            if self.cycle % P.positions_every == 0 and not en_on:
                self.loss_hits = self.loss_hits + 1 if self.start_value - v > P.max_daily_loss else 0
                if self.loss_hits >= 2:
                    self.log({"type": "loss_stop", "value": v, "start_value": self.start_value})
                    self.say(f"Loss limit hit ({self.start_value - v:,.0f}). Cancelling everything and stopping.")
                    raise KeyboardInterrupt
        for act in (self.plan() if self.pos_ok else []):
            cost = 1 if act["op"] == "take" else act["cost"]
            if self.dry or self.spend(cost):
                self.execute(act)
        if self.cycle % 4 == 0:
            self.snapshot(v)
        self.cycle += 1

    def snapshot(self, v):
        # Disk (Oct 14: C: 99% full): full rows for every market only every 10th snapshot (~4 min); otherwise just the
        # markets that matter (position, resting orders, exit lots, or a fill in the last 45 min, for markouts).
        self.snap_n = getattr(self, "snap_n", 0) + 1
        full = self.snap_n % 10 == 1
        now = self.clock()
        rows = []
        for m in self.markets:
            ex = m["exchange_id"]
            if not full and not (abs(self.pos.get(ex, 0.0)) > 0 or self.live[ex]["bid"] or self.live[ex]["ask"]
                                 or self.lots.get(ex) or now - self.last_fill_t.get(ex, -1e18) < 2700):
                continue
            s = self.fv.get(*self.key(m))
            lv = self.live[ex]
            top = self.tops.get(ex, {})
            rows.append({"ex": ex, "fv": None if not s else round(s.fv, 4), "sig": None if not s else round(s.sigma_hourly, 4),
                         "bb": top.get("bestBid"), "ba": top.get("bestAsk"),
                         "my_bid": lv["bid"][0]["price"] if lv["bid"] else None,
                         "my_ask": lv["ask"][0]["price"] if lv["ask"] else None,
                         "pos": self.pos.get(ex, 0.0)})
        fvd = {"poll_errors": getattr(self.fv, "poll_errors", None), "bad": len(getattr(self.fv, "bad", {}) or {}),
               "last_ok_age": round(self.clock() - getattr(self.fv, "last_ok", 0), 1)}
        self.log({"type": "snap", "value": v, "balance": self.balance, "tokens": round(self.tokens, 2), "fv_health": fvd, "rows": rows})
        if fvd["last_ok_age"] > 120:
            self.say(f"WARNING Kalshi fair values stale for {fvd['last_ok_age']:.0f}s (poll errors {fvd['poll_errors']}, "
                     f"bad tickers {fvd['bad']}): all orders pulled until it recovers")
        if self.verbose and self.cycle % 8 == 0:
            pnl = "" if v is None or self.start_value is None else f" P&L since start {v - self.start_value:+,.0f}"
            self.say(f"--- value {v if v is None else round(v):,}{pnl}")
            for r in rows:
                if r["pos"] or r["my_bid"] is not None or r["my_ask"] is not None:   # console: only what we hold / quote
                    self.say(f"  {self.by_ex[r['ex']]['title'][:46]:46} FV {r['fv']}  book {r['bb']}/{r['ba']}  "
                             f"me {r['my_bid']}/{r['my_ask']}  pos {r['pos']:+.0f}")

    def shutdown(self):
        if not self.dry:
            if len(self.by_ex) > 20:     # one write instead of one per market (writes are paced at ~29/min)
                try:
                    self.c.cancel_all()
                except Exception as e:
                    self.say(f"cancel-all failed: {e}")
            else:
                for ex in self.by_ex:
                    try:
                        self.c.cancel_all(exchange_id=ex)
                    except Exception as e:
                        self.say(f"cancel failed for {ex}: {e}")
        self.log({"type": "stop"})
        self.logf.close()

    def run(self, max_cycles: int | None = None):
        try:
            while max_cycles is None or self.cycle < max_cycles:
                if STOP_FILE.exists() or (self.stop_file and self.stop_file.exists()):
                    self.say("STOP file found.")
                    break
                t0 = self.clock()
                self.step()
                cyc = self.P.en_cycle_s if self.en_on() else self.P.cycle_s
                self.sleep(max(0.0, cyc - (self.clock() - t0)))
        except KeyboardInterrupt:
            pass
        finally:
            self.say("Shutting down: cancelling resting orders...")
            self.shutdown()
