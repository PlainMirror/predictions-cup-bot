"""A Kalshi market that closes is not a reversal. No network, no DB.
Run from bot\\:  py tests\\test_closed_market.py   -> ALL PASS

What happened on Nov 3-4 2026: when Kalshi closed a decided race to settle it, the quote became
bid 0.00 / ask 1.00 with no size. The bot read the mid of that, 0.50, as the fair value. A race we
held at 0.998 "fell" to 0.50, the reversal breaker blocked it, and after three of those it stopped
all leaning and open entries. 16 decided races were blocked this way.

The Virginia Senate quotes below are the ones recorded that night (00:03-00:05 UTC, Nov 4).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # bot/
import kalshi
from mm.params import Params
from mm import fv as fvmod
from mm.election import ElectionNight

T = [1_793_750_000.0]
clock = lambda: T[0]
RACE_VA, RACE_FL = ("senate", "VA"), ("senate", "FL")
REF = [{"office": "senate", "place": "VA", "party": "D", "ticker": "SENATEVA-26-D"},
       {"office": "senate", "place": "VA", "party": "R", "ticker": "SENATEVA-26-R"},
       {"office": "senate", "place": "FL", "party": "D", "ticker": "SENATEFL-26-D"},
       {"office": "senate", "place": "FL", "party": "R", "ticker": "SENATEFL-26-R"}]


def q(ticker, bid, ask, bid_size=100.0, ask_size=100.0):
    return {"ticker": ticker, "yes_bid_dollars": bid, "yes_ask_dollars": ask,
            "yes_bid_size_fp": bid_size, "yes_ask_size_fp": ask_size}


class ScriptedFV(fvmod.KalshiFV):
    """KalshiFV with the HTTP call replaced by a scripted set of quotes. Everything else is the real class."""

    def __init__(self, params):
        real = kalshi.fetch_reference
        kalshi.fetch_reference = lambda *a, **k: REF
        try:
            super().__init__(params, clock=clock)
        finally:
            kalshi.fetch_reference = real
        self.now_quotes = {}

    def _fetch(self, chunk):
        for t in chunk:
            m = self.now_quotes.get(t)
            if m is None:
                continue
            self.quotes[t] = m
            if fvmod.empty_book(m):
                self.closed.add(t)
                continue
            self.closed.discard(t)
            self.quote_t[t] = clock()
        return True


class StubEngine:
    """The parts of the engine that check_reversals touches."""

    def __init__(self, P, fv, pos):
        self.P, self.fv, self.pos, self.logs = P, fv, pos, []
        self.by_ex = {"VAR": {"race": RACE_VA, "party": "R", "title": "Will the Republican Party win the VA senate?"},
                      "FLD": {"race": RACE_FL, "party": "D", "title": "Will the Democratic Party win the FL senate?"}}

    def key(self, m):
        return m["race"], m["party"]

    def log(self, rec):
        self.logs.append(rec)

    def say(self, msg):
        pass


ok = True


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {name}{('  ' + str(detail)) if detail else ''}")


def run(fv, en, secs, step=10.0):
    end = T[0] + secs
    while T[0] < end:
        fv.poll()
        en.check_reversals(T[0])
        T[0] += step


P = Params()
fv = ScriptedFV(P)
fv.track([(RACE_VA, "D"), (RACE_VA, "R"), (RACE_FL, "D"), (RACE_FL, "R")])
# short Virginia R (we sold the loser) and long Florida D
eng = StubEngine(P, fv, {"VAR": -31758.0, "FLD": 5000.0})
en = ElectionNight(eng, poll_close={}, slow=[], state_file=None)

# ---- both races called for our side
fv.now_quotes = {"SENATEVA-26-D": q("SENATEVA-26-D", 0.998, 0.999, 33.0, 9696.9),
                 "SENATEVA-26-R": q("SENATEVA-26-R", 0.001, 0.003, 39423.12, 2956.37),
                 "SENATEFL-26-D": q("SENATEFL-26-D", 0.985, 0.990), "SENATEFL-26-R": q("SENATEFL-26-R", 0.010, 0.015)}
run(fv, en, 300)
s = fv.get(RACE_VA, "R")
check("before the close: VA R fair value is near 0", s.fv < 0.01 and not s.closed, round(s.fv, 4))
check("before the close: nothing blocked", not en.st["blocked"], en.st["blocked"])
fv_before = s.fv

# ---- Kalshi closes Virginia: the recorded quote, bid 0 / ask 1 / no size
fv.now_quotes["SENATEVA-26-D"] = q("SENATEVA-26-D", 0.0, 1.0, 0.0, 0.0)
fv.now_quotes["SENATEVA-26-R"] = q("SENATEVA-26-R", 0.0, 1.0, 0.0, 0.0)
run(fv, en, 600)
s = fv.get(RACE_VA, "R")
check("empty book is recognised", fvmod.empty_book(fv.now_quotes["SENATEVA-26-D"]))
check("closed market: fair value keeps its last real value, not 0.50", abs(s.fv - fv_before) < 1e-12, round(s.fv, 4))
check("closed market: flagged closed", s.closed)
check("closed market: fair value ages, so the staleness guards stop new entries", s.age_s > P.en_fv_max_age, round(s.age_s))
check("closed market: no 0.50 anywhere in the history", all(abs(f - 0.5) > 0.4 for _, f in fv.hist[(RACE_VA, "R")]))
check("closed market: race NOT blocked by the reversal breaker", "senate:VA" not in en.st["blocked"], en.st["blocked"])
check("closed market: logged once", sum(r["type"] == "en_market_closed" for r in eng.logs) == 1,
      [r["type"] for r in eng.logs])
check("the open race is unaffected", not fv.get(RACE_FL, "D").closed and fv.get(RACE_FL, "D").age_s < 20)

# ---- a real reversal on a live book must still trip the breaker
fv.now_quotes["SENATEFL-26-D"] = q("SENATEFL-26-D", 0.79, 0.81)
fv.now_quotes["SENATEFL-26-R"] = q("SENATEFL-26-R", 0.19, 0.21)
run(fv, en, 200)
check("real reversal (0.99 -> 0.80, live book): race blocked", "senate:FL" in en.st["blocked"], en.st["blocked"])
check("real reversal: logged as en_breaker", any(r["type"] == "en_breaker" and r.get("race") == "senate:FL" for r in eng.logs))

# ---- one side closing before the other must not leave the open side normalised against nothing
fv2 = ScriptedFV(P)
fv2.track([(RACE_VA, "D"), (RACE_VA, "R")])
fv2.now_quotes = {"SENATEVA-26-D": q("SENATEVA-26-D", 0.90, 0.92), "SENATEVA-26-R": q("SENATEVA-26-R", 0.08, 0.10)}
fv2.poll(); T[0] += 15; fv2.poll()
d_before = fv2.get(RACE_VA, "D").fv
fv2.now_quotes["SENATEVA-26-R"] = q("SENATEVA-26-R", 0.0, 1.0, 0.0, 0.0)
T[0] += 15; fv2.poll()
check("one side closed: the other side's fair value does not jump to 1.0",
      abs(fv2.get(RACE_VA, "D").fv - d_before) < 1e-12 and fv2.get(RACE_VA, "D").closed, round(fv2.get(RACE_VA, "D").fv, 4))

# ---- a market that reopens is live again
fv2.now_quotes["SENATEVA-26-R"] = q("SENATEVA-26-R", 0.08, 0.10)
T[0] += 15; fv2.poll()
check("reopened market: no longer closed, fresh again", not fv2.get(RACE_VA, "D").closed and fv2.get(RACE_VA, "D").age_s < 1)

print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
