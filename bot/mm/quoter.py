"""Quote construction: pure functions, no I/O (unit-testable, shared by live and paper trading).

Avellaneda-Stoikov in practical form, in YES-price terms:
  reservation r = FV - skew * inventory_fraction * h       (lean away from the inventory you already hold)
  half-spread h = max(min_half, base + vol_mult * sigma_horizon + Kalshi half-spread) * regime multiplier
  bid = r - h, ask = r + h, then: never cross other people's quotes (that is a take, handled separately),
  improve the best price by only one tick when theory allows ("penny"), never add risk through FV.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class Inputs:
    fv: float
    ref_half_spread: float
    sigma_hourly: float
    exposure: float              # shares, + = long this contract (sibling-party positions already netted in)
    risk_used_race: float        # worst-case loss already committed in this race (SUSQies)
    risk_used_total: float
    best_bid_others: float | None
    best_ask_others: float | None
    spread_mult: float = 1.0
    size_mult: float = 1.0
    skew_mult: float = 1.0


@dataclass
class Quote:
    bid: float | None
    bid_qty: int
    ask: float | None
    ask_qty: int
    half: float
    reservation: float
    note: str = ""


def snap(p: float, tick: float, mode: str) -> float:
    x = p / tick
    x = math.floor(x + 1e-9) if mode == "down" else math.ceil(x - 1e-9)
    return round(min(1 - tick, max(tick, x * tick)), 4)


def half_spread(inp: Inputs, P) -> float:
    sig_h = inp.sigma_hourly * math.sqrt(P.vol_horizon_min / 60)
    h = max(P.min_half_spread, P.base_half_spread + P.vol_mult * sig_h + inp.ref_half_spread)
    return h * inp.spread_mult


def capacities(inp: Inputs, P) -> tuple[float, float]:
    """Shares we may still BUY / SELL before hitting per-race or total worst-case-loss limits."""
    fv = inp.fv
    room_race = max(0.0, P.max_risk_per_race - inp.risk_used_race)
    room_total = max(0.0, P.max_total_risk - inp.risk_used_total)
    room = min(room_race, room_total)
    x = inp.exposure
    # buying adds long risk (lose ~fv per share) unless it reduces a short
    can_buy = (max(0.0, -x) + room / max(fv, 0.01))
    can_sell = (max(0.0, x) + room / max(1 - fv, 0.01))
    return can_buy, can_sell


def make_quote(inp: Inputs, P) -> Quote:
    tick, fv = P.tick, inp.fv
    if not (P.min_fv <= fv <= P.max_fv) or inp.size_mult <= 0:
        return Quote(None, 0, None, 0, 0.0, fv, "outside FV band or size 0")
    h = half_spread(inp, P)
    unit_risk = fv if inp.exposure > 0 else (1 - fv)
    inv_frac = max(-1.0, min(1.0, inp.exposure * unit_risk / P.max_risk_per_race))
    r = fv - P.skew_strength * inp.skew_mult * inv_frac * h
    bid_t, ask_t = r - h, r + h
    # never add risk through fair value
    if inv_frac >= 0:
        bid_t = min(bid_t, fv - P.min_edge_vs_fv)
    if inv_frac <= 0:
        ask_t = max(ask_t, fv + P.min_edge_vs_fv)
    bid, ask = snap(bid_t, tick, "down"), snap(ask_t, tick, "up")
    if P.penny:
        if inp.best_bid_others is not None and inp.best_bid_others + tick <= bid:
            bid = round(inp.best_bid_others + tick, 4)
        if inp.best_ask_others is not None and inp.best_ask_others - tick >= ask:
            ask = round(inp.best_ask_others - tick, 4)
    # do not cross other people's resting orders (crossing = taking, decided in take_orders)
    if inp.best_ask_others is not None and bid >= inp.best_ask_others:
        bid = round(inp.best_ask_others - tick, 4)
    if inp.best_bid_others is not None and ask <= inp.best_bid_others:
        ask = round(inp.best_bid_others + tick, 4)
    if bid >= ask:
        bid = round(ask - tick, 4)
    can_buy, can_sell = capacities(inp, P)
    # equal share size on both sides (equal sensitivity to FV moves); worst case per side <= quote_notional
    shares = P.quote_notional * inp.size_mult / max(fv, 1 - fv)
    bid_qty = int(min(shares, can_buy)) if bid >= tick else 0
    ask_qty = int(min(shares, can_sell)) if ask <= 1 - tick else 0
    return Quote(bid if bid_qty >= 1 else None, bid_qty, ask if ask_qty >= 1 else None, ask_qty, h, r,
                 f"inv {inv_frac:+.2f}")


def take_orders(inp: Inputs, P, bids: list[tuple[float, float]], asks: list[tuple[float, float]]):
    """Stale prices: others' asks well below FV (buy them) or bids well above FV (sell to them).

    bids/asks: other people's levels [(price, qty)], best first. Returns [(action, limit_price, qty)]."""
    out = []
    edge = max(P.take_edge, 1.5 * half_spread(inp, P) if inp.spread_mult > 1 else P.take_edge)
    can_buy, can_sell = capacities(inp, P)
    lim = inp.fv - edge
    qty, worst = 0.0, None
    for px, q in asks:
        if px > lim + 1e-9:
            break
        qty += q
        worst = px
    if worst is not None:
        qty = min(qty, P.take_max_notional / max(worst, 0.01), can_buy)
        if qty >= 1:
            out.append(("buy", worst, int(qty)))
    lim = inp.fv + edge
    qty, worst = 0.0, None
    for px, q in bids:
        if px < lim - 1e-9:
            break
        qty += q
        worst = px
    if worst is not None:
        qty = min(qty, P.take_max_notional / max(1 - worst, 0.01), can_sell)
        if qty >= 1:
            out.append(("sell", worst, int(qty)))
    return out
