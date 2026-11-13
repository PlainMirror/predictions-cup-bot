"""One-off fast trade-history back-fill (READ-ONLY). Runs alongside the collector; works from the END of the market list
(the collector's own back-fill works from the start), both mark progress in the shared `backfill` table.

    py backfill_fast.py [--reads 60]
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

from collector import DB, iso_to_epoch
from sm_client import SuperMarket


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reads", type=int, default=60)
    a = ap.parse_args()
    db = sqlite3.connect(DB, timeout=60)
    sm = SuperMarket(reads_per_min=a.reads, writes_per_min=1)
    sm.use_tournament()
    exs = [r[0] for r in db.execute("SELECT exchange_id FROM markets ORDER BY CAST(exchange_id AS INTEGER) DESC")]
    t0, n_tr = time.time(), 0
    for i, eid in enumerate(exs):
        row = db.execute("SELECT done, cursor FROM backfill WHERE exchange_id=?", (eid,)).fetchone()
        if row and row[0]:
            continue
        cursor = row[1] if row else None
        while True:
            row = db.execute("SELECT done FROM backfill WHERE exchange_id=?", (eid,)).fetchone()
            if row and row[0]:
                break  # the collector finished it meanwhile
            d = sm._get(f"/exchanges/{eid}/trades", tournamentId=sm.tournament_id, limit=200, cursor=cursor)
            for x in d.get("data") or []:
                q = x.get("size", x.get("quantity"))
                db.execute("INSERT OR IGNORE INTO trades VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (str(x["id"]), eid, str(x.get("marketId") or ""), iso_to_epoch(x.get("createdAt")),
                            x.get("price"), abs(float(q)) if q is not None else None, None, x.get("side"), "rest", time.time()))
                n_tr += 1
            pg = d.get("pagination") or {}
            cursor = pg.get("nextCursor")
            done = not pg.get("hasMore") or not cursor
            db.execute("INSERT OR REPLACE INTO backfill VALUES (?,?,?)", (eid, int(done), cursor))
            db.commit()
            if done:
                break
        print(f"{time.strftime('%H:%M:%S')} exchange {eid} done ({i + 1}/{len(exs)}), {n_tr:,} trades so far, "
              f"{(time.time() - t0) / 60:.1f} min", flush=True)
    print("fast back-fill complete", flush=True)


if __name__ == "__main__":
    main()
