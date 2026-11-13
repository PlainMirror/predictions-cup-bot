"""Election-night take mode. Runs inside MMEngine when P.election_mode is on, from P.en_start_et
(Nov 3 17:00 ET) to P.en_end_et (Nov 4 11:55 ET). It replaces the normal cycle: no quotes, no gap/model entries, no exit
policy. Each cycle:
  1. Eligible races: the state's polls have closed (data/poll_close.csv, latest closing in the state; chamber control
     from en_control_open_et), Kalshi fresh, race not blocked by the reversal breaker.
  2. Conservative fair value: buys use the LOWEST Kalshi FV over the hold window, sells the HIGHEST, so a fresh Kalshi
     move is acted on only once it has held (60 s for called, 120 s otherwise). Tier from the distance to 0 or 1:
     called (<= 1 - en_called_fv), leaning (<= 1 - en_lean_fv), open. Edge = tier edge (x en_slow_mult in slow-settling
     races, data/election_slow.csv) + Kalshi's half-spread.
  3. Every book level through FV -/+ edge is a candidate, ranked by return per SUSQie spent (slow races at
     en_slow_weight) and filled greedily within the per-race cap (fraction of V0 by tier), the cap on leaning + open
     risk, and the cash we have. Closing a position needs no cash and frees some.
  4. Recycling: takes left unfilled for lack of cash are funded by selling called winners we hold (ours: FV >=
     en_recycle_fv held 60 s, price >= en_recycle_min_px) when the take returns >= the give-up + en_recycle_margin.
     Positions of the hand-placed Nov 1-3 book count too (decided Oct 19): all caps and recycling use the TOTAL position.
  5. Orders go out through /orders/batch (one write per 50 orders, en_take_ttl_s expiry each), recycling first.
Guards: reversal breaker (a held side that peaked as called/leaning and fell back blocks its race; en_rev_n reversals
within en_rev_window_s block all leaning/open entries for the night), data/EN_FREEZE (no new risk, recycling and
closing continue), writes paused after a 429, nothing after en_end_et. The mark-to-FV loss stop is off in this mode.
State (V0, blocks, peaks) survives restarts in data/en_state.json.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import uuid
from pathlib import Path

from mm.events import et_to_utc

DATA = Path(__file__).resolve().parents[2] / "data"
POLL_CLOSE = DATA / "poll_close.csv"
SLOW = DATA / "election_slow.csv"
STATE_FILE = DATA / "en_state.json"
FREEZE_FILE = DATA / "EN_FREEZE"


def load_poll_close(path: Path = POLL_CLOSE) -> dict[str, float]:
    out = {}
    if path.exists():
        for r in csv.DictReader(path.open(encoding="utf-8")):
            if r.get("state") and r.get("close_et"):
                out[r["state"].strip().upper()] = et_to_utc(r["close_et"].strip()).timestamp()
    return out


def load_slow(path: Path = SLOW) -> list[str]:
    if not path.exists():
        return []
    return [r["scope"].strip() for r in csv.DictReader(path.open(encoding="utf-8")) if (r.get("scope") or "").strip()]


def race_state(race) -> str | None:
    office, place = race
    return None if office == "control" else place[:2].upper()


def scope_matches(scope: str, race) -> bool:
    """'control' | 'ST' (every race in the state) | 'office:ST' (all of that office there) | 'office:PLACE' (exact)."""
    office, place = race
    if scope == "control":
        return office == "control"
    if ":" not in scope:
        return office != "control" and place[:2].upper() == scope.upper()
    o, p = scope.split(":", 1)
    return office == o and (place == p or place.startswith(p + "-"))


def race_key(race) -> str:
    return f"{race[0]}:{race[1]}"


class ElectionNight:
    def __init__(self, eng, poll_close: dict | None = None, slow: list | None = None,
                 state_file: Path | None = STATE_FILE, freeze_file: Path = FREEZE_FILE):
        self.e, self.P = eng, eng.P
        P = self.P
        self.start = et_to_utc(P.en_start_et).timestamp()
        self.end = et_to_utc(P.en_end_et).timestamp()
        self.control_open = et_to_utc(P.en_control_open_et).timestamp()
        self.poll_close = load_poll_close() if poll_close is None else poll_close
        self.slow_scopes = load_slow() if slow is None else slow
        self.state_file, self.freeze_file = state_file, freeze_file
        self.st = {"start": P.en_start_et, "v0": None, "blocked": {}, "reversals": [], "uncalled_blocked": False,
                   "peak": {}}
        if state_file and state_file.exists():
            try:
                saved = json.loads(state_file.read_text(encoding="utf-8"))
                if saved.get("start") == P.en_start_et:          # same night: keep V0, blocks and peaks
                    self.st.update(saved)
            except (ValueError, OSError):
                pass
        self.caps = {"called": P.en_cap_called, "lean": P.en_cap_lean, "open": P.en_cap_open}
        self.edges = {"called": P.en_edge_called, "lean": P.en_edge_lean, "open": P.en_edge_open}
        self.lock: dict[tuple, float] = {}      # (ex, action) -> no new order before this time
        self.write_block = 0.0                  # after a 429: no order writes before this time
        self.announced = self.ended = False
        self.cancel_done = False
        self.last_summary = 0.0
        self.sent: list[dict] = []              # sent orders (console summary)
        self.unknown_states: set = set()
        self.closed_seen: set = set()           # exchange ids already logged as closed on Kalshi
        self.last_cand_log = 0.0

    # ---- helpers ------------------------------------------------------------------------------------------
    def started(self, now: float) -> bool:
        return now >= self.start

    def save(self):
        if not self.state_file:
            return
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.st, indent=1), encoding="utf-8")
        tmp.replace(self.state_file)

    def tier_of(self, f: float) -> str:
        x = min(f, 1 - f)
        if x <= 1 - self.P.en_called_fv + 1e-9:
            return "called"
        return "lean" if x <= 1 - self.P.en_lean_fv + 1e-9 else "open"

    def is_slow(self, race) -> bool:
        return any(scope_matches(s, race) for s in self.slow_scopes)

    def eligible(self, race, now: float) -> bool:
        st = race_state(race)
        if st is None:
            return now >= self.control_open
        t = self.poll_close.get(st)
        if t is None:
            if st not in self.unknown_states:
                self.unknown_states.add(st)
                self.e.log({"type": "en_unknown_state", "state": st})
                self.e.say(f"ELECTION: no poll-closing time for {st} in data/poll_close.csv: its races are never traded")
            return False
        return now >= t or not self.P.en_gate

    def window(self, m, secs):
        w = getattr(self.e.fv, "window", None)
        return w(m["race"], m["party"], secs) if w else None

    def side_fv(self, m, action: str):
        """Conservative FV for taking `action` (buy: low end, sell: high end of the hold window) and its tier."""
        w60 = self.window(m, self.P.en_hold_called_s)
        if w60 is None:
            return None
        f60 = w60[0] if action == "buy" else w60[1]
        if self.tier_of(f60) == "called":
            return f60, "called"
        w = self.window(m, self.P.en_hold_s)
        if w is None:
            return None
        f = w[0] if action == "buy" else w[1]
        return f, self.tier_of(f)

    def risk_now(self):
        """Worst-case-style risk now: |position| x (FV if long YES else 1 - FV), summed per race over every contract
        (hedges ignored), total positions incl. the hand-placed book. Also the part in races not yet called."""
        e = self.e
        race_var: dict[tuple, float] = {}
        unc = 0.0
        for ex, x in e.pos.items():
            m = e.by_ex.get(ex)
            if not m or abs(x) < 1:
                continue
            s = e.fv.get(*e.key(m))
            f = s.fv if s else 0.5
            var = abs(x) * (f if x > 0 else 1 - f)
            race_var[m["race"]] = race_var.get(m["race"], 0.0) + var
            if self.tier_of(f) != "called":
                unc += var
        return race_var, unc

    # ---- guards ---------------------------------------------------------------------------------------------
    def check_reversals(self, now: float):
        """A held side that peaked as called (or leaning) and has stayed below en_rev_called (en_rev_lean) for the
        whole called-hold window blocks its race for the night."""
        e, P, st = self.e, self.P, self.st
        changed = False
        for ex, x in e.pos.items():
            m = e.by_ex.get(ex)
            if not m or abs(x) < 1:
                continue
            s = e.fv.get(*e.key(m))
            w = self.window(m, P.en_hold_called_s)
            if s is None or w is None:
                continue
            if getattr(s, "closed", False):
                # Kalshi closed the market (to settle it). That is not a reversal. On Nov 3-4 the empty
                # book read as 0.50 and blocked 16 decided races.
                if ex not in self.closed_seen:
                    self.closed_seen.add(ex)
                    e.log({"type": "en_market_closed", "race": race_key(m["race"]), "exchange_id": ex, "last_fv": s.fv, "pos": x})
                continue
            if s.age_s > 60:
                continue
            ours_now = s.fv if x > 0 else 1 - s.fv
            ours_held = w[1] if x > 0 else 1 - w[0]       # best our side has been over the window
            pk = max(st["peak"].get(ex, 0.0), ours_now)
            if pk != st["peak"].get(ex):
                st["peak"][ex] = round(pk, 4)
                changed = True
            rk = race_key(m["race"])
            if rk in st["blocked"]:
                continue
            if (pk >= P.en_called_fv and ours_held < P.en_rev_called) or (P.en_lean_fv <= pk < P.en_called_fv and ours_held < P.en_rev_lean):
                st["blocked"][rk] = f"reversal {m['title'][9:60]}: peak {pk:.3f} -> {ours_now:.3f}"
                st["reversals"].append(now)
                changed = True
                e.log({"type": "en_breaker", "race": rk, "exchange_id": ex, "peak": pk, "now": ours_now, "pos": x})
                e.say(f"ALERT reversal: {m['title'][9:60]} our side peaked {pk:.3f}, now {ours_now:.3f}: race blocked")
                recent = [t for t in st["reversals"] if now - t <= P.en_rev_window_s]
                if len(recent) >= P.en_rev_n and not st["uncalled_blocked"]:
                    st["uncalled_blocked"] = True
                    e.log({"type": "en_breaker", "global": True, "reversals": len(recent)})
                    e.say(f"ALERT {len(recent)} reversals in {P.en_rev_window_s / 60:.0f} min: no more leaning/open entries "
                          "tonight (remove uncalled_blocked from data/en_state.json to resume)")
        if changed:
            self.save()

    def set_hot(self, now: float):
        """Races whose polls closed in the last 3 h, or whose FV moved >= 2c in 10 min, get fast Kalshi polling."""
        e = self.e
        first, second = [], []
        for m in e.markets:
            race = m["race"]
            st = race_state(race)
            t = self.control_open if st is None else self.poll_close.get(st)
            if t is not None and t <= now <= t + 3 * 3600:
                first.append(race)
            else:
                w = self.window(m, 600)
                if w and w[1] - w[0] >= 0.02:
                    second.append(race)
        hot = list(dict.fromkeys(first + second))[:self.P.en_hot_max]
        if hasattr(e.fv, "hot"):
            e.fv.hot = set(hot)

    # ---- the cycle --------------------------------------------------------------------------------------------
    def plan(self) -> list[dict]:
        e, P = self.e, self.P
        now = e.clock()
        acts: list[dict] = []
        if not self.announced:
            self.announced = True
            e.log({"type": "en_start", "poll_close_states": len(self.poll_close), "slow": self.slow_scopes,
                   "v0": self.st["v0"], "blocked": self.st["blocked"]})
            e.say(f"ELECTION MODE from {P.en_start_et} ET: take-only vs Kalshi, caps {P.en_cap_called:.0%}/"
                  f"{P.en_cap_lean:.0%}/{P.en_cap_open:.0%} of V0 per race, leaning+open <= {P.en_cap_uncalled:.0%}; "
                  f"stop placing at {P.en_end_et} ET. Freeze new risk: create data/EN_FREEZE.")
        if not self.cancel_done:
            self.cancel_done = True
            if any(v["bid"] or v["ask"] for v in e.live.values()):
                acts.append({"op": "en_cancel_all", "prio": 0, "exchange_id": None, "cost": 1})
        if now >= self.end:
            if not self.ended:
                self.ended = True
                e.log({"type": "en_end"})
                e.say("ELECTION MODE: past the end time, placing nothing more (positions ride to settlement).")
            return acts
        v = e.value()
        if self.st["v0"] is None:
            if v is None:
                return acts
            self.st["v0"] = round(v, 2)
            self.save()
            e.log({"type": "en_v0", "v0": v})
        v0 = self.st["v0"]
        self.check_reversals(now)
        self.set_hot(now)
        self.summary(now)
        if e.balance is None or now < self.write_block:
            return acts
        frozen = self.freeze_file.exists()
        race_var, unc_used = self.risk_now()
        units = self.candidates(now)
        if units and now - self.last_cand_log >= 30:
            self.last_cand_log = now
            e.log({"type": "en_candidates", "n": len(units), "top": [
                {k: (round(u[k], 4) if isinstance(u[k], float) else u[k]) for k in ("ex", "action", "px", "qty", "tier", "f", "ret", "closing")}
                for u in sorted(units, key=lambda u: -u["score"])[:12]]})
        alloc = {"cash": e.balance - P.en_cash_buffer, "pend_race": {}, "pend_unc": 0.0, "orders": {}}
        short = self.allocate(units, alloc, race_var, unc_used, v0, frozen)
        recycles = self.recycle(short, alloc, now) if short else []
        if recycles:
            self.allocate(short, alloc, race_var, unc_used, v0, frozen)
        orders = recycles + sorted(alloc["orders"].values(), key=lambda o: -o["ret"])
        for i in range(0, len(orders), P.en_batch_max):
            acts.append({"op": "en_batch", "prio": 1, "exchange_id": None, "cost": 1, "orders": orders[i:i + P.en_batch_max]})
        return acts

    def candidates(self, now: float) -> list[dict]:
        """Every book level through the conservative FV by the tier's edge, split into the part that closes a position
        we hold (no cash needed) and the part that opens risk."""
        e, P = self.e, self.P
        out = []
        for m in e.markets:
            ex, race = m["exchange_id"], m["race"]
            if not self.eligible(race, now):
                continue
            s = e.fv.get(*e.key(m))
            if s is None or s.age_s > P.en_fv_max_age:
                continue
            slow = self.is_slow(race)
            blocked = race_key(race) in self.st["blocked"]
            x = e.pos.get(ex, 0.0)
            for action in ("buy", "sell"):
                if self.lock.get((ex, action), 0.0) > now:
                    continue
                sf = self.side_fv(m, action)
                if sf is None:
                    continue
                f, tier = sf
                if 2 * s.ref_half_spread > (P.en_called_max_spread if tier == "called" else P.fv_max_ref_spread) + 1e-9:
                    continue
                edge = self.edges[tier] * (P.en_slow_mult if slow else 1.0) + s.ref_half_spread
                levels = e.others(ex, "ask" if action == "buy" else "bid")
                closable = max(0.0, -x) if action == "buy" else max(0.0, x)
                for px, q in levels:
                    if (action == "buy" and px > f - edge + 1e-9) or (action == "sell" and px < f + edge - 1e-9):
                        break
                    cost_ps = px if action == "buy" else 1 - px
                    ret = ((f - px) if action == "buy" else (px - f)) / max(cost_ps, 0.005)
                    # caps are on value at stake (our side at Kalshi FV), the same measure risk_now() uses for what we
                    # already hold: a called take at 0.70 puts 0.99 a share at stake if the call is wrong
                    risk_ps = max(f if action == "buy" else 1 - f, 0.01)
                    u = {"m": m, "ex": ex, "race": race, "action": action, "px": px, "tier": tier, "f": f, "ret": ret,
                         "score": ret * (P.en_slow_weight if slow else 1.0), "cost_ps": cost_ps, "risk_ps": risk_ps,
                         "slow": slow, "blocked": blocked}
                    c = min(q, closable)
                    closable -= c
                    if c >= 1:
                        out.append(dict(u, qty=c, closing=True))
                    if q - c >= 1:
                        out.append(dict(u, qty=q - c, closing=False))
        return out

    def allocate(self, units, alloc, race_var, unc_used, v0, frozen) -> list[dict]:
        """Greedy by score within caps and cash; adds to alloc['orders'] (one order per contract and side: qty summed,
        limit = the worst level used). Returns the opening demand left unfilled for lack of cash (for recycling)."""
        P = self.P
        short = []
        for u in sorted(units, key=lambda u: -u["score"]):
            q = u["qty"]
            if u["closing"]:
                q = int(q)
                alloc["cash"] += q * (1 - u["cost_ps"])          # closing credits 1 per netted pair
            else:
                if frozen or u["blocked"] or (u["tier"] != "called" and self.st["uncalled_blocked"]):
                    continue
                race = u["race"]
                room = self.caps[u["tier"]] * v0 - race_var.get(race, 0.0) - alloc["pend_race"].get(race, 0.0)
                if u["tier"] != "called":
                    room = min(room, P.en_cap_uncalled * v0 - unc_used - alloc["pend_unc"])
                q = min(q, max(0.0, room) / u["risk_ps"])
                q_cash = min(q, max(0.0, alloc["cash"]) / u["cost_ps"])
                if q - q_cash >= 1:
                    short.append(dict(u, qty=q - q_cash))
                q = int(q_cash)
                if q < 1:
                    continue
                alloc["cash"] -= q * u["cost_ps"]
                alloc["pend_race"][race] = alloc["pend_race"].get(race, 0.0) + q * u["risk_ps"]
                if u["tier"] != "called":
                    alloc["pend_unc"] += q * u["risk_ps"]
            q = int(q)
            if q < 1:
                continue
            k = (u["ex"], u["action"])
            o = alloc["orders"].get(k)
            if o is None:
                o = alloc["orders"][k] = {"ex": u["ex"], "action": u["action"], "qty": 0, "px": u["px"], "tier": u["tier"],
                                          "f": round(u["f"], 4), "ret": u["ret"], "closing_only": True,
                                          "title": u["m"]["title"][9:60]}
            o["qty"] += q
            o["px"] = u["px"] if (u["action"] == "buy") == (u["px"] > o["px"]) else o["px"]   # worst level used
            o["closing_only"] &= u["closing"]
            o["kind"] = "exit" if o["closing_only"] else "take"
        return short

    def recycle(self, short, alloc, now) -> list[dict]:
        """Sell called winners we hold to fund the best takes cash could not cover. Cheapest give-up first."""
        e, P = self.e, self.P
        best = max(u["ret"] for u in short)
        need = sum(u["qty"] * u["cost_ps"] for u in short)
        opts = []
        for ex, x in e.pos.items():
            m = e.by_ex.get(ex)
            if not m or abs(x) < 1:
                continue
            action = "sell" if x > 0 else "buy"
            if (self.lock.get((ex, action), 0.0) > now or (ex, "buy") in alloc["orders"]
                    or (ex, "sell") in alloc["orders"]):        # never cross our own take (self-trade prevention)
                continue
            w = self.window(m, P.en_hold_called_s)
            s = e.fv.get(*e.key(m))
            # A market Kalshi has closed has an old fair value by design. Selling a winner that was
            # called when it closed only reduces risk, so it stays available to fund better takes.
            if w is None or s is None or (s.age_s > P.en_fv_max_age and not getattr(s, "closed", False)):
                continue
            ours = w[0] if x > 0 else 1 - w[1]                    # the worst our side has been over the window
            if ours < P.en_recycle_fv:
                continue
            levels = e.others(ex, "bid" if x > 0 else "ask")
            for px, q in levels:
                proceeds = px if x > 0 else 1 - px
                if proceeds < P.en_recycle_min_px - 1e-9:
                    break
                give = (1 - proceeds) / proceeds
                if best < give + P.en_recycle_margin:
                    break
                opts.append((give, m, ex, action, px, q, proceeds, abs(x)))
        out, left = [], {}
        for give, m, ex, action, px, q, proceeds, held in sorted(opts, key=lambda o: o[0]):
            if need <= 0:
                break
            left.setdefault(ex, held)
            n = int(min(q, left[ex], need / proceeds))
            if n < 1:
                continue
            left[ex] -= n
            need -= n * proceeds
            alloc["cash"] += n * proceeds
            o = next((r for r in out if r["ex"] == ex), None)
            if o is None:
                o = {"ex": ex, "action": action, "qty": 0, "px": px, "tier": "called", "f": None, "ret": -give,
                     "kind": "exit", "recycle": True, "title": m["title"][9:60]}
                out.append(o)
            o["qty"] += n
            o["px"] = px
        if out:
            e.log({"type": "en_recycle", "best_unfilled_ret": round(best, 4), "orders": out})
        return out

    # ---- writes ------------------------------------------------------------------------------------------------
    def execute(self, act: dict):
        e, P = self.e, self.P
        now = e.clock()
        if act["op"] == "en_cancel_all":
            if e.dry:
                e.log({"type": "dry_action", "op": "en_cancel_all"})
                return
            try:
                e.c.cancel_all()
                e.live = {ex: {"bid": [], "ask": []} for ex in e.live}
                e.log({"type": "en_cancel_all"})
            except Exception as err:
                self.cancel_done = False
                e.log({"type": "error", "op": "en_cancel_all", "error": str(err)[:300]})
            return
        orders = act["orders"]
        for o in orders:
            self.lock[(o["ex"], o["action"])] = now + P.en_lock_s
        if e.dry:
            e.log({"type": "dry_action", "op": "en_batch", "orders": orders})
            for o in orders:
                e.say(f"DRY EN {o['kind'].upper()} {o['action']} {o['qty']} @ {o['px']} ({o['tier']}, FV {o['f']}) {o['title']}")
            return
        exp = (dt.datetime.fromtimestamp(now, dt.timezone.utc) + dt.timedelta(seconds=P.en_take_ttl_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
        body = [{"exchange_id": o["ex"], "side": "yes", "action": o["action"], "quantity": int(o["qty"]), "price": o["px"],
                 "expiration": exp} for o in orders]
        try:
            res = e.c.place_batch(body, idempotency_key=str(uuid.uuid4()))
        except Exception as err:
            status = getattr(err, "status", None)
            if status == 429:
                self.write_block = now + float((getattr(err, "headers", None) or {}).get("Retry-After") or 30)
            e.force_orders = True
            e.log({"type": "error", "op": "en_batch", "error": str(err)[:500], "orders": orders})
            e.say(f"ERROR election batch ({len(orders)} orders): {str(err)[:160]}")
            return
        http = res.get("http_status")
        if http == 429:
            self.write_block = now + float(res.get("retry_after") or 30)
        brief = []
        for r in res.get("results") or []:
            i = r.get("index")
            if not isinstance(i, int) or not 0 <= i < len(orders):
                continue
            o = orders[i]
            data = r.get("data") or {}
            if r.get("ok"):
                e._register(data, o["ex"], 1 if o["action"] == "buy" else -1, o["px"], o["kind"], {"mode": "en", "tier": o["tier"]})
                traded = abs(float(data.get("quantityTraded") or 0))
                self.sent.append({"t": now, "kind": o["kind"], "qty": traded, "cost": traded * (o["px"] if o["action"] == "buy" else 1 - o["px"])})
                e.say(f"EN {o['kind'].upper()} {o['action']} {o['qty']} @ {o['px']} -> traded {traded:.0f} "
                      f"({o['tier']}, FV {o['f']}, ret {o['ret']:+.1%}) {o['title']}")
            else:
                if r.get("status") == 429:
                    self.write_block = max(self.write_block, now + 30)
                if r.get("status") == 502:
                    e.force_orders = True                         # ORDER_STATUS_UNKNOWN: reconcile before resending
                e.say(f"EN order failed ({r.get('status')}): {o['action']} {o['qty']} @ {o['px']} {o['title']}: {str(data)[:120]}")
            brief.append({"i": i, "ok": r.get("ok"), "status": r.get("status"), "traded": data.get("quantityTraded"),
                          "orderId": data.get("orderId"), "code": data.get("code")})
        e.log({"type": "en_batch", "http_status": http, "orders": orders, "results": brief})

    def summary(self, now: float):
        if now - self.last_summary < 60:
            return
        self.last_summary = now
        e = self.e
        by = {"called": 0.0, "lean": 0.0, "open": 0.0}
        for ex, x in e.pos.items():
            m = e.by_ex.get(ex)
            s = e.fv.get(*e.key(m)) if m else None
            if s is None or abs(x) < 1:
                continue
            by[self.tier_of(s.fv)] += abs(x) * (s.fv if x > 0 else 1 - s.fv)
        last = [r for r in self.sent if now - r["t"] <= 600]
        self.sent = [r for r in self.sent if now - r["t"] <= 3600]
        takes = [r for r in last if r["kind"] == "take"]
        flags = (" FROZEN" if self.freeze_file.exists() else "") + (" NO-UNCALLED" if self.st["uncalled_blocked"] else "") + \
                (f" writes paused {self.write_block - now:.0f}s" if self.write_block > now else "")
        e.say(f"EN cash {e.balance if e.balance is None else round(e.balance):,} | held: called {by['called']:,.0f} "
              f"lean {by['lean']:,.0f} open {by['open']:,.0f} | 10 min: {len(takes)} takes "
              f"{sum(r['cost'] for r in takes):,.0f}, {len(last) - len(takes)} exits/recycles | "
              f"blocked races {len(self.st['blocked'])} | hot {len(getattr(e.fv, 'hot', ()))}{flags}")
        e.log({"type": "en_summary", "cash": e.balance, "held": by, "takes_10m": len(takes), "blocked": len(self.st["blocked"])})
