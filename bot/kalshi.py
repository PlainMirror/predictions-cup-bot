"""Reference fair values from Kalshi's public market-data API (no account or key needed).

Kalshi is a CFTC-regulated real-money exchange with tight spreads on the 2026 races, and
Susquehanna is a market maker there, so its mid price is the best free "fair value" anchor.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from common import STATES, http_json, qs

BASE = "https://api.elections.kalshi.com/trade-api/v2"

# Series that hold party-winner markets for the 2026 general election.
_SERIES_RE = re.compile(r"^(CONTROLH|CONTROLS|SENATE[A-Z]{2}S?|KXSENATELA|GOVPARTY-?[A-Z]{2}|KXGOVPARTY[A-Z]{2}"
                        r"|KXHOUSERACE|(KX)?HOUSE[A-Z]{2}\d{1,2})$")
_DISTRICT_SERIES_RE = re.compile(r"^(?:KX)?HOUSE([A-Z]{2})(\d{1,2})$")
_TITLE_RE = re.compile(r"^(.+?) (Senate|Governor) winner\?", re.I)
MAX_SPREAD = 0.15


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _party(market_ticker: str) -> str:
    suffix = market_ticker.rsplit("-", 1)[-1].upper()
    return suffix if suffix in ("D", "R") else "OTHER"


def _race_key(event: dict):
    series, ticker, title = event["series_ticker"], event["event_ticker"], event.get("title") or ""
    if series == "CONTROLH":
        return ("control", "house")
    if series == "CONTROLS":
        return ("control", "senate")
    if series == "KXHOUSERACE":
        m = re.match(r"KXHOUSERACE-([A-Z]{2})(\d{1,2}|AL)-", ticker)
        if not m:
            return None
        d = m.group(2)
        return ("house", f"{m.group(1)}-{'AL' if d == 'AL' else f'{int(d):02d}'}")
    m = _DISTRICT_SERIES_RE.match(series)
    if m:
        return ("house", f"{m.group(1)}-{int(m.group(2)):02d}")
    m = _TITLE_RE.match(title)
    if m and m.group(1) in STATES:
        return (m.group(2).lower(), STATES[m.group(1)])
    return None


def list_series() -> list[str]:
    d = http_json("GET", f"{BASE}/series" + qs({"category": "Elections"}))
    return sorted(s["ticker"] for s in d.get("series", []) if _SERIES_RE.match(s["ticker"]))


def fetch_events(series: str) -> list[dict]:
    out, cursor = [], None
    while True:
        d = http_json("GET", f"{BASE}/events" + qs({"series_ticker": series, "status": "open", "limit": 200,
                                                     "with_nested_markets": "true", "cursor": cursor}))
        out += d.get("events", [])
        cursor = d.get("cursor")
        if not cursor:
            return out
        time.sleep(0.1)


def fetch_reference(cache: Path | None = None, max_age_s: int = 900) -> list[dict]:
    """One row per (race, party) with Kalshi bid/ask/mid. Cached to disk to stay polite."""
    if cache and cache.exists() and time.time() - cache.stat().st_mtime < max_age_s:
        return json.loads(cache.read_text(encoding="utf-8"))
    rows = []
    for series in list_series():
        for ev in fetch_events(series):
            # 2026 cycle only. Tickers are inconsistent (KS 2026 is "-27", NH "-28" is the 2028 race),
            # so filter on when the markets expect to settle: between Nov 2026 and Mar 2027.
            exp = (ev.get("markets") or [{}])[0].get("expected_expiration_time") or ""
            if not ("2026-11" <= exp[:7] <= "2027-03"):
                continue
            key = _race_key(ev)
            if not key:
                continue
            for m in ev.get("markets", []):
                bid, ask = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
                last = _f(m.get("last_price_dollars"))
                two_sided = bid is not None and ask is not None and 0 < ask and ask - bid <= MAX_SPREAD
                mid = (bid + ask) / 2 if two_sided else None  # a one-sided or very wide book is not a price
                rows.append({
                    "office": key[0], "place": key[1], "party": _party(m["ticker"]),
                    "candidate": m.get("yes_sub_title"), "ticker": m["ticker"],
                    "bid": bid, "ask": ask, "last": last, "mid": mid,
                    "spread": (ask - bid) if bid is not None and ask is not None else None,
                    "volume": _f(m.get("volume_fp")), "volume_24h": _f(m.get("volume_24h_fp")),
                })
        time.sleep(0.05)
    rows = _dedupe(rows)
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    return rows


def _dedupe(rows: list[dict]) -> list[dict]:
    """Some races are listed under two series (e.g. GOVPARTYNH and GOVPARTY-NH); keep the most traded."""
    best: dict[tuple, dict] = {}
    for r in rows:
        k = (r["office"], r["place"], r["party"], r["candidate"] if r["party"] == "OTHER" else "")
        if k not in best or (r["volume"] or 0) > (best[k]["volume"] or 0):
            best[k] = r
    return list(best.values())


def as_lookup(rows: list[dict]) -> dict[tuple, dict]:
    """(office, place) -> {"D": row, "R": row, "OTHER": [rows], "other_mid": summed mid of non-D/R}."""
    out: dict[tuple, dict] = {}
    for r in rows:
        race = out.setdefault((r["office"], r["place"]), {"OTHER": [], "other_mid": 0.0})
        if r["party"] == "OTHER":
            race["OTHER"].append(r)
            race["other_mid"] += r["mid"] or 0.0
        else:
            race[r["party"]] = r
    return out


if __name__ == "__main__":
    import sys
    rows = fetch_reference(Path(__file__).parent.parent / "data" / "kalshi_reference.json", max_age_s=0)
    by = as_lookup(rows)
    print(f"{len(rows)} contracts across {len(by)} races", file=sys.stderr)
    for (office, place), race in sorted(by.items()):
        d, r = race.get("D"), race.get("R")
        fmt = lambda x: f"{x['mid']:.3f} ({x['bid']}/{x['ask']})" if x and x["mid"] is not None else "-"
        print(f"{office:9} {place:6} D {fmt(d):24} R {fmt(r):24} other {race['other_mid']:.3f}")
