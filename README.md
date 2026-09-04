# Backtest Trade Engine

[Build a strategy with its own dataset and timeframe](STRATEGY_PLUGIN_GUIDE_FA.md) — includes the copy-ready `example_strategy.py` configured for `data_candle/btc_1m_data.csv`.

[Research workflow: nested walk-forward, sealed holdout, Top 100 snapshots, and strategy plug-ins](RESEARCH_GUIDE_FA.md)

[راهنمای فارسی](README_FA.md)

A candle-by-candle BTC backtesting and parameter-optimization project. It supports long/short positions, leverage, fees, liquidation, scale-ins, monthly controls, MA/EMA/ADX/ATR/volume/RSI scoring, interactive chart review, multiprocessing optimization, checkpoints, and out-of-sample validation.

> Research software only. A backtest is not a promise of future performance.

## Execution model and correctness

- Candle `i` must close before its indicators and signal are known.
- Signal-based entries and exits fill at candle `i+1` open, avoiding look-ahead execution.
- Liquidation is checked from the active candle's high/low.
- Fees are included in closed-trade net profit.
- Positions still open at the end are marked to the final close. Results expose realized profit, unrealized profit, and open-position count separately.
- `--start` is inclusive and `--end` is exclusive.

## Requirements and data

- Python 3.9+
- `pandas`, `numpy`, `matplotlib`, `mplfinance`
- `openpyxl` for the default formatted Excel reports

```powershell
python -m pip install pandas numpy matplotlib mplfinance
python -m pip install openpyxl
```

The default file is configured in `fetch_calculate_data.py` as `data_candle/btc_15m_data_2018_to_2026.csv`. Required columns are `Open time`, `Open`, `High`, `Low`, `Close`, `Volume`, and `Close time`.

## Quick start

```powershell
# Normal backtest with interactive chart
python ma_strategy.py

# Fast, non-interactive research run
python ma_strategy.py --start 2025-01-01 --end 2026-01-01 `
  --no-chart --no-trade-log --quiet --print-result

# Reproducible smart optimization
python optimize.py --mode smart --profile focused --tests 5000 -w 8 `
  --start 2023-01-01 --end 2025-01-01

# Continuous staged search; stop safely with Ctrl+C
python optimize.py --auto -w 16
```

## `ma_strategy.py` manual

### Date and candle-index ranges

Dates select an inclusive start and exclusive end:

```powershell
python ma_strategy.py --start 2025-01-01 --end 2025-06-01
```

Integer candle indices are also accepted:

```powershell
python ma_strategy.py --start 100000 --end 120000
```

The end must resolve after the start. Date lookup uses timestamps in the configured candle CSV.

### Every command-line option

| Option | Default | Meaning |
|---|---:|---|
| `-h`, `--help` | — | Show built-in help. |
| `--start VALUE` | `2025-01-01` | Inclusive ISO date or candle index. |
| `--end VALUE` | `2026-02-23` | Exclusive ISO date or candle index. |
| `--params-source config\|best\|file` | `config` | Select parameter source. |
| `--best-params FILE` | `outputs/optimize/best_params.json` | Winner used by `--params-source best`. |
| `--params-file FILE` | — | Custom JSON used by `--params-source file`. |
| `--config FILE` | — | Legacy alias for `--params-source file --params-file FILE`. |
| `--set NAME=VALUE` | — | Override one validated setting; repeat as needed. |
| `--list-params` | — | Print all default settings as JSON and exit. |
| `--no-chart` | off | Do not open the interactive chart. |
| `--save-chart FILE` | — | Save PNG, PDF, or SVG; combine with `--no-chart` for headless export. |
| `--quiet` | off | Suppress trade and summary console messages. |
| `--no-trade-log` | off | Skip trade CSV, monthly CSV, and XLSX generation. |
| `--excel` | on | Create the formatted multi-sheet XLSX report (kept for compatibility). |
| `--no-excel` | off | Skip XLSX generation and keep only the raw CSV reports. |
| `--output-dir DIR` | `outputs` | Root for trade and monthly reports. |
| `--result-json FILE` | — | Save the final result dictionary as JSON. |
| `--print-result` | off | Print the final result dictionary as JSON. |

