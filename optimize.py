"""Parallel full-grid and budgeted adaptive optimization for ``ma_strategy``.

Examples:
    python optimize.py --mode smart --tests 5000 -w 8
    python optimize.py --mode grid -w 8
"""

import argparse
import csv
import itertools
import json
import math
import multiprocessing
import os
import random
import time
from pathlib import Path

from ma_strategy import ma_strategy
from strategy_config import build_ma_strategy_config


# Every key is an existing MAStrategyConfig setting.  Defaults in
# strategy_config.py remain unchanged; the optimizer only passes candidates via
# ``tune`` and writes the winning values to JSON.
param_grid = {
    # Capital baseline (singletons prevent meaningless score scaling)
    "balance": [1000],
    "save_money": [0],
    "fee_rate": [0.0005],
    # Entry context and thresholds
    "entry_score_threshold": [6, 7, 8, 9, 10, 11, 12],
    "exit_score_threshold": [4, 5, 6, 7, 8, 9, 10],
    "ma_distance_threshold": [0.0008, 0.0010, 0.00125, 0.0015, 0.00159, 0.0020, 0.0025, 0.0030, 0.0040],
    "candle_move_threshold": [0.002, 0.004, 0.006, 0.008, 0.010, 0.012, 0.015],
    "impulse_move_threshold_pct": [0.75, 1.0, 1.5, 2.0, 2.5, 3.0],
    "impulse_lookback": [3, 4, 5, 6, 8, 10],
    "late_entry_atr_mult": [0.5, 0.8, 1.0, 1.2, 1.5, 2.0],
    "late_entry_body_ratio": [0.4, 0.6, 0.8, 1.0, 1.2],
    "late_entry_ema_pct": [0.002, 0.003, 0.004, 0.005, 0.007, 0.010],
    # Exit behavior
    "slope_window": [2, 3, 5, 8, 13],
    "trail_activate_pct": [0.003, 0.005, 0.007, 0.010, 0.015, 0.020],
    "trail_retrace_pct": [0.0015, 0.002, 0.003, 0.004, 0.005, 0.008],
    "loss_exit_pct_1": [0.015, 0.02, 0.025, 0.03, 0.04, 0.05],
    "loss_exit_pct_2": [0.03, 0.04, 0.05, 0.06, 0.08],
    "profit_exit_pct_1": [0.02, 0.03, 0.04, 0.05, 0.075, 0.10, 0.15],
    "profit_exit_pct_2": [0.06, 0.08, 0.10, 0.12, 0.15, 0.20],
    "loss_lock_step_pct": [0.005, 0.01, 0.015, 0.02, 0.03],
    "adx_exit_threshold": [12, 15, 18, 21, 24, 27],
    "adx_exit_lookback": [1, 2, 3, 5, 8],
    "opposite_atr_body_mult": [0.4, 0.6, 0.8, 1.0, 1.25, 1.5],
    "sharp_move_threshold_pct": [6, 8, 10, 12, 15, 18, 20],
    "sharp_move_lookback_candles": [100, 200, 300, 450, 600, 800, 1000],
    "post_cross_penalty_candles": [5, 10, 15, 20, 30],
    # Indicator periods and filters
    "ema_16_period": [10, 12, 14, 16, 18, 20, 24],
    "ma_50_period": [35, 40, 45, 50, 55, 60, 70],
    "ma_100_period": [80, 90, 100, 102, 110, 125, 140],
    "ma_200_period": [160, 180, 198, 200, 220, 240, 260],
    "period_adx": [8, 10, 12, 14, 16, 18, 21],
    "period_atr": [8, 10, 12, 14, 16, 18, 21],
    "period_atr_ma": [7, 10, 14, 18, 21, 28, 35],
    "period_vol_avg": [6, 8, 10, 12, 15, 18, 21, 30],
    "period_rsi": [7, 9, 11, 14, 18, 21],
    "entry_adx_threshold": [12, 15, 18, 20, 20.5, 22, 25, 28, 32],
    "entry_atr_threshold": [0.7, 0.85, 1.0, 1.1, 1.2, 1.35, 1.5],
    "volume_spike_multiplier": [0.9, 1.0, 1.1, 1.2, 1.24, 1.3, 1.45, 1.6, 1.8],
    "adx_filter": [False, True],
    "atr_filter": [False, True],
    "volume_filter": [False, True],
    # Score weights
    "entry_score_cross": [1, 2, 3, 4],
    "entry_score_ema_vs_ma50": [1, 2, 3, 4],
    "entry_score_ma_trend": [1, 2, 3],
    "entry_score_ma_distance_or_candle": [1, 2, 3],
    "entry_score_adx": [1, 2, 3],
    "entry_score_volume": [1, 2, 3],
    "entry_late_penalty": [0, 1, 2, 3, 4],
    "exit_score_loss_guard_1": [1, 2, 3, 4],
    "exit_score_loss_guard_2": [1, 2, 3, 4],
    "exit_score_profit_guard_1": [1, 2, 3, 4],
    "exit_score_profit_guard_2": [1, 2, 3, 4],
    "exit_score_ema_slope": [1, 2, 3, 4],
    "exit_score_ema_cross": [1, 2, 3, 4],
    "exit_score_ma_trend": [1, 2, 3],
    "exit_score_trailing": [1, 2, 3, 4],
    "exit_score_adx": [1, 2, 3],
    "exit_score_opposite_candle": [1, 2, 3],
    "post_cross_penalty_score": [0, 1, 2, 3, 4, 5],
    # Position sizing, safety, monthly controls, and scale-ins
    "trade_amount_percent": [0.2, 0.3, 0.4, 0.5, 0.6, 0.75, 0.9],
    "leverage": [1, 2, 3, 4, 5, 7, 10, 12],
    "safe_leverage_low": [1, 2, 3, 4],
    "safe_leverage_med": [2, 3, 4, 5, 6],
    "safe_leverage_high": [3, 4, 5, 6, 8],
    "safe_leverage_balance_pct_low": [60, 70, 75, 80],
    "safe_leverage_balance_pct_med": [70, 75, 80, 85],
    "safe_leverage_balance_pct_high": [80, 85, 90, 95],
    "save_money_recover_trigger_pct": [60, 65, 70, 75, 80],
    "max_open_trades": [1, 2, 3, 4, 5, 8],
    "cooldown_after_big_pnl": [0, 4, 8, 12, 24, 48, 96],
    "monthly_profit_percent_stop_trade": [5, 7, 9, 12, 15, 20],
    "monthly_loss_percent_stop_trade": [8, 12, 16, 19, 20, 25, 30],
    "monthly_compound": [0, 1, 2, 3, 5, 8],
    "monthly_profit_close_filter": [False, True],
    "monthly_loss_close_filter": [False, True],
    "consecutive_losses_month_stop_filter": [False, True],
    "consecutive_losses_stop_until_month": [3, 4, 5, 6, 8, 10],
    "skip_logic": [False, True],
    "scale_in_enabled": [False, True],
    "scale_entry_amount_percent": [0.05, 0.1, 0.15, 0.2, 0.3],
    "scale_entry_profit_trigger_pct": [0.005, 0.01, 0.02, 0.03, 0.039, 0.04, 0.06],
    "scale_entry_loss_trigger_pct": [0.005, 0.01, 0.02, 0.03, 0.04, 0.06],
    "scale_entry_on_profit_enabled": [False, True],
    "scale_entry_on_loss_enabled": [False, True],
    "profit_scale_entry_filter_enabled": [False, True],
    "profit_scale_entry_min_score": [2, 3, 4, 5, 6],
    "profit_scale_entry_atr_ratio_min": [0.8, 0.9, 1.0, 1.1, 1.25, 1.5],
    # RSI monthly sub-strategy used inside ma_strategy
    "rsi_trade_monthly_filter_on": [False, True],
    "rsi_long_open_monthly_profit": [10, 15, 20, 25, 30, 35],
    "rsi_long_close_monthly_profit": [60, 70, 79, 80, 85, 90],
    "rsi_short_open_monthly_profit": [55, 60, 65, 70, 75, 80],
    "rsi_short_close_monthly_profit": [10, 15, 20, 25, 30, 35],
    "rsi_long_tp_pct": [0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10],
    "rsi_long_sl_pct": [0.02, 0.03, 0.04, 0.05, 0.06, 0.08],
    "rsi_short_tp_pct": [0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10],
    "rsi_short_sl_pct": [0.01, 0.02, 0.03, 0.04, 0.05, 0.06],
    "rsi_max_open_trades": [1, 2, 3, 4],
    "rsi_trade_amount_percent": [0.1, 0.2, 0.3, 0.4, 0.5],
    "rsi_leverage": [1, 2, 3, 4, 5, 6, 8],
    "rsi_cooldown_filter": [False, True],
    "rsi_cooldown_bars": [0, 4, 8, 10, 12, 16, 24, 48],
    "lowest_rsi_last_n_value": [1, 2, 3, 5, 8, 13],
    "highest_rsi_last_n_value": [1, 2, 3, 5, 8, 13],
    "rsi_entry_buffer": [2, 4, 6, 8, 10, 12],
    "rsi_distance_threshold": [4, 6, 8, 10, 12, 15, 20],
    # Chart-only settings remain fixed because charts are disabled in optimize mode.
    "plot_max_candles": [1200],
    "plot_end_offset": [0],
    "plot_step_candles": [300],
    "plot_min_zoom_candles": [80],
    "plot_max_render_candles": [1600],
    "plot_zoom_in_factor": [0.8],
    "plot_zoom_out_factor": [1.6],
    "plot_window_width_scale": [0.94],
    "plot_window_height_scale": [0.90],
    "plot_drag_preview_factor": [0.42],
    "plot_drag_update_interval_ms": [16],
    "plot_yscale_drag_sensitivity": [0.0030],
    "plot_post_cross_penalty_markers": [True],
}

