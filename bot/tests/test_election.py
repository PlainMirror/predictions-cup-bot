"""Election-night take mode (mm/election.py) on a synthetic night: simulated exchange, scripted Kalshi, fake clock.
No network, no DB. Run from bot\\:  py tests\\test_election.py   -> ALL PASS

Script (ET, Nov 3-4 2026; V0 = 3k cash + 39.6k of RI Gov D held from the Nov 1-3 book):
  FL Sen   Kalshi D 0.55 -> 0.99 at 20:01 (polls close 20:00). Stale D asks 0.60/0.62/0.70, R bids 0.40/0.38.
  PA-07    Kalshi D 0.50 -> 0.80 at 20:30 (open tier). Stale D asks 0.55 (3k) / 0.60 (20k). Cap 5% of V0.
  TX Sen   Kalshi D 0.40 -> 0.99 at 20:30, polls close 21:00: nothing before 21:00.
  AK Gov   slow race (edge x2): Kalshi D 0.90 from 01:00; D ask 0.86 (needs 12.5c) -> no take; 0.70 -> take.
  NH Gov   Kalshi D 0.99 from 22:50; a stale 0.90 ask at 23:00 meets a 429 first, then goes through.
  RI Gov D held +40,000 (Kalshi 0.99, bids 0.97): recycled when cash runs out.
  22:00 FL D falls to 0.80 -> reversal breaker blocks FL; EN_FREEZE 22:30-23:00; FL settles 01:00.
Caps are on value at stake (our side at Kalshi FV): FL and TX can take only 30% of V0 each, PA-07 5%.
"""
import sys
import json
import tempfile
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # bot/
from mm.params import Params
from mm.fv import FVState, KalshiFV
from mm.engine import MMEngine
from mm.election import ElectionNight
from mm.events import et_to_utc
import run_mm

T = [et_to_utc("2026-11-03T16:50").timestamp()]
clock = lambda: T[0]
at = lambda s: et_to_utc(s).timestamp()


class SimExchange:
    """Platform rules: buys need cash (clipped, counted in self.clipped), opposite-side buys net and credit 1 per pair."""
    def __init__(self, cash):
        self.cash, self.pos, self.books, self.fills_log = cash, {}, {}, []
        self.settled, self.next_id, self.batches, self.clipped, self.fail_next_batch = {}, 1, [], 0, None

    def tournament_info(self):
        return {"myBalance": self.cash}

    def prices(self, ids):
        out = {}
        for ex in ids:
            b, a = self.books.get(ex, ([], []))
            out[ex] = {"exchangeId": ex, "bestBid": b[0][0] if b else None, "bestAsk": a[0][0] if a else None}
        return out

    def orderbook(self, ex, depth=15):
        b, a = self.books.get(ex, ([], []))
        return {"bids": [{"price": p, "quantity": q} for p, q in b[:depth]], "asks": [{"price": p, "quantity": q} for p, q in a[:depth]]}

    def open_orders(self):
        return {"data": []}

    def positions(self):
        out = [{"exchangeId": ex, "quantity": q, "settled": False} for ex, q in self.pos.items() if abs(q) > 1e-9]
        out += [{"exchangeId": ex, "quantity": q, "settled": True} for ex, q in self.settled.items()]
        return {"positions": out}

    def fills(self, limit=50):
        return {"data": list(reversed(self.fills_log[-limit:]))}

    def place(self, ex, side, action, quantity, price, idempotency_key=None, expiration=None):
        assert side == "yes"
        buy = action == "buy"
        b, a = self.books.get(ex, ([], []))
        levels = a if buy else b
        oid, self.next_id = self.next_id, self.next_id + 1
        left, traded, cost = float(quantity), 0.0, 0.0
        for lv in levels:
            px, q = lv
            if left <= 0 or (buy and px > price + 1e-9) or (not buy and px < price - 1e-9):
                break
            n = min(left, q)
            x = self.pos.get(ex, 0.0)
            close = min(n, max(0.0, -x)) if buy else min(n, max(0.0, x))
            open_cost_ps = px if buy else 1 - px
            afford = (self.cash + close * (1 - open_cost_ps)) / open_cost_ps + close
            if n > afford + 1e-9:
                self.clipped += 1
                n = max(0.0, int(afford))
            if n <= 0:
                break
            close = min(n, close)
            self.cash += close * (1 - open_cost_ps) - (n - close) * open_cost_ps
            self.pos[ex] = x + (n if buy else -n)
            lv[1] -= n
            left -= n
            traded += n
            cost += n * px
            self.fills_log.append({"id": f"f{len(self.fills_log) + 1}", "orderId": oid, "exchangeId": ex,
                                   "price": px if buy else round(1 - px, 4), "side": "yes" if buy else "no",
                                   "quantity": n if buy else -n})
        self.books[ex] = ([l for l in b if l[1] > 0.5], [l for l in a if l[1] > 0.5])
        return {"orderId": oid, "open": False, "remainingQuantity": 0, "quantityTraded": traded, "totalCost": cost}

    def place_batch(self, orders, idempotency_key=None, retries=0):
        self.batches.append((T[0], orders))
        if self.fail_next_batch:
            code, self.fail_next_batch = self.fail_next_batch, None
            return {"results": [{"index": i, "ok": False, "status": code, "data": {"code": "RATE_LIMITED", "error": "x"}}
                                for i in range(len(orders))], "http_status": code, "retry_after": "20"}
        return {"results": [{"index": i, "ok": True, "status": 200,
                             "data": self.place(o["exchange_id"], o["side"], o["action"], o["quantity"], o["price"])}
                            for i, o in enumerate(orders)]}

    def cancel_all(self, exchange_id=None, market_id=None):
        return {"cancelled": 0}

    def settle(self, ex, yes_won: bool):
        q = self.pos.pop(ex, 0.0)
        self.cash += q if (q > 0 and yes_won) else (-q if (q < 0 and not yes_won) else 0.0)
        self.settled[ex] = q