```powershell
python ma_strategy.py --help
python ma_strategy.py --list-params
```

### Parameter sources and precedence

`config` reads defaults from `strategy_config.py` without rewriting it:

```powershell
python ma_strategy.py --params-source config
```

`best` loads an optimizer winner:

```powershell
python ma_strategy.py --params-source best
python ma_strategy.py --params-source best `
  --best-params outputs/optimize/run_01/best_params.json
```

`file` loads a custom JSON object:

```powershell
python ma_strategy.py --params-source file --params-file configs/conservative.json
```

`--set` is applied last and can temporarily override either source:

```powershell
python ma_strategy.py --params-source best `
  --set leverage=3 --set trade_amount_percent=0.25 --set adx_filter=false
```

Values use JSON syntax when possible (`true`, `false`, numbers, strings, `null`). Unknown names are rejected before the run begins.

### Useful run recipes

```powershell
# Fast benchmark without files or GUI
python ma_strategy.py --start 2024-01-01 --end 2025-01-01 `
  --no-chart --no-trade-log --quiet --print-result

# Save a chart without opening a window
python ma_strategy.py --start 2025-01-01 --end 2025-04-01 `
  --no-chart --save-chart outputs/charts/q1_2025.png

# Independent experiment outputs and JSON result
python ma_strategy.py --output-dir outputs/runs/conservative `
  --result-json outputs/runs/conservative/result.json `
  --set leverage=3 --set trade_amount_percent=0.25

# CSV, monthly CSV, and the default XLSX report without a chart
python ma_strategy.py --no-chart
```

### Backtest outputs

Unless disabled, files are written below `--output-dir`:

```text
outputs/
├── trades/data_orders.csv
├── trades/data_orders_summary.csv # TOTAL/LONG/SHORT quality and risk comparison
├── trades/data_orders.xlsx       # default; disable with --no-excel
└── monthly/monthly_data_orders.csv
```

The XLSX workbook freezes headers, enables filters, applies semantic profit/loss and Long/Short color coding, and keeps the detailed strategy sheets. The raw CSV remains available for scripts and data tools.

The result dictionary includes final balance, total/realized/unrealized profit, open positions, return percent, closed trades, wins/losses, win rate, maximum drawdown, score, profit factor, expectancy, Calmar ratio, and MA/RSI/Scale sub-strategy statistics. Long and Short are also reported independently with net/gross profit, return contribution, win rate, profit factor, expectancy, average win/loss, payoff ratio, best/worst trade, fees, duration, isolated directional drawdown, loss streak, liquidations, and open-position counts. `long_profit + short_profit` reconciles to realized profit (subject only to output rounding).

The Excel trade report starts with a Long-vs-Short dashboard and includes Side Comparison, Monthly Analysis, Exit Reasons, Long Trades, Short Trades, and cumulative/monthly charts. The raw trade CSV adds normalized `side`, `outcome`, and total/Long/Short cumulative-profit columns.

### Interactive chart controls

- Mouse wheel: cursor-centered zoom.
- Left drag: move backward or forward through history.
- Right drag: adjust the selected panel's vertical scale.
- Double-click price/equity: auto-fit its visible y-range.
- Left / `A`: older candles; Right / `D`: newer candles.
- Up / `W` / Page Up: oldest window; Down / `S` / Page Down: newest window.
- `0` / Home: full history; `1` / End: default recent window.
- Hover near a trade marker: show its entry/exit reason.

Chart settings can be changed temporarily with `--set`:

```powershell
python ma_strategy.py `
  --set plot_max_candles=800 `
  --set plot_max_render_candles=600 `
  --set plot_drag_update_interval_ms=100
```

The renderer parses arrays and timestamps once, indexes markers, aggregates OHLC while zoomed out, uses a low-density drag preview, and throttles hover redraws. Zooming in restores full candle detail.

### Programmatic use

```python
from ma_strategy import ma_strategy

