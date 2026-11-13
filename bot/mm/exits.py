"""Exit policy for mispricing trades (pure functions; shared by live, dry and shadow modes).

A mispricing trade: we TAKE a stale order at price p0 while fair value (Kalshi) is FV, e.g. buy YES at 0.80 with FV 0.91.
The gap we are owed is gap = d * (FV - p0) (here 0.11). Holding to settlement earns all of it but locks the cash until
Nov 4+. The exit policy trades part of the gap for getting the cash back sooner. Prices are YES terms; d = +1 for a long
YES position, -1 for a long NO position (= short YES).

  1. TARGET  On entry, rest a closing order capturing `start_capture` of the gap (near FV). Resting orders lock no cash.
  2. GLIDE   Over `glide_hours` the target slides to `end_capture` of the gap, so it gets more attractive to students
             the longer it sits. Gap is always measured against the CURRENT FV, so the target follows Kalshi.
  3. NO EDGE If FV has moved through our entry (gap <= 0), rest the exit at FV itself (stop adding EV losses).
  4. STOP    Optional: if FV moved against us by >= stop_fv_move since entry, exit at FV (thesis broken).
  5. CASH    `need_cash` or after `final_exit_et` (default Oct 31 noon): jump straight to `end_capture` to get flat,
             freeing cash for the final scenario book.
The research question is the trio (start_capture, end_capture, glide_hours): convergence.py measures how quickly contest
prices close gaps to Kalshi, and shadow.py replays exit policies on the collected order books and trades.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class ExitParams:
    start_capture: float = 0.9       # first target: 90% of the gap (close to FV)
    end_capture: float = 0.4         # after glide_hours: 40% of the gap
    glide_hours: float = 48.0
    stop_fv_move: float | None = None
    final_exit_et: str = "2026-10-31T12:00"
    min_profit: float = 0.005        # never rest an exit less than half a cent better than entry (unless no edge left)
    time_stop_h: float | None = None  # after this many hours: exit at market (contest-price evidence: gap trades pay
                                      # only when held 72-120 h; Oct 12 study)
    liquidate_et: str = "2026-10-31T18:00"   # from here on every lot exits at market: all cash free for the final bets
    market_floor: float = 0.10       # a market exit never sells below FV - this (or buys above FV + this)


@dataclass
class Lot:
    exchange_id: str
    d: int            # +1 long YES, -1 long NO
    qty: float
    p0: float         # entry price, YES terms
    fv0: float        # FV at entry
    t0: float         # entry time (epoch s)
    mode: str = "fv"  # "fv" (gap to Kalshi) or "model" (contest-price model)
    tp: float | None = None       # model lots: take-profit price (entry + predicted move)
    hold_h: float | None = None   # model lots: exit at market after this many hours
    horizon_h: float | None = None  # gap lots opened in the late window: glide and time stop end after this many hours
                                    # (= at the Oct 31 liquidation) instead of glide_hours / time_stop_h


def snap(p: float, tick: float, up: bool) -> float:
    x = p / tick
    x = math.ceil(x - 1e-9) if up else math.floor(x + 1e-9)
    return round(min(1 - tick, max(tick, x * tick)), 4)


def exit_price(lot: Lot, fv_now: float, now: float, X: ExitParams, tick: float = 0.005,
               past_final: bool = False, need_cash: bool = False, past_liquidate: bool = False) -> tuple[float | None, str]:
    """Limit price (YES terms) for the resting closing order and the reason.

    Long YES (d=+1) closes by SELLING YES at that price; long NO (d=-1) closes by BUYING YES at that price.
    Returns (None, reason) when the lot must exit at market (time stop, model hold over, liquidation deadline).
    Rounding is always in our favour (sell prices up, buy prices down)."""
    d = lot.d
    up = d > 0
    if past_liquidate:
        return None, "liquidate: exit at market"
    if lot.mode == "model":
        if lot.hold_h is not None and now - lot.t0 >= lot.hold_h * 3600:
            return None, "model hold over: exit at market"
        tgt = lot.tp if lot.tp is not None else lot.p0 + d * X.min_profit
        if d * (tgt - lot.p0) < X.min_profit:
            tgt = lot.p0 + d * X.min_profit
        return snap(tgt, tick, up), "model take-profit"
    glide_h, stop_h = X.glide_hours, X.time_stop_h
    if lot.horizon_h is not None:            # late-window lot: compress the glide and the time stop into the time left
        glide_h = min(glide_h, lot.horizon_h) if glide_h > 0 else glide_h
        stop_h = lot.horizon_h if stop_h is None else min(stop_h, lot.horizon_h)
    if stop_h is not None and now - lot.t0 >= stop_h * 3600:
        return None, "time stop: exit at market"
    gap = d * (fv_now - lot.p0)
    if X.stop_fv_move is not None and d * (fv_now - lot.fv0) <= -X.stop_fv_move:
        return snap(fv_now, tick, up), "stop: FV moved against entry"
    if gap <= 0:
        return snap(fv_now, tick, up), "no edge left: exit at FV"
    if past_final or need_cash:
        cap = X.end_capture
        why = "final exit" if past_final else "cash needed"
    else:
        w = min(1.0, max(0.0, (now - lot.t0) / 3600 / glide_h)) if glide_h > 0 else 1.0
        cap = X.start_capture + w * (X.end_capture - X.start_capture)
        why = f"target {cap:.0%} of gap"
    target = lot.p0 + d * max(cap * gap, X.min_profit)
    return snap(target, tick, up), why
