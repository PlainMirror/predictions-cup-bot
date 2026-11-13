# Market maker

## How it works
```
Kalshi (public API, polled each cycle) ──► fair value per contract (microprice, normalised per race)
                                            volatility (EWMA, seeded from 7 days of hourly candles)
                                            jump flag (move >= 2c within 2 min = unscheduled news)
data/events.csv (debates, FEC, CPI, FOMC, early voting, election night) ──► regime per race
contest API: bulk top-of-book, order books, open orders, positions, fills
        │
        ▼
quoter.make_quote  (Avellaneda-Stoikov, practical form, YES-price terms)
   reservation  r = FV − skew × inventory_fraction × h          inventory counts the D and R books of a race together
   half-spread  h = max(1c, 0.5c + σ_30min + Kalshi half-spread) × regime multiplier
   bid/ask      = r ∓ h, improve the best price by one tick if theory allows, never cross, never add risk through FV
   size         = equal shares both sides, capped by per-race and total worst-case-loss limits
quoter.take_orders: buy asks ≥ 2c below FV / sell into bids ≥ 2c above FV (20 s expiry so nothing rests)
        │
        ▼
engine: spend ≤ 26 writes/min by priority: dangerous quotes → takes → requotes (≥ 2 ticks moved and ≥ 20 s old)
        log everything to data/mm_logs/*.jsonl  →  markouts.py  →  data/fills_features.csv (ML training table)
```

## Run it
```powershell
$env:SM_API_KEY = "<key with read + trade>"
py run_mm.py --select --top 30   # rank markets by flow x capturable spread; writes data/mm_markets.json
py run_mm.py --dry               # live data, shows the quotes it WOULD place; nothing sent. Run this first.
py run_mm.py --live              # trade. Ctrl+C or create data\STOP to stop; it cancels all resting orders.
py markouts.py                   # after a day: per-market edge and +1m/+5m/+30m markouts
py leaders.py                    # how the leaderboard leaders are making money
py run_mm.py --paper [--no-house] [--noise 5] [--hours 48]   # simulator, no key needed
```
Tune with a JSON file of overrides (`--params my.json`), e.g. `{"quote_notional": 4000, "max_risk_per_race": 20000}`.
Don't trade by hand in markets the bot is making: it treats every open order there as its own and will cancel it.

## Which markets
`--select` scores each market by **24h volume × the spread we can actually earn**. Markets with a one-tick
book (0.5c wide) already have Susquehanna's market maker in them. We'd sit behind its queue at a worse
price and rarely fill, so they score low. In the simulator, making markets against a tight house quote earned about **+0.5k per 48h**
across 9 markets. Books priced only by students earned **+7k to +35k per 48h**, depending on how many student orders
arrive (5–20 per hour per market; this assumption is the big unknown, and `--select` will measure it). The money is in the
~200 student-priced markets, not the featured ones. The bot uses about 1.5 writes/min per 9 markets, so it can run 50–100 markets
under the 30 writes/min limit (it slows its loop to 6–8 s when running more than 15 markets, to stay under 100 reads/min).

## The event calendar: what it does, and why it isn't "buy before, sell on the day"
The last 30 days of Kalshi prices: typical moves are 0.3–0.5c per hour and 1–2c per day in competitive races. The biggest
days were unscheduled news (Maine −7.5c on Sep 24, PA-07 −11c overnight Sep 11), not debates, and moves went both ways.
A scheduled event has no predictable direction, so buying the day before is a coin flip. What events reliably change is
risk: price jumps and informed traders. So the calendar makes the bot:
- **before** (2–3 h): spreads ×1.5, size ×0.5, lean harder toward flat inventory;
- **during** (debates: severity 2–3): pull quotes in the affected races (`live_event_action: "widen"` to keep quoting ×3 wide);
- **after**: spreads ×1.5 while the market settles down. Volume is usually highest here, so this is where spread income peaks;
- **jumps** (any 2c move within 2 minutes on Kalshi): spreads ×2 for 10 minutes. This covers polls and scandals that no calendar has.
The stale-price taker stays on throughout. After news, the contest lags Kalshi, which is the reliable way to profit from events.

Edit `data/events.csv` as dates firm up. Debate times marked TBC are guesses, and Kansas Oct 21 needs checking.

## ML: what helps and what doesn't
| Use | Model | Status |
|---|---|---|
| Fair value | None. Kalshi's microprice is already a real-money consensus. Optional: a **Kalman filter** blending Kalshi + Polymarket when Kalshi is stale or one-sided. | built (Kalshi only) |
| Volatility → spread width | **EWMA** of FV changes (built). Upgrade path: GARCH(1,1) on logit changes with event dummies. Low marginal value. | built |
| Where to place quotes | **Fill-probability model**: logistic regression P(fill within T) ~ distance from FV in ticks, book spread, recent trade rate, time of day. Choose the price that maximises edge × P(fill). | after ~1–2k quotes logged |
| When we're being picked off | **Markout model**: gradient-boosted trees (LightGBM) or logistic regression predicting the 5-minute markout from: contest-vs-Kalshi gap, Kalshi move in the last 1–5 min, book imbalance, trade size, minutes to the next event, jump flag. Widen or skip when it predicts a loss. Train on `fills_features.csv` with time-ordered CV. | after ~1 week of live fills |
| Skip | Deep learning / LSTMs / reinforcement learning: a few thousand fills over 3.5 weeks is far too little data, and they overfit. Avellaneda–Stoikov already *is* the right model for quoting. | — |

## Honest economics
Market-making income is limited by order flow, not capital. Resting orders don't lock cash, so a bigger balance doesn't
make more fills. Income ≈ (shares students trade against you) × (about 1–1.5c edge). Even 100 markets with healthy
student flow is plausibly tens of thousands of SUSQies a day, not hundreds of thousands. Scaling to more markets and
keeping the taker on are the main levers.
