"""Collector DB maintenance: archive old order-book rows to a compressed file, delete them, compact the DB.

    py db_maint.py --days 3          # keep the last 3 days of books (collector must be STOPPED: data/STOP_COLLECTOR)

Rows are written to data/archive/books_<from>_<to>.csv.gz and the archive is read back and counted BEFORE anything is
deleted. Trades, Kalshi quotes and the leaderboard are never touched. Old books are only needed for research (the live
engine and the shadows read the latest book per market).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "market_data.sqlite"
COLS = ["exchange_id", "recv_ts", "asof_seq", "asof_at", "best_bid", "best_ask", "bid_qty", "ask_qty", "bids", "asks", "src"]


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=3.0)
    a = ap.parse_args()
    if not (ROOT / "data" / "STOP_COLLECTOR").exists():
        raise SystemExit("Stop the collector first (create data/STOP_COLLECTOR and wait for it to exit).")
    cutoff = time.time() - a.days * 86400
    db = sqlite3.connect(DB, timeout=120)
    db.execute("PRAGMA busy_timeout=120000")
    lo, n = db.execute("SELECT MIN(recv_ts), COUNT(*) FROM books WHERE recv_ts < ?", (cutoff,)).fetchone()
    if not n:
        print("nothing older than the cutoff")
        return
    tag = f"{dt.datetime.fromtimestamp(lo):%Y%m%d_%H%M}_{dt.datetime.fromtimestamp(cutoff):%Y%m%d_%H%M}"
    out = ROOT / "data" / "archive" / f"books_{tag}.csv.gz"
    out.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    with gzip.open(out, "wt", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(COLS)
        for row in db.execute(f"SELECT {','.join(COLS)} FROM books WHERE recv_ts < ? ORDER BY rowid", (cutoff,)):
            w.writerow(row)
    with gzip.open(out, "rt", encoding="utf-8", newline="") as fh:
        back = sum(1 for _ in csv.reader(fh)) - 1
    print(f"archived {n:,} rows -> {out.name} ({out.stat().st_size / 1e6:.1f} MB) in {time.time() - t0:.0f}s; read back {back:,}")
    if back != n:
        raise SystemExit("row count mismatch: nothing deleted")
    size0 = DB.stat().st_size
    db.execute("DELETE FROM books WHERE recv_ts < ?", (cutoff,))
    db.commit()
    t1 = time.time()
    db.execute("VACUUM")
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.close()
    print(f"deleted {n:,} rows; VACUUM {time.time() - t1:.0f}s; DB {size0 / 1e6:.0f} MB -> {DB.stat().st_size / 1e6:.0f} MB")


if __name__ == "__main__":
    main()