# param_grid = {
#     "ema_16_period": [10, 12, 14, 16, 18, 20, 24],
#     "ma_50_period": [35, 40, 45, 50, 55, 60, 70],
#     "ma_100_period": [80, 90, 100, 102, 110, 125, 140],
#     "ma_200_period": [160, 180, 198, 200, 220, 240, 260],
# }

RESULT_COLUMNS = [
    "final_balance_static", "final_balance_dynamic", "total_profit",
    "total_profit_percent", "closed_trades", "wins", "losses",
    "maximum_drawdown", "win_rate", "profit_months", "loss_months",
    "score", "profit_factor", "expectancy_percent", "calmar_ratio",
    "rsi_total_trades", "rsi_wins", "rsi_losses", "rsi_winrate",
    "rsi_total_profit", "scale_total_trades", "scale_wins", "scale_losses",
    "scale_winrate", "scale_total_profit",
]

_WORKER_START = "2025-01-01"
_WORKER_END = "2026-02-23"


def grid_size(grid):
    return math.prod(len(values) for values in grid.values())


def iter_grid_candidates(grid):
    """Yield the full Cartesian grid lazily without allocating it in memory."""
    keys = tuple(grid)
    for combo in itertools.product(*(grid[key] for key in keys)):
        yield dict(zip(keys, combo))


