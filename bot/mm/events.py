"""Scheduled-event calendar (data/events.csv) -> quoting regime for each race.

Events are NOT trade signals: a debate can move a race either way, and the 30-day Kalshi history shows the
biggest moves came from unscheduled news, not debates. Events tell the market maker when flow and
adverse-selection risk rise, so it widens, shrinks size and leans inventory toward flat before, and pulls
or widens quotes in the affected races while the event is live.

severity 1: pre/post widen only (mild)   2: widen pre/post, widen x live_event_mult during   3: pull during
"""
from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass
from pathlib import Path

CSV = Path(__file__).resolve().parents[2] / "data" / "events.csv"
DST_END_UTC = dt.datetime(2026, 11, 1, 6, 0, tzinfo=dt.timezone.utc)  # 2am EDT Nov 1, 2026


def et_to_utc(s: str) -> dt.datetime:
    naive = dt.datetime.fromisoformat(s)
    for off in (4, 5):
        cand = (naive + dt.timedelta(hours=off)).replace(tzinfo=dt.timezone.utc)
        if (off == 4) == (cand < DST_END_UTC):
            return cand
    return (naive + dt.timedelta(hours=5)).replace(tzinfo=dt.timezone.utc)


@dataclass
class Event:
    start: dt.datetime
    end: dt.datetime
    scope: list[str]
    severity: int
    pre: dt.timedelta
    post: dt.timedelta
    name: str

    def applies(self, race: tuple) -> bool:
        office, place = race
        for s in self.scope:
            s = s.strip()
            if s == "ALL" or s == office or s == f"{office}:{place}":
                return True
        return False


@dataclass
class Regime:
    spread_mult: float = 1.0
    size_mult: float = 1.0
    skew_mult: float = 1.0
    pull: bool = False
    label: str = "normal"


class Calendar:
    def __init__(self, path: Path = CSV):
        self.events: list[Event] = []
        if path.exists():
            for r in csv.DictReader(path.open(encoding="utf-8")):
                start = et_to_utc(r["start_et"])
                self.events.append(Event(start, start + dt.timedelta(minutes=float(r["duration_min"])),
                                         r["scope"].split(";"), int(r["severity"]),
                                         dt.timedelta(hours=float(r["pre_hours"])),
                                         dt.timedelta(hours=float(r["post_hours"])), r["name"]))

    def regime(self, now: dt.datetime, race: tuple, p) -> Regime:
        out = Regime()
        for e in self.events:
            if not e.applies(race):
                continue
            if e.start - e.pre <= now < e.start:
                out = _worse(out, Regime(p.pre_event_mult if e.severity > 1 else 1.2, 0.5, 2.0, False, f"pre:{e.name}"))
            elif e.start <= now < e.end:
                if e.severity >= 3 or (e.severity == 2 and p.live_event_action == "pull"):
                    out = _worse(out, Regime(p.live_event_mult, 0.0, 2.0, True, f"live:{e.name}"))
                elif e.severity == 2:
                    out = _worse(out, Regime(p.live_event_mult, 0.5, 2.0, False, f"live:{e.name}"))
                else:
                    out = _worse(out, Regime(1.3, 0.75, 1.5, False, f"live:{e.name}"))
            elif e.end <= now < e.end + e.post:
                out = _worse(out, Regime(p.post_event_mult if e.severity > 1 else 1.2, 0.75, 1.5, False, f"post:{e.name}"))
        return out

    def upcoming(self, now: dt.datetime, hours: float = 48) -> list[Event]:
        return [e for e in self.events if now - e.post <= e.start <= now + dt.timedelta(hours=hours)]


def _worse(a: Regime, b: Regime) -> Regime:
    return Regime(max(a.spread_mult, b.spread_mult), min(a.size_mult, b.size_mult), max(a.skew_mult, b.skew_mult),
                  a.pull or b.pull, b.label if b.spread_mult >= a.spread_mult else a.label)
