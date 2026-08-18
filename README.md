# Backtest Trader Bot

BTC strategy backtesting bot in Python.  
It reads historical candle CSV data, simulates trades candle-by-candle, and outputs performance reports.

## Features
- Long/short backtesting with fee and liquidation handling
- Scoring-based entries/exits (EMA/MA, ADX, ATR, volume, momentum)
- Post-cross sharp-move negative exit score logic
- Monthly controls and loss-streak stop logic
- Parameter optimization with multiprocessing
- Interactive chart rendering for backtest review

## Project Structure
- `ma_strategy.py`: MA strategy rules and signal flow
- `trade_engine.py`: execution, accounting, position lifecycle, logging, CSV reports, and chart lifecycle
- `optimize.py`: grid-search optimizer
- `trade_csv_logger.py`: low-level CSV writer used by `trade_engine.py`
- `check_monthly_data.py`: monthly summary generator
- `chart_renderer.py`: chart UI
- `data_candle/`: historical input candles
- `outputs/`: generated files
  - `outputs/trades/data_orders.csv`
  - `outputs/monthly/monthly_data_orders.csv`
  - `outputs/optimize/optimization_results.csv`
  - `outputs/optimize/best_params.txt`

## Requirements
- Python 3.9+
- `pandas`
- `numpy`
- `mplfinance`

Install:
```bash
pip install pandas numpy mplfinance
```

## Required Candle CSV Columns
- `Open time`
- `Open`
- `High`
- `Low`
- `Close`
- `Volume`
- `Close time`

Default data path is configured in `fetch_calculate_data.py`:
`./data_candle/btc_15m_data_2018_to_2026.csv`

## Run Backtest

Run with the default range:

```bash
python ma_strategy.py
```

Or provide an inclusive start and exclusive end:

```bash
python ma_strategy.py --start 2025-01-01 --end 2025-06-01
```

Programmatic use supports dates or candle indices:

```python
from ma_strategy import ma_strategy

result = ma_strategy(start="2025-01-01", end="2025-06-01")
```

Check generated files in `outputs/`.

## Trade Engine Defaults

`AccountState` owns portfolio balances and aggregate trade metrics. `Position`
owns the complete state of one open order. Direct `open_long` and `open_short`
calls use 100% of available capital and 1x leverage unless overridden. Closing
methods accept these objects, close the full supplied position, and update the
account atomically.

The MA strategy evaluates signals after candle `i` has closed and fills every
signal-based entry or exit at candle `i+1` open. The final candle can update
mark-to-market and liquidation state, but cannot create an unfillable order.

```python
from trade_engine import AccountState, Position, TradeEngine

engine = TradeEngine(optimize=True, verbose=False)
account = AccountState(balance=1000.0)

opened = engine.open_long(0, [100.0], ["2026-01-01 00:00:00"], account)
position = Position.from_open_result(
    opened,
    trade_id="manual_0001",
    side="long",
    entry_index=0,
    high_price=100.0,
    low_price=100.0,
    reason="manual",
)

closed = engine.close_long(
    0,
    [110.0],
    ["2026-01-01 00:15:00"],
    position,
    account,
    fee_rate=0.0005,
    cooldown_after_big_pnl=12,
)

custom_account = AccountState(balance=1000.0)
custom = engine.open_short(
    0,
    [100.0],
    ["2026-01-01 00:00:00"],
    custom_account,
    trade_amount_percent=0.25,
    leverage=3,
)
```

## Run Optimization

The default smart mode performs an adaptive, reproducible search. It starts with
exploration and then mutates the best candidates found so far:

```bash
python optimize.py --mode smart --tests 5000 -w 8
```

To test every Cartesian combination in `param_grid`, use full-grid mode. The
grid is intentionally large, so reduce its values first if a complete run is
not practical:

```bash
python optimize.py --mode grid -w 8
```

Useful options include `--start`, `--end`, `--seed`, `--batch-size`, and
`--output-dir`. Market data and previously calculated indicators are cached in
each worker process, which substantially reduces repeated-test overhead.

Optimization writes:

- `outputs/optimize/optimization_results.csv`: every completed candidate.
- `outputs/optimize/best_params.json`: only the winning parameter values.
- `outputs/optimize/optimization_summary.json`: run metadata and best metrics.
- `outputs/optimize/top_results.json`: the top 20 candidates by robust score.

Run a normal backtest with the winning JSON without changing strategy defaults:

```bash
python ma_strategy.py --params-source best
```

Switch back to the values written in `strategy_config.py` at any time:

```bash
python ma_strategy.py --params-source config
```

Use a winner from another optimization directory:

```bash
python ma_strategy.py --params-source best --best-params outputs/optimize/run_2023_2025/best_params.json
```

The old `--config FILE` option is still supported as an alias for a custom JSON.
The strategy never rewrites `strategy_config.py`; promotion of tested values remains
an explicit user decision.

Optimization can start from either the Python defaults or a previously found winner:

```bash
# Fresh focused search around strategy_config.py
python optimize.py --mode smart --profile focused --base-source config --tests 5000 -w 8

# Refine an existing winner, preserving its parameters outside the selected profile
python optimize.py --mode smart --profile exit --base-source best \
  --base-params outputs/optimize/best_params.json --tests 5000 -w 8 \
  --output-dir outputs/optimize/refine_exit
```

Profiles are `focused`, `signal`, `exit`, `risk`, `rsi`, and `full`. Sequential
profile runs are usually more efficient than optimizing every variable at once.
Use `--resume` to continue a compatible interrupted run.

For a more reliable winner, reserve a later, non-overlapping validation period:

```bash
python optimize.py --mode smart --profile signal --tests 10000 -w 8 \
  --start 2023-01-01 --end 2025-01-01 \
  --validation-start 2025-01-01 --validation-end 2026-01-01 \
  --min-trades 50 --max-drawdown 35
```

With validation enabled, `best_training_params.json` preserves the training winner,
while `best_params.json` becomes the validation-selected winner. Detailed holdout
results are written to `validation_results.json`.

The optimizer score combines return, drawdown, Calmar ratio, profit factor,
expectancy, win rate, monthly consistency, trade-count confidence, and a
liquidation penalty. No-trade candidates receive a losing score.

## Recent Strategy/Project Updates
- Added negative exit score component:
  - If a sharp move is detected in the lookback window (default `400` candles),
    and EMA/MA cross happened, a temporary post-cross penalty is applied for `15` candles.
- Sharp move detection improved to strongest directional move (ordered move), not simple high/low span.
- Added monthly loss-streak stop controls:
  - `consecutive_losses_month_stop_filter`
  - `consecutive_losses_stop_until_month`
- Output files reorganized under `outputs/` folders for cleaner repository structure.
- Optimizer and summary writers now create output directories automatically.

## Notes
- Optimization mode disables per-trade CSV logging for speed.
- If CSV is open in another app (like Excel), writing may wait/fail until file is closed.
- This repository is for research/education, not financial advice.
