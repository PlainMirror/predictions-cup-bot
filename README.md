# Predictions Cup Bot

This is the trading system I built for the Susquehanna Predictions Cup, a play-money prediction market tournament on the 2026 US midterm elections, with cash prizes for the top three. Students traded contracts like "Will the Democratic Party win the Maine Senate?" on the contest's own exchange, while the same races were trading on Kalshi with real money. My starting point was that Kalshi's real-money prices are a better estimate of each race than a book priced mostly by other students. So whenever a contest order sat far enough away from Kalshi, the bot took it, and then waited for the gap to close.

It traded live from October 11 to election night (November 3), starting from 100,000 play-money "SUSQies". No real money was involved.

## Results

- October 11 to 25: the account went from 100,000 to 104,802 (+4.8%), all of it realised. By October 25 the bot had closed every position, and the gaps had mostly dried up (a 6.5-hour session on October 26 found nothing to take), so it stayed flat at 104,802 until election night.
- Through October 22 the live ledger has 612 exits on 182,682 shares, realised +3,343.
- Final account value: **120,718 (+20.7%)**. Election night added 15,916 on top of the 104,802: about 10,800 from the Maine Senate bet (see "The final Senate bets" below) and the rest from the election-night engine.
- That was not enough to place. The recorded leaderboard goes down to rank 100, and on its last snapshot (November 5) every account on it was above +20,718.

## How it works

```
Kalshi public API     ->  fair value per contract (size-weighted mid of Kalshi's book, normalised per race)
contest realtime feed ->  collector.py  ->  SQLite (books, trades, Kalshi prices, leaderboard)
                                 |
                                 v
mm/engine.py, every few seconds
  gap takes    take any contest order at least 6c through fair value (only when fair value is 0.10 to 0.90)
  model takes  contest_model.py predicts a 2c+ move in the next 24 h  ->  hold 24 h
  exits        a resting exit for every lot: target glides from 70% to 0% of the gap over 120 h, time stop at 120 h
  risk         3k per take, 8k per race, 70k total (gaps of 8c+: 8k per take, 16k per race), 5k loss stop
  API budget   under 100 reads and 30 writes a minute
                                 |
                                 v
data/mm_logs/*.jsonl  ->  markouts.py, research/ledger.py
```

Two extra rules applied to new entries only: nothing at contest prices below 0.10 or above 0.90, and a constraint to lean toward the party I judged more likely to win (`lean_party`, `lean_extra`): entries against that party needed 2c more edge. The party is left blank in the parameter files here.

## Why these rules?

### Do small gaps pay?

No. This is the live ledger (October 12 to 22) split by how far the contest was from Kalshi when the bot entered:

| Gap at entry | Exits | Shares | P&L per share | Share of shares that made money |
|---|---|---|---|---|
| under 6c | 114 | 37,412 | +0.19c | 46% |
| 6 to 8c | 361 | 76,780 | +3.59c | 92% |
| 8 to 10c | 13 | 9,767 | +3.42c | 100% |

Gaps under 6c barely broke even after the spread, which is why the take threshold went up to 6c. The exit route mattered too: lots that hit their resting target made +3.8c a share (86% of shares won, median hold 86 hours), while lots that ran into the 120-hour time stop made only +0.6c.

### What did the backtest say?

`research/policy_grid.py` replays every contest trade from October 10 to 22 through the engine's exit logic, with a 70k budget (P&L marked at contest prices, full table in `research/policy_grid_out.txt`):

| Policy | P&L | Entries |
|---|---|---|
| live settings (6c edge, exit 70% to 30% over 120 h) | 11,202 | 3,176 |
| exit gliding to 0% instead of 30% | 11,515 | 3,440 |
| no time stop | 10,172 | 2,238 |
| 5c edge | 6,741 | 4,655 |
| 8c edge | 11,890 | 829 |
| fair value band 0.05 to 0.95 instead of 0.10 to 0.90 | 8,277 | 3,831 |

So a lower threshold lets in a lot more trades that don't pay, and widening the price band makes it worse. An 8c threshold made about the same money with a quarter of the entries. Taking this into account, on October 22 I kept the 6c threshold but sized gaps of 8c or more at 8k per take and 16k per race, and moved the exit glide to 0%.

### Did the contest model help?

Not really. `contest_model.py` is a ridge regression of each contract's next-24-hour contest price move, using its gap to Kalshi, how far that gap is from its own 24 and 72-hour average, the mismatch between the D and R contracts in the same race, and short-term momentum. In a walk-forward test (October 7 to 12) predictions of 2c or more made +0.5c per trade with about a 60% hit rate. Live it was about flat: +230 across 118 exits.

## Market making (built and simulated, never run live)

`mm/quoter.py` quotes both sides with a practical Avellaneda-Stoikov model: the reservation price leans away from inventory, the half-spread widens with volatility and around scheduled events (quotes are pulled during debates), and doubles for 10 minutes after any sudden 2c move on Kalshi. The paper simulator shows how much it depends on who else is in the book:

| 48 h paper run (`run_mm.py --paper`) | P&L | Fills | Edge at fill |
|---|---|---|---|
| against a tight house market maker | +625 | 130 | +0.81c a share |
| books priced only by students (20 orders an hour per market) | +35,644 | 8,579 | +1.28c a share |

That second number rests entirely on the assumed student order flow, which I couldn't measure well enough to trust. The quoting side was never tested in the shadow engine, so it never ran live, and every live result above comes from gap and model trades.

## Election night

From 5 pm ET on November 3 the engine switched to a take-only mode (`mm/election.py`):

