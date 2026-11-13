"""How did the leaders get there? Read-only diagnosis (needs SM_API_KEY with `read`).

    py leaders.py            -> reports/leaders_<stamp>.md

1. Top 100 by P&L (all time and today) with trades, volume and ROI-on-volume. The pattern tells the style:
     few trades + huge P&L + high ROI      -> concentrated directional bets (and/or marks in thin books)
     many trades + big volume + ROI ~1-5%  -> market making / arbitrage
2. Contest markets ranked by how far the price moved from its opening (initial) price: where early money was made.
3. Contest last price vs Kalshi fair value: where leaderboard marks may be inflated (or a real mispricing remains).
   The interim leaderboard marks positions at "valuation prices"; only the settled balance counts at the end.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import kalshi
from common import parse_contest_title
from sm_client import SuperMarket

ROOT = Path(__file__).resolve().parent.parent


def main():
    sm = SuperMarket()
    t = sm.use_tournament()
    out = [f"# Leaderboard diagnosis {dt.datetime.now():%Y-%m-%d %H:%M}",
           f"Tournament {t['name']} | members {t.get('memberCount')} | markets {t.get('marketCount')} | my balance {t.get('myBalance')}", ""]

    for period in ("all", "1d"):
        board = sm.leaderboard(period=period, limit=100)
        rows = board.get("leaderboard") or []
        out += [f"## Top by P&L, period={period} (my rank: {board.get('myRank')})", "",
                "| rank | user | P&L | trades | volume | ROI on volume % | win rate % | P&L per trade |",
                "|---|---|---|---|---|---|---|---|"]
        for r in rows[:50]:
            per = (r.get("pnl", 0) / r["tradesCount"]) if r.get("tradesCount") else 0
            out.append(f"| {r.get('rank')} | {r.get('username') or r.get('profileId', '')[:8]} | {r.get('pnl', 0):,.0f} | "
                       f"{r.get('tradesCount')} | {r.get('volume', 0):,.0f} | {r.get('roi')} | {r.get('winRate')} | {per:,.0f} |")
        out.append("")

    # markets: move from initial price, last vs Kalshi
    lookup = kalshi.as_lookup(kalshi.fetch_reference(ROOT / "data" / "kalshi_reference.json"))
    mk = sm.markets(status=None)
    rows = []
    for m in mk:
        for ex in m.get("exchanges") or []:
            ini, last = ex.get("initialPrice"), ex.get("latestPrice")
            parsed = parse_contest_title(m["title"])
            fv = None
            if parsed:
                race = lookup.get(parsed[0]) or {}
                fv = (race.get(parsed[1]) or {}).get("mid")
            rows.append({"title": m["title"], "id": m["id"], "status": m.get("status"), "initial": ini, "last": last, "fv": fv,
                         "move": None if ini is None or last is None else last - ini,
                         "gap": None if fv is None or last is None else last - fv})
    f = lambda x: "-" if x is None else f"{x:.3f}"
    moved = sorted([r for r in rows if r["move"] is not None], key=lambda r: -abs(r["move"]))
    out += ["## Biggest moves from the opening price (where early buyers/sellers made multiples)", "",
            "| market | initial | last | move | Kalshi FV | YES multiple | NO multiple |", "|---|---|---|---|---|---|---|"]
    for r in moved[:40]:
        ym = r["last"] / r["initial"] if r["initial"] else 0
        nm = (1 - r["last"]) / (1 - r["initial"]) if r["initial"] < 1 else 0
        out.append(f"| {r['title']} | {f(r['initial'])} | {f(r['last'])} | {r['move']:+.3f} | {f(r['fv'])} | {ym:.2f}x | {nm:.2f}x |")
    gaps = sorted([r for r in rows if r["gap"] is not None], key=lambda r: -abs(r["gap"]))
    out += ["", "## Last price furthest from Kalshi (inflated marks, or edge still on the table)", "",
            "| market | last | Kalshi FV | gap |", "|---|---|---|---|"]
    for r in gaps[:40]:
        out.append(f"| {r['title']} | {f(r['last'])} | {f(r['fv'])} | {r['gap']:+.3f} |")

    # biggest single trades in the most-moved markets
    out += ["", "## Largest trades in the 10 most-moved markets", ""]
    for r in moved[:10]:
        m = next(x for x in mk if x["id"] == r["id"])
        ex = (m.get("exchanges") or [{}])[0].get("id")
        if not ex:
            continue
        tr = sm._get(f"/exchanges/{ex}/trades", tournamentId=sm.tournament_id, limit=200).get("data") or []
        big = sorted(tr, key=lambda x: -(x.get("size") or 0))[:5]
        out.append(f"- **{r['title']}**: " + "; ".join(f"{x.get('size')} @ {x.get('price')} ({x.get('side')}, {str(x.get('createdAt'))[:16]})" for x in big))

    (ROOT / "reports").mkdir(exist_ok=True)
    path = ROOT / "reports" / f"leaders_{dt.datetime.now():%Y%m%d_%H%M}.md"
    path.write_text("\n".join(out), encoding="utf-8")
    print(f"Wrote {path}")


if __name__ == "__main__":
    main()
