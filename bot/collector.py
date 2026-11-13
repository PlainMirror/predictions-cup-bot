"""24/7 data collector (READ-ONLY, places no orders).

    py collector.py            # run until Ctrl+C or data/STOP_COLLECTOR exists

Writes data/market_data.sqlite:
  markets     every contest contract (title, race, party, opening price, first seen)
  books       every order-book change for every market (realtime `tournament:{id}:markets`, free of read budget)
  trades      every contest trade (realtime + REST backfill of history since Oct 1)
  kalshi      Kalshi quotes for the matching contracts, stored only when they change (polled every 15 s)
  leaderboard top 100 (all-time and today), hourly
  events      connection/resync log and NEW-MARKET alerts (a new market opening at 0.50 is the leaders' original edge)

Budget: about 25 contest reads/min (book resyncs, trade backfill, market list, reconcile), leaving ~75 for other tools.
Realtime rules followed: per-market revision tracking; REST resync on gaps, resyncRequired, reconnects; books applied by asOf.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import sqlite3
import sys
import time
from pathlib import Path

import kalshi
from common import http_json, keep_awake, parse_contest_title, qs
from sm_client import SuperMarket

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "market_data.sqlite"
STOP = ROOT / "data" / "STOP_COLLECTOR"

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets(exchange_id TEXT PRIMARY KEY, market_id TEXT, title TEXT, office TEXT, place TEXT,
    party TEXT, initial_price REAL, status TEXT, first_seen REAL);
CREATE TABLE IF NOT EXISTS books(exchange_id TEXT, recv_ts REAL, asof_seq INTEGER, asof_at TEXT, best_bid REAL, best_ask REAL,
    bid_qty REAL, ask_qty REAL, bids TEXT, asks TEXT, src TEXT);
CREATE INDEX IF NOT EXISTS books_ex_ts ON books(exchange_id, recv_ts);
CREATE TABLE IF NOT EXISTS trades(trade_id TEXT PRIMARY KEY, exchange_id TEXT, market_id TEXT, ts REAL, price REAL,
    quantity REAL, seq INTEGER, side TEXT, src TEXT, recv_ts REAL);
CREATE INDEX IF NOT EXISTS trades_ex_ts ON trades(exchange_id, ts);
CREATE TABLE IF NOT EXISTS kalshi(ticker TEXT, ts REAL, bid REAL, ask REAL, bid_size REAL, ask_size REAL, last REAL);
CREATE INDEX IF NOT EXISTS kalshi_t_ts ON kalshi(ticker, ts);
CREATE TABLE IF NOT EXISTS kalshi_map(ticker TEXT PRIMARY KEY, office TEXT, place TEXT, party TEXT, candidate TEXT);
CREATE TABLE IF NOT EXISTS leaderboard(ts REAL, period TEXT, rank INTEGER, profile_id TEXT, username TEXT, pnl REAL,
    trades INTEGER, volume REAL, roi REAL);
CREATE TABLE IF NOT EXISTS events(ts REAL, kind TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS backfill(exchange_id TEXT PRIMARY KEY, done INTEGER, cursor TEXT);
"""