def _nearest_value(values, target):
    if target in values:
        return target
    if isinstance(target, (int, float)) and not isinstance(target, bool):
        numeric = [value for value in values if isinstance(value, (int, float))]
        if numeric:
            return min(numeric, key=lambda value: abs(value - target))
    return values[0]


def is_valid_candidate(candidate):
    """Reject combinations that violate basic parameter relationships."""
    ma_periods = [
        candidate.get("ema_16_period"), candidate.get("ma_50_period"),
        candidate.get("ma_100_period"), candidate.get("ma_200_period"),
    ]
    if all(value is not None for value in ma_periods) and ma_periods != sorted(ma_periods):
        return False
    leverage_tiers = [
        candidate.get("safe_leverage_low"), candidate.get("safe_leverage_med"),
        candidate.get("safe_leverage_high"), candidate.get("leverage"),
    ]
    if all(value is not None for value in leverage_tiers) and leverage_tiers != sorted(leverage_tiers):
        return False
    balance_tiers = [
        candidate.get("safe_leverage_balance_pct_low"),
        candidate.get("safe_leverage_balance_pct_med"),
        candidate.get("safe_leverage_balance_pct_high"),
    ]
    if all(value is not None for value in balance_tiers) and balance_tiers != sorted(balance_tiers):
        return False
    return True


