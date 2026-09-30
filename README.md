# quote_probe

Same-moment quotes for **ETH/USDT only** (both directions) at $10K / $50K / $100K.
Live runs append to `data/quotes_log_v3.csv` (gitignored). A trimmed example of the output is in `data/sample_quotes.csv`.

## Run (Windows)
```
pip install -r requirements.txt
python quote_probe.py --mock   # offline logic test
python quote_probe.py          # live
```
Optional env: `ONEINCH_API_KEY`, `ETH_RPC_URL`.

`scripts/check_1inch.py` is a one-off diagnostic for the 1inch API vs web-app gap (needs `ONEINCH_API_KEY`).

## Layout
```
quote_probe.py        main probe
scripts/              one-off diagnostics
data/                 logs (gitignored) + sample_quotes.csv
```

## Venues
| Venue | How | Fee / gas treatment |
|---|---|---|
| Tokenlon v5 | WebSocket (publisher.tokenlon.im), throwaway key per run, in memory only | received = maker amount x (1 - feeFactor); gas paid by relayer (charge unconfirmed) |
| Tokenlon v6 | GET api.tokenlon.im/v6/swap/quote/1 | amountOut is pre-fee, pre-gas: fee deducted once, gasUsed x gasPrice deducted |
| CoW Swap | public quote API | gas netted in fee; excludes solver surplus |
| ParaSwap | public prices API | gasCostUSD deducted |
| Uniswap v3 | QuoterV2, single pool | floor only, not a routed price |
| 1inch | Swap API (key) | excluded from "best" (~24 bps gap vs web app unexplained) |

## Key columns
- `bps_vs_best_competitor`: net received vs best competitor (excl. Tokenlon and 1inch). For Tokenlon rows, >= 0 = Tokenlon wins.
- `pre_fee_bps_vs_best_competitor` (Tokenlon rows only): same, before Tokenlon's fee. Gap between the two = fee effect; pre-fee gap = route / liquidity effect.
- `fee_bps`, `source` (v5: protocol/mmCode; v6: orderType/protocols), `quote_id` (quote to backend when debugging).
- `bps_vs_mid`: vs Uniswap USDC/WETH 0.05% pool mid. Indicative only (pool mid can sit a few bps off market).

## Status
- v5 / v6 parsing checked against backend's documented sample responses (2026-09-29); not yet run live by Claude.
- Test traffic is not excluded from SSR / MM stats (agreed: volume negligible).
