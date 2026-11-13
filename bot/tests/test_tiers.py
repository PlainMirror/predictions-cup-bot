"""Oct 22 upgrades: tiered sizing (gaps >= 8c: 8k/take, 16k/race), late-window entries (big tier only, >= 24 h before
the Oct 31 liquidation, glide and time stop over the hours left), exit glide to 0%. Synthetic, no network.
Run from bot\\:  py tests\\test_tiers.py   -> ALL PASS"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # bot/
from mm.params import Params
from mm import quoter
from mm.fv import FVState
from mm.engine import MMEngine
from mm.events import Calendar, et_to_utc
from mm.exits import Lot, exit_price
import run_mm

P = Params.load(Path(__file__).resolve().parents[2] / "data" / "live_params_70k.json")
P.entry_mode = "fv"                           # model off for the test (no data load)
P.lean_party, P.lean_extra = "D", 0.02        # example lean for these checks (the published params leave the party blank)
X = run_mm.parse_exit("0.7,0.0,120,120")
T = [et_to_utc("2026-10-24T12:00").timestamp()]
ok = True


def check(name, got, want):
    global ok
    good = got == want
    ok &= good
    print(f"{'PASS' if good else 'FAIL'}  {name}: got {got}, want {want}")


class Sim:
    def __init__(self):
        self.cash, self.pos, self.books, self.nid, self.fl = 100_000.0, {}, {}, 1, []

    def tournament_info(self):
        return {"myBalance": self.cash}

    def prices(self, ids):
        return {ex: {"exchangeId": ex, "bestBid": (self.books[ex][0] or [[None]])[0][0],
                     "bestAsk": (self.books[ex][1] or [[None]])[0][0]} for ex in ids}

    def orderbook(self, ex, depth=15):
        b, a = self.books[ex]
        return {"bids": [{"price": p, "quantity": q} for p, q in b], "asks": [{"price": p, "quantity": q} for p, q in a]}

    def open_orders(self):
        return {"data": []}

    def positions(self):
        return {"positions": [{"exchangeId": ex, "quantity": q, "settled": False} for ex, q in self.pos.items() if abs(q) > 1e-9]}

    def fills(self, limit=50):
        return {"data": list(reversed(self.fl[-limit:]))}

    def place(self, ex, side, action, quantity, price, idempotency_key=None, expiration=None):
        buy = action == "buy"
        oid, self.nid = self.nid, self.nid + 1
        b, a = self.books[ex]
        left, traded = float(quantity), 0.0
        for lv in (a if buy else b):
            if left <= 0 or (buy and lv[0] > price + 1e-9) or (not buy and lv[0] < price - 1e-9):
                break
            n = min(left, lv[1])
            self.cash -= n * (lv[0] if buy else 1 - lv[0])
            self.pos[ex] = self.pos.get(ex, 0.0) + (n if buy else -n)
            self.fl.append({"id": f"f{len(self.fl)}", "orderId": oid, "exchangeId": ex, "filledAt": T[0],
                            "price": lv[0] if buy else round(1 - lv[0], 4), "side": "yes" if buy else "no",
                            "quantity": n if buy else -n})
            lv[1] -= n
            left -= n
            traded += n
        self.books[ex] = ([l for l in b if l[1] > 0], [l for l in a if l[1] > 0])
        return {"orderId": oid, "open": False, "remainingQuantity": 0, "quantityTraded": traded}

    def cancel_all(self, exchange_id=None, market_id=None):
        pass


class FV:
    def __init__(self):
        self.v = {}

    def poll(self):
        pass

    def get(self, race, party):
        f = self.v.get((race, party))
        return None if f is None else FVState(f, 0.005, 0.0, 0.004, False, True)


def mk(ex, place, party):
    return {"exchange_id": ex, "market_id": ex, "title": f"Will the {'Democratic' if party == 'D' else 'Republican'} Party win the {place} Senate?",
            "race": ("senate", place), "party": party, "quote": False}


markets = [mk("AD", "AA", "D"), mk("AR", "AA", "R"), mk("BD", "BB", "D"), mk("CD", "CC", "D")]
fv, sx = FV(), Sim()
e = MMEngine(sx, fv, P, markets, calendar=Calendar(Path("no_events.csv")), clock=lambda: T[0], sleep=lambda s: None,
             exit_params=X, books_per_cycle=10_000, log_name="t_tiers.jsonl", verbose=False)
D, R = markets[0], markets[1]
print(f"params: take_edge {P.take_edge}, big_edge {P.big_edge}, big {P.big_take_notional:,.0f}/take {P.big_risk_per_race:,.0f}/race, "
      f"normal {P.take_max_notional:,.0f}/{P.max_risk_per_race:,.0f}, lean {P.lean_party} +{P.lean_extra}, late_min_h {P.late_min_h}")


def inp(f=0.50, used=0.0):
    return quoter.Inputs(fv=f, ref_half_spread=0.005, sigma_hourly=0.004, exposure=0.0, risk_used_race=used,
                         risk_used_total=used, best_bid_others=None, best_ask_others=None)


def tk(m, asks=(), bids=(), used=0.0, big_only=False):
    out = e.fv_takes(m, inp(used=used), [list(x) for x in bids], [list(x) for x in asks], big_only=big_only)
    return [(a, px, q, e.take_tier[(m["exchange_id"], a)]) for a, px, q in out]


# ---- tier selection and sizes (FV 0.50)
check("7c gap D buy -> normal tier, 3k/take", tk(D, asks=[(0.43, 20000)]), [("buy", 0.43, int(3000 / 0.43), "std")])
check("9c gap D buy -> big tier, 8k/take", tk(D, asks=[(0.41, 30000)]), [("buy", 0.41, int(8000 / 0.41), "big")])
check("9c + 7c levels -> big sweeps only the >= 8c level", tk(D, asks=[(0.41, 5000), (0.43, 20000)]), [("buy", 0.41, 5000, "big")])
check("R buy 9c (against lean) -> normal tier (big needs 10c)", tk(R, asks=[(0.41, 30000)]), [("buy", 0.41, int(3000 / 0.41), "std")])
check("R buy 11c (against lean) -> big tier", tk(R, asks=[(0.39, 30000)]), [("buy", 0.39, int(8000 / 0.39), "big")])
check("D sell 9c (against lean) -> normal tier", tk(D, bids=[(0.59, 30000)]), [("sell", 0.59, int(3000 / 0.41), "std")])
check("race at 9k: 7c gap -> nothing (normal cap 8k)", tk(D, asks=[(0.43, 20000)], used=9000), [])
check("race at 9k: 9c gap -> big, room 7k / FV 0.5", tk(D, asks=[(0.41, 30000)], used=9000), [("buy", 0.41, 14000, "big")])
check("big_only: 7c gap -> nothing", tk(D, asks=[(0.43, 20000)], big_only=True), [])
check("big_only: 9c gap -> big", tk(D, asks=[(0.41, 30000)], big_only=True), [("buy", 0.41, int(8000 / 0.41), "big")])

# ---- late window through the engine's own cycle
fv.v = {(("senate", "BB"), "D"): 0.50, (("senate", "CC"), "D"): 0.50, (("senate", "AA"), "D"): 0.50, (("senate", "AA"), "R"): 0.50}


def cycle_at(s, books):
    T[0] = et_to_utc(s).timestamp()
    sx.books = {"AD": ([[0.45, 100]], [[0.55, 100]]), "AR": ([[0.45, 100]], [[0.55, 100]]), **books}
    n0 = sum(1 for _ in open(e.log_path))
    for _ in range(3):
        e.step()
        T[0] += 5
    import json
    return [json.loads(l) for i, l in enumerate(open(e.log_path)) if i >= n0 and '"type": "take"' in l[:20]]


L = cycle_at("2026-10-25T12:00", {"BD": ([[0.40, 100]], [[0.43, 5000]]), "CD": ([[0.40, 100]], [[0.60, 100]])})
check("Oct 25 (150 h left): 7c gap taken, normal tier", [(r["exchange_id"], r["tier"], r["late"]) for r in L][:1], [("BD", "std", False)])
L = cycle_at("2026-10-27T12:00", {"BD": ([[0.40, 100]], [[0.43, 5000]]), "CD": ([[0.40, 100]], [[0.41, 5000]])})
check("Oct 27 (102 h left): 7c gap NOT taken, 9c gap taken as late big", [(r["exchange_id"], r["tier"], r["late"]) for r in L], [("CD", "big", True)])
lot = [l for l in e.lots.get("CD", [])][-1]
check("late lot horizon = hours to Oct 31 18:00 (~102 h)", round(lot.horizon_h), 102)
check("early lot keeps the full horizon (None)", [l.horizon_h for l in e.lots.get("BD", [])][:1], [None])
L = cycle_at("2026-10-30T19:00", {"BD": ([[0.40, 100]], [[0.40, 5000]]), "CD": ([[0.40, 100]], [[0.38, 5000]])})
check("Oct 30 19:00 (23 h left): no entries even at 10-12c", L, [])

# ---- exits: glide to 0%, late-lot horizon
t0 = 1_000_000.0
nl = Lot("x", 1, 100, 0.40, 0.50, t0)
late = Lot("x", 1, 100, 0.40, 0.50, t0, horizon_h=48)
h = lambda hrs: t0 + hrs * 3600
check("normal lot at 60 h: target 35% of gap (0.7 -> 0.0 over 120 h)", exit_price(nl, 0.50, h(60), X)[0], 0.435)
check("normal lot at 119 h: target ~ entry + 0.5c", exit_price(nl, 0.50, h(119), X)[0], 0.405)
check("normal lot at 120 h: time stop (market)", exit_price(nl, 0.50, h(120), X)[0], None)
check("late lot (48 h horizon) at 24 h: 35% of gap", exit_price(late, 0.50, h(24), X)[0], 0.435)
check("late lot at 48 h: time stop at the horizon", exit_price(late, 0.50, h(48), X)[0], None)
check("model lots unaffected by horizon", exit_price(Lot("x", 1, 100, 0.40, 0.5, t0, mode="model", tp=0.43, hold_h=24), 0.5, h(10), X)[0], 0.43)
print("ALL PASS" if ok else "SOME FAILED")
e.shutdown()
