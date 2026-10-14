# quote_probe

Same-moment quotes for **ETH/USDT, ETH/USDC, ETH/DAI and USDT/USDC** (both directions) at $50K / $100K / $300K.
Live runs append to `data/quotes_log_v3.csv` (gitignored). A trimmed example of the output is in `data/sample_quotes.csv`.

## Weekly workflow
1. `quote_probe.py` runs 3x daily at 11:00 / 16:00 / 21:00 local (each run ~5 min, 144 quotes) via Windows Task
   Scheduler; register with `powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1` (`-Remove` to undo).
   Scheduled runs use a hidden launcher; Task Scheduler tracks the probe until it finishes and receives its exit code.
   Each run's console output is appended to `data/probe_runs.log`. Manual runs still work; avoid starting one within
   ~15 min of another (CoW returned HTTP 429 on back-to-back runs) - same-slot runs are de-duplicated anyway.
2. On report day run `report.py`. Default window = rolling last 7 days to now (UTC); `--end "YYYY-MM-DD HH:MM"`
   moves the end, `--days N` changes the length, `--week 2026-W40` gives an ISO week, `--list-weeks` shows what's logged.
   Runs inside the same 3h UTC slot count once (`--slot-hours`). Outputs in `data/reports/`:
   `quote_overview_<label>.csv` (section 1), `price_impact_<label>.csv` (section 2), `quote_summary_<label>.json`
   (window, runs before/after de-dup, sample count, errors by venue).

$10K rows logged before 2026-10-09 are ignored by the report.

v5 rows logged before 2026-10-13 are **AMM-only** (strategy `AMMV2,RFQV2`; RFQV2 never answered). From 2026-10-13 the
probe also requests PMMV5 market-maker quotes, as the app does. A paired check on 2026-10-13 put PMMV5 5-15 bps better
on ETH/USDT and ETH/USDC (up to 24 bps on USDT->ETH $300K) and identical on ETH/DAI and USDT/USDC, so older v5 win rates
on the USD pairs understate the app. Don't pool windows across this date.

Section 1 overview: one row per pair (both directions pooled), an `All pairs` total, and one row per direction.
- `win_<size>_pct`: share of samples where **Tokenlon v5** net received >= best competitor (CoW, ParaSwap, Uniswap v3, 1inch).
  A sample = one run-slot x one direction x one size. Samples with no v5 quote are excluded and counted in `v5_no_quote`.
- `prefee_win_<size>_pct` / `avg_prefee_gap_50k_bps`: same, using v5's amount before its own fee. Net vs pre-fee
  difference = fee effect; the pre-fee gap = route / MM pricing effect.
- `main_loss_to` / `main_loss_share_pct`: competitor that was best in most of v5's losing samples.
- `avg_gap_50k_bps` / `median_gap_50k_bps`: v5 vs best competitor at $50K (core tier), negative = v5 worse.
- `status_net` / `status_prefee` by $50K win rate: green >= 60% (target), yellow 50-60%, red < 50%.
- `low_n` = 1 when fewer than 10 samples at $50K: treat that row's status as indicative only.

Section 2 price impact: per venue, bps worse at $100K / $300K than at $50K (positive = worse), paired within the
same run and direction, gas added back (so falling gas share at size doesn't look like better pricing).

## Setup & run
Needs Python 3.9+. A virtualenv is recommended (and required on Homebrew Python, which blocks global `pip install`).

**macOS / Linux**
```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 quote_probe.py --mock   # offline logic test
python3 quote_probe.py          # live
```
Optional env: `export ONEINCH_API_KEY=...`, `export ETH_RPC_URL=...`

If you installed Python from python.org on macOS and see `CERTIFICATE_VERIFY_FAILED`, run
`/Applications/Python 3.x/Install Certificates.command` once.

**Windows (PowerShell)**
The repo lives in Google Drive, which syncs `.venv` between machines, and a venv only runs on the OS that built it.
Keep the Windows venv outside the synced folder:
```
python -m venv $env:USERPROFILE\.venvs\quote_probe
& $env:USERPROFILE\.venvs\quote_probe\Scripts\Activate.ps1
pip install -r requirements.txt
python quote_probe.py --mock   # offline logic test
python quote_probe.py          # live
```
Optional env: `$env:ONEINCH_API_KEY="..."` (cmd: `set ONEINCH_API_KEY=...`), same for `ETH_RPC_URL`.

`scripts/check_1inch.py` is a one-off diagnostic for the 1inch API vs web-app gap (needs `ONEINCH_API_KEY`).

## Layout
```
quote_probe.py        main probe (scheduled 3x daily)
report.py             weekly overview from the log
scripts/              schedule_windows.ps1 (Task Scheduler setup), one-off diagnostics
data/                 logs + reports/ (gitignored) + sample_quotes.csv
```

## Venues
| Venue | How | Fee / gas treatment |
|---|---|---|
| Tokenlon v5 | WebSocket (publisher.tokenlon.im), throwaway key per run, in memory only; strategy AMMV1,AMMV2,PMMV5,RFQV1,RFQV2 | received = maker amount x (1 - feeFactor); gas paid by relayer (charge unconfirmed) |
| Tokenlon v6 | GET api.tokenlon.im/v6/swap/quote/1 | amountOut is pre-fee, pre-gas: fee deducted once, gasUsed x gasPrice deducted |
| CoW Swap | public quote API | gas netted in fee; excludes solver surplus |
| ParaSwap | public prices API | gasCostUSD deducted |
| Uniswap v3 | QuoterV2, single pool | floor only, not a routed price |
| 1inch | Swap API (key) | counts as a competitor; API quote ~24 bps below web app (unexplained), may understate 1inch |

## Key columns
- `bps_vs_best_competitor`: net received vs best competitor (excl. Tokenlon). For Tokenlon rows, >= 0 = Tokenlon wins.
- `pre_fee_bps_vs_best_competitor` (Tokenlon rows only): same, before Tokenlon's fee. Gap between the two = fee effect; pre-fee gap = route / liquidity effect.
- `fee_bps`, `source` (v5: protocol/mmCode; v6: orderType/protocols), `quote_id` (quote to backend when debugging).
- `bps_vs_mid`: vs Uniswap USDC/WETH 0.05% pool mid. Indicative only (pool mid can sit a few bps off market).

## Notes
- `data/sample_quotes.csv` is a single illustrative snapshot from one run, not a benchmark. Quotes move with market conditions, size and time; results will differ between runs.
- Running the probe sends a small number of quote requests to each venue. No orders are placed or signed.
