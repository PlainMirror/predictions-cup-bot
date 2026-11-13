"""Shared helpers: HTTP with retries, US state names, title parsing."""
from __future__ import annotations

import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "predictions-cup-research/0.1"


class HttpError(Exception):
    def __init__(self, status: int, body: str, headers: dict | None = None):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body
        self.headers = headers or {}

    def code(self) -> str | None:
        try:
            return json.loads(self.body)["error"]["code"]
        except Exception:
            return None


def http_json(method: str, url: str, headers: dict | None = None, body: dict | None = None,
              timeout: float = 30, retries: int = 4):
    """JSON request with backoff on 429 / 503 / network errors. Other 4xx raise immediately."""
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8")
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", errors="replace")
            err = HttpError(e.code, text, dict(e.headers))
            retryable = e.code in (429, 502, 503) or (e.code == 409 and err.code() == "REQUEST_IN_FLIGHT")
            if not retryable or attempt == retries:
                raise err
            wait = float(e.headers.get("Retry-After") or 0) or (2 ** attempt)
            time.sleep(wait + random.uniform(0, 0.5 * wait))
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries:
                raise
            time.sleep(2 ** attempt + random.random())


def keep_awake():
    """Ask Windows not to idle-sleep while this process runs (Oct 14-15: the PC slept/hibernated ~10 of 16 h with the
    bot running; power plan = sleep after 10 min, hibernate after 15 on AC). Released automatically when the process
    exits. Lid close / power button / Start > Sleep still sleep the PC."""
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)   # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    except Exception:
        pass


def qs(params: dict) -> str:
    clean = {k: v for k, v in params.items() if v is not None}
    return ("?" + urllib.parse.urlencode(clean)) if clean else ""


STATES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA",
    "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE", "Florida": "FL", "Georgia": "GA",
    "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA",
    "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO", "Montana": "MT",
    "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM",
    "New York": "NY", "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT",
    "Virginia": "VA", "Washington": "WA", "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}

PARTY_WORD = {"democratic": "D", "republican": "R"}

_CONTROL = re.compile(r"Will the (Democratic|Republican) Party win the U\.?S\.? (House|Senate)\??", re.I)
_STATEWIDE = re.compile(r"Will the (Democratic|Republican) Party win the (.+?) (Senate|Governor)(?: race)?\??$", re.I)
_HOUSE = re.compile(r"Will the (Democratic|Republican) Party win the ([A-Z]{2})-(\d{1,2}|AL) House race\??", re.I)


def parse_contest_title(title: str):
    """Map a contest market title to (race_key, party). race_key is (office, place).

    Returns None for titles that do not follow the standard party-wins-race pattern
    (those are reported separately so a human can price them)."""
    t = title.strip()
    m = _CONTROL.match(t)
    if m:
        return ("control", m.group(2).lower()), PARTY_WORD[m.group(1).lower()]
    m = _HOUSE.match(t)
    if m:
        dist = m.group(3).upper()
        dist = dist if dist == "AL" else f"{int(dist):02d}"
        return ("house", f"{m.group(2).upper()}-{dist}"), PARTY_WORD[m.group(1).lower()]
    m = _STATEWIDE.match(t)
    if m:
        state = STATES.get(m.group(2).strip().title()) or STATES.get(m.group(2).strip())
        if state:
            return (m.group(3).lower(), state), PARTY_WORD[m.group(1).lower()]
    return None


def round_tick(p: float, tick: float = 0.005, mode: str = "nearest") -> float:
    """Snap to the platform's 0.005 tick inside [0.005, 0.995]."""
    import math
    x = p / tick
    x = math.floor(x + 1e-9) if mode == "down" else math.ceil(x - 1e-9) if mode == "up" else round(x)
    return min(0.995, max(0.005, round(x * tick, 3)))