result = ma_strategy(
    tune={"leverage": 3, "trade_amount_percent": 0.25},
    start="2025-01-01",
    end="2025-06-01",
    show_chart=False,
    write_trades=False,
    verbose=False,
)
print(result["total_profit_percent"])
```

## `optimize.py` manual

The optimizer evaluates `ma_strategy` with charting, verbose output, and trade-file I/O disabled. Market data and indicators are cached inside worker processes.

### Smart mode versus grid mode

- `smart`: uses `--tests` as a budget, explores first, keeps elites, and gradually mutates promising areas. Recommended.
- `grid`: evaluates every valid Cartesian combination in the selected profile. Built-in grids are enormous; check with `--dry-run` first.

### Every command-line option

| Option | Default | Meaning |
|---|---:|---|
| `-h`, `--help` | — | Show built-in help. |
| `--mode smart\|grid` | `smart` | Adaptive budget or full Cartesian grid. |
| `--tests N` | `5000` | Smart-mode candidate budget; not a grid-mode limit. |
| `--profile NAME` | mode-dependent | `full` in Auto mode, `focused` otherwise; staged mode selects `signal`, `exit`, `risk_core`, `rsi`, and `scale` automatically. |
| `--base-source config\|best\|file` | `config` | Baseline outside the selected profile. |
| `--base-params FILE` | `outputs/optimize/best_params.json` | JSON for `best` or `file`. |
| `-w N`, `--workers N` | up to `8` | Worker processes; use `1` for easiest debugging. |
| `--batch-size N` | `0` | Checkpoint batch size; `0` is automatic. |
| `--chunksize N` | `0` | Multiprocessing task chunk; `0` is automatic. |
| `--elite-size N` | `20` | Top candidates guiding smart mutations. |
| `--seed N` | `42` | Reproducible smart-search seed. |
| `--date-policy auto\|fixed` | `auto` | Derive recent ranges from the latest candle or preserve explicit dates. |
| `--start VALUE` | auto-derived | Inclusive candidate-search date/index; fixed fallback is `2023-01-01`. |
| `--end VALUE` | auto-derived | Exclusive search end; fixed fallback is `2025-04-01`. |
| `--validation-start VALUE` | — | Inclusive inner-validation start; pair with end. |
| `--validation-end VALUE` | — | Exclusive inner-validation end; pair with start. |
| `--validation-top N` | `20` | Training finalists evaluated on inner validation. |
| `--overfit-penalty X` | `0.25` | Penalty when train score exceeds validation score. |
| `--min-trades N` | `0` | Disqualify candidates with too few closed trades. |
| `--max-drawdown X` | — | Disqualify candidates above this absolute drawdown %. |
| `--output-dir DIR` | `outputs/optimize` | Checkpoints and results. |
| `--resume` | off | Continue a compatible results CSV. |
| `--log-every N` | `10` | Progress interval; `0` is silent. |
| `--top-n N` | `20` | Ranked candidates saved to `top_results.json`. |
| `--excel-top N` | `5000` | Best candidates included in XLSX; `0` disables the workbook. |
| `--list-profiles` | — | Print profile sizes and exit. |
| `--dry-run` | — | Print planned search size and exit. |

```powershell
python optimize.py --help
python optimize.py --list-profiles
python optimize.py --profile risk --mode grid --dry-run
```

### Profiles

| Profile | Intended use |
|---|---|
| `focused` | Entry/exit scores plus MA/EMA, ADX, ATR, volume, and RSI timing; practical recency-focused search. |
| `signal` | Entry thresholds, indicators, filters, and entry weights. |
| `exit` | Exit thresholds, trailing behavior, guards, and exit weights. |
| `risk` | Sizing, leverage, monthly rules, cooldowns, and scale-ins. |
| `rsi` | Embedded RSI monthly sub-strategy. |
| `full` | Every tunable parameter; use only with a smart budget. |

Sequential profile refinement is normally more efficient and easier to validate than optimizing everything at once.

### Recommended workflows

```powershell
# Focused search from Python defaults
python optimize.py --mode smart --profile focused --tests 5000 -w 8 --date-policy fixed `
  --start 2023-01-01 --end 2025-04-01 `
  --output-dir outputs/optimize/focused_01

