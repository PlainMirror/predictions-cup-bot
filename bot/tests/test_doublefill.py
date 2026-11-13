"""Oct 21 live: a resting exit (AK Gov D +4,105) filled; the positions read showed it at once, the fills feed 6-8 s later,
and the engine subtracted it a second time (D -4,105 on paper). The phantom hedge let a second AK Gov R short through
the 8k race cap (10.1k at stake). Synthetic exchange (positions immediate, fills 6 s late), fake clock, no network.
Run from bot\\:  py tests\\test_doublefill.py        (add "old" to replay without the fix: the cap is breached)"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # bot/
from mm.params import Params
from mm.fv import FVState
from mm.engine import MMEngine
from mm.events import Calendar, et_to_utc
from mm.exits import Lot
import run_mm

old = len(sys.argv) > 1 and sys.argv[1] == "old"
T = [et_to_utc("2026-10-24T12:00").timestamp()]
LAG = 6.0


class LagExchange:
    def __init__(self):
        self.cash, self.pos, self.books, self.orders, self.fills_log, self.nid = 50_000.0, {}, {}, {}, [], 1

    def tournament_info(self):
        return {"myBalance": self.cash}

    def prices(self, ids):
        return {ex: {"exchangeId": ex, "bestBid": (self.books[ex][0] or [[None]])[0][0],
                     "bestAsk": (self.books[ex][1] or [[None]])[0][0]} for ex in ids}

    def orderbook(self, ex, depth=15):
        b, a = self.books[ex]
        return {"bids": [{"price": p, "quantity": q} for p, q in b], "asks": [{"price": p, "quantity": q} for p, q in a]}

    def open_orders(self):
        return {"data": [{"id": oid, "exchangeId": o["ex"], "side": "yes", "action": "buy" if o["bid"] else "sell",
                          "priceLimit": o["px"], "quantity": o["qty"], "open": True} for oid, o in self.orders.items()]}

    def positions(self):                       # immediate (the platform's positions read)
        return {"positions": [{"exchangeId": ex, "quantity": q, "settled": False} for ex, q in self.pos.items() if abs(q) > 1e-9]}

    def fills(self, limit=50):                 # LAG seconds behind
        return {"data": [f for f in reversed(self.fills_log) if f["filledAt"] <= T[0] - LAG][:limit]}

    def _fill(self, oid, ex, buy, n, px):
        x = self.pos.get(ex, 0.0)
        close = min(n, max(0.0, -x)) if buy else min(n, max(0.0, x))
        c = px if buy else 1 - px
        self.cash += close * (1 - c) - (n - close) * c
        self.pos[ex] = x + (n if buy else -n)
        self.fills_log.append({"id": f"f{len(self.fills_log)}", "orderId": oid, "exchangeId": ex, "filledAt": T[0],
                               "price": px if buy else round(1 - px, 4), "side": "yes" if buy else "no",
                               "quantity": n if buy else -n})

    def place(self, ex, side, action, quantity, price, idempotency_key=None, expiration=None):
        buy = action == "buy"
        oid, self.nid = self.nid, self.nid + 1
        b, a = self.books[ex]
        left, traded = float(quantity), 0.0
        for lv in (a if buy else b):
            if left <= 0 or (buy and lv[0] > price + 1e-9) or (not buy and lv[0] < price - 1e-9):
                break
            n = min(left, lv[1])
            self._fill(oid, ex, buy, n, lv[0])
            lv[1] -= n
            left -= n
            traded += n
        self.books[ex] = ([l for l in b if l[1] > 0], [l for l in a if l[1] > 0])
        if left > 0 and expiration and (buy and (not a or a[0][0] > price) or not buy and (not b or b[0][0] < price)):
            self.orders[oid] = {"ex": ex, "bid": buy, "px": price, "qty": left}
        return {"orderId": oid, "open": oid in self.orders, "remainingQuantity": left if oid in self.orders else 0,
                "quantityTraded": traded}

    def place_multi_leg(self, legs, idempotency_key=None, relationship_id=None):
        return {"results": [{"data": self.place(l["exchange_id"], l["side"], l["action"], l["quantity"], l.get("price"),
                                                expiration=l.get("expiration"))} for l in legs]}

    def cancel(self, oid):
        self.orders.pop(oid, None)

    def cancel_all(self, exchange_id=None, market_id=None):
        self.orders = {k: o for k, o in self.orders.items() if exchange_id and o["ex"] != exchange_id}

    def lift(self, ex):
        """Someone takes our whole resting order on ex: positions see it now, fills in LAG seconds."""
        for oid, o in list(self.orders.items()):
            if o["ex"] == ex:
                self._fill(oid, ex, o["bid"], o["qty"], o["px"])
                del self.orders[oid]
                return o
        return None


class FV:
    vals = {(("governor", "XX"), "D"): 0.817, (("governor", "XX"), "R"): 0.183}

    def poll(self):
        pass

    def get(self, race, party):
        return FVState(self.vals[(race, party)], 0.005, 0.0, 0.004, False, True)


mk = [{"exchange_id": "D", "market_id": "D", "title": "Will the Democratic Party win the XX Governor?", "race": ("governor", "XX"),
       "party": "D", "quote": False},
      {"exchange_id": "R", "market_id": "R", "title": "Will the Republican Party win the XX Governor?", "race": ("governor", "XX"),
       "party": "R", "quote": False}]
P = Params()
P.entry_mode, P.take_edge, P.max_risk_per_race, P.max_total_risk, P.positions_every = "fv", 0.06, 8000.0, 70000.0, 2
sx = LagExchange()
sx.books = {"D": ([[0.70, 1000]], [[0.80, 5000]]), "R": ([[0.245, 20000]], [[0.26, 1000]])}
sx.pos = {"D": 4105.0, "R": -5939.0}
e = MMEngine(sx, FV(), P, mk, calendar=Calendar(Path("no_events.csv")), clock=lambda: T[0], sleep=lambda s: None,
             exit_params=run_mm.parse_exit("0.7,0.3,120,120"), books_per_cycle=10_000, log_name="t_doublefill.jsonl",
             verbose=False)
e.lots = {"D": [Lot("D", 1, 4105.0, 0.725, 0.80, T[0] - 3600)], "R": [Lot("R", -1, 5939.0, 0.272, 0.20, T[0] - 3600)]}
if old:                                   # replay without the fix: every resting fill counts again
    e.refresh_positions_orig = e.refresh_positions
    def rp():
        e.refresh_positions_orig()
        e.pos_read_t = -1e18
    e.refresh_positions = rp
risk = lambda: abs(sx.pos.get("R", 0) - sx.pos.get("D", 0)) * (1 - 0.183)
min_d = []
for i in range(30):
    if i == 6:
        o = sx.lift("D")
        print(f"cycle {i}: D exit lifted: {o}")
    e.step()
    min_d.append(e.pos.get("D", 0.0))
    T[0] += 3.0
print(f"R {sx.pos.get('R'):+.0f}, D {sx.pos.get('D'):+.0f}; race risk {risk():,.0f} (cap 8,000); engine's lowest D {min(min_d):+.0f}")
ok = risk() <= 8000 + 50 and min(min_d) >= -1
print("PASS" if ok else "FAIL", "(old engine expected to FAIL)" if old else "")
e.shutdown()