class FakeFV:
    """Kalshi FV from a scripted path; history appended at each poll like KalshiFV (window() is KalshiFV's own)."""
    window = KalshiFV.window

    def __init__(self, path):
        self.path, self.hist, self.hot, self.clock = path, {}, set(), clock

    def poll(self):
        for key in self.path:
            self.hist.setdefault(key, deque(maxlen=5000)).append((T[0], self.path[key](T[0])))

    def get(self, race, party):
        h = self.hist.get((race, party))
        if not h:
            return None
        return FVState(fv=h[-1][1], ref_half_spread=0.005, age_s=0.0, sigma_hourly=0.004, jumping=False, ref_spread_ok=True)


def mk(ex, office, place, party):
    return {"exchange_id": ex, "market_id": ex, "title": f"Will the {'Democratic' if party == 'D' else 'Republican'} Party win the {place} {office}?",
            "race": (office, place), "party": party, "quote": False}


markets = [mk("FLD", "senate", "FL", "D"), mk("FLR", "senate", "FL", "R"), mk("PAD", "house", "PA-07", "D"),
           mk("TXD", "senate", "TX", "D"), mk("AKD", "governor", "AK", "D"), mk("RID", "governor", "RI", "D"),
           mk("NHD", "governor", "NH", "D")]


def step_path(t_switch, before, after):
    return lambda t: after if t >= t_switch else before


fl_d = lambda t: 0.80 if t >= at("2026-11-03T22:00") else (0.99 if t >= at("2026-11-03T20:01") else 0.55)
path = {
    (("senate", "FL"), "D"): fl_d,
    (("senate", "FL"), "R"): lambda t: 1 - fl_d(t),
    (("house", "PA-07"), "D"): step_path(at("2026-11-03T20:30"), 0.50, 0.80),
    (("senate", "TX"), "D"): step_path(at("2026-11-03T20:30"), 0.40, 0.99),
    (("governor", "AK"), "D"): step_path(at("2026-11-04T01:00"), 0.60, 0.90),
    (("governor", "RI"), "D"): lambda t: 0.99,
    (("governor", "NH"), "D"): step_path(at("2026-11-03T22:50"), 0.60, 0.99),
}
fv = FakeFV(path)
sx = SimExchange(cash=3_000.0)
sx.books = {"FLD": ([[0.54, 2000]], [[0.60, 5000], [0.62, 5000], [0.70, 10000]]),
            "FLR": ([[0.40, 5000], [0.38, 5000]], [[0.46, 2000]]),
            "PAD": ([[0.45, 1000]], [[0.55, 3000], [0.60, 20000]]),
            "TXD": ([[0.39, 1000]], [[0.45, 30000]]),
            "AKD": ([[0.55, 1000]], [[0.86, 5000]]),
            "RID": ([[0.97, 50000], [0.965, 50000]], [[0.99, 1000]]),
            "NHD": ([[0.55, 1000]], [[0.99, 1000]])}
