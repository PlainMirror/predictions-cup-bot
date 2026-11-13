# Predictions Cup toolkit

Pure Python 3 standard library (nothing to install). Run everything from this `bot/` folder.

## One-time setup
1. On sig.thesuper.market: **My Profile → API Keys → Create New API Key**. Tick `read` (and `trade` if you
   want `trade.py --live`). Copy it immediately; it is shown once.
2. Store it for your shell session only (do not paste it into any file you share):
   ```powershell
   $env:SM_API_KEY = "<your key>"
   ```
3. Make at least one trade on the site (or via the API) so you are enrolled and ranked.

## Daily loop
```powershell
py scan.py                                   # ~1-2 min: contest prices + Kalshi fair values + engine violations
# read reports\scan_<stamp>.md ; set approve=1 on rows you want in reports\orders_<stamp>.csv
py trade.py orders_<stamp>.csv               # dry run: shows exactly what would be sent
py trade.py orders_<stamp>.csv --live        # sends; pairs/quotes go as atomic multi-leg orders
```
Your own estimates go in `..\data\fv_overrides.csv` and override Kalshi for that contract.

## Files
| file | what it does |
|---|---|
| `kalshi.py` | Free Kalshi market data for ~500 races (control, Senate, governor, House). `py kalshi.py` prints the table. |
| `sm_client.py` | Contest API client (paced under 100 reads / 30 writes per minute, retries 429/503). |
| `senate_model.py` | Correlated Monte Carlo of Senate control; backs out the race correlation implied by Kalshi. |
| `scan.py` | Finds ENGINE / PAIR / CHAMBER / EDGE / QUOTE opportunities, sizes them, writes the report + order CSV. |
| `trade.py` | Places approved orders (dry run unless `--live`), logs to `..\data\trade_log.jsonl`. |
| `run_mm.py` + `mm/` | **Market maker**: fair value, quoting, event calendar, risk limits, paper simulator. See `mm/README.md`. |
| `markouts.py` | Adverse-selection check on live fills; writes the ML training table. |
| `leaders.py` | Leaderboard diagnosis: how the leaders are making money. |
| `collector.py` | 24/7 market-data recorder → `data/market_data.sqlite` (books, trades + history, Kalshi, leaderboard, new-market alerts). |
| `run_mm.py --shadow` | The real engine trading VIRTUAL orders against live data (takes + exits everywhere, MM where selected). |
| `convergence.py`, `liquidity.py`, `allocate.py` | Gap-closing speed, market-making market ranking, depth-aware allocation. All read-only. |

`py scan.py --snapshot ..\data\sample_contest_snapshot.json` runs offline on a synthetic snapshot
(prices copied from the public pages on 2026-10-09, with fake ±0.01 books), which is how the code was tested
before the first live run.