def iso_to_epoch(s):
    if s is None:
        return None
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class Collector:
    def __init__(self):
        self.db = sqlite3.connect(DB)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        # collector's own share of the per-account budget
        self.sm = SuperMarket(reads_per_min=25, writes_per_min=4)
        self.t = self.sm.use_tournament()
        self.tid, self.slug = self.t["id"], self.t["slug"]
        self.ex_market: dict[str, str] = {}
        self.market_exs: dict[str, list[str]] = {}
        self.book_ver: dict[str, tuple] = {}
        self.book_saved: dict[str, tuple] = {}
        self.last_rev: dict[str, int] = {}
        self.resync_q: asyncio.Queue = asyncio.Queue()
        self.queued: set = set()
        self.last_msg = time.time()
        self.kalshi_last: dict[str, tuple] = {}
        self.stats = {"batches": 0, "trades": 0, "books": 0, "gaps": 0, "kalshi_rows": 0}

    # ---------------------------------------------------------------- utilities
    def event(self, kind, detail):
        self.db.execute("INSERT INTO events VALUES (?,?,?)", (time.time(), kind, json.dumps(detail, default=str)[:4000]))
        self.db.commit()

    def say(self, msg):
        print(f"{dt.datetime.now():%H:%M:%S} {msg}", flush=True)

    async def rest(self, fn, *a, **k):
        return await asyncio.to_thread(fn, *a, **k)

    # ---------------------------------------------------------------- markets
    async def refresh_markets(self, first=False):
        mk = await self.rest(self.sm.markets, None)
        new = []
        for m in mk:
            parsed = parse_contest_title(m["title"])
            for ex in m.get("exchanges") or []:
                eid = ex["id"]
                self.ex_market[eid] = m["id"]
                self.market_exs.setdefault(m["id"], [])
                if eid not in self.market_exs[m["id"]]:
                    self.market_exs[m["id"]].append(eid)
                row = self.db.execute("SELECT 1 FROM markets WHERE exchange_id=?", (eid,)).fetchone()
                if row is None:
                    office, place, party = (parsed[0][0], parsed[0][1], parsed[1]) if parsed else (None, None, None)
                    self.db.execute("INSERT INTO markets VALUES (?,?,?,?,?,?,?,?,?)",
                                    (eid, m["id"], m["title"], office, place, party, ex.get("initialPrice"), m.get("status"), time.time()))
                    if not first:
                        new.append((m["id"], eid, m["title"], ex.get("initialPrice")))
                else:
                    self.db.execute("UPDATE markets SET status=? WHERE exchange_id=?", (m.get("status"), eid))
        self.db.commit()
        for mid, eid, title, ip in new:
            self.event("NEW_MARKET", {"market_id": mid, "exchange_id": eid, "title": title, "initial_price": ip})
            self.say(f"*** NEW MARKET *** {title} (market {mid}, opening price {ip}). Alert only: nothing traded.")
            print("\a", end="", flush=True)
            await self.queue_resync(mid)
        return len(mk)

    # ---------------------------------------------------------------- books & trades
    def store_book(self, eid, book, src):
        asof = book.get("asOf") or {}
        ver = (asof.get("sequence") or -1, asof.get("at") or "")
        if ver[0] != -1 and eid in self.book_ver and ver <= self.book_ver[eid]:
            return
        self.book_ver[eid] = ver
        bids, asks = book.get("bids") or [], book.get("asks") or []
        bb = float(bids[0]["price"]) if bids else None
        ba = float(asks[0]["price"]) if asks else None
        # store only when the top of book changes, or at most once a minute (deep-level churn by bots is noise)
        top = (bb, ba, float(bids[0]["quantity"]) if bids else None, float(asks[0]["quantity"]) if asks else None)
        prev = self.book_saved.get(eid)
        if src == "ws" and prev and time.time() - prev[1] < 60:
            (pbb, pba, pbq, paq) = prev[0]
            same_px = pbb == bb and pba == ba
            small = lambda a, b: a is None or b is None or abs(a - b) <= 0.2 * max(a, b)
            if same_px and small(pbq, top[2]) and small(paq, top[3]):
                return  # same prices and top sizes within 20%: bot churn, not information
        self.book_saved[eid] = (top, time.time())
        self.db.execute("INSERT INTO books VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (eid, time.time(), asof.get("sequence"), asof.get("at"), bb, ba,
                         float(bids[0]["quantity"]) if bids else None, float(asks[0]["quantity"]) if asks else None,
                         json.dumps([[float(x["price"]), float(x["quantity"])] for x in bids[:8]]),
                         json.dumps([[float(x["price"]), float(x["quantity"])] for x in asks[:8]]), src))
        self.stats["books"] += 1

    def store_trade(self, tr, src):
        tid = tr.get("id")
        if tid is None:
            return
        eid = str(tr.get("exchangeId") or tr.get("exchange_id") or "")
        qty = tr.get("quantity", tr.get("size"))
        cur = self.db.execute("INSERT OR IGNORE INTO trades VALUES (?,?,?,?,?,?,?,?,?,?)",
                              (str(tid), eid, str(tr.get("marketId") or self.ex_market.get(eid, "")),
                               iso_to_epoch(tr.get("executedAt") or tr.get("createdAt")), tr.get("price"),
                               abs(float(qty)) if qty is not None else None, tr.get("sequence"), tr.get("side"), src, time.time()))
        self.stats["trades"] += cur.rowcount

    async def queue_resync(self, market_id):
        if market_id not in self.queued:
            self.queued.add(market_id)
            await self.resync_q.put(market_id)

    async def resync_worker(self):
        while True:
            mid = await self.resync_q.get()
            self.queued.discard(mid)
            try:
                ob = await self.rest(self.sm._get, f"/markets/{mid}/orderbook", tournamentId=self.tid, depth=200)
                books = ob.get("exchanges") or ob.get("books") or []
                if not books and "bids" in ob:
                    books = [ob]
                for b in books:
                    eid = str(b.get("exchangeId") or b.get("id") or (self.market_exs.get(mid) or [None])[0])
                    self.store_book(eid, b, "rest")
                for eid in self.market_exs.get(mid, []):
                    tr = await self.rest(self.sm._get, f"/exchanges/{eid}/trades", tournamentId=self.tid, limit=200)
                    for x in tr.get("data") or []:
                        x["exchangeId"] = eid
                        self.store_trade(x, "rest")
                self.db.commit()
            except Exception as e:
                self.event("resync_error", {"market": mid, "error": str(e)[:300]})

    def on_market_batch(self, mid, batch):
        for b in batch.get("books") or []:
            self.store_book(str(b.get("exchangeId")), b, "ws")
        d = batch.get("delivery") or {}
        rev, prev = d.get("revision"), d.get("previousRevision")
        last = self.last_rev.get(mid)
        if batch.get("resyncRequired"):
            if rev is not None and (last is None or rev > last):
                self.last_rev[mid] = rev
            return asyncio.create_task(self.queue_resync(mid))
        if rev is not None and last is not None and rev <= last:
            return
        if rev is not None:
            self.last_rev[mid] = rev
        if last is not None and prev is not None and prev > last:
            self.stats["gaps"] += 1
            asyncio.create_task(self.queue_resync(mid))
        for tr in batch.get("trades") or []:
            if tr.get("sequence") is None:
                asyncio.create_task(self.queue_resync(mid))
            self.store_trade(tr, "ws")
        for s in batch.get("marketSettled") or []:
            self.event("SETTLED", s)
            self.say(f"market settled: {s}")

    def on_markets_batch(self, message):
        self.last_msg = time.time()
        self.stats["batches"] += 1
        payload = message.get("payload", message)
        if self.stats["batches"] == 1:
            self.event("first_batch_sample", payload)
        for entry in payload.get("markets") or []:
            self.on_market_batch(str(entry.get("marketId")), entry.get("batch") or {})
        self.db.commit()

    # ---------------------------------------------------------------- realtime
    async def realtime_session(self):
        """One authenticated socket; returns when it should be rebuilt (token age, silence, error)."""
        from realtime import AsyncRealtimeClient, RealtimeSubscribeStates
        tok = await self.rest(self.sm._write, "POST", "/realtime/token")
        client = AsyncRealtimeClient(f"{tok['supabaseUrl']}/realtime/v1", tok["anonKey"], auto_reconnect=True)
        await client.connect()
        await client.set_auth(tok["token"])
        ch = client.channel(f"tournament:{self.tid}:markets", {"config": {"broadcast": {"ack": False, "self": False},
                                                                           "presence": {"key": "", "enabled": False},
                                                                           "private": True}})
        ch.on_broadcast("markets_batch", self.on_markets_batch)
        ch.on_broadcast("book_dirty", lambda m: asyncio.create_task(
            self.queue_resync(str((m.get("payload") or {}).get("marketId")))))
        ch.on_broadcast("market_settled", lambda m: self.event("SETTLED", m.get("payload")))
        state = {"status": None, "err": None}

        def on_status(status, err):
            state["status"], state["err"] = status, err
            self.event("subscribe_status", {"status": str(status), "err": str(err) if err else None})
            if status == RealtimeSubscribeStates.SUBSCRIBED:
                self.say("realtime: subscribed to all markets")

        await ch.subscribe(on_status)
        started = time.time()
        self.last_msg = time.time()
        try:
            while True:
                await asyncio.sleep(10)
                if STOP.exists():
                    return "stop"
                if state["status"] == RealtimeSubscribeStates.CHANNEL_ERROR:
                    self.say(f"realtime channel error: {state['err']}")
                    return "error"
                if time.time() - started > 2.5 * 3600:
                    return "token refresh"
                if time.time() - self.last_msg > 900:
                    return "silent 15 min"
        finally:
            try:
                await client.close()
            except Exception:
                pass

    async def realtime_loop(self):
        while not STOP.exists():
            try:
                why = await self.realtime_session()
            except Exception as e:
                why = f"exception {str(e)[:200]}"
            self.event("realtime_rebuild", why)
            if why == "stop":
                return
            self.say(f"realtime reconnect ({why}); resyncing all markets")
            for mid in list(self.market_exs):
                await self.queue_resync(mid)
            await asyncio.sleep(5)

    # ---------------------------------------------------------------- periodic tasks
    async def kalshi_loop(self):
        rows = await asyncio.to_thread(kalshi.fetch_reference, ROOT / "data" / "kalshi_reference.json", 6 * 3600)
        races = {(o, pl) for o, pl in self.db.execute("SELECT office, place FROM markets WHERE office IS NOT NULL")}
        tickers = []
        for r in rows:
            self.db.execute("INSERT OR REPLACE INTO kalshi_map VALUES (?,?,?,?,?)",
                            (r["ticker"], r["office"], r["place"], r["party"], r["candidate"]))
            if (r["office"], r["place"]) in races:
                tickers.append(r["ticker"])
        self.db.commit()
        self.say(f"kalshi: polling {len(tickers)} contracts every 15 s")
        while not STOP.exists():
            t0 = time.time()
            for i in range(0, len(tickers), 40):
                try:
                    d = await asyncio.to_thread(http_json, "GET", f"{kalshi.BASE}/markets" + qs({"tickers": ",".join(tickers[i:i + 40])}), None, None, 20, 1)
                except Exception as e:
                    self.event("kalshi_error", str(e)[:200])
                    continue
                for m in d.get("markets", []):
                    f = lambda k: float(m[k]) if m.get(k) not in (None, "") else None
                    q = (f("yes_bid_dollars"), f("yes_ask_dollars"), f("yes_bid_size_fp"), f("yes_ask_size_fp"), f("last_price_dollars"))
                    if self.kalshi_last.get(m["ticker"]) != q:
                        self.kalshi_last[m["ticker"]] = q
                        self.db.execute("INSERT INTO kalshi VALUES (?,?,?,?,?,?,?)", (m["ticker"], time.time(), *q))
                        self.stats["kalshi_rows"] += 1
                await asyncio.sleep(0.2)
            self.db.commit()
            await asyncio.sleep(max(1.0, 15 - (time.time() - t0)))

    async def leaderboard_loop(self):
        while not STOP.exists():
            for period in ("all", "1d"):
                try:
                    b = await self.rest(self.sm.leaderboard, period, 100)
                    now = time.time()
                    for e in b.get("leaderboard") or []:
                        self.db.execute("INSERT INTO leaderboard VALUES (?,?,?,?,?,?,?,?,?)",
                                        (now, period, e.get("rank"), e.get("profileId"), e.get("username"), e.get("pnl"),
                                         e.get("tradesCount"), e.get("volume"), e.get("roi")))
                    self.db.commit()
                except Exception as e:
                    self.event("leaderboard_error", str(e)[:200])
            await asyncio.sleep(3600)

    async def markets_loop(self):
        while not STOP.exists():
            await asyncio.sleep(120)
            try:
                await self.refresh_markets()
            except Exception as e:
                self.event("markets_error", str(e)[:200])

    async def reconcile_loop(self):
        """Every 2 min: compare bulk top-of-book with what we hold; resync markets that disagree."""
        while not STOP.exists():
            await asyncio.sleep(120)
            try:
                px = await self.rest(self.sm.prices, list(self.ex_market))
                bad = 0
                for eid, p in px.items():
                    row = self.db.execute("SELECT best_bid, best_ask FROM books WHERE exchange_id=? ORDER BY recv_ts DESC LIMIT 1",
                                          (eid,)).fetchone()
                    if row is None or row[0] != p.get("bestBid") or row[1] != p.get("bestAsk"):
                        bad += 1
                        await self.queue_resync(self.ex_market[eid])
                self.event("reconcile", {"mismatched": bad})
            except Exception as e:
                self.event("reconcile_error", str(e)[:200])

    async def backfill_loop(self):
        """Slowly pull each exchange's full trade history (all trades since Oct 1)."""
        await asyncio.sleep(60)
        for eid in list(self.ex_market):
            if STOP.exists():
                return
            row = self.db.execute("SELECT done, cursor FROM backfill WHERE exchange_id=?", (eid,)).fetchone()
            if row and row[0]:
                continue
            cursor = row[1] if row else None
            while True:
                while self.resync_q.qsize() > 5:  # live resyncs first
                    await asyncio.sleep(5)
                try:
                    d = await self.rest(self.sm._get, f"/exchanges/{eid}/trades", tournamentId=self.tid, limit=200, cursor=cursor)
                except Exception as e:
                    self.event("backfill_error", {"ex": eid, "error": str(e)[:200]})
                    break
                for x in d.get("data") or []:
                    x["exchangeId"] = eid
                    self.store_trade(x, "rest")
                pg = d.get("pagination") or {}
                cursor = pg.get("nextCursor")
                done = not pg.get("hasMore") or not cursor
                self.db.execute("INSERT OR REPLACE INTO backfill VALUES (?,?,?)", (eid, int(done), cursor))
                self.db.commit()
                if done:
                    break
        self.say("trade history backfill complete")

    async def status_loop(self):
        while not STOP.exists():
            await asyncio.sleep(300)
            n_tr = self.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
            self.say(f"status: batches {self.stats['batches']}, new trades {self.stats['trades']} (total {n_tr}), "
                     f"book rows {self.stats['books']}, kalshi rows {self.stats['kalshi_rows']}, gaps {self.stats['gaps']}, "
                     f"resync queue {self.resync_q.qsize()}")

    async def main(self):
        n = await self.refresh_markets(first=True)
        self.say(f"Collector up: tournament {self.slug}, {n} markets, db {DB}")
        self.event("start", {"markets": n})
        loops = [self.realtime_loop, self.resync_worker, self.kalshi_loop, self.leaderboard_loop,
                 self.markets_loop, self.reconcile_loop, self.backfill_loop, self.status_loop]
        tasks = [asyncio.create_task(self.supervise(f)) for f in loops]
        while not STOP.exists():
            await asyncio.sleep(5)
        self.say("STOP_COLLECTOR found: shutting down")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.db.commit()

    async def supervise(self, fn):
        """Keep one loop alive: log and restart it if it crashes, so one bug never stops collection."""
        while not STOP.exists():
            try:
                await fn()
                return
            except Exception as e:
                import traceback
                self.event("task_crash", {"task": fn.__name__, "error": traceback.format_exc()[-1500:]})
                self.say(f"task {fn.__name__} crashed: {e!r}; restarting in 30 s")
                await asyncio.sleep(30)


if __name__ == "__main__":
    if STOP.exists():
        STOP.unlink()
    keep_awake()
    try:
        asyncio.run(Collector().main())
    except KeyboardInterrupt:
        print("collector stopped")
        sys.exit(0)