- A race only became eligible once its state's polls had closed (`data/poll_close.csv`).
- Each race was put in a tier by Kalshi's fair value, held for 60 to 120 seconds so a single tick couldn't trigger anything: called (0.97 or more), leaning (0.85 to 0.97) or open. The required edge was 3c, 6c or 8c plus Kalshi's half-spread, and twice that in slow-counting races (`data/election_slow.csv`).
- Risk was capped at 30% of the 5 pm account value per called race, 10% per leaning race and 5% per open race.
- When cash ran out, it sold called winners at 0.96 or more to fund better takes.
- A reversal breaker blocked any race where our side had been called and then fell back below 0.90.
- A file switch (`data/EN_FREEZE`) stopped new risk.

`tests/test_election.py` runs all of it on a scripted night with a fake exchange and clock.

On the night it took called races from 7 pm (the House and the Virginia Senate, for example). Overnight, Kalshi's prices for 16 races that were already decided dropped to exactly 0.50, six of them in the same second, most likely as Kalshi closed and settled those markets. The reversal breaker read that as the races flipping and blocked them, so those positions were held to settlement and their cash couldn't be recycled. The breaker should have checked whether Kalshi had closed a market before treating a move as a reversal.

It does now. The recorded quotes show what a closed market looks like: bid 0.00, ask 1.00 and no size, and the mid of that is 0.50. `mm/fv.py` no longer reads an empty book as a price. The fair value keeps its last real value and is marked closed, it ages so the staleness guards stop new entries, the breaker skips it, and a called winner in a closed market can still be sold to fund other takes. `tests/test_closed_market.py` replays the Virginia Senate quotes from that night and checks that the race is not blocked, and that a real reversal on a live book (0.99 falling to 0.80) still is.

### The final Senate bets

On November 3 I also made two hold-to-settlement bets, and the races came out of the same data the bot was built on. For every competitive race I lined up the contest price against Kalshi from the bot's recorded and live prices, compared both with the final polls-only forecast (Silver Bulletin, November 2) and recent polls, and then read the live order books to see how much was actually for sale at a good price.

Two races stood out. In the Kansas and Maine Senate races, Democrats were selling at about 0.66 in the contest, close to Kalshi (0.64 and 0.70), while the polls-only forecast gave them about 75%. Both also had real depth: about 33,000 shares were offered at 0.70 or less in Kansas, and 65,000 in Maine. (Note: this is a different kind of trade from the rest of the bot. Here the contest and Kalshi agreed, so the edge depended on the polls-only forecast being closer to the truth than both markets.) I left out races where the contest was already at or above the models (Alaska Senate, Texas Governor, Senate control), and Iowa and Texas Senate, where almost nothing was for sale.

The orders went in through `bot/trade.py` as limit buys capped at 0.70 that expired after 3 minutes. Maine filled 37,192 shares at 0.70. Kansas filled nothing, because by the time the order ran (7:32 pm ET) no one was selling at 0.70 or less. Democrats won Maine, and the bet made about 10,800: the engine sold 11,380 of the shares back at 0.96 to 0.965 during the night to fund other takes, and the rest settled at 1.

## Hardware

The bot ran on my laptop instead of an always-on server, so sleep, Windows updates and power loss took it offline for much of the contest, and the results above come from a bot that wasn't running full time. A small cloud machine running the collector and the engine as services that restart on their own would remove that limit, and the engine already picks up its open positions when it starts.

## Things that went wrong

- A double count. On October 21 an exit fill showed up first in the positions read and then again a few seconds later in the fills feed, and the engine subtracted it twice. That phantom hedge let a second short through, so one race had 10.1k at stake against an 8k cap. The fix only counts a fill if it is newer than the last positions read, and `tests/test_doublefill.py` replays the exact sequence.
- Stale data after a sleep. After one 10-hour sleep the contest model kept predicting from the pre-sleep table. It now makes no predictions at all when its data is stale.

## Running it

The engine itself is pure Python standard library, and the research and model scripts need numpy, pandas and scikit-learn (`pip install -r requirements.txt`). These run without any account:

```bash
cd bot
python run_mm.py --paper                  # 48 h market-making simulation against a house market maker
python run_mm.py --paper --no-house       # same, books priced only by students
python tests/test_election.py             # election-night mode on a scripted night
python tests/test_rules.py                # entry price band and lean
python tests/test_tiers.py                # size tiers, late-window entries, exit glide
python tests/test_doublefill.py           # the October 21 double count, replayed
python tests/test_closed_market.py        # a Kalshi market closing is not a reversal
```

Live trading (`run_mm.py --live`, `collector.py`, `scan.py`, `trade.py`) needs an API key for the contest platform in `SM_API_KEY`, and the tournament is over now. `bot/README.md` and `bot/mm/README.md` describe the full toolkit.

| Path | What it is |
|---|---|
| `bot/mm/engine.py` | the trading loop: fair values, takes, exits, risk limits, API budget |
| `bot/mm/election.py` | election-night take mode |
| `bot/mm/fv.py`, `bot/kalshi.py` | Kalshi fair values |
| `bot/mm/exits.py` | exit targets and time stops |
| `bot/mm/quoter.py`, `bot/mm/paper.py`, `bot/mm/shadow.py` | quoting, paper exchange, shadow exchange (virtual orders on live data) |
| `bot/contest_model.py` | the 24-hour contest price model |
| `bot/collector.py` | 24/7 recorder of contest books, trades, Kalshi prices and the leaderboard |
| `bot/sm_client.py` | contest API client, paced under the rate limits |
| `bot/backtest.py`, `research/` | backtests, the live ledger and the policy grid |
| `data/` | event calendar, poll closing times, slow-counting races, and the live parameter files |

The recorded market data (about 2.4 GB) and the logs aren't included. Not affiliated with Susquehanna, and the platform's API documentation isn't included either.

Tested with Python 3.13.