# Refine exits around an existing winner
python optimize.py --mode smart --profile exit --tests 5000 -w 8 --date-policy fixed `
  --base-source file --base-params outputs/optimize/focused_01/best_params.json `
  --start 2023-01-01 --end 2025-04-01 `
  --output-dir outputs/optimize/exit_01

# Non-overlapping inner validation (not the final unseen holdout)
python optimize.py --mode smart --profile signal --tests 10000 -w 8 --date-policy fixed `
  --start 2023-01-01 --end 2024-01-01 `
  --validation-start 2024-01-01 --validation-end 2025-04-01 `
  --validation-top 30 --overfit-penalty 0.35 `
  --min-trades 50 --max-drawdown 35 `
  --output-dir outputs/optimize/signal_validated

# Resume the same compatible run
python optimize.py --mode smart --profile signal --tests 10000 -w 8 --date-policy fixed `
  --start 2023-01-01 --end 2024-01-01 `
  --validation-start 2024-01-01 --validation-end 2025-04-01 `
  --validation-top 30 --overfit-penalty 0.35 `
  --min-trades 50 --max-drawdown 35 `
  --output-dir outputs/optimize/signal_validated --resume

# Run the selected winner
python ma_strategy.py --params-source best `
  --best-params outputs/optimize/signal_validated/best_params.json
```

Resume requires the same profile/grid columns and compatible values. Use a new output directory after changing the profile or grid.

### Scoring and validation

Ranking combines return, maximum drawdown, Calmar ratio, profit factor, expectancy, win rate, monthly consistency, trade-count confidence, and a liquidation penalty. No-trade candidates receive a losing score. `--min-trades` and `--max-drawdown` are hard constraints.

With validation, robust score equals validation score minus `overfit_penalty × max(0, train_score - validation_score)`.

### Optimizer outputs

```text
optimization_results.csv     every completed candidate/checkpoint
optimization_results.xlsx    winner dashboard, rankings, parameters, directional/risk, RSI, and Scale sheets
best_params.json              final selected winner
best_params_manifest.json     explicit winner status, selection basis, and file-role guide
optimization_summary.json     metadata and winner metrics
top_results.json              top --top-n candidates
best_training_params.json     training winner with validation
validation_results.json       inner-validation finalist details
```

JSON writes are atomic. CSV is flushed after each batch for reliable resume. Workers receive the base parameter set once at startup and only candidate deltas are transferred per task. Smart mode combines exploration and crossover with deterministic one-step refinement around elite candidates while suppressing duplicate effective configurations.

### Continuous Auto mode

`--auto` runs a resumable campaign until `Ctrl+C`. It uses the main `full` parameter grid by default; pass `--profile focused` only when a deliberately smaller search is wanted. An existing compatible checkpoint in the output directory is resumed automatically, even when `--resume` is omitted. A new campaign also warm-starts from compatible values in `--base-params` when that file exists.

Date handling defaults to `--date-policy auto`. A new campaign works backwards from the latest valid candle, reserves the latest five calendar months as sealed Holdout, inserts a one-month Embargo, reserves the preceding eight months for reporting-only Research OOS, and uses the preceding 27 months for Auto Development. Resolved dates are frozen in campaign state and manifests, so appending candles never moves a resumed experiment. Use `--date-policy fixed` to preserve explicit `--auto-*`, `--wf-*`, and `--holdout-*` boundaries.

With the current dataset ending at `2026-08-01 00:00:00` exclusive, Auto resolves to Stress `2023-03-01 -> 2023-09-01`, Validation `2023-09-01 -> 2024-03-01`, Discovery `2024-03-01 -> 2025-06-01`, Research through `2026-02-01`, Embargo through `2026-03-01`, and sealed Holdout through `2026-08-01`.

The version-2 engine uses two cheap expanding Discovery rungs, full Discovery, Validation, Stress, three disjoint walk-forward folds, and a final search-history test. With the defaults, 2,000 candidates are reduced by successive halving before the expensive stages; 500 reach Validation, 250 reach Stress, 150 reach the internal walk-forward, and 100 reach Final. Large campaigns retain every historical result, while surrogate fitting uses up to 10,000 deterministic score-quantile samples so startup cost stays bounded. A compact `surrogate_history_cache.json.gz` is bootstrapped from a blend of whole-history and recent cycles, updated incrementally, and reused after restart; continuous runs keep both candidate and surrogate history in memory. Resume shows progress for storage preparation, candidate generation, model training, and pool scoring. Auto is intentionally restricted to the pre-OOS search zone by default:

```text
Discovery    2024-03-01 -> 2025-06-01
Validation   2023-09-01 -> 2024-03-01
Stress       2023-03-01 -> 2023-09-01
Walk-forward three disjoint folds inside 2023-03-01 -> 2024-03-01
Final        2023-03-01 -> 2025-06-01
```

Nested research starts from the rolling Development start and reports four two-month OOS folds after the `2025-06-01` boundary through `2026-02-01`. February is the Embargo; the sealed range begins at `2026-03-01` and currently includes every candle through `2026-07-31 23:45:00`. Snapshot manifests record their exclusive development end; overlapping or unverifiable performance-selected seeds are rejected unless explicitly run as contaminated diagnostics, which can never pass all acceptance gates.

```powershell
# Start an unlimited campaign; its dated output directory is selected automatically
python .\optimize.py --auto -w 16