sx.pos["RID"] = 40000.0
P = Params()
P.entry_mode, P.election_mode, P.take_enabled = "none", True, True
tmp = Path(tempfile.mkdtemp())
e = MMEngine(sx, fv, P, markets, clock=clock, sleep=lambda s: None, exit_params=run_mm.parse_exit("0.7,0.3,120,120"),
             books_per_cycle=10_000, log_name="t_election.jsonl", verbose=False)
e.en = ElectionNight(e, poll_close={"FL": at("2026-11-03T20:00"), "PA": at("2026-11-03T20:00"), "TX": at("2026-11-03T21:00"),
                                     "AK": at("2026-11-04T01:00"), "RI": at("2026-11-03T20:00"), "NH": at("2026-11-03T20:00")},
                     slow=["AK"], state_file=tmp / "en_state.json", freeze_file=tmp / "EN_FREEZE")
ok = True


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {name}{('  ' + str(detail)) if detail else ''}")


def run_until(s, dt=10.0, hook=None):
    end = at(s)
    while T[0] < end:
        fv.poll()
        if hook:
            hook()
        e.step()
        T[0] += dt


run_until("2026-11-03T19:59")
check("nothing traded before polls close", not sx.batches, sx.batches[:1])
check("V0 recorded at 17:00", e.en.st["v0"] is not None and abs(e.en.st["v0"] - (3_000 + 40_000 * 0.99)) < 1, e.en.st["v0"])
v0 = e.en.st["v0"]

run_until("2026-11-03T20:01:50")
check("FL called at 20:01: no take before the 60 s hold", not any(o["exchange_id"].startswith("FL") for _, b in sx.batches for o in b))
cash_before = sx.cash
run_until("2026-11-03T20:04")
fl = [o for t, b in sx.batches for o in b if o["exchange_id"] in ("FLD", "FLR")]
check("FL takes after the hold (buy D asks, sell R bids)", {o["exchange_id"] for o in fl} == {"FLD", "FLR"}, fl)
fl_risk = sx.pos.get("FLD", 0) * 0.99 + (-sx.pos.get("FLR", 0)) * 0.99
check("FL value at stake fills the 30% of V0 cap and no more", 0.28 * v0 <= fl_risk <= 0.30 * v0 + 1, (round(fl_risk), round(0.30 * v0)))
check("FL: best returns first (D 0.60 and R 0.40 filled, D 0.70 at 41% untouched)",
      sx.books["FLD"][1][-1] == [0.70, 10000] and sx.pos.get("FLD", 0) >= 5000 and sx.pos.get("FLR", 0) <= -4990,
      (sx.books["FLD"][1], sx.pos.get("FLD"), sx.pos.get("FLR")))
rec = [o for t, b in sx.batches for o in b if o["exchange_id"] == "RID"]
check("cash ran out -> RI Gov D winner recycled at >= 0.96", rec and all(o["action"] == "sell" and o["price"] >= 0.96 for o in rec), rec[:2])
first = next(b for t, b in sx.batches if any(o["exchange_id"] == "RID" for o in b))
check("recycle orders go first in their batch", first[0]["exchange_id"] == "RID", [o["exchange_id"] for o in first])
check("no order ever clipped for cash (allocator respects cash)", sx.clipped == 0, sx.clipped)
check("one write per cycle at most (batches of <= 50)", all(len(b) <= 50 for _, b in sx.batches))

