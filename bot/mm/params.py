"""Market-making parameters. Override any of them with a JSON file: run_mm.py --params my.json"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field


@dataclass
class Params:
    # --- loop and budgets (contest: 100 reads + 30 writes per minute per ACCOUNT) ---
    cycle_s: float = 4.0                 # main loop period
    writes_per_min: int = 26             # leave headroom under 30
    orders_every: int = 1                # read open orders every N cycles
    positions_every: int = 2             # read positions every N cycles
    fills_every: int = 1                 # read fills every cycle (exits must see their own fills before re-acting)
    book_refresh_s: float = 30.0         # full order-book refresh per market (to see depth excluding our orders)

    # --- fair value (Kalshi) ---
    fv_max_age_s: float = 25.0           # older Kalshi data => pull quotes in that market
    kalshi_poll_s: float = 10.0          # Kalshi quote refresh period (1,008 tickers = 26 requests per poll)
    fv_max_ref_spread: float = 0.06      # Kalshi spread wider than this => FV unreliable, pull quotes
    use_microprice: bool = True          # size-weighted mid instead of plain mid

    # --- quoting (all prices in YES terms, probability units) ---
    tick: float = 0.005
    min_half_spread: float = 0.010       # never quote tighter than +-1c around reservation price
    base_half_spread: float = 0.005      # added to the vol term
    vol_mult: float = 1.0                # half spread += vol_mult * sigma over the horizon below
    vol_horizon_min: float = 30.0        # how long a quote may sit before we expect to refresh it
    default_hourly_sigma: float = 0.004  # prior hourly sd (prob units) until live estimates exist
    min_edge_vs_fv: float = 0.005        # never buy above FV - this (or sell below FV + this) when adding risk
    penny: bool = True                   # improve the best price by one tick when theory allows
    min_fv: float = 0.04                 # do not make markets in contracts priced below this...
    max_fv: float = 0.96                 # ...or above this (tick risk, little flow)
    requote_ticks: int = 2               # requote when target moves at least this many ticks...
    min_quote_life_s: float = 20.0       # ...and the quote has rested this long (dangerous quotes are pulled at once)
    quote_ttl_s: float = 900.0           # quotes auto-expire on the exchange; the bot re-places them. If the bot dies
                                         # (crash, laptop sleep), nothing stale stays on the book longer than this. 0 = never

    # --- sizing and inventory ---
    quote_notional: float = 1500.0       # quote size in shares = quote_notional / max(FV, 1-FV), same on both sides
    max_risk_per_race: float = 8000.0    # worst-case loss allowed per race (both D and R books together)
    max_total_risk: float = 45000.0      # worst-case loss across everything
    skew_strength: float = 1.0           # at max inventory the reservation price shifts by this many half-spreads
    max_daily_loss: float = 6000.0       # mark-to-FV loss since start of day => cancel everything and stop

    # --- taking stale prices (lag arbitrage against Kalshi) ---
    take_enabled: bool = True
    take_edge: float = 0.02              # take when a resting price is this far through FV
    take_max_notional: float = 3000.0
    take_ttl_s: int = 20                 # take orders expire quickly so remainders never rest
    take_min_fv: float = 0.10            # only take in competitive races: long-shot gaps (FV < 0.10 or > 0.90) are sticky
    take_max_fv: float = 0.90            # (convergence study: they take ~200 h to close half the gap; they lock cash)
    entry_mode: str = "fv"               # "fv" = gaps vs Kalshi; "model" = contest-price model; "both"; "none" = exits only
    model_thr: float = 0.02              # model mode: predicted 24h contest move needed to enter
    model_slip: float = 0.005            # model mode: pay at most the model's reference contest price + this
    model_hold_h: float = 24.0           # model mode: exit at market after this many hours (take-profit at entry + prediction)
    model_reserve: float = 5000.0        # "both": gap entries stop this far below max_total_risk, leaving room for model trades
    fresh_h: float = 0.0                 # >0: only take gaps that opened within this many hours (fresh-gap rule; off = 0)
    fresh_max: float = 0.01              # "opened" = back then the contest was within this of Kalshi in our direction
    # entry rules, Oct 16. Price band for NEW entries (fv and model), on the contest prices we would trade at: below 0.10 /
    # above 0.90 gap trades did not pay (sell YES at 0.05-0.10 with a >=4c gap: -0.1c, 49% hit; buy YES at 0.90-0.95:
    # +0.7c, 55%) and tie up the most cash per cent of edge (a short at 0.08 locks 0.92 a share). Exits are unaffected.
    entry_min_px: float = 0.10
    entry_max_px: float = 0.90
    # Directional lean: entries betting AGAINST lean_party (buy the other major party's YES, or sell lean_party's YES) need
    # lean_extra more edge (fv: take_edge + lean_extra; model: model_thr + lean_extra). "" = off.
    lean_party: str = ""
    lean_extra: float = 0.0
    # Tiered sizing (Oct 22 review: since Oct 10, gaps >= 8c paid +2.5c at 120 h with 92% hit vs +0.9c / 61% for 6-8c;
    # backtest with these sizes +43% at contest prices on the same capital). Gaps >= big_edge (+ lean_extra against the
    # lean party) get big_take_notional per take and big_risk_per_race per race; smaller gaps keep take_max_notional /
    # max_risk_per_race, so they cannot add to a race that is already above the normal cap. 0 = off.
    big_edge: float = 0.0
    big_take_notional: float = 8000.0
    big_risk_per_race: float = 16000.0
    # Late entries: once a full time_stop_h hold no longer fits before the Oct 31 liquidation, keep taking BIG-tier gaps
    # while at least late_min_h hours remain; their glide and time stop end at the liquidation. 0 = off.
    late_min_h: float = 0.0

    # --- jumps (unscheduled news) ---
    jump_threshold: float = 0.02         # FV move within jump_window_s that counts as news
    jump_window_s: float = 120.0
    jump_cooldown_s: float = 600.0       # widen for this long after a jump
    jump_mult: float = 2.0

    # --- events (data/events.csv) ---
    pre_event_mult: float = 1.5
    post_event_mult: float = 1.5
    live_event_action: str = "pull"      # "pull" quotes during a live event in affected races, or "widen"
    live_event_mult: float = 3.0
    election_night_cutoff_et: str = "2026-11-03T17:00"  # MM stops quoting here; switch to take-only

    # --- election night (mm/election.py). OFF unless a params file sets election_mode true:
    # then from en_start_et the engine is take-only against Kalshi instead of pulling everything at the cutoff above ---
    election_mode: bool = False
    en_start_et: str = "2026-11-03T17:00"
    en_end_et: str = "2026-11-04T11:55"          # nothing placed after this (trading closes at noon)
    en_control_open_et: str = "2026-11-03T19:00" # chamber-control markets eligible from here (open tier until called)
    en_gate: bool = True                         # a race is eligible only after its state's polls close (data/poll_close.csv)
    en_cycle_s: float = 3.0
    en_called_fv: float = 0.97                   # tiers by Kalshi FV, either side: called >= 0.97 held en_hold_called_s,
    en_lean_fv: float = 0.85                     # leaning 0.85-0.97 and open 0.15-0.85 held en_hold_s
    en_hold_called_s: float = 60.0
    en_hold_s: float = 120.0
    en_edge_called: float = 0.03                 # take edge by tier, plus Kalshi's half-spread
    en_edge_lean: float = 0.06
    en_edge_open: float = 0.08
    en_called_max_spread: float = 0.03           # called tier needs Kalshi spread <= this (others: fv_max_ref_spread)
    en_slow_mult: float = 2.0                    # slow-settling races (data/election_slow.csv): edge x this and
    en_slow_weight: float = 0.5                  # half the priority (their cash cannot be recycled on the night)
    en_cap_called: float = 0.30                  # per-race risk, fraction of V0 (account value at en_start); set Oct 19
    en_cap_lean: float = 0.10
    en_cap_open: float = 0.05
    en_cap_uncalled: float = 0.35                # leaning + open risk across all races
    en_cash_buffer: float = 200.0
    en_fv_max_age: float = 15.0
    en_take_ttl_s: int = 10                      # election takes expire fast (a stale resting bid is what we pick off)
    en_lock_s: float = 8.0                       # no repeat order on the same contract and side within this
    en_batch_max: int = 50                       # orders per /orders/batch call (one write)
    en_recycle_fv: float = 0.985                 # sell a held called winner (our side's FV, held en_hold_called_s)
    en_recycle_min_px: float = 0.96              # at a price >= this, when the best take left unfilled for lack of
    en_recycle_margin: float = 0.02              # cash returns >= the give-up (1 - px) / px + this
    en_rev_called: float = 0.90                  # reversal breaker: a held side that peaked >= en_called_fv and is now
    en_rev_lean: float = 0.70                    # < 0.90 (or peaked >= en_lean_fv, now < 0.70) blocks its race;
    en_rev_n: int = 3                            # en_rev_n reversals within en_rev_window_s block all leaning/open entries
    en_rev_window_s: float = 1800.0
    en_hot_max: int = 80                         # races polled on Kalshi every kalshi_hot_poll_s
    kalshi_hot_poll_s: float = 2.0

    markets: list = field(default_factory=list)  # contest market ids to make; empty => auto-select

    @classmethod
    def load(cls, path: str | None) -> "Params":
        p = cls()
        if path:
            for k, v in json.load(open(path, encoding="utf-8")).items():
                if not hasattr(p, k):
                    raise SystemExit(f"unknown parameter {k}")
                setattr(p, k, v)
        return p

    def dump(self) -> dict:
        return asdict(self)