# Stop safely
# Press Ctrl+C once

# Continue the exact cycle and stage (--resume is optional when state exists)
python .\optimize.py --auto --resume -w 16

# Inspect the plan without running backtests
python .\optimize.py --auto --dry-run -w 16

# Run two cycles for a bounded experiment
python .\optimize.py --auto --auto-cycles 2 -w 16 `
  --output-dir outputs/optimize/auto_two_cycles
```

After the first cycle, Auto mode can create numeric values not present in the coarse grid. For example, an elite value of `20` in `[10, 20, 30]` produces local tests such as `19` and `21`. Float gaps become finer across cycles. Values remain inside the original numeric bounds, boolean/enum parameters remain discrete, invalid relationships are rejected, and previously planned discovery combinations are not repeated.

Auto mode learns parameter importance from completed Discovery results. Once enough full-Discovery history exists, an internal dependency-free Extra Trees ensemble learns nonlinear parameter interactions. It scores a larger unevaluated pool and selects 55% for predicted quality, 20% for model uncertainty, and 25% for random exploration. Local Hall-of-Fame mutations and crossover still feed that pool, so the model guides the existing search instead of replacing it.

Staged Auto mode searches one related parameter family at a time and locks each
phase winner into the next phase. The default 50-cycle block is Signal (10), Exit
(10), Risk (10), RSI (10), and Scale-in (10). It keeps at least 500 internal
finalists, writes the best 100 to `top_100.csv`, saves the winner to
`best_params.json`, and creates an immutable snapshot every 50 completed cycles.
Those top candidates seed both parent generation and Extra Trees training in the
next block.

Every 50-cycle snapshot also runs a deterministic random-window audit. By
default, the best 10 finalists are evaluated on the same 50 random 6-12 month
windows (500 backtests total). Their earliest and recent boundaries are derived
from the rolling Development and Discovery starts. The audit ranks consistency using per-window
percentiles, median performance, the worst decile, positive-window ratio, failed
windows, and score dispersion. Its most robust candidate becomes the snapshot's
final `best_params.json`.

```powershell
python optimize.py --auto --staged -w 16
python optimize.py --auto --staged --auto-cycles 50 -w 16
python optimize.py --auto --staged --dry-run -w 16
```

Successive halving evaluates every selected candidate on a short recent range, promotes the best fraction to a larger range, and only then runs full Discovery. Minimum-trade constraints are scaled to rung length. Walk-forward evaluates fixed finalists on disjoint chronological folds; its score combines median, mean, worst-fold performance, and a variation penalty. Every fold has its own resumable CSV checkpoint.

