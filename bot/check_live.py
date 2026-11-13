"""One-shot status of the live bot (READ-ONLY: 3 API reads, never places or cancels anything).

    py check_live.py              # last 24 h
    py check_live.py --hours 6

Prints: running processes, newest live log (params, pauses, trades, realised P&L, errors, stops), collector/shadow
freshness, and the platform account (cash + positions, which positions lack an exit order, lots vs positions).
Note: Explorer / Get-ChildItem show stale size and mtime for the open log files, so this reads the files themselves.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import glob
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LOGS = DATA / "mm_logs"
f = lambda t: dt.datetime.fromtimestamp(t).strftime("%m-%d %H:%M")


def tail_records(path: Path, nbytes: int = 300_000) -> list[dict]:
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        n = fh.tell()
        fh.seek(max(0, n - nbytes))
        lines = fh.read().splitlines()[1:]
    return [json.loads(x) for x in lines if x.strip()]


def processes():
    cmd = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -match "
           "'run_mm|collector|overnight' } | ForEach-Object { \"$($_.ProcessId)|$($_.CreationDate)|$($_.CommandLine)\" }")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True).stdout
    rows = [r.split("|", 2) for r in out.strip().splitlines() if r.count("|") >= 2]
    print("PROCESSES")
    for pid, created, cl in rows:
        print(f"  {pid:>6}  {created[:19]}  {cl[cl.find('bot'):][:150]}")
    if not any("--live" in r[2] for r in rows):
        print("  !! no live engine running")
    for s in ("collector.py", "--shadow", "overnight.py"):
        if not any(s in r[2] for r in rows):
            print(f"  !! {s} not running")


def live_log(hours: float):
    files = sorted(glob.glob(str(LOGS / "live_*.jsonl")))
    if not files:
        print("no live log")
        return
    path = Path(files[-1])
    with open(path, encoding="utf-8") as fh:
        start = json.loads(fh.readline())
    since = time.time() - hours * 3600
    mk = {m["exchange_id"]: m["title"][9:55] for m in start.get("markets", [])}
    P = start.get("params", {})
    print(f"\nLIVE LOG {path.name} (started {f(start['t'])})")
    print("  params: " + ", ".join(f"{k}={P.get(k)}" for k in ("entry_mode", "take_edge", "max_total_risk", "max_daily_loss",
                                                               "model_thr", "entry_min_px", "entry_max_px", "lean_party", "lean_extra")))
    snaps, ev = [], []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            d = json.loads(line)
            if d.get("t", 0) < since:
                continue
            (snaps if d["type"] == "snap" else ev).append(d)
    if snaps:
        last = snaps[-1]
        gaps = [f"{f(a['t'])}->{f(b['t'])} ({(b['t'] - a['t']) / 60:.0f} min)" for a, b in zip(snaps, snaps[1:]) if b["t"] - a["t"] > 180]
        print(f"  last snapshot {time.time() - last['t']:.0f} s ago: engine value {last['value']:,.0f} (Kalshi-marked), "
              f"cash {last['balance']:,.2f}, FV health {last['fv_health']}")
        print(f"  pauses > 3 min in the last {hours:g} h: {gaps or 'none'}")
    c = collections.Counter(d["type"] for d in ev)
    print(f"  events ({hours:g} h): {dict(c)}")
    pnl = collections.defaultdict(float)
    for d in ev:
        if d["type"] == "exit_fill":
            pnl[mk.get(d["exchange_id"], d["exchange_id"])] += d["pnl"]
    if pnl:
        print(f"  realised on exits: {sum(pnl.values()):+,.1f}  " +
              ", ".join(f"{k[:30]} {v:+.0f}" for k, v in sorted(pnl.items(), key=lambda kv: -abs(kv[1]))[:8]))
    takes = [d for d in ev if d["type"] in ("take", "exit") and (d.get("res") or {}).get("quantityTraded")]
    for d in takes[-15:]:
        r = d["res"]
        print(f"  {f(d['t'])} {d['type']:4} {d['action']:4} {abs(float(r['quantityTraded'])):>6.0f} @ {d['price']:<5} "
              f"FV {d['fv']:.3f} {d.get('mode') or '':5} {mk.get(d['exchange_id'], d['exchange_id'])}")
    errs = collections.Counter(d.get("error", "")[:70] for d in ev if d["type"] == "error" and "already closed" not in d.get("error", ""))
    if errs:
        print(f"  errors: {dict(errs.most_common(5))}")
    for d in ev:
        if d["type"] in ("loss_stop", "stop", "positions_confirmed", "adopt"):
            print(f"  {f(d['t'])} {d['type']} {json.dumps({k: v for k, v in d.items() if k not in ('type', 't')})[:120]}")


def feeds():
    db = sqlite3.connect(f"file:{DATA / 'market_data.sqlite'}?mode=ro", uri=True, timeout=10)
    r = db.execute("SELECT recv_ts FROM books ORDER BY rowid DESC LIMIT 1").fetchone()
    print(f"\nFEEDS  collector newest book {time.time() - r[0]:.0f} s old", end="")
    sh = sorted(glob.glob(str(LOGS / "shadowboth_*.jsonl")))
    if sh:
        # a full shadow snapshot (every 10th, ~230 rows) can be longer than a small tail: read enough for a whole line
        recs = tail_records(Path(sh[-1]), 400_000)
        age = f"{time.time() - recs[-1]['t']:.0f} s ago" if recs else "unknown (no complete record in the tail)"
        print(f" | shadow {Path(sh[-1]).name} last write {age}", end="")
    stops = [p.name for p in DATA.glob("STOP*")]
    print(f" | STOP files: {stops or 'none'}")


def account():
    sys.path.insert(0, str(ROOT / "bot"))
    from sm_client import SuperMarket
    c = SuperMarket(reads_per_min=20)
    c.use_tournament()
    pos = [p for p in c.positions().get("positions", []) if not p.get("settled") and p.get("quantity")]
    orders = c.open_orders().get("data") or []
    cash = float(c.tournament_info().get("myBalance") or 0)
    mv = sum(p["marketValue"] for p in pos)
    print(f"\nACCOUNT  cash {cash:,.2f} + positions {mv:,.2f} = platform {cash + mv:,.2f}  "
          f"(unrealised {sum(p['unrealizedPnl'] for p in pos):+,.2f}; {len(pos)} positions, {len(orders)} open orders)")
    oo = collections.Counter(str(o["exchangeId"]) for o in orders)
    lots = collections.defaultdict(float)
    try:
        for l in json.load(open(DATA / "live_lots.json", encoding="utf-8")):
            lots[l["exchange_id"]] += l["d"] * l["qty"]
    except FileNotFoundError:
        pass
    except (ValueError, UnicodeDecodeError):
        print("  !! data/live_lots.json is unreadable (crash leftover?): the engine sets it aside at its next start")
    for p in sorted(pos, key=lambda p: -p["marketValue"]):
        ex, q = str(p["exchangeId"]), p["quantity"]
        px = p["avgCost"] if q > 0 else 1 - p["avgCost"]
        flags = ("" if oo.get(ex) or abs(q) < 2 else "  NO EXIT ORDER") + ("" if abs(lots.get(ex, 0) - q) < 1 else f"  lots {lots.get(ex, 0):+.0f}")
        print(f"  {p['marketTitle'][9:55]:46} {q:+7.0f} @{px:.3f} now {p['currentPrice']:.3f} unrl {p['unrealizedPnl']:+7.1f}{flags}")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24)
    a = ap.parse_args()
    print(f"check_live {dt.datetime.now():%a %b %d %H:%M}")
    processes()
    live_log(a.hours)
    feeds()
    account()


if __name__ == "__main__":
    main()