class SmartCandidateGenerator:
    """Reproducible elite-guided search over the discrete parameter space."""

    def __init__(self, grid, seed=42):
        if not grid or any(not values for values in grid.values()):
            raise ValueError("param_grid must contain at least one value per parameter")
        self.grid = {key: tuple(values) for key, values in grid.items()}
        self.keys = tuple(self.grid)
        self.mutable_keys = tuple(key for key, values in self.grid.items() if len(values) > 1)
        self.random = random.Random(seed)
        self.seen = set()
        self.baseline_attempted = False
        defaults = build_ma_strategy_config()
        self.baseline = {
            key: _nearest_value(values, getattr(defaults, key, values[0]))
            for key, values in self.grid.items()
        }

    def _signature(self, candidate):
        return tuple(candidate[key] for key in self.keys)

    def _random_candidate(self):
        return {key: self.random.choice(values) for key, values in self.grid.items()}

    def _guided_candidate(self, elites):
        # Start from one proven combination and mutate only a focused subset.
        # This preserves useful parameter interactions in a high-dimensional grid.
        candidate = dict(self.random.choice(elites)["params"])
        mutation_count = min(
            len(self.mutable_keys),
            max(2, round(math.sqrt(max(1, len(self.mutable_keys))))),
        )
        for key in self.random.sample(self.mutable_keys, mutation_count):
            values = self.grid[key]
            elite_value = candidate[key]
            if self.random.random() < 0.65 and len(values) > 1:
                index = values.index(elite_value)
                offset = self.random.choice((-1, 1))
                candidate[key] = values[max(0, min(len(values) - 1, index + offset))]
            else:
                candidate[key] = self.random.choice(values)
        return candidate

    def generate(self, count, elites=None):
        candidates = []
        attempts = 0
        max_attempts = max(1000, count * 100)
        while len(candidates) < count and attempts < max_attempts:
            attempts += 1
            if not self.baseline_attempted:
                candidate = dict(self.baseline)
                self.baseline_attempted = True
            elif elites and self.random.random() >= 0.25:
                candidate = self._guided_candidate(elites)
            else:
                candidate = self._random_candidate()
            signature = self._signature(candidate)
            if signature in self.seen or not is_valid_candidate(candidate):
                continue
            self.seen.add(signature)
            candidates.append(candidate)
        return candidates


def _parse_bound(value):
    return int(value) if str(value).isdigit() else value


def _init_worker(start, end):
    global _WORKER_START, _WORKER_END
    _WORKER_START = start
    _WORKER_END = end
    # Warm the largest repeated I/O cost once per process.
    from trade_engine import TradeEngine
    TradeEngine.load_market_data(start=start, end=end)


def _evaluate_task(task):
    index, params = task
    started = time.perf_counter()
    try:
        result = ma_strategy(
            tune={**params, "optimize": True},
            start=_WORKER_START,
            end=_WORKER_END,
        )
        error = None
    except Exception as exc:  # return errors to the parent without killing the run
        result = None
        error = f"{type(exc).__name__}: {exc}"
    return index, params, result, time.perf_counter() - started, error


def _score(result):
    if not result:
        return -math.inf
    value = result.get("score", -math.inf)
    try:
        value = float(value)
    except (TypeError, ValueError):
        return -math.inf
    return value if math.isfinite(value) else -math.inf


def _json_safe(value):
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "item"):
        return _json_safe(value.item())
    return value


def _write_json(path, payload):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(_json_safe(payload), file, indent=2, ensure_ascii=False)
        file.write("\n")
    os.replace(temporary, path)


def _write_best_files(output_dir, best, mode, requested_tests, completed, elapsed, seed):
    if best is None:
        return
    _write_json(Path(output_dir) / "best_params.json", best["params"])
    _write_json(Path(output_dir) / "optimization_summary.json", {
        "mode": mode,
        "requested_tests": requested_tests,
        "completed_tests": completed,
        "seed": seed,
        "elapsed_seconds": round(elapsed, 3),
        "best_test_index": best["index"],
        "best_duration_seconds": round(best["duration"], 4),
        "best_params": best["params"],
        "best_metrics": best["result"],
    })


def _result_row(keys, index, params, result, duration):
    row = {"test_index": index, "duration_s": round(duration, 4)}
    row.update({key: params[key] for key in keys})
    row.update({key: result.get(key) for key in RESULT_COLUMNS})
    return row