Cross-range ranking never compares raw scores directly. Auto preserves `objective_score`, records the exact `range_candles`, and calculates `time_normalized_score = objective_score × candles_per_year / range_candles`. `candles_per_year` is inferred from the selected strategy's own `TIMEFRAME` and dataset (35,064 for 15m and 525,960 for 1m). Stage percentiles remain duration-neutral, while transformed-quality, walk-forward stability, and normal train/validation comparisons use the normalized rate. Version-1 stage CSV files receive these columns atomically before resume.

The optimization hot path skips allocation for non-triggered liquidation checks, uses direct slotted position access, caches cross-window calculations, and avoids repeated empty-position work. On the included 2025-01-01 to 2026-02-23 range, a warm single-process backtest dropped to roughly 0.15 seconds while all 59 saved winner metrics remained bit-for-bit/numerically identical. Multi-worker throughput can be substantially higher; 0.01-second single-test latency is not promised because every candle still has to be simulated.

Every new cycle records its parent in `training_parent.json`. After the first complete cycle, the best Hall-of-Fame parameters become the next cycle's baseline and mutation parent, so training continues along the strongest known path while retaining random exploration.

Candidate reports put decisions and critical risk/performance metrics before parameter columns. `candidate_catalog.csv/json` provides the reusable ranking, while `candidates/rank_NNN_params.json` and the matching summary file make every finalist directly inspectable. `ACCEPT` means eligible for independent Research, `WATCH` means promising but below one or more Auto thresholds, and `REJECT` prevents a fragile high-score result from becoming `best_params.json`.

`auto_report.xlsx` and each 50-cycle `snapshot_report.xlsx` provide a colored decision dashboard rather than only raw columns. They include campaign start/update time, elapsed hours, exact test and historical-stability ranges, Top-candidate and Long-vs-Short charts, decision colors, directional metrics, aggregate monthly statistics, the number of months returning at least 8%, the same statistics for the latest 12 observed months, and a month-by-month sheet for the best candidate. Finalists retain compact monthly equity returns so these counts reflect actual monthly performance; discovery candidates do not carry this extra payload.

Always load the root `best_params.json` in the output directory. `best_params_manifest.json` says whether it is final or provisional and explains why cycle/checkpoint copies are not the preferred file. Standard optimization also marks the exact selected row in the Excel Rankings sheet and repeats the filename on the Dashboard.

| Auto option | Default | Meaning |
|---|---:|---|
| `--auto` | off | Start the continuous staged campaign. |
| `--auto-tests N` | `2000` | New discovery candidates per cycle. |
| `--auto-validation-top N` | `500` | Discovery finalists sent to validation. |
| `--auto-stress-top N` | `250` | Validation finalists sent to stress testing. |
| `--auto-walk-forward-top N` | `150` | Stress finalists evaluated on every time fold. |
| `--auto-final-top N` | `100` | Stress finalists sent to the full-range test. |
| `--auto-hall-size N` | `100` | Winners retained across cycles. |
| `--auto-cycles N` | `0` | Completed-cycle limit; `0` means until `Ctrl+C`. |
| `--auto-discovery-start VALUE` | auto-derived | Recent Discovery start; honored directly in fixed mode. |
| `--auto-validation-start VALUE` | auto-derived | Validation start; honored directly in fixed mode. |
| `--auto-stress-start VALUE` | auto-derived | Stress/development start; honored directly in fixed mode. |
| `--auto-stress-end VALUE` | auto-derived | Exclusive end of the older stability-only slice; honored directly in fixed mode. |
| `--auto-end VALUE` | auto-derived | Exclusive Development end; honored directly in fixed mode. |
| `--auto-importance-target METRIC` | `objective_score` | Importance target: objective score, profit, or profit %. |
| `--auto-halving-rungs N` | `2` | Cheap expanding Discovery rungs; `0` disables them. |
| `--auto-halving-keep RATIO` | `0.25` | Fraction promoted after every cheap rung. |
| `--auto-surrogate-min-samples N` | `64` | Full-Discovery history required before Extra Trees activates. |
| `--auto-surrogate-pool N` | `8` | Candidate-pool multiplier scored by the surrogate. |
| `--auto-surrogate-trees N` | `64` | Number of randomized regression trees. |
| `--auto-surrogate-max-samples N` | `10000` | Representative historical samples used to train the trees. |
| `--staged` | off | Run the five locked 10-cycle parameter phases. |
| `--stage-cycles N` | `10` | Cycles allocated to each staged phase. |
| `--snapshot-cycles N` | `50` | Completed cycles per staged snapshot. |
| `--snapshot-top N` | `100` | Winners written and reused at each snapshot. |
| `--random-audit-tests N` | `500` | Total random-period tests after each snapshot. |
| `--random-audit-top N` | `10` | Finalists compared on shared random periods. |
| `--random-audit-earliest VALUE` | auto-derived | Earliest allowed audit candle. |
| `--random-audit-recent-start VALUE` | auto-derived | Boundary for recent audit windows. |
| `--random-audit-recent-ratio X` | `0.70` | Required share of recent windows. |
| `--random-audit-min-months N` | `6` | Minimum random-window duration. |
| `--random-audit-max-months N` | `12` | Maximum random-window duration. |
| `--auto-walk-forward-folds N` | `3` | Disjoint chronological folds; `0` disables them. |
| `--auto-walk-forward-stability-penalty FLOAT` | `0.15` | Penalty for performance variation between folds. |