run_until("2026-11-03T20:29:50")
check("TX called at 20:30 but polls close 21:00", True)
run_until("2026-11-03T20:35")
pa = sx.pos.get("PAD", 0)
check("PA-07 open tier: buys only after the 120 s hold, capped at 5% of V0", 0 < pa * 0.80 <= 0.05 * v0 + 1, (pa, round(pa * 0.80), round(0.05 * v0)))
pa_batches = [t for t, b in sx.batches for o in b if o["exchange_id"] == "PAD"]
check("PA-07 first order >= 120 s after the move", pa_batches and pa_batches[0] >= at("2026-11-03T20:32"), pa_batches[:1])
check("TX nothing before 21:00", sx.pos.get("TXD", 0) == 0)
run_until("2026-11-03T21:03")
check("TX traded after 21:00 (called), within its 30% cap", 0 < sx.pos.get("TXD", 0) * 0.99 <= 0.30 * v0 + 1, sx.pos.get("TXD"))

# reversal: FL D falls to 0.80 at 22:00 -> blocked after the 60 s hold; fresh FL asks must not be taken
run_until("2026-11-03T22:02")
check("reversal breaker blocked FL", "senate:FL" in e.en.st["blocked"], e.en.st["blocked"])
sx.books["FLD"] = ([[0.50, 1000]], [[0.60, 5000]])
n0 = len(sx.batches)
run_until("2026-11-03T22:30")
check("no new FL entries after the block", not any(o["exchange_id"] == "FLD" and o["action"] == "buy" for _, b in sx.batches[n0:] for o in b))

# freeze: a stale NH ask (called at 22:50) during EN_FREEZE must not be taken
(tmp / "EN_FREEZE").write_text("x")
sx.books["NHD"] = ([[0.55, 1000]], [[0.90, 1000]])
run_until("2026-11-03T23:00")
check("EN_FREEZE: no new risk", sx.pos.get("NHD", 0) == 0, sx.pos.get("NHD"))
(tmp / "EN_FREEZE").unlink()

# 429 on the next batch -> writes paused 20 s (Retry-After), then the NH take goes through
sx.fail_next_batch = 429
nb = len(sx.batches)
run_until("2026-11-03T23:00:10")
check("429: batch failed, writes paused", len(sx.batches) == nb + 1 and e.en.write_block > T[0], round(e.en.write_block - T[0]))
run_until("2026-11-03T23:01")
check("after the pause the NH take is sent again and fills", sx.pos.get("NHD", 0) == 1000, sx.pos.get("NHD"))

# settlement of FL at 01:00: lots closed, no loss stop (mode keeps running)
run_until("2026-11-04T00:59")
sx.settle("FLD", True)
sx.settle("FLR", False)
stopped = False
try:
    run_until("2026-11-04T01:01")
except KeyboardInterrupt:
    stopped = True
check("settlement: no loss stop", not stopped)
L = [json.loads(l) for l in open(e.log_path)]
check("settlement logged as settle_close", any(r["type"] == "settle_close" for r in L))

# slow race AK (edge 3c x2 + spread): ask 0.86 vs FV 0.90 is not enough; 0.70 is (leaning tier, 6c x2 + 0.5c)
run_until("2026-11-04T01:05")
check("slow AK: 4c gap not taken (needs 12.5c)", sx.pos.get("AKD", 0) == 0, sx.pos.get("AKD"))
sx.books["AKD"] = ([[0.55, 1000]], [[0.70, 2000]])
run_until("2026-11-04T01:06")
check("slow AK: 20c gap taken", sx.pos.get("AKD", 0) > 0, sx.pos.get("AKD"))

# end time: nothing after 11:55
sx.books["TXD"] = ([[0.39, 1000]], [[0.60, 5000]])
run_until("2026-11-04T11:54:50", dt=30)
nb = len(sx.batches)
sx.books["PAD"] = ([[0.45, 1000]], [[0.50, 5000]])
run_until("2026-11-04T12:05")
check("nothing placed after 11:55", len(sx.batches) == nb, sx.batches[nb:][:1])
check("never clipped for cash over the night", sx.clipped == 0, sx.clipped)
check("cash never negative", sx.cash >= -1e-6, round(sx.cash, 2))
types = {}
for r in L:
    types[r["type"]] = types.get(r["type"], 0) + 1
print("log events:", {k: v for k, v in types.items() if k.startswith(("en_", "settle", "error", "loss"))})
print("final: cash", round(sx.cash), "positions", {k: round(v) for k, v in sx.pos.items()})
print("ALL PASS" if ok else "SOME FAILED")
e.shutdown()
