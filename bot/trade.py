"""Place the orders you approved in a scan's orders CSV.

    py trade.py orders_20261010_0930.csv           # DRY RUN: prints what it would send
    py trade.py orders_20261010_0930.csv --live    # sends (needs SM_API_KEY with the `trade` scope)

Only rows with approve=1 are used; edit price/quantity in the CSV first if you like.
Rows sharing a `group` (pair arbs, engine fixes, quotes) go as ONE atomic multi-leg order, so you are
never left holding one leg of an arbitrage. Every response is appended to data/trade_log.jsonl.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import uuid
from collections import OrderedDict
from pathlib import Path

from common import HttpError, round_tick

ROOT = Path(__file__).resolve().parent.parent


def load(path: Path) -> "OrderedDict[str, list[dict]]":
    groups: "OrderedDict[str, list[dict]]" = OrderedDict()
    for i, row in enumerate(csv.DictReader(path.open(encoding="utf-8"))):
        if row["approve"].strip() not in ("1", "y", "yes", "Y"):
            continue
        if not row["quantity"].strip():
            raise SystemExit(f"row {i + 2}: approved but has no quantity ({row['title']})")
        leg = {"exchange_id": row["exchange_id"], "side": row["side"], "action": row["action"],
               "quantity": int(float(row["quantity"])), "price": round_tick(float(row["price"])),
               "title": row["title"], "kind": row["kind"], "relationship_id": row.get("relationship_id") or None}
        groups.setdefault(row["group"] or f"single{i}", []).append(leg)
    return groups


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("orders_csv")
    ap.add_argument("--live", action="store_true", help="actually send orders")
    ap.add_argument("--quote-ttl-hours", type=float, default=6,
                    help="QUOTE orders auto-expire after this long so stale quotes cannot be picked off (0 = never)")
    args = ap.parse_args()
    path = Path(args.orders_csv)
    if not path.exists():
        path = ROOT / "reports" / args.orders_csv
    groups = load(path)
    if not groups:
        raise SystemExit("No rows with approve=1.")
    if args.quote_ttl_hours > 0:
        exp = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=args.quote_ttl_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for legs in groups.values():
            for l in legs:
                if l["kind"] == "QUOTE":
                    l["expiration"] = exp

    sm = None
    if args.live:
        from sm_client import SuperMarket
        sm = SuperMarket()
        t = sm.use_tournament()
        print(f"Tournament {t['name']} | balance {t.get('myBalance')}")
    log = (ROOT / "data" / "trade_log.jsonl").open("a", encoding="utf-8")

    for gid, legs in groups.items():
        desc = " + ".join(f"{l['action']} {l['quantity']} {l['side'].upper()} @ {l['price']:.3f} [{l['title'][:55]}]" for l in legs)
        if not sm:
            print(f"DRY  {gid}: {desc}")
            continue
        key = str(uuid.uuid4())
        try:
            if len(legs) == 1:
                l = legs[0]
                res = sm.place(l["exchange_id"], l["side"], l["action"], l["quantity"], l["price"], idempotency_key=key,
                               expiration=l.get("expiration"))
            else:
                rel = legs[0]["relationship_id"] if legs[0]["kind"] == "ENGINE" else None
                res = sm.place_multi_leg(legs, idempotency_key=key, relationship_id=rel)
            print(f"SENT {gid}: {desc}\n     -> {json.dumps(res)[:300]}")
        except HttpError as e:
            res = {"error": e.code(), "status": e.status, "body": e.body[:500]}
            print(f"FAIL {gid}: {desc}\n     -> {res}")
        log.write(json.dumps({"at": dt.datetime.now(dt.timezone.utc).isoformat(), "group": gid, "legs": legs,
                              "idempotencyKey": key, "result": res}) + "\n")
    if not sm:
        print("\nDry run only. Re-run with --live to send.")


if __name__ == "__main__":
    main()