Auto checkpoints are flushed per result and stored per cycle/stage. Result tables put scores, profit/loss, balances, drawdown, and trade statistics before the parameter columns. Once a cycle is complete, its result CSVs are losslessly converted to `.csv.gz`; resume and surrogate-history loading read these files directly without extraction. Temporary checkpoints and redundant plans are removed, so copying the complete `auto` directory remains the safest portable checkpoint while requiring substantially less space. Version-1 state is migrated in place: an interrupted legacy cycle finishes from its existing plans, and the new engine activates on the next unplanned cycle. Resume requires the same profile, grid, base parameters, ranges, funnel sizes, risk constraints, and version-2 engine settings. Worker count and logging frequency may change.

```text
auto_state.json              exact campaign/cycle/stage checkpoint
best_params.json             best robust Hall-of-Fame parameters
best_params_manifest.json    canonical winner pointer and final/provisional status
hall_of_fame.json/.csv       cross-cycle robust winners
parameter_importance.json/.csv learned mutation priorities
auto_summary.json            campaign status and best result
auto_report.xlsx             Hall of Fame and Parameter Importance sheets
cycles/cycle_*/              active CSVs and compressed completed *.csv.gz results
cycles/cycle_*/surrogate_search.json  model history and selection diagnostics
cycles/cycle_*/walk_forward_summary.json fold scores and stability ranking
cycles/cycle_*/training_parent.json  baseline winner used by that cycle
cycles/cycle_*/best_params.json      latest stage winner in that cycle
cycles/cycle_*/checkpoints/*/best_params.json  temporary winner while a stage is active
```

## Testing

```powershell
python -m unittest discover -v
python -m py_compile ma_strategy.py optimize.py trade_engine.py chart_renderer.py
```

## Troubleshooting

- Slow chart: lower `plot_max_candles` / `plot_max_render_candles`, or use `--no-chart`.
- Slow reports: use `--no-excel` to skip the formatted workbook.
- Locked CSV/XLSX: close it in Excel or choose another `--output-dir`.
- Optimizer RAM pressure: lower `--workers`, then `--batch-size`.
- Windows multiprocessing problem: use `-w 1` to expose the original error.
- No trades: inspect thresholds, range, `--min-trades`, and parameter source.
- Unknown JSON/`--set` key: run `python ma_strategy.py --list-params`.

## Project structure

```text
ma_strategy.py          strategy, CLI, and signal flow
example_strategy.py     copy-ready strategy plug-in with its own 1m dataset
market_data.py          strategy-owned data, timeframe, coverage, and indices
trade_engine.py         execution, accounting, liquidation, reports
trade_csv_logger.py     CSV and optional XLSX writer
chart_renderer.py       interactive/exportable chart
indicators.py           vectorized indicators
strategy_config.py      typed defaults
optimize.py             smart/grid multiprocessing optimizer
check_monthly_data.py   monthly report builder
data_candle/            input candles
outputs/                generated artifacts
```
