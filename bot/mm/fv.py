"""Live fair values from Kalshi for the contracts we make markets in.

FV(race, party) = size-weighted mid ("microprice") of Kalshi's contract for that party, normalised so the
race's outcomes (D, R, independents) sum to 1. Volatility is an EWMA of FV changes, seeded with the
hourly standard deviation of the last 7 days of Kalshi candles. A move larger than `jump_threshold` within
`jump_window_s` marks the contract as "jumping" (unscheduled news) for `jump_cooldown_s`.
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import kalshi
from common import http_json, qs

DATA = Path(__file__).resolve().parents[2] / "data"


@dataclass
class FVState:
    fv: float
    ref_half_spread: float   # half of Kalshi's spread for this contract
    age_s: float             # seconds since the last successful Kalshi read
    sigma_hourly: float      # sd of FV changes per hour, probability units
    jumping: bool
    ref_spread_ok: bool
    closed: bool = False     # Kalshi has stopped quoting this race; fv is the last value before it did


def empty_book(m: dict) -> bool:
    """True when a Kalshi market shows no bid and no offer (bid 0.00, ask 1.00).

    That is what a market looks like once Kalshi closes it to settle. Its "mid"
    is 0.50, which says nothing about the race, so it must never be read as a price.
    """
    try:
        return float(m["yes_bid_dollars"]) <= 0.0 and float(m["yes_ask_dollars"]) >= 1.0
    except (TypeError, ValueError, KeyError):
        return False


class KalshiFV:
    def __init__(self, params, clock=time.time):
        self.p, self.clock = params, clock
        rows = kalshi.fetch_reference(DATA / "kalshi_reference.json", max_age_s=6 * 3600)
        self.race_tickers: dict[tuple, dict[str, list[str]]] = {}
        for r in rows:
            self.race_tickers.setdefault((r["office"], r["place"]), {}).setdefault(r["party"], []).append(r["ticker"])
        self.tracked: set[tuple] = set()          # (race, party)
        self.quotes: dict[str, dict] = {}
        self.last_ok = 0.0
        self.hist: dict[tuple, deque] = {}        # (race, party) -> (t, fv)
        self.var_rate: dict[tuple, float] = {}    # variance per second
        self.jump_until: dict[tuple, float] = {}
        self.ref_spread: dict[tuple, float] = {}
        self.quote_t: dict[str, float] = {}       # ticker -> time of its last successful read
        self.bad: dict[str, float] = {}           # ticker -> time it was found failing (excluded for 30 min)
        self.closed: set[str] = set()             # tickers whose last read was an empty book (market closed)
        self.last_poll = 0.0
        self.poll_errors = 0
        self.hot: set[tuple] = set()              # races re-polled every kalshi_hot_poll_s (set by election mode)
        self.last_hot = 0.0

    # ---- setup ----
    def track(self, keys: list[tuple]):
        for race, party in keys:
            if race in self.race_tickers and party in self.race_tickers[race]:
                self.tracked.add((race, party))

    def has(self, race, party) -> bool:
        return (race, party) in self.tracked

    def calibrate(self, days: int = 7):
        """Seed volatility from Kalshi hourly candles (one request per tracked contract)."""
        now = int(self.clock())
        for key in sorted(self.tracked):
            ticker = self.race_tickers[key[0]][key[1]][0]
            series = ticker.split("-")[0]
            try:
                d = http_json("GET", f"{kalshi.BASE}/series/{series}/markets/{ticker}/candlesticks" +
                              qs({"start_ts": now - days * 86400, "end_ts": now, "period_interval": 60}))
            except Exception:
                continue
            mids = []
            for c in d.get("candlesticks", []):
                b, a = c["yes_bid"].get("close_dollars"), c["yes_ask"].get("close_dollars")
                if b is not None and a is not None and 0 < float(a) and float(a) - float(b) <= 0.1:
                    mids.append((float(a) + float(b)) / 2)
            if len(mids) > 24:
                ch = [mids[i] - mids[i - 1] for i in range(1, len(mids))]
                sd = math.sqrt(sum(x * x for x in ch) / len(ch))
                self.var_rate[key] = max(sd, 0.001) ** 2 / 3600
            time.sleep(0.1)

    # ---- polling ----
    def _fetch(self, chunk: list[str]) -> bool:
        try:
            d = http_json("GET", f"{kalshi.BASE}/markets" + qs({"tickers": ",".join(chunk)}), retries=1)
        except Exception:
            return False
        now = self.clock()
        for m in d.get("markets", []):
            self.quotes[m["ticker"]] = m
            if empty_book(m):
                # Not a quote: leave quote_t alone so the fair value ages and the
                # staleness guards stop new entries on it.
                self.closed.add(m["ticker"])
                continue
            self.closed.discard(m["ticker"])
            self.quote_t[m["ticker"]] = now
        return True

    def poll(self):
        """Batch reads of Kalshi quotes. A failing batch no longer discards the whole poll (Oct 12: one bad batch froze
        the live engine's FV for 12 h): it is split and failing tickers are excluded for 30 min. Age is per ticker.
        Every kalshi_poll_s all tracked races; in between, the `hot` races (election night) every kalshi_hot_poll_s."""
        now = self.clock()
        tracked_races = {race for race, _ in self.tracked}
        hot = self.hot & tracked_races
        full = now - self.last_poll >= getattr(self.p, "kalshi_poll_s", 10.0)
        if not full and not (hot and now - self.last_hot >= getattr(self.p, "kalshi_hot_poll_s", 2.0)):
            return
        self.last_hot = now
        if full:
            self.last_poll = now
        scope = tracked_races if full else hot
        self.bad = {t: tb for t, tb in self.bad.items() if now - tb < 1800}
        tickers = sorted({t for race in scope for ts in self.race_tickers[race].values() for t in ts} - set(self.bad))
        ok_any = False
        for i in range(0, len(tickers), 40):
            chunk = tickers[i:i + 40]
            if self._fetch(chunk):
                ok_any = True
                continue
            self.poll_errors += 1
            for t in chunk:                 # isolate the failing ticker(s)
                if self._fetch([t]):
                    ok_any = True
                else:
                    self.bad[t] = now
        if ok_any:
            self.last_ok = self.clock()
        now = self.clock()
        for key in self.tracked:
            if key[0] not in scope:
                continue
            fv = self._fv(key)
            if fv is None:
                continue
            h = self.hist.setdefault(key, deque(maxlen=2000))
            if h:
                t0, f0 = h[-1]
                dt = max(now - t0, 1.0)
                # EWMA of variance per second with a ~2 hour half-life in time
                w = 1 - 0.5 ** (dt / 7200)
                self.var_rate[key] = (1 - w) * self.var_rate.get(key, (self.p.default_hourly_sigma ** 2) / 3600) + w * (fv - f0) ** 2 / dt
            h.append((now, fv))
            recent = [f for t, f in h if now - t <= self.p.jump_window_s]
            if recent and max(recent) - min(recent) >= self.p.jump_threshold:
                self.jump_until[key] = now + self.p.jump_cooldown_s

    def _price(self, ticker: str):
        m = self.quotes.get(ticker)
        if not m or ticker in self.closed:
            return None, None
        try:
            b, a = float(m["yes_bid_dollars"]), float(m["yes_ask_dollars"])
        except (TypeError, ValueError, KeyError):
            return None, None
        if a <= 0 or a < b:
            return None, None
        mid = (a + b) / 2
        if self.p.use_microprice:
            try:
                bs, as_ = float(m.get("yes_bid_size_fp") or 0), float(m.get("yes_ask_size_fp") or 0)
            except ValueError:
                bs = as_ = 0
            if bs + as_ > 0:
                mid = min(a, max(b, (b * as_ + a * bs) / (bs + as_)))
        return mid, a - b

    def _fv(self, key):
        race, party = key
        if self.race_closed(race):
            return None                 # no new value: the last real one stays in the history
        prices, spread = {}, None
        for pty, tickers in self.race_tickers[race].items():
            for t in tickers:
                px, sp = self._price(t)
                if px is None:
                    if pty == party:
                        return None
                    continue
                prices[(pty, t)] = px
                if pty == party:
                    spread = sp
        own = sum(v for (pty, _), v in prices.items() if pty == party)
        total = sum(prices.values())
        self.ref_spread[key] = spread
        return own / total if 0.85 <= total <= 1.15 else own

    def race_closed(self, race) -> bool:
        """Any contract of the race showing an empty book. One side closing first would otherwise
        leave the other side normalised against nothing."""
        return any(t in self.closed for ts in self.race_tickers.get(race, {}).values() for t in ts)

    def get(self, race, party) -> FVState | None:
        key = (race, party)
        h = self.hist.get(key)
        if not h:
            return None
        now = self.clock()
        spread = self.ref_spread.get(key) or 0.0
        sig = math.sqrt(self.var_rate.get(key, self.p.default_hourly_sigma ** 2 / 3600) * 3600)
        own = [t for t in self.race_tickers[race].get(party, []) if t in self.quote_t]
        age = now - max(self.quote_t[t] for t in own) if own else now - self.last_ok
        return FVState(fv=h[-1][1], ref_half_spread=spread / 2, age_s=age,
                       sigma_hourly=sig, jumping=self.jump_until.get(key, 0) > now,
                       ref_spread_ok=spread <= self.p.fv_max_ref_spread, closed=self.race_closed(race))

    def window(self, race, party, secs: float):
        """(lowest, highest) FV over the last `secs`, counting the value in force when the window opened. None unless
        the history reaches back that far (election mode acts on a move only once it has held for the window)."""
        h = self.hist.get((race, party))
        if not h:
            return None
        start = self.clock() - secs
        vals = []
        for t, f in reversed(h):
            vals.append(f)
            if t <= start:
                return min(vals), max(vals)
        return None

    def fv_at(self, race, party, t: float):
        """FV at (or just before) time t, for markout analysis."""
        h = self.hist.get((race, party)) or []
        best = None
        for ts, f in h:
            if ts <= t:
                best = f
            else:
                break
        return best