def run_optimization(args, grid=None):
    grid = param_grid if grid is None else grid
    keys = tuple(grid)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    results_path = output_dir / "optimization_results.csv"
    fieldnames = ["test_index", *keys, *RESULT_COLUMNS, "duration_s"]

    if args.mode == "grid":
        requested_tests = grid_size(grid)
        candidate_source = iter_grid_candidates(grid)
    else:
        requested_tests = min(args.tests, grid_size(grid))
        candidate_source = None

    workers = max(1, args.workers)
    batch_size = args.batch_size or max(32, workers * 8)
    chunksize = args.chunksize or max(1, batch_size // (workers * 4))
    start = _parse_bound(args.start)
    end = _parse_bound(args.end)
    started = time.perf_counter()
    completed = 0
    failed = 0
    next_index = 1
    best = None
    ranked = []

    print(f"Mode: {args.mode} | tests: {requested_tests:,} | workers: {workers}")
    print(f"Range: {start} -> {end}")

    with results_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()

        pool = None
        if workers > 1:
            pool = multiprocessing.Pool(workers, initializer=_init_worker, initargs=(start, end))
        else:
            _init_worker(start, end)

        def evaluate(batch):
            tasks = [(offset, candidate) for offset, candidate in batch]
            if pool is not None:
                return pool.imap_unordered(_evaluate_task, tasks, chunksize=chunksize)
            return map(_evaluate_task, tasks)

        def consume(batch):
            nonlocal completed, failed, best, ranked
            for index, params, result, duration, error in evaluate(batch):
                completed += 1
                if error:
                    failed += 1
                    print(f"[{completed}/{requested_tests}] test {index} failed: {error}")
                    continue
                writer.writerow(_result_row(keys, index, params, result, duration))
                record = {"index": index, "params": params, "result": result, "duration": duration}
                ranked.append(record)
                ranked.sort(key=lambda item: _score(item["result"]), reverse=True)
                del ranked[max(20, args.elite_size):]
                if best is None or _score(result) > _score(best["result"]):
                    best = record
                if args.log_every and (completed % args.log_every == 0 or completed == requested_tests):
                    best_score = _score(best["result"]) if best else -math.inf
                    elapsed = time.perf_counter() - started
                    print(f"[{completed:,}/{requested_tests:,}] best_score={best_score:.4f} elapsed={elapsed:.1f}s")
            csv_file.flush()
            _write_best_files(
                output_dir, best, args.mode, requested_tests, completed,
                time.perf_counter() - started, args.seed,
            )

        try:
            if args.mode == "grid":
                while True:
                    candidates = list(itertools.islice(candidate_source, batch_size))
                    if not candidates:
                        break
                    batch = list(enumerate(candidates, start=next_index))
                    next_index += len(batch)
                    consume(batch)
            else:
                generator = SmartCandidateGenerator(grid, seed=args.seed)
                while completed < requested_tests:
                    count = min(batch_size, requested_tests - completed)
                    elites = ranked[:args.elite_size]
                    candidates = generator.generate(count, elites=elites)
                    if not candidates:
                        break
                    batch = list(enumerate(candidates, start=next_index))
                    next_index += len(batch)
                    consume(batch)
        finally:
            if pool is not None:
                pool.close()
                pool.join()

    elapsed = time.perf_counter() - started
    _write_best_files(output_dir, best, args.mode, requested_tests, completed, elapsed, args.seed)
    _write_json(output_dir / "top_results.json", [
        {"rank": rank, **record}
        for rank, record in enumerate(ranked[:20], start=1)
    ])
    print(f"Finished {completed:,} tests ({failed} failed) in {elapsed:.1f}s")
    print(f"Results: {results_path}")
    if best:
        print(f"Best score: {_score(best['result']):.4f}")
        print(f"Best params: {output_dir / 'best_params.json'}")
    return best


def build_parser():
    parser = argparse.ArgumentParser(description="Optimize ma_strategy in full-grid or smart mode.")
    parser.add_argument("--mode", choices=("smart", "grid"), default="smart",
                        help="smart uses a test budget; grid evaluates the full Cartesian product")
    parser.add_argument("--tests", type=int, default=5000,
                        help="number of candidates in smart mode (default: 5000)")
    parser.add_argument("-w", "--workers", type=int, default=min(8, os.cpu_count() or 1))
    parser.add_argument("--batch-size", type=int, default=0,
                        help="adaptive batch size (0=auto)")
    parser.add_argument("--chunksize", type=int, default=0,
                        help="multiprocessing task chunksize (0=auto)")
    parser.add_argument("--elite-size", type=int, default=20,
                        help="top candidates used to guide smart search")
    parser.add_argument("--seed", type=int, default=42,
                        help="random seed for reproducible smart searches")
    parser.add_argument("--start", default="2025-01-01", help="inclusive date or candle index")
    parser.add_argument("--end", default="2026-02-23", help="exclusive date or candle index")
    parser.add_argument("--output-dir", default=os.path.join("outputs", "optimize"))
    parser.add_argument("--log-every", type=int, default=10,
                        help="print progress every N completed tests (0=silent)")
    return parser


def main():
    args = build_parser().parse_args()
    if args.tests <= 0:
        raise SystemExit("--tests must be greater than zero")
    if args.elite_size <= 0:
        raise SystemExit("--elite-size must be greater than zero")
    multiprocessing.freeze_support()
    run_optimization(args)


if __name__ == "__main__":
    main()
