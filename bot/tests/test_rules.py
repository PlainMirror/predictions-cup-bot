"""Entry rules Oct 16: entry price band [0.10, 0.90] and a party lean (+2c edge to bet against the lean party).
Synthetic books, no network."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # bot/
from mm.params import Params
from mm import quoter
from mm.engine import MMEngine
from mm.shadow import ShadowExchange
import run_mm

P = Params.load(Path(__file__).resolve().parents[2] / "data" / "live_params_70k.json")
P.lean_party, P.lean_extra = "D", 0.02  # example lean for these checks (the published params leave the party blank)
mk = [{"exchange_id": "D1", "title": "Will the Democratic Party win the X Senate?", "race": ("senate", "XX"), "party": "D", "quote": False},
      {"exchange_id": "R1", "title": "Will the Republican Party win the X Senate?", "race": ("senate", "XX"), "party": "R", "quote": False},
      {"exchange_id": "I1", "title": "Will an independent win the X Senate?", "race": ("senate", "XX"), "party": "I", "quote": False}]


class NoFV:
    def get(self, *a): return None
    def poll(self): pass


# the shadow exchange opens the collector DB; these checks only use synthetic books, so an empty file is enough
import sqlite3, tempfile
EMPTY_DB = Path(tempfile.mkdtemp()) / "empty.sqlite"
sqlite3.connect(EMPTY_DB).close()
e = MMEngine(ShadowExchange(db_path=EMPTY_DB), NoFV(), P, mk, exit_params=run_mm.parse_exit("0.7,0.3,120,120"), log_name="t_rules.jsonl", verbose=False)
D, R, I = mk
ok = True


def check(name, got, want):
    global ok
    good = got == want
    ok &= good
    print(f"{'PASS' if good else 'FAIL'}  {name}: got {got}, want {want}")


check("lean: buy D", e.against_lean(D, "buy"), False)
check("lean: sell D", e.against_lean(D, "sell"), True)
check("lean: buy R", e.against_lean(R, "buy"), True)
check("lean: sell R", e.against_lean(R, "sell"), False)
check("lean: buy independent", e.against_lean(I, "buy"), False)


def inp(fv):
    return quoter.Inputs(fv=fv, ref_half_spread=0.005, sigma_hourly=0.004, exposure=0.0, risk_used_race=0.0,
                         risk_used_total=0.0, best_bid_others=None, best_ask_others=None)


act = lambda tks: [(a, px) for a, px, _ in tks]
# fv takes (take_edge 0.06; against the lean needs 0.08)
check("fv: buy D 7c gap (pro-lean)", act(e.fv_takes(D, inp(0.50), [], [(0.43, 1000)])), [("buy", 0.43)])
check("fv: buy R 7c gap (against lean) blocked", act(e.fv_takes(R, inp(0.50), [], [(0.43, 1000)])), [])
check("fv: buy R 9c gap allowed", act(e.fv_takes(R, inp(0.50), [], [(0.41, 1000)])), [("buy", 0.41)])
check("fv: buy R sweep stops at 8c", act(e.fv_takes(R, inp(0.50), [], [(0.41, 1000), (0.43, 1000)])), [("buy", 0.41)])
check("fv: sell D 7c gap (against lean) blocked", act(e.fv_takes(D, inp(0.50), [(0.57, 1000)], [])), [])
check("fv: sell D 9c gap allowed", act(e.fv_takes(D, inp(0.50), [(0.59, 1000)], [])), [("sell", 0.59)])
check("fv: sell R 7c gap (pro-lean)", act(e.fv_takes(R, inp(0.50), [(0.57, 1000)], [])), [("sell", 0.57)])
check("band: buy D at 0.09 blocked", act(e.fv_takes(D, inp(0.16), [], [(0.09, 1000)])), [])
check("band: sweep 0.095 -> 0.10 blocked", act(e.fv_takes(D, inp(0.17), [], [(0.095, 100), (0.10, 1000)])), [])
check("band: buy D at 0.10 ok", act(e.fv_takes(D, inp(0.17), [], [(0.10, 1000)])), [("buy", 0.10)])
check("band: sell R at 0.12 ok (pro-lean)", act(e.fv_takes(R, inp(0.05), [(0.12, 1000)], [])), [("sell", 0.12)])
check("band: sell R at 0.085 blocked", act(e.fv_takes(R, inp(0.02), [(0.085, 1000)], [])), [])
check("band: buy D at 0.91 blocked", act(e.fv_takes(D, inp(0.98), [], [(0.91, 1000)])), [])


class Stub:
    def __init__(self, pred, ref): self.p, self.r = pred, ref
    def pred(self, ex): return self.p
    def ref(self, ex): return self.r


def model(m, pred, ref, bids=(), asks=()):
    e.model = Stub(pred, ref)
    t = e.model_take(m, inp(0.5), list(bids), list(asks))
    return None if t is None else t[:2]


check("model: buy R +3c (against lean, needs 4c)", model(R, 0.03, 0.40, asks=[(0.40, 500)]), None)
check("model: buy R +5c", model(R, 0.05, 0.40, asks=[(0.40, 500)]), ("buy", 0.40))
check("model: buy D +3c (pro-lean)", model(D, 0.03, 0.40, asks=[(0.40, 500)]), ("buy", 0.40))
check("model: sell D -3c (against lean)", model(D, -0.03, 0.40, bids=[(0.40, 500)]), None)
check("model: sell R -3c (pro-lean)", model(R, -0.03, 0.40, bids=[(0.40, 500)]), ("sell", 0.40))
check("model band: sell R at 0.08 blocked", model(R, -0.05, 0.08, bids=[(0.08, 500)]), None)
check("model band: buy D at 0.93 blocked", model(D, 0.05, 0.93, asks=[(0.93, 500)]), None)
print("ALL PASS" if ok else "SOME FAILED")
e.shutdown()
