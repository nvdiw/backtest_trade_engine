"""Strategy-agnostic optimization and research validation.

Examples:
    python optimize.py --auto -w 16
    python optimize.py --auto --resume -w 16
    python optimize.py --mode smart --tests 5000 -w 8
    python optimize.py --mode grid -w 8
"""

from excel_charts import add_report_chart
from optimize_overview import OVERVIEW_COLUMNS, comparison_rows, write_overview, print_leaders
from optimizer_evidence import apply_profit_learning, chronological_probe_indices
from monthly_reporting import MONTHLY_COLUMNS
from campaign_reporting import publish_campaign

import argparse
import calendar
import csv
from dataclasses import replace
import gzip
import hashlib
import itertools
import json
import math
import multiprocessing
import os
import random
import shutil
import signal
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from strategy_adapter import (
    StrategyAdapter,
    load_grid_source,
    resolve_strategy,
)


def ma_strategy(*args, **kwargs):
    """Load MA only when selected; other plug-ins do not require its module."""
    from ma_strategy import ma_strategy as run_ma
    return run_ma(*args, **kwargs)
from research_statistics import (
    deflated_sharpe_ratio,
    grid_ordinal_position,
    moving_block_bootstrap_ci,
    parameter_plateau_scores,
    performance_from_returns,
    probability_of_backtest_overfitting,
)
from market_data_audit import (
    AuditConfig,
    MarketDataAuditError,
    audit_market_data,
    build_run_fingerprints,
    fingerprint_config,
    fingerprint_data,
)
from market_data import MarketDataSource
from strategy_workspace import output_path, claim_output, assert_strategy_path, output_session
from runtime_settings import add_runtime_arguments, configure_runtime, runtime_session, apply_process_policy


# Preserve legacy imports without loading MA settings during a Pulse run.
def __getattr__(name):
    if name in {'FULL_PARAM_GRID', 'FOCUSED_PARAM_GRID', 'LEGACY_FOCUSED_PARAM_GRID',
                'PARAMETER_PROFILES', 'STAGED_AUTO_PHASES', 'param_grid',
                'build_ma_strategy_config', 'load_ma_strategy_tune', 'DIRECTIONAL_FIELDS'}:
        import ma_strategy_config
        return getattr(ma_strategy_config, name)
    raise AttributeError(name)


def build_ma_strategy_config(tune=None):
    from ma_strategy_config import build_ma_strategy_config as build
    return build(tune)


def _strategy_staged_phases(args):
    """Return a strategy-owned staged schedule, preserving the MA default."""
    adapter = _adapter_from_args(args)
    profiles = getattr(args, "_parameter_profiles", None) or adapter.discovered_profiles()
    declared = getattr(adapter.module, "STAGED_PHASES", None)
    if declared is None:
        if adapter.identifier == "ma_strategy:ma_strategy":
            from ma_strategy_config import STAGED_AUTO_PHASES
            if getattr(args, 'directional', False):
                return tuple((f'{side}_{name}', f'{side}_{profile}')
                             for name, profile in STAGED_AUTO_PHASES for side in ('long', 'short')) + (('portfolio', 'portfolio'),)
            return STAGED_AUTO_PHASES
        return ()
    phases = []
    for item in declared:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError("STAGED_PHASES entries must be (phase_name, profile_name)")
        phase_name, profile_name = str(item[0]), str(item[1])
        if profile_name not in profiles:
            raise ValueError(
                f"STAGED_PHASES profile {profile_name!r} is not exposed by "
                f"{adapter.identifier}"
            )
        phases.append((phase_name, profile_name))
    if not phases:
        raise ValueError("STAGED_PHASES cannot be empty")
    return tuple(phases)

RESULT_COLUMNS = [
    *MONTHLY_COLUMNS,
    "final_balance_static", "final_balance_dynamic", "final_balance",
    "final_balance_without_fee",
    "total_profit", "realized_profit", "unrealized_profit", "open_positions",
    "total_fees", "saved_money", "liquidations", "total_profit_percent",
    "closed_trades", "wins", "losses", "long_trades", "long_wins",
    "long_losses", "short_trades", "short_wins", "short_losses",
    "long_profit", "short_profit", "stronger_side", "directional_profit_gap",
    "long_profit_contribution_share_percent", "short_profit_contribution_share_percent",
    "long_breakeven_trades", "long_win_rate", "long_net_profit",
    "long_return_contribution_percent", "long_gross_profit", "long_gross_loss",
    "long_profit_factor", "long_expectancy", "long_expectancy_percent",
    "long_average_win", "long_average_loss", "long_payoff_ratio",
    "long_best_trade", "long_worst_trade", "long_total_fees",
    "long_average_duration_minutes", "long_maximum_drawdown",
    "long_max_consecutive_losses", "long_liquidations", "long_open_positions",
    "short_breakeven_trades", "short_win_rate", "short_net_profit",
    "short_return_contribution_percent", "short_gross_profit", "short_gross_loss",
    "short_profit_factor", "short_expectancy", "short_expectancy_percent",
    "short_average_win", "short_average_loss", "short_payoff_ratio",
    "short_best_trade", "short_worst_trade", "short_total_fees",
    "short_average_duration_minutes", "short_maximum_drawdown",
    "short_max_consecutive_losses", "short_liquidations", "short_open_positions",
    "maximum_drawdown", "win_rate", "profit_months", "loss_months",
    "score", "profit_factor", "expectancy_percent", "calmar_ratio",
    "rsi_total_trades", "rsi_wins", "rsi_losses", "rsi_winrate",
    "rsi_total_profit", "rsi_long_trades", "rsi_long_wins", "rsi_long_losses",
    "rsi_long_winrate", "rsi_long_profit", "rsi_short_trades", "rsi_short_wins",
    "rsi_short_losses", "rsi_short_winrate", "rsi_short_profit",
    "scale_total_trades", "scale_wins", "scale_losses", "scale_winrate",
    "scale_total_profit", "scale_long_trades", "scale_long_wins",
    "scale_long_losses", "scale_long_winrate", "scale_long_profit",
    "scale_short_trades", "scale_short_wins", "scale_short_losses",
    "scale_short_winrate", "scale_short_profit",
]

IMPORTANT_RESULT_COLUMNS = [
    *MONTHLY_COLUMNS,
    "total_profit_percent", "total_profit", "maximum_drawdown",
    "closed_trades", "win_rate", "profit_factor", "expectancy_percent",
    "calmar_ratio", "liquidations", "final_balance", "total_fees",
    "stronger_side", "long_profit", "short_profit", "directional_profit_gap",
    "long_trades", "long_win_rate", "long_profit_factor",
    "long_expectancy", "long_maximum_drawdown", "long_liquidations",
    "short_trades", "short_win_rate", "short_profit_factor",
    "short_expectancy", "short_maximum_drawdown", "short_liquidations",
]

DERIVED_RESULT_COLUMNS = ["objective_score", "profit_per_trade"]
AUTO_TIME_COLUMNS = ["time_normalized_score", "range_candles"]
CANDLES_PER_YEAR_15M = 365.25 * 24 * 4
_ACTIVE_MARKET_DATA_SOURCE = None
_ACTIVE_CANDLES_PER_YEAR = CANDLES_PER_YEAR_15M
SURROGATE_MAX_TRAINING_SAMPLES = 10_000
SURROGATE_CACHE_BOOTSTRAP_CYCLES = 24
SURROGATE_CACHE_VERSION = 2

# Fixed-mode bootstrap values. Auto mode replaces them from the final candle.
DEFAULT_DEVELOPMENT_START = "2023-01-01"
DEFAULT_AUTO_VALIDATION_START = "2023-07-01"
DEFAULT_AUTO_DISCOVERY_START = "2024-01-01"
DEFAULT_DEVELOPMENT_END = "2025-04-01"
DEFAULT_RESEARCH_END = "2026-01-01"
DEFAULT_HOLDOUT_START = "2026-01-01"
DEFAULT_ROLLING_DEVELOPMENT_MONTHS = 24
DEFAULT_ROLLING_OOS_MONTHS = 8
DEFAULT_ROLLING_EMBARGO_MONTHS = 1
DEFAULT_ROLLING_HOLDOUT_MONTHS = 5
DEFAULT_ROLLING_STRESS_MONTHS = 3
DEFAULT_ROLLING_VALIDATION_MONTHS = 6

_WORKER_START = DEFAULT_DEVELOPMENT_START
_WORKER_END = DEFAULT_DEVELOPMENT_END
_WORKER_BASE_TUNE = {}
_WORKER_USE_INDICATOR_WARMUP = True
_WORKER_INDICATOR_WARMUP_CANDLES = None
_WORKER_STRATEGY_SPEC = "ma"
_WORKER_RESEARCH = False
DEFAULT_OUTPUT_DIR = os.path.join("outputs", "optimize")


def _adapter_from_spec(specification=None):
    """Resolve an adapter while preserving tests that patch ``optimize.ma_strategy``."""
    adapter = resolve_strategy(specification or "ma")
    if adapter.identifier == "ma_strategy:ma_strategy":
        adapter = replace(adapter, function=ma_strategy)
    return adapter


def _adapter_from_args(args):
    cached = getattr(args, "_strategy_adapter", None)
    if cached is not None:
        return cached
    adapter = _adapter_from_spec(getattr(args, "strategy", "ma"))
    try:
        args._strategy_adapter = adapter
    except Exception:
        pass
    return adapter


def _freeze_strategy_tune(adapter, tune):
    """Expand a selected delta into a self-contained, reproducible tune object."""
    tune = dict(tune or {})
    try:
        resolved = adapter.default_values(tune)
    except (KeyError, TypeError, ValueError):
        # Synthetic test grids and permissive plug-ins may contain parameters
        # unknown to an otherwise strict config builder. Preserve them while
        # still freezing every default the strategy can expose.
        resolved = adapter.default_values({})
    return {**dict(resolved), **tune}


def _profiles_from_args(args):
    cached = getattr(args, "_parameter_profiles", None)
    if cached is not None:
        return cached
    adapter = _adapter_from_args(args)
    grid_source = getattr(args, "param_grid", None)
    if grid_source:
        profiles = load_grid_source(grid_source)
    else:
        profiles = adapter.discovered_profiles()
        if not profiles:
            raise ValueError(
                f"strategy {adapter.identifier} does not expose param_grid or "
                "PARAMETER_PROFILES; provide --param-grid JSON|module:attribute"
            )
    validator = getattr(adapter.module, 'validate_parameter_grid', None)
    if callable(validator):
        for profile_grid in profiles.values():
            validator(profile_grid)
    try:
        args._parameter_profiles = profiles
    except Exception:
        pass
    return profiles


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


def is_valid_candidate(candidate, strategy_adapter=None):
    """Reject combinations that violate basic parameter relationships."""
    if strategy_adapter is not None and not strategy_adapter.validate_candidate(candidate):
        return False
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
    """Reproducible adaptive search over discrete or locally refined values."""

    def __init__(
        self,
        grid,
        seed=42,
        baseline_params=None,
        continuous_refinement=False,
        parameter_importance=None,
        mutation_guidance=None,
        refinement_round=1,
        strategy_adapter=None,
    ):
        if not grid or any(not values for values in grid.values()):
            raise ValueError("param_grid must contain at least one value per parameter")
        self.grid = {key: tuple(values) for key, values in grid.items()}
        self.keys = tuple(self.grid)
        self.mutable_keys = tuple(key for key, values in self.grid.items() if len(values) > 1)
        self.random = random.Random(seed)
        self.continuous_refinement = bool(continuous_refinement)
        self.refinement_round = max(1, int(refinement_round))
        self.strategy_adapter = strategy_adapter
        supplied_importance = parameter_importance or {}
        self.parameter_importance = {
            key: max(0.01, float(supplied_importance.get(key, 1.0)))
            for key in self.mutable_keys
        }
        supplied_guidance = mutation_guidance or {}
        self.mutation_guidance = {
            key: dict(supplied_guidance.get(key, {})) for key in self.mutable_keys
        }
        self.seen = set()
        self.local_queue = []
        self.local_queued = set()
        self.baseline_attempted = False
        defaults = (
            strategy_adapter.default_values(baseline_params)
            if strategy_adapter is not None
            else build_ma_strategy_config(baseline_params)
        )
        if isinstance(defaults, dict):
            defaults = dict(defaults)
            for key in self.grid:
                if key.startswith(('long_', 'short_')) and defaults.get(key) is None:
                    defaults[key] = defaults.get(key.split('_', 1)[1], self.grid[key][0])
        else:
            for key in self.grid:
                if key.startswith(('long_', 'short_')) and getattr(defaults, key, None) is None:
                    setattr(defaults, key, getattr(defaults, key.split('_', 1)[1], self.grid[key][0]))
        self.baseline = {
            key: _nearest_value(
                values,
                (
                    defaults.get(key, values[0])
                    if isinstance(defaults, dict)
                    else getattr(defaults, key, values[0])
                ),
            )
            for key, values in self.grid.items()
        }

    def _signature(self, candidate):
        return tuple(candidate[key] for key in self.keys)

    def _canonicalize(self, candidate):
        """Collapse inactive conditional parameters to avoid duplicate backtests."""
        candidate = dict(candidate)
        if candidate.get("scale_in_enabled") is False:
            for key in self.keys:
                if key.startswith(("scale_entry_", "profit_scale_entry_")):
                    candidate[key] = self.baseline[key]
        else:
            if candidate.get("scale_entry_on_profit_enabled") is False:
                for key in ("scale_entry_profit_trigger_pct",):
                    if key in candidate:
                        candidate[key] = self.baseline[key]
            if candidate.get("scale_entry_on_loss_enabled") is False:
                for key in ("scale_entry_loss_trigger_pct",):
                    if key in candidate:
                        candidate[key] = self.baseline[key]
            if candidate.get("profit_scale_entry_filter_enabled") is False:
                for key in (
                    "profit_scale_entry_min_score",
                    "profit_scale_entry_atr_ratio_min",
                ):
                    if key in candidate:
                        candidate[key] = self.baseline[key]
        if candidate.get("rsi_trade_monthly_filter_on") is False:
            for key in self.keys:
                if (
                    key.startswith(("rsi_", "lowest_rsi_", "highest_rsi_"))
                    and key != "rsi_trade_monthly_filter_on"
                ):
                    candidate[key] = self.baseline[key]
        elif candidate.get("rsi_cooldown_filter") is False:
            if "rsi_cooldown_bars" in candidate:
                candidate["rsi_cooldown_bars"] = self.baseline["rsi_cooldown_bars"]
        inactive_filter_settings = (
            ("adx_filter", ("entry_adx_threshold", "entry_score_adx")),
            ("atr_filter", ("entry_atr_threshold",)),
            ("volume_filter", ("volume_spike_multiplier", "entry_score_volume")),
            (
                "consecutive_losses_month_stop_filter",
                ("consecutive_losses_stop_until_month",),
            ),
        )
        for switch, dependent_keys in inactive_filter_settings:
            if candidate.get(switch) is False:
                for key in dependent_keys:
                    if key in candidate:
                        candidate[key] = self.baseline[key]
        if self.strategy_adapter is not None:
            candidate = self.strategy_adapter.canonicalize_candidate(
                candidate, self.baseline
            )
        return candidate

    def _random_candidate(self):
        return {key: self.random.choice(values) for key, values in self.grid.items()}

    @staticmethod
    def _is_numeric_values(values):
        return all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in values
        )

    def _numeric_refinement_step(self, values):
        ordered = sorted(set(values))
        if all(isinstance(value, int) and not isinstance(value, bool) for value in ordered):
            return 1
        gaps = [
            right - left for left, right in zip(ordered, ordered[1:])
            if right > left
        ]
        if not gaps:
            return 0
        # Each completed auto cycle can halve the smallest coarse-grid gap. The
        # cap avoids creating meaningless floating-point precision indefinitely.
        divisor = 2 ** min(4, self.refinement_round)
        return min(gaps) / divisor

    @staticmethod
    def _float_precision(values, step):
        def decimal_places(value):
            text = f"{float(value):.12f}".rstrip("0")
            return len(text.partition(".")[2])

        return min(12, max([decimal_places(value) for value in values] + [decimal_places(step)]))

    def _refined_neighbors(self, key, current):
        values = self.grid[key]
        if not self.continuous_refinement or not self._is_numeric_values(values):
            ordered = list(values)
            if current in ordered:
                index = ordered.index(current)
            else:
                index = min(range(len(ordered)), key=lambda i: abs(ordered[i] - current))
            return [
                ordered[neighbor_index]
                for neighbor_index in (index - 1, index + 1)
                if 0 <= neighbor_index < len(ordered)
            ]

        lower, upper = min(values), max(values)
        step = self._numeric_refinement_step(values)
        if step <= 0:
            return []
        precision = self._float_precision(values, step)
        neighbors = []
        guidance = self.mutation_guidance.get(key, {})
        preferred_direction = int(guidance.get("direction", 0) or 0)
        directions = [-1, 1]
        if preferred_direction in (-1, 1):
            directions.sort(key=lambda item: item != preferred_direction)
        step_multiplier = max(1, min(3, int(guidance.get("step_multiplier", 1) or 1)))
        for direction in directions:
            value = current + direction * step * step_multiplier
            value = max(lower, min(upper, value))
            if all(isinstance(item, int) and not isinstance(item, bool) for item in values):
                value = int(round(value))
            else:
                value = round(value, precision)
            if value != current and value not in neighbors:
                neighbors.append(value)
        return neighbors

    def _weighted_mutation_keys(self, count):
        available = list(self.mutable_keys)
        chosen = []
        while available and len(chosen) < count:
            weights = [self.parameter_importance.get(key, 1.0) for key in available]
            key = self.random.choices(available, weights=weights, k=1)[0]
            available.remove(key)
            chosen.append(key)
        return chosen

    def _mutate_key(self, candidate, key, prefer_local=True):
        values = self.grid[key]
        neighbors = self._refined_neighbors(key, candidate[key])
        if prefer_local and neighbors and self.random.random() < 0.85:
            guidance = self.mutation_guidance.get(key, {})
            confidence = max(0.0, min(1.0, float(guidance.get("confidence", 0.0))))
            preferred_direction = int(guidance.get("direction", 0) or 0)
            preferred = [
                value for value in neighbors
                if preferred_direction and (value - candidate[key]) * preferred_direction > 0
            ]
            if preferred and self.random.random() < 0.50 + 0.45 * confidence:
                candidate[key] = self.random.choice(preferred)
            else:
                candidate[key] = self.random.choice(neighbors)
        else:
            candidate[key] = self.random.choice(values)

    def _elite_choice(self, elites):
        # Rank weighting prevents one early lucky candidate from monopolizing search.
        weights = list(range(len(elites), 0, -1))
        return self.random.choices(elites, weights=weights, k=1)[0]

    def _guided_candidate(self, elites, progress=0.0, crossover_probability=0.20):
        # Occasionally cross two good candidates, then mutate. Mutation becomes
        # narrower as the budget is consumed (exploration -> exploitation).
        parent = self._elite_choice(elites)
        candidate = {key: parent["params"][key] for key in self.keys}
        if len(elites) > 1 and self.random.random() < crossover_probability:
            other = self._elite_choice(elites)["params"]
            side_choices = {side: self.random.random() < 0.5 for side in ('long', 'short')}
            for key in self.mutable_keys:
                # Preserve a parent's complete directional setup rather than
                # splicing incompatible MA periods and exit settings together.
                side = key.split('_', 1)[0]
                take_other = side_choices[side] if side in side_choices else self.random.random() < 0.5
                if take_other:
                    candidate[key] = other[key]

        max_mutations = max(2, round(math.sqrt(max(1, len(self.mutable_keys)))))
        mutation_count = max(1, round(max_mutations * (1.0 - 0.70 * progress)))
        mutation_count = min(len(self.mutable_keys), mutation_count)
        for key in self._weighted_mutation_keys(mutation_count):
            self._mutate_key(candidate, key, prefer_local=True)
        return candidate

    def _crossover_candidate(self, elites, progress=0.0):
        candidate = self._guided_candidate(
            elites, progress=progress, crossover_probability=1.0
        )
        return candidate

    def _queue_elite_neighbors(self, elites):
        """Queue deterministic one-step neighbors around the current elites."""
        for elite in elites:
            parent = {key: elite["params"][key] for key in self.keys}
            ordered_keys = sorted(
                self.mutable_keys,
                key=lambda key: self.parameter_importance.get(key, 1.0),
                reverse=True,
            )
            for key in ordered_keys:
                current = parent[key]
                for neighbor in self._refined_neighbors(key, current):
                    candidate = dict(parent)
                    candidate[key] = neighbor
                    candidate = self._canonicalize(candidate)
                    signature = self._signature(candidate)
                    if (
                        signature not in self.seen
                        and signature not in self.local_queued
                        and is_valid_candidate(candidate, self.strategy_adapter)
                    ):
                        self.local_queue.append(candidate)
                        self.local_queued.add(signature)

    def generate(self, count, elites=None, progress=0.0, progress_label=None):
        candidates = []
        attempts = 0
        max_attempts = max(1000, count * 100)
        if elites:
            self._queue_elite_neighbors(elites)
        while len(candidates) < count and attempts < max_attempts:
            attempts += 1
            if not self.baseline_attempted:
                candidate = dict(self.baseline)
                self.baseline_attempted = True
            elif self.local_queue and self.random.random() < (0.35 + 0.50 * progress):
                candidate = self.local_queue.pop(0)
                self.local_queued.discard(self._signature(candidate))
            elif elites and self.random.random() >= max(0.12, 0.40 * (1.0 - progress)):
                candidate = self._guided_candidate(elites, progress=progress)
            else:
                candidate = self._random_candidate()
            candidate = self._canonicalize(candidate)
            signature = self._signature(candidate)
            if signature in self.seen or not is_valid_candidate(
                candidate, self.strategy_adapter
            ):
                continue
            self.seen.add(signature)
            candidates.append(candidate)
            if progress_label and (
                len(candidates) == count
                or len(candidates) % max(1, math.ceil(count / 20)) == 0
            ):
                _show_loading_progress(progress_label, len(candidates), count)
        return candidates

    def generate_auto(self, count, elites=None, progress_label=None):
        """Generate the 25% exploration / 15% crossover / 60% local mix."""
        if not elites:
            return self.generate(count, progress_label=progress_label)
        self._queue_elite_neighbors(elites)
        candidates = []
        attempts = 0
        max_attempts = max(2000, count * 200)
        while len(candidates) < count and attempts < max_attempts:
            attempts += 1
            roll = self.random.random()
            if roll < 0.25:
                candidate = self._random_candidate()
            elif roll < 0.40:
                candidate = self._crossover_candidate(elites, progress=0.85)
            elif self.local_queue and self.random.random() < 0.50:
                candidate = self.local_queue.pop(0)
                self.local_queued.discard(self._signature(candidate))
            else:
                candidate = self._guided_candidate(
                    elites, progress=0.85, crossover_probability=0.0
                )
            candidate = self._canonicalize(candidate)
            signature = self._signature(candidate)
            if signature in self.seen or not is_valid_candidate(
                candidate, self.strategy_adapter
            ):
                continue
            self.seen.add(signature)
            candidates.append(candidate)
            if progress_label and (
                len(candidates) == count
                or len(candidates) % max(1, math.ceil(count / 20)) == 0
            ):
                _show_loading_progress(progress_label, len(candidates), count)
        return candidates


def _parse_bound(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _maximum_candidate_warmup(strategy_spec, base_tune, tasks, enabled=True):
    """Use one shared warmup so worker data/indicator caches hit consistently."""
    if not enabled:
        return 0
    adapter = _adapter_from_spec(strategy_spec)
    warmups = []
    for _candidate_id, params in tasks:
        try:
            warmups.append(adapter.warmup_candles({**base_tune, **params}))
        except (KeyError, TypeError, ValueError):
            continue
    return max(warmups, default=adapter.warmup_candles(base_tune))


def _init_worker(
    start,
    end,
    base_tune=None,
    ignore_keyboard_interrupt=False,
    use_indicator_warmup=True,
    strategy_spec="ma",
    research=False,
    indicator_warmup_candles=None,
):
    global _WORKER_START, _WORKER_END, _WORKER_BASE_TUNE
    global _WORKER_USE_INDICATOR_WARMUP, _WORKER_INDICATOR_WARMUP_CANDLES
    global _WORKER_STRATEGY_SPEC, _WORKER_RESEARCH
    apply_process_policy()
    if ignore_keyboard_interrupt:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    _WORKER_START = start
    _WORKER_END = end
    _WORKER_BASE_TUNE = dict(base_tune or {})
    _WORKER_USE_INDICATOR_WARMUP = bool(use_indicator_warmup)
    _WORKER_INDICATOR_WARMUP_CANDLES = (
        max(0, int(indicator_warmup_candles))
        if indicator_warmup_candles is not None else None
    )
    _WORKER_STRATEGY_SPEC = strategy_spec or "ma"
    _WORKER_RESEARCH = bool(research)
    # Warm the largest repeated I/O cost once per process when the adapter can.
    _adapter_from_spec(_WORKER_STRATEGY_SPEC).preload(
        start, end, _WORKER_BASE_TUNE, _WORKER_USE_INDICATOR_WARMUP,
        _WORKER_INDICATOR_WARMUP_CANDLES,
    )


def _evaluate_candidate(
    index, params, base_tune, start, end, use_indicator_warmup=True,
    strategy_spec="ma", research=False, indicator_warmup_candles=None,
):
    started = time.perf_counter()
    try:
        result = _adapter_from_spec(strategy_spec).evaluate(
            tune={**base_tune, **params},
            start=start,
            end=end,
            use_indicator_warmup=use_indicator_warmup,
            indicator_warmup_candles=indicator_warmup_candles,
            research=research,
        )
        error = None
    except Exception as exc:  # return errors to the parent without killing the run
        result = None
        error = f"{type(exc).__name__}: {exc}"
    return index, params, result, time.perf_counter() - started, error


def _evaluate_task(task):
    """Small multiprocessing payload: workers merge the shared base tune locally."""
    index, params = task
    return _evaluate_candidate(
        index,
        params,
        _WORKER_BASE_TUNE,
        _WORKER_START,
        _WORKER_END,
        _WORKER_USE_INDICATOR_WARMUP,
        _WORKER_STRATEGY_SPEC,
        _WORKER_RESEARCH,
        _WORKER_INDICATOR_WARMUP_CANDLES,
    )


def _evaluate_random_window_task(task):
    """Evaluate one full configuration on one independently selected time window."""
    apply_process_policy()
    if len(task) == 6:
        test_index, candidate_id, window_id, params, start, end = task
        strategy_spec = "ma"
    else:
        test_index, candidate_id, window_id, params, start, end, strategy_spec = task
    started = time.perf_counter()
    try:
        result = _adapter_from_spec(strategy_spec).evaluate(
            tune=params,
            start=start,
            end=end,
            research=True,
        )
        error = None
    except Exception as exc:
        result = None
        error = f"{type(exc).__name__}: {exc}"
    return (
        test_index, candidate_id, window_id, params, start, end,
        result, time.perf_counter() - started, error,
    )


def _score(result):
    if not result:
        return -math.inf
    value = result.get("score", -math.inf)
    try:
        value = float(value)
    except (TypeError, ValueError):
        return -math.inf
    return value if math.isfinite(value) else -math.inf


def _objective_score(result, min_trades=0, max_drawdown=None):
    """Apply research-quality constraints before a result can become elite."""
    value = _score(result)
    if not result or value == -math.inf:
        return -math.inf
    if int(result.get("closed_trades", 0) or 0) < min_trades:
        return -math.inf
    if max_drawdown is not None:
        raw_drawdown = result.get("maximum_drawdown")
        if raw_drawdown is None or abs(float(raw_drawdown)) > max_drawdown:
            return -math.inf
    return value


def _robust_validation_score(
    train_result,
    validation_result,
    overfit_penalty=0.25,
    train_candles=None,
    validation_candles=None,
):
    """Prefer strong validation performance and penalize train-only excess."""
    train_score = _score(train_result)
    validation_score = _score(validation_result)
    if train_candles is not None and validation_candles is not None:
        normalized_train = _time_normalized_score(train_score, train_candles)
        normalized_validation = _time_normalized_score(
            validation_score, validation_candles
        )
        if normalized_train is not None:
            train_score = normalized_train
        if normalized_validation is not None:
            validation_score = normalized_validation
    if not math.isfinite(validation_score):
        return -math.inf
    optimistic_gap = max(0.0, train_score - validation_score)
    return validation_score - overfit_penalty * optimistic_gap


def _time_normalized_score(score, range_candles):
    """Convert a raw optimizer score to a timeframe-aware annual rate."""
    numeric_score = _finite_number(score)
    try:
        candles = int(range_candles)
    except (TypeError, ValueError):
        return None
    if numeric_score is None or candles <= 0:
        return None
    return numeric_score * _ACTIVE_CANDLES_PER_YEAR / candles


def _range_candle_count(range_start, range_end):
    return max(1, _bound_index(range_end) - _bound_index(range_start))


AUTO_STAGE_ORDER = ("discovery", "validation", "stress", "walk_forward", "final")
AUTO_STAGE_WEIGHTS = {
    # Recent Discovery evidence is intentionally stronger than older regimes.
    "discovery": 0.35,
    "validation": 0.15,
    "stress": 0.10,
    "walk_forward": 0.30,
    "final": 0.10,
}
AUTO_STATE_VERSION = 2
LEGACY_AUTO_STATE_VERSIONS = {1, AUTO_STATE_VERSION}


def _finite_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _record_comparable_score(record):
    normalized = _finite_number(record.get("time_normalized_score"))
    return normalized if normalized is not None else _finite_number(
        record.get("objective_score")
    )


def _surrogate_target(record):
    """Return a range-comparable learning label, preferring funnel feedback."""
    for key in ("learning_score", "robust_score", "time_normalized_score"):
        value = _finite_number(record.get(key))
        if value is not None:
            return value
    return _finite_number(record.get("objective_score"))


def _score_percentiles(records, score_getter=_record_comparable_score):
    """Return tie-aware [0, 1] percentiles without assuming score scale."""
    ranked = sorted(
        (
            (float(score), record.get("candidate_id"))
            for record in records
            for score in [score_getter(record)]
            if score is not None and record.get("candidate_id") is not None
        ),
        key=lambda item: item[0],
    )
    if not ranked:
        return {}
    if len(ranked) == 1:
        return {ranked[0][1]: 1.0}
    denominator = max(1, len(ranked) - 1)
    output = {}
    index = 0
    while index < len(ranked):
        end = index + 1
        while end < len(ranked) and ranked[end][0] == ranked[index][0]:
            end += 1
        percentile = ((index + end - 1) / 2) / denominator
        for _, candidate_id in ranked[index:end]:
            output[candidate_id] = percentile
        index = end
    return output


def _annotate_discovery_learning_scores(records):
    """Use within-range ranks so cycles with different durations remain comparable."""
    percentiles = _score_percentiles(records)
    for record in records:
        candidate_id = record.get("candidate_id")
        if candidate_id in percentiles:
            record["learning_score"] = percentiles[candidate_id]
            record["learning_source"] = "normalized_discovery_rank"
    return records


def _apply_funnel_learning_scores(history, stage_records):
    """Teach the surrogate which Discovery candidates survive robust later stages."""
    if not stage_records or "discovery" not in stage_records:
        return history
    weights = AUTO_STAGE_WEIGHTS
    stage_percentiles = {
        stage: _score_percentiles(records)
        for stage, records in stage_records.items()
        if stage in weights and records
    }
    discovery_ids = set(stage_percentiles.get("discovery", {}))
    targets = {}
    for candidate_id in discovery_ids:
        targets[candidate_id] = sum(
            weights[stage] * percentiles.get(candidate_id, 0.0)
            for stage, percentiles in stage_percentiles.items()
        )
    for record in history:
        candidate_id = record.get("candidate_id")
        if candidate_id in targets:
            record["learning_score"] = targets[candidate_id]
            record["learning_source"] = "robust_funnel_rank"
    return history


def _importance_target(record, target):
    if target == "objective_score":
        return _surrogate_target(record)
    return _finite_number((record.get("result") or {}).get(target))


def _learn_parameter_importance(records, parameter_keys, target="objective_score"):
    """Estimate which parameters explain the largest share of result variance.

    Exact values are grouped for small discrete spaces. Highly varied numeric
    parameters are binned so locally refined off-grid values remain useful.
    A small exploration floor prevents an early noisy estimate from permanently
    freezing any parameter.
    """
    usable = []
    for record in records:
        value = _importance_target(record, target)
        if value is not None:
            usable.append((record["params"], value))
    if len(usable) < 4:
        equal = 1.0 / max(1, len(parameter_keys))
        return {
            key: {"weight": equal, "effect": 0.0, "groups": 0, "samples": len(usable)}
            for key in parameter_keys
        }

    targets = [value for _, value in usable]
    overall_mean = statistics.fmean(targets)
    total_variance = statistics.fmean((value - overall_mean) ** 2 for value in targets)
    raw = {}
    for key in parameter_keys:
        key_values = [params[key] for params, _ in usable]
        unique = list(dict.fromkeys(key_values))
        numeric = all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in unique
        )
        groups = {}
        if numeric and len(unique) > 12:
            lower, upper = min(unique), max(unique)
            width = (upper - lower) / 8 if upper > lower else 0
            for (params, target_value) in usable:
                group = 0 if width == 0 else min(7, int((params[key] - lower) / width))
                groups.setdefault(group, []).append(target_value)
        else:
            for params, target_value in usable:
                groups.setdefault((type(params[key]).__name__, params[key]), []).append(target_value)

        between = sum(
            len(values) * (statistics.fmean(values) - overall_mean) ** 2
            for values in groups.values()
        ) / len(usable)
        effect = between / total_variance if total_variance > 0 else 0.0
        confidence = min(1.0, len(usable) / max(20.0, len(groups) * 4.0))
        raw[key] = {
            "effect": max(0.0, min(1.0, effect)) * confidence,
            "groups": len(groups),
            "samples": len(usable),
        }

    # The floor reserves exploration for every variable while high-effect
    # variables receive most local mutations and deterministic neighbor tests.
    scores = {key: 0.05 + item["effect"] for key, item in raw.items()}
    total = sum(scores.values()) or 1.0
    return {
        key: {**raw[key], "weight": scores[key] / total}
        for key in parameter_keys
    }


def _smooth_parameter_importance(previous, learned, previous_weight=0.65):
    if not previous:
        return learned
    blended = {}
    for key, item in learned.items():
        old = previous.get(key, {})
        weight = previous_weight * float(old.get("weight", 0.0)) + (
            1.0 - previous_weight
        ) * float(item.get("weight", 0.0))
        blended[key] = {**item, "weight": weight}
    total = sum(item["weight"] for item in blended.values()) or 1.0
    for item in blended.values():
        item["weight"] /= total
    return blended


def _learn_mutation_guidance(records, parameter_keys, grid):
    """Learn promising numeric directions and safe local step sizes in O(rows*keys)."""
    usable = [
        record for record in records
        if _surrogate_target(record) is not None
    ]
    guidance = {}
    if len(usable) < 8:
        return guidance
    targets = [_surrogate_target(record) for record in usable]
    target_mean = statistics.fmean(targets)
    target_variance = statistics.fmean(
        (value - target_mean) ** 2 for value in targets
    )
    top_count = max(2, math.ceil(len(usable) * 0.20))
    top_records = sorted(
        usable, key=lambda record: _surrogate_target(record), reverse=True
    )[:top_count]
    for key in parameter_keys:
        values = [record.get("params", {}).get(key) for record in usable]
        if not values or not all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in values
        ):
            continue
        lower, upper = min(values), max(values)
        if lower == upper or target_variance <= 1e-15:
            continue
        value_mean = statistics.fmean(values)
        value_variance = statistics.fmean(
            (value - value_mean) ** 2 for value in values
        )
        if value_variance <= 1e-15:
            continue
        covariance = statistics.fmean(
            (value - value_mean) * (target - target_mean)
            for value, target in zip(values, targets)
        )
        correlation = covariance / math.sqrt(value_variance * target_variance)
        confidence = min(1.0, abs(correlation))
        direction = 1 if correlation > 0.05 else -1 if correlation < -0.05 else 0
        ordered_grid = sorted({
            value for value in grid.get(key, ())
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        })
        hard_lower = ordered_grid[0] if ordered_grid else lower
        hard_upper = ordered_grid[-1] if ordered_grid else upper
        span = hard_upper - hard_lower
        top_values = [record["params"][key] for record in top_records]
        top_mean = statistics.fmean(top_values)
        boundary_pressure = 0
        if span > 0 and top_mean >= hard_upper - 0.10 * span:
            boundary_pressure = 1
        elif span > 0 and top_mean <= hard_lower + 0.10 * span:
            boundary_pressure = -1
        if boundary_pressure and (direction == 0 or confidence < 0.20):
            direction = boundary_pressure
        step_multiplier = 3 if confidence >= 0.65 else 2 if confidence >= 0.30 else 1
        guidance[key] = {
            "direction": direction,
            "confidence": confidence,
            "step_multiplier": step_multiplier,
            "boundary_pressure": boundary_pressure,
            "samples": len(usable),
        }
    return guidance


class ExtraTreesSurrogate:
    """Small dependency-free extremely-randomized tree ensemble.

    It is intentionally limited to the numeric/boolean parameter spaces used by
    this optimizer.  The ensemble predicts both a mean score and disagreement
    between trees, which lets auto mode balance exploitation and exploration.
    """

    def __init__(
        self, n_trees=32, max_depth=10, min_leaf=4, max_features=None, seed=42,
    ):
        self.n_trees = max(1, int(n_trees))
        self.max_depth = max(1, int(max_depth))
        self.min_leaf = max(1, int(min_leaf))
        self.max_features = max_features
        self.seed = int(seed)
        self.trees = []

    @staticmethod
    def _leaf(targets, indices):
        return ("leaf", statistics.fmean(targets[index] for index in indices))

    def _build_tree(self, features, targets, indices, depth, randomizer):
        if depth >= self.max_depth or len(indices) < self.min_leaf * 2:
            return self._leaf(targets, indices)
        node_targets = [targets[index] for index in indices]
        if max(node_targets) - min(node_targets) <= 1e-12:
            return self._leaf(targets, indices)

        feature_count = len(features[0])
        requested = self.max_features or max(
            1, round(2.0 * math.sqrt(feature_count))
        )
        selected_features = randomizer.sample(
            range(feature_count), min(feature_count, requested)
        )
        best = None
        for feature_index in selected_features:
            values = [features[index][feature_index] for index in indices]
            lower, upper = min(values), max(values)
            if lower == upper:
                continue
            # More randomized thresholds substantially improve split quality in
            # wide, conditional parameter spaces while keeping the model cheap.
            for _ in range(8):
                threshold = randomizer.uniform(lower, upper)
                left = []
                right = []
                for index in indices:
                    target = (
                        left
                        if features[index][feature_index] <= threshold
                        else right
                    )
                    target.append(index)
                if len(left) < self.min_leaf or len(indices) - len(left) < self.min_leaf:
                    continue
                left_mean = statistics.fmean(targets[index] for index in left)
                right_mean = statistics.fmean(targets[index] for index in right)
                loss = sum((targets[index] - left_mean) ** 2 for index in left)
                loss += sum((targets[index] - right_mean) ** 2 for index in right)
                if best is None or loss < best[0]:
                    best = (loss, feature_index, threshold, left, right)
        if best is None:
            return self._leaf(targets, indices)
        _, feature_index, threshold, left, right = best
        return (
            "node", feature_index, threshold,
            self._build_tree(features, targets, left, depth + 1, randomizer),
            self._build_tree(features, targets, right, depth + 1, randomizer),
        )

    def fit(self, features, targets, progress_label=None):
        if not features or len(features) != len(targets):
            raise ValueError("surrogate training features and targets must be non-empty")
        sample_count = len(features)
        self.trees = []
        for tree_index in range(self.n_trees):
            randomizer = random.Random(self.seed + tree_index * 104729)
            # Random subsampling gives useful model disagreement without letting
            # duplicate bootstrap rows dominate small optimization histories.
            subset_size = max(self.min_leaf * 2, round(sample_count * 0.80))
            subset_size = min(sample_count, subset_size)
            indices = randomizer.sample(range(sample_count), subset_size)
            self.trees.append(
                self._build_tree(features, targets, indices, 0, randomizer)
            )
            if progress_label and (
                tree_index + 1 == self.n_trees
                or (tree_index + 1) % max(1, math.ceil(self.n_trees / 20)) == 0
            ):
                _show_loading_progress(progress_label, tree_index + 1, self.n_trees)
        return self

    @staticmethod
    def _predict_tree(tree, features):
        while tree[0] == "node":
            _, feature_index, threshold, left, right = tree
            tree = left if features[feature_index] <= threshold else right
        return tree[1]

    def predict_mean_std(self, feature_rows, progress_label=None):
        if not self.trees:
            raise ValueError("surrogate must be fitted before prediction")
        output = []
        total = len(feature_rows)
        for index, row in enumerate(feature_rows, start=1):
            predictions = [self._predict_tree(tree, row) for tree in self.trees]
            output.append((
                statistics.fmean(predictions),
                statistics.pstdev(predictions) if len(predictions) > 1 else 0.0,
            ))
            if progress_label and (
                index == total or index % max(1, math.ceil(total / 20)) == 0
            ):
                _show_loading_progress(progress_label, index, total)
        return output


def _record_percentiles(records):
    valid = [
        record for record in records
        if _record_comparable_score(record) is not None
    ]
    valid.sort(
        key=lambda record: _record_comparable_score(record),
        reverse=True,
    )
    denominator = max(1, len(valid) - 1)
    return {
        record["candidate_id"]: 1.0 - rank / denominator
        for rank, record in enumerate(valid)
    }


def _combine_auto_stage_records(stage_records):
    """Rank candidates across every completed, non-overlapping market regime."""
    completed_stages = [stage for stage in AUTO_STAGE_ORDER if stage in stage_records]
    if not completed_stages:
        return []
    final_stage = completed_stages[-1]
    record_maps = {
        stage: {record["candidate_id"]: record for record in records}
        for stage, records in stage_records.items()
    }
    percentiles = {
        stage: _record_percentiles(records) for stage, records in stage_records.items()
    }
    stage_weights = AUTO_STAGE_WEIGHTS
    if "walk_forward" not in completed_stages:
        # Preserve the ranking contract used by version-1 campaigns while an
        # interrupted legacy cycle is being finished.
        stage_weights = {
            "discovery": 0.40, "validation": 0.30,
            "stress": 0.20, "final": 0.10,
        }
    combined = []
    for latest in stage_records[final_stage]:
        candidate_id = latest["candidate_id"]
        if any(candidate_id not in record_maps[stage] for stage in completed_stages):
            continue
        ranks = []
        weighted_total = 0.0
        weight_total = 0.0
        stage_scores = {}
        stage_metrics = {}
        qualified = True
        for stage in completed_stages:
            record = record_maps[stage][candidate_id]
            score = _finite_number(record.get("objective_score"))
            normalized_score = _record_comparable_score(record)
            percentile = percentiles[stage].get(candidate_id)
            if score is None or normalized_score is None or percentile is None:
                qualified = False
                break
            weight = stage_weights[stage]
            ranks.append(percentile)
            weighted_total += weight * percentile
            weight_total += weight
            stage_scores[stage] = score
            stage_metrics[stage] = {
                **record["result"],
                "time_normalized_score": normalized_score,
                "range_start": record.get("range_start"),
                "range_end": record.get("range_end"),
                "range_candles": record.get("range_candles"),
                "duration_s": record.get("duration"),
            }
        if not qualified:
            robust_score = -math.inf
        else:
            weighted_rank = weighted_total / weight_total
            dispersion = statistics.pstdev(ranks) if len(ranks) > 1 else 0.0
            transformed_quality = sum(
                stage_weights[stage]
                * math.copysign(
                    math.log1p(abs(stage_metrics[stage]["time_normalized_score"])),
                    stage_metrics[stage]["time_normalized_score"],
                )
                for stage in completed_stages
            ) / weight_total
            robust_score = (
                50.0 * weighted_rank
                + 20.0 * min(ranks)
                - 10.0 * dispersion
                + 10.0 * transformed_quality
            )
        combined_record = {
            "candidate_id": candidate_id,
            "params": latest["params"],
            "robust_score": robust_score,
            "recency_score": (
                100.0 * percentiles.get("discovery", {}).get(candidate_id)
                if percentiles.get("discovery", {}).get(candidate_id) is not None
                else None
            ),
            "stage_consistency_score": (
                100.0 * (1.0 - statistics.pstdev(ranks)) if ranks else None
            ),
            "worst_stage_percentile": min(ranks) if ranks else None,
            "stage_scores": stage_scores,
            "stage_metrics": stage_metrics,
        }
        combined_record.update(_auto_candidate_decision(combined_record))
        combined.append(combined_record)
    combined.sort(key=_auto_candidate_rank_key, reverse=True)
    return combined


def _auto_candidate_decision(record):
    """Classify Auto evidence for research triage, never for live deployment."""
    robust = _finite_number(record.get("robust_score"))
    recency = _finite_number(record.get("recency_score"))
    consistency = _finite_number(record.get("stage_consistency_score"))
    worst = _finite_number(record.get("worst_stage_percentile"))
    stages = record.get("stage_metrics", {}) or {}
    final_metrics = stages.get("final", {}) or {}
    final_return = _finite_number(final_metrics.get("total_profit_percent"))
    final_drawdown = _finite_number(final_metrics.get("maximum_drawdown"))
    liquidations = sum(
        int((metrics or {}).get("liquidations", 0) or 0)
        for metrics in stages.values()
    )
    reject_reasons = []
    if robust is None:
        reject_reasons.append("invalid robust score")
    if liquidations > 0:
        reject_reasons.append("liquidation detected")
    if final_return is not None and final_return <= 0:
        reject_reasons.append("non-positive final return")
    if final_drawdown is not None and abs(final_drawdown) > 50:
        reject_reasons.append("final drawdown above 50%")
    if worst is not None and worst < 0.10:
        reject_reasons.append("worst stage below 10th percentile")
    if recency is not None and recency < 10:
        reject_reasons.append("recent Discovery below 10th percentile")
    complete = all(stage in stages for stage in AUTO_STAGE_ORDER)
    if reject_reasons:
        decision = "REJECT"
        reasons = reject_reasons
    elif (
        complete
        and worst is not None and worst >= 0.50
        and recency is not None and recency >= 60
        and consistency is not None and consistency >= 75
        and final_return is not None and final_return > 0
    ):
        decision = "ACCEPT"
        reasons = [
            "stable across every Auto stage",
            "strong recent Discovery rank",
            "positive full-development return",
            "eligible for independent Research validation",
        ]
    else:
        decision = "WATCH"
        reasons = []
        if not complete:
            reasons.append("Auto funnel is not complete")
        if worst is None or worst < 0.50:
            reasons.append("worst-stage rank is below ACCEPT threshold")
        if recency is None or recency < 60:
            reasons.append("recent Discovery rank is below ACCEPT threshold")
        if consistency is None or consistency < 75:
            reasons.append("cross-stage consistency is below ACCEPT threshold")
        if final_return is None:
            reasons.append("full-development return is not available")
    return {
        "decision": decision,
        "decision_scope": "Auto triage only; Research and sealed Holdout still required",
        "decision_reasons": reasons,
    }


def _auto_candidate_rank_key(record):
    """Prefer research-eligible evidence before raw score magnitude."""
    decision_priority = {"ACCEPT": 2, "WATCH": 1, "REJECT": 0}
    return (
        decision_priority.get(record.get("decision"), 1),
        _finite_number(record.get("robust_score")) or -math.inf,
        _finite_number(record.get("recency_score")) or -math.inf,
        _finite_number(record.get("stage_consistency_score")) or -math.inf,
    )


def _market_data_coverage(source=None):
    """Return bounds for the active strategy's own candle dataset."""
    source = source or _ACTIVE_MARKET_DATA_SOURCE
    if source is not None:
        return source.coverage()

    # Backward-compatible fallback for callers importing this helper directly.
    from get_candle_index import _open_times

    open_times = _open_times().dropna()
    if open_times.empty:
        raise ValueError("market data does not contain a valid Open time")
    recent = open_times.tail(100).sort_values()
    deltas = recent.diff().dropna()
    candle_delta = deltas.median() if not deltas.empty else timedelta(minutes=15)
    if candle_delta <= timedelta(0):
        candle_delta = timedelta(minutes=15)
    return {
        "first_candle": open_times.iloc[0].strftime("%Y-%m-%d %H:%M:%S"),
        "last_candle": open_times.iloc[-1].strftime("%Y-%m-%d %H:%M:%S"),
        "end_exclusive": (
            open_times.iloc[-1] + candle_delta
        ).strftime("%Y-%m-%d %H:%M:%S"),
        "interval_seconds": float(candle_delta.total_seconds()),
    }


def _latest_market_end():
    """Return an exclusive timestamp immediately after the last valid candle."""
    return _market_data_coverage()["end_exclusive"]


def _shift_calendar_months(value, months):
    """Shift a datetime by whole calendar months while preserving valid days."""
    month_index = value.year * 12 + (value.month - 1) + int(months)
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _date_bound(value):
    """Use compact ISO dates for midnight bounds and timestamps otherwise."""
    if value.hour == value.minute == value.second == value.microsecond == 0:
        return value.strftime("%Y-%m-%d")
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _apply_strategy_date_defaults(args, module, arguments):
    """Strategy defaults never override explicit CLI choices or fixed dates."""
    if args.date_policy != 'auto':
        return
    for name, value in getattr(module, 'AUTO_DATE_DEFAULTS', {}).items():
        flag = '--' + name.replace('_', '-')
        if not any(a == flag or a.startswith(flag + '=') for a in arguments):
            setattr(args, name, value)


def _rolling_date_protocol(args, coverage=None):
    """Anchor optimization to a recent window ending at the latest candle.

    Only Stress uses the short immediately preceding historical slice. It is
    excluded from Final so older regimes cannot dominate parameter selection.
    """
    coverage = coverage or _market_data_coverage()
    data_start = datetime.fromisoformat(coverage["first_candle"])
    data_end = datetime.fromisoformat(coverage["end_exclusive"])
    development_end = data_end
    development_start = _shift_calendar_months(
        development_end, -int(args.rolling_development_months)
    )
    stability_start = _shift_calendar_months(
        development_start, -int(args.rolling_stress_months)
    )
    requested_stability_start = stability_start
    stability_start = max(stability_start, data_start)
    if development_start <= data_start:
        raise ValueError(
            'dataset is too short for the requested development window plus Stress; '
            'reduce --rolling-development-months or supply more history'
        )
    validation_start = development_start
    discovery_start = _shift_calendar_months(
        validation_start, int(args.rolling_validation_months)
    )

    # These bounds remain available to the separate research/holdout workflow.
    # Auto candidate search itself ends at development_end (the newest candle).
    holdout_start = _shift_calendar_months(
        data_end, -int(args.rolling_holdout_months)
    )
    research_end = _shift_calendar_months(
        holdout_start, -int(args.rolling_embargo_months)
    )
    if not stability_start < validation_start < discovery_start < development_end:
        raise ValueError(
            "rolling date policy leaves no recent Discovery range; reduce validation "
            "months or increase development months"
        )
    return {
        "policy": "auto",
        "dataset_first_candle": coverage["first_candle"],
        "stress_clipped_to_data": stability_start != requested_stability_start,
        "dataset_last_candle": coverage["last_candle"],
        "dataset_end_exclusive": coverage["end_exclusive"],
        "development_start": _date_bound(development_start),
        "stability_start": _date_bound(stability_start),
        "stability_end": _date_bound(development_start),
        "validation_start": _date_bound(validation_start),
        "discovery_start": _date_bound(discovery_start),
        "development_end": _date_bound(development_end),
        "research_end": _date_bound(research_end),
        "embargo_start": _date_bound(research_end),
        "holdout_start": _date_bound(holdout_start),
        "holdout_end": _date_bound(data_end),
        "months": {
            "development": int(args.rolling_development_months),
            "oos": int(args.rolling_oos_months),
            "embargo": int(args.rolling_embargo_months),
            "holdout": int(args.rolling_holdout_months),
            "historical_stability": int(args.rolling_stress_months),
            "validation": int(args.rolling_validation_months),
        },
    }


def _apply_date_policy(args, coverage=None):
    """Resolve automatic dates once; fixed mode preserves every CLI boundary."""
    if getattr(args, "date_policy", "auto") == "fixed":
        stress_end = args.auto_stress_end or args.auto_validation_start
        protocol = {
            "policy": "fixed",
            "development_start": args.auto_stress_start,
            "stability_start": args.auto_stress_start,
            "stability_end": stress_end,
            "validation_start": args.auto_validation_start,
            "discovery_start": args.auto_discovery_start,
            "development_end": args.auto_end,
            "research_end": args.wf_end,
            "holdout_start": args.holdout_start,
            "holdout_end": args.holdout_end,
        }
    else:
        protocol = _rolling_date_protocol(args, coverage=coverage)
        args.start = protocol["development_start"]
        args.end = protocol["development_end"]
        args.auto_stress_start = protocol["stability_start"]
        args.auto_stress_end = protocol["stability_end"]
        args.auto_validation_start = protocol["validation_start"]
        args.auto_discovery_start = protocol["discovery_start"]
        args.auto_end = protocol["development_end"]
        args.random_audit_earliest = protocol["development_start"]
        args.random_audit_recent_start = protocol["discovery_start"]
        args.wf_start = protocol["development_start"]
        args.wf_end = protocol["research_end"]
        args.holdout_start = protocol["holdout_start"]
        args.holdout_end = "latest"
    args._date_protocol = protocol
    return protocol


def _restore_frozen_date_protocol(args, protocol):
    """Apply persisted boundaries so resumed campaigns never drift with new candles."""
    if not protocol:
        return
    mappings = {
        "development_start": ("start", "auto_stress_start", "wf_start", "random_audit_earliest"),
        "stability_start": ("auto_stress_start",),
        "stability_end": ("auto_stress_end",),
        "validation_start": ("auto_validation_start",),
        "discovery_start": ("auto_discovery_start", "random_audit_recent_start"),
        "development_end": ("end", "auto_end"),
        "research_end": ("wf_end",),
        "holdout_start": ("holdout_start",),
    }
    for protocol_key, argument_names in mappings.items():
        value = protocol.get(protocol_key)
        if value is None:
            continue
        for argument_name in argument_names:
            setattr(args, argument_name, value)
    args._date_protocol = dict(protocol)


def _load_frozen_date_protocol(output_dir):
    """Find the newest authoritative protocol across the campaign workflow."""
    output_dir = Path(output_dir)
    candidates = (
        output_dir / "walk_forward_research" / "walk_forward_report.json",
        output_dir / "staged_state.json",
        output_dir / "auto_state.json",
    )
    for path in candidates:
        payload = _load_json(path, {}) if path.is_file() else {}
        protocol = (payload or {}).get("date_protocol")
        if isinstance(protocol, dict) and protocol:
            return protocol
    return None


def _timestamp_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _strategy_data_file(args, adapter=None):
    adapter = adapter or _adapter_from_args(args)
    explicit = getattr(args, "data_file", None)
    if explicit:
        return Path(explicit)
    discovered = adapter.data_file
    if discovered:
        return Path(discovered)
    if adapter.identifier == "ma_strategy:ma_strategy":
        from fetch_calculate_data import DATA_FILE

        return Path(DATA_FILE)
    return None


def _activate_strategy_market_data(args, adapter=None):
    """Make date/index math follow the selected strategy and its timeframe."""
    global _ACTIVE_MARKET_DATA_SOURCE, _ACTIVE_CANDLES_PER_YEAR
    adapter = adapter or _adapter_from_args(args)
    data_file = _strategy_data_file(args, adapter)
    if data_file is None:
        _ACTIVE_MARKET_DATA_SOURCE = None
        _ACTIVE_CANDLES_PER_YEAR = CANDLES_PER_YEAR_15M
        return None
    source = MarketDataSource(data_file, adapter.timeframe)
    coverage = source.coverage()
    args._detected_timeframe = coverage['timeframe']
    interval_seconds = float(coverage["interval_seconds"])
    _ACTIVE_MARKET_DATA_SOURCE = source
    _ACTIVE_CANDLES_PER_YEAR = 365.25 * 24 * 60 * 60 / interval_seconds
    args._market_data_source = source
    return source


def _run_research_preflight(args, output_dir, resolved_config):
    """Audit data and fingerprint the exact research environment once per run."""
    mode = getattr(args, "data_audit", "off")
    if mode == "off":
        return None
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    adapter = _adapter_from_args(args)
    workflow = (
        "sealed_holdout"
        if getattr(args, "sealed_holdout", False)
        else "nested_walk_forward"
        if getattr(args, "research", False)
        else "auto_development"
        if getattr(args, "auto", False)
        else "standard_development"
    )
    data_file = _strategy_data_file(args, adapter)
    if data_file is None:
        payload = {
            "passed": mode != "strict",
            "warning": (
                f"strategy {adapter.identifier} exposes no DATA_FILE; use --data-file "
                "for auditable research"
            ),
        }
        _write_json(output_dir / "market_data_audit.json", payload)
        if mode == "strict":
            raise ValueError(payload["warning"])
        return payload
    policies = {}
    if mode == "warn":
        from market_data_audit import DEFAULT_ISSUE_POLICIES

        policies = {code: "warn" for code in DEFAULT_ISSUE_POLICIES}
    source = adapter.market_data_source(data_file)
    expected_interval = source.interval() if source is not None else "15min"
    report = audit_market_data(
        data_file,
        AuditConfig(policies=policies, expected_interval=expected_interval),
    )
    _write_json(output_dir / "market_data_audit.json", report.to_dict())
    if mode == "strict":
        report.raise_for_errors()
    fingerprints = build_run_fingerprints(
        data_file,
        Path(__file__).resolve().parent,
        resolved_config,
        code_root=Path(__file__).resolve().parent,
    )
    manifest = {
        "created_at": _timestamp_now(),
        "workflow": workflow,
        "strategy": adapter.identifier,
        "data_file": str(data_file.resolve()),
        "audit_summary": report.summary(),
        "fingerprints": fingerprints.to_dict(),
        "resolved_config": resolved_config,
    }
    _write_json(output_dir / "research_manifest.json", manifest)
    manifest_dir = output_dir / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_id = fingerprints.combined_sha256[:12]
    _write_json(
        manifest_dir / f"{timestamp}_{workflow}_{run_id}.json", manifest
    )
    _write_json(
        manifest_dir / f"{timestamp}_{workflow}_{run_id}_data_audit.json",
        report.to_dict(),
    )
    return manifest


def _show_loading_progress(label, completed, total):
    """Render one in-place startup progress line for potentially large histories."""
    if total < 5:
        return
    percent = 100.0 * completed / max(1, total)
    end = "\n" if completed >= total else ""
    print(
        f"\r{label}: {percent:6.2f}% ({completed:,}/{total:,})",
        end=end,
        flush=True,
    )


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


def _replace_with_retry(temporary, path, attempts=20):
    """Replace a file atomically, tolerating short-lived Windows file locks."""
    for attempt in range(attempts):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt + 1 >= attempts:
                raise
            # Editors, indexers, antivirus, and sync clients can briefly open a
            # destination without FILE_SHARE_DELETE, which makes os.replace fail.
            time.sleep(min(0.05 * (2 ** attempt), 1.0))


def _write_json(path, payload):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(_json_safe(payload), file, indent=2, ensure_ascii=False)
        file.write("\n")
    _replace_with_retry(temporary, path)


def _save_optimizer_workbook(
    results_path, output_path, parameter_keys, max_rows=5000, selected_best=None
):
    """Create a filterable, frozen-header XLSX companion to the raw CSV."""
    try:
        import pandas as pd
        from openpyxl import load_workbook
        from openpyxl.chart import BarChart, Reference
        from openpyxl.chart.label import DataLabelList
        from openpyxl.formatting.rule import ColorScaleRule
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter
        from openpyxl.worksheet.table import Table, TableStyleInfo
    except ImportError:
        return None

    results_path = Path(results_path)
    if not results_path.is_file():
        return None
    max_rows = max(1, int(max_rows))
    ranked = None
    selected_index = selected_best.get("index") if selected_best else None
    selected_export_row = None
    for chunk in pd.read_csv(results_path, chunksize=50_000):
        if chunk.empty:
            continue
        if selected_index is not None and "test_index" in chunk:
            match = chunk[chunk["test_index"] == selected_index]
            if not match.empty:
                selected_export_row = match.iloc[[0]].copy()
        score_column = "objective_score" if "objective_score" in chunk else "score"
        chunk = chunk.sort_values(score_column, ascending=False, na_position="last").head(max_rows)
        ranked = chunk if ranked is None else pd.concat([ranked, chunk], ignore_index=True)
        ranked = ranked.sort_values(
            score_column, ascending=False, na_position="last"
        ).head(max_rows)
    if ranked is None or ranked.empty:
        return None
    if (
        selected_export_row is not None
        and selected_index not in set(ranked.get("test_index", ()))
    ):
        ranked = pd.concat([ranked, selected_export_row], ignore_index=True)
    ranked = ranked.reset_index(drop=True)
    ranked.insert(0, "rank", range(1, len(ranked) + 1))
    if selected_index is None:
        selected_index = ranked.iloc[0].get("test_index")
    selections = [
        "SELECTED WINNER - USE best_params.json"
        if row.get("test_index") == selected_index else ""
        for _, row in ranked.iterrows()
    ]
    if not any(selections):
        selections[0] = "RANK #1 IN EXPORTED ROWS - CHECK best_params_manifest.json"
    ranked.insert(1, "selection", selections)

    def existing(columns):
        return [column for column in columns if column in ranked.columns]

    identity = existing([
        "rank", "selection", "test_index", "score", "objective_score", "duration_s"
    ])
    selected_rows = ranked[ranked["test_index"] == selected_index]
    best = selected_rows.iloc[0] if not selected_rows.empty else ranked.iloc[0]
    dashboard_rows = [
        {"Section": "Winner", "Metric": "Recommended parameter file", "Value": "best_params.json"},
        {"Section": "Winner", "Metric": "Selection status", "Value": best.get("selection")},
        {"Section": "Winner", "Metric": "Test index", "Value": best.get("test_index")},
        {
            "Section": "Winner", "Metric": "Selected score",
            "Value": (
                selected_best.get("selection_score")
                if selected_best and selected_best.get("selection_score") is not None
                else best.get("objective_score")
            ),
        },
        {"Section": "Performance", "Metric": "Total profit %", "Value": best.get("total_profit_percent")},
        {"Section": "Performance", "Metric": "Total profit", "Value": best.get("total_profit")},
        {"Section": "Performance", "Metric": "Maximum drawdown %", "Value": best.get("maximum_drawdown")},
        {"Section": "Performance", "Metric": "Closed trades", "Value": best.get("closed_trades")},
        {"Section": "Performance", "Metric": "Win rate %", "Value": best.get("win_rate")},
        {"Section": "Performance", "Metric": "Profit factor", "Value": best.get("profit_factor")},
        {"Section": "Direction", "Metric": "Stronger side", "Value": best.get("stronger_side")},
        {"Section": "Long", "Metric": "Long net profit", "Value": best.get("long_profit")},
        {"Section": "Long", "Metric": "Long trades", "Value": best.get("long_trades")},
        {"Section": "Long", "Metric": "Long win rate %", "Value": best.get("long_win_rate")},
        {"Section": "Long", "Metric": "Long profit factor", "Value": best.get("long_profit_factor")},
        {"Section": "Long", "Metric": "Long max drawdown %", "Value": best.get("long_maximum_drawdown")},
        {"Section": "Short", "Metric": "Short net profit", "Value": best.get("short_profit")},
        {"Section": "Short", "Metric": "Short trades", "Value": best.get("short_trades")},
        {"Section": "Short", "Metric": "Short win rate %", "Value": best.get("short_win_rate")},
        {"Section": "Short", "Metric": "Short profit factor", "Value": best.get("short_profit_factor")},
        {"Section": "Short", "Metric": "Short max drawdown %", "Value": best.get("short_maximum_drawdown")},
    ]
    best_param_values = (
        dict(selected_best.get("params", {})) if selected_best
        else {key: best.get(key) for key in parameter_keys}
    )
    best_params = pd.DataFrame([
        {
            "parameter": key,
            "value": value,
            "role": "optimized" if key in parameter_keys else "fixed/base",
        }
        for key, value in best_param_values.items()
    ])
    sheets = {
        "Dashboard": pd.DataFrame(dashboard_rows),
        "Rankings": ranked,
        "Best Parameters": best_params,
        "Parameters": ranked[identity + existing(parameter_keys)],
        "Directional Metrics": ranked[identity + existing([
            "stronger_side", "long_profit", "short_profit", "directional_profit_gap",
            "long_trades", "long_wins", "long_losses", "long_win_rate",
            "long_profit_factor", "long_expectancy", "long_average_win",
            "long_average_loss", "long_payoff_ratio", "long_maximum_drawdown",
            "long_total_fees", "long_liquidations", "short_trades", "short_wins",
            "short_losses", "short_win_rate", "short_profit_factor",
            "short_expectancy", "short_average_win", "short_average_loss",
            "short_payoff_ratio", "short_maximum_drawdown", "short_total_fees",
            "short_liquidations",
        ])],
        "Risk Quality": ranked[identity + existing([
            "total_profit_percent", "total_profit", "maximum_drawdown", "closed_trades",
            "win_rate", "profit_factor", "expectancy_percent", "calmar_ratio",
            "liquidations", "total_fees", "profit_per_trade",
        ])],
        "RSI Metrics": ranked[identity + [
            column for column in ranked.columns if column.startswith("rsi_")
        ]],
        "Scale Metrics": ranked[identity + [
            column for column in ranked.columns if column.startswith("scale_")
        ]],
    }
    core_columns = existing([
        "test_index", "score", "objective_score", "final_balance",
        "final_balance_without_fee", "total_profit", "realized_profit",
        "unrealized_profit", "open_positions", "total_fees", "saved_money",
        "liquidations", "long_trades", "short_trades",
        "total_profit_percent", "closed_trades", "wins", "losses", "win_rate",
        "maximum_drawdown", "profit_factor", "expectancy_percent", "calmar_ratio",
        "profit_per_trade", "duration_s",
    ])
    sheets["Core Metrics"] = ranked[core_columns]

    output_path = Path(output_path)
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        for sheet_name, sheet_frame in sheets.items():
            sheet_frame.to_excel(writer, sheet_name=sheet_name, index=False)

    workbook = load_workbook(output_path)
    header_fill = PatternFill("solid", fgColor="17365D")
    header_font = Font(color="FFFFFF", bold=True)
    section_fills = {
        "Winner": "FFF2CC", "Performance": "D9EAF7", "Direction": "E4DFEC",
        "Long": "E2F0D9", "Short": "FCE4D6",
    }
    thin_gray = Side(style="thin", color="D9E1F2")
    for table_index, worksheet in enumerate(workbook.worksheets, start=1):
        worksheet.freeze_panes = "A2"
        worksheet.auto_filter.ref = worksheet.dimensions
        worksheet.sheet_view.showGridLines = False
        for cell in worksheet[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")
        worksheet.row_dimensions[1].height = 24
        for column_index, cells in enumerate(worksheet.columns, start=1):
            values = [str(cell.value or "") for cell in cells[:200]]
            width = min(42, max(10, max(map(len, values), default=10) + 2))
            worksheet.column_dimensions[get_column_letter(column_index)].width = width
        if worksheet.max_row >= 2 and worksheet.max_column >= 1:
            table = Table(
                displayName=f"OptimizerTable{table_index}", ref=worksheet.dimensions
            )
            table.tableStyleInfo = TableStyleInfo(
                name="TableStyleMedium2", showRowStripes=True, showFirstColumn=False,
                showLastColumn=False, showColumnStripes=False,
            )
            worksheet.add_table(table)
        headers = {cell.value: cell.column for cell in worksheet[1]}
        for metric in (
            "score", "objective_score", "total_profit", "total_profit_percent",
            "win_rate", "long_profit", "short_profit", "long_win_rate",
            "short_win_rate", "profit_factor",
        ):
            column = headers.get(metric)
            if column and worksheet.max_row >= 3:
                letter = get_column_letter(column)
                worksheet.conditional_formatting.add(
                    f"{letter}2:{letter}{worksheet.max_row}",
                    ColorScaleRule(
                        start_type="min", start_color="F8696B",
                        mid_type="percentile", mid_value=50, mid_color="FFEB84",
                        end_type="max", end_color="63BE7B",
                    ),
                )
        for header, column in headers.items():
            header_text = str(header or "").lower()
            if header_text.endswith(("_percent", "_pct")) or any(token in header_text for token in (
                "profit_percent", "win_rate", "drawdown", "contribution_share_percent",
            )):
                for row in range(2, worksheet.max_row + 1):
                    worksheet.cell(row, column).number_format = '0.00"%";[Red]-0.00"%"'
            elif any(token in header_text for token in (
                "profit", "fees", "expectancy", "average_win", "average_loss",
                "best_trade", "worst_trade",
            )) and "factor" not in header_text:
                for row in range(2, worksheet.max_row + 1):
                    worksheet.cell(row, column).number_format = '$#,##0.00;[Red]-$#,##0.00'
        if worksheet.title == "Dashboard":
            worksheet.sheet_properties.tabColor = "4472C4"
            worksheet.column_dimensions["A"].width = 18
            worksheet.column_dimensions["B"].width = 31
            worksheet.column_dimensions["C"].width = 24
            for row in range(2, worksheet.max_row + 1):
                section = str(worksheet.cell(row, 1).value or "")
                fill = PatternFill("solid", fgColor=section_fills.get(section, "F2F2F2"))
                for column in range(1, 4):
                    worksheet.cell(row, column).fill = fill
                    worksheet.cell(row, column).border = Border(bottom=thin_gray)
                worksheet.cell(row, 1).font = Font(bold=True, color="17365D")
                metric = str(worksheet.cell(row, 2).value or "").lower()
                if "%" in metric or "drawdown" in metric:
                    worksheet.cell(row, 3).number_format = '0.00"%";[Red]-0.00"%"'
                elif "profit" in metric and "factor" not in metric:
                    worksheet.cell(row, 3).number_format = '$#,##0.00;[Red]-$#,##0.00'
        elif worksheet.title == "Rankings":
            worksheet.sheet_properties.tabColor = "70AD47"
            worksheet.freeze_panes = "D2"
            for cell in worksheet[2]:
                cell.fill = PatternFill("solid", fgColor="C6EFCE")
                cell.font = Font(bold=True, color="006100")
        elif worksheet.title == "Directional Metrics":
            worksheet.sheet_properties.tabColor = "8064A2"
            worksheet.freeze_panes = "D2"
            for header, column in headers.items():
                header_text = str(header or "")
                if header_text.startswith("long_"):
                    worksheet.cell(1, column).fill = PatternFill("solid", fgColor="548235")
                elif header_text.startswith("short_"):
                    worksheet.cell(1, column).fill = PatternFill("solid", fgColor="C0504D")

    rankings_ws = workbook["Rankings"]
    dashboard_ws = workbook["Dashboard"]

    def add_dashboard_detail_table(start_row, start_column, title, headers, rows):
        end_column = start_column + len(headers) - 1
        dashboard_ws.merge_cells(
            start_row=start_row, start_column=start_column,
            end_row=start_row, end_column=end_column,
        )
        title_cell = dashboard_ws.cell(start_row, start_column, title)
        title_cell.fill = PatternFill("solid", fgColor="17365D")
        title_cell.font = Font(color="FFFFFF", bold=True, size=11)
        title_cell.alignment = Alignment(horizontal="left", vertical="center")
        dashboard_ws.row_dimensions[start_row].height = 22
        for offset, header in enumerate(headers):
            cell = dashboard_ws.cell(start_row + 1, start_column + offset, header)
            cell.fill = PatternFill("solid", fgColor="D9EAF7")
            cell.font = Font(color="17365D", bold=True)
            cell.alignment = Alignment(horizontal="center", vertical="center")
        for row_offset, row_values in enumerate(rows, start=2):
            for column_offset, value in enumerate(row_values):
                cell = dashboard_ws.cell(
                    start_row + row_offset, start_column + column_offset, value
                )
                cell.border = Border(bottom=thin_gray)
                cell.alignment = Alignment(
                    horizontal="right" if isinstance(value, (int, float)) else "left"
                )
                header = headers[column_offset]
                if header in {"Profit %", "Drawdown %"}:
                    cell.number_format = '0.00"%";[Red]-0.00"%"'
                elif "Profit" in header or header == "Gap":
                    cell.number_format = '$#,##0.00;[Red]-$#,##0.00'
                elif header in {"Score"}:
                    cell.number_format = '0.000'
                if header == "Long Profit":
                    cell.fill = PatternFill("solid", fgColor="E2F0D9")
                elif header == "Short Profit":
                    cell.fill = PatternFill("solid", fgColor="FCE4D6")
        widths = {
            "Rank": 8, "Test": 10, "Score": 12, "Profit %": 12,
            "Net Profit": 15, "Drawdown %": 13, "Long Profit": 15,
            "Short Profit": 15, "Gap": 14, "Stronger Side": 14,
        }
        for offset, header in enumerate(headers):
            letter = get_column_letter(start_column + offset)
            dashboard_ws.column_dimensions[letter].width = widths.get(header, 13)

    top_rows = []
    for _, row in ranked.head(10).iterrows():
        top_rows.append([
            row.get("rank"), row.get("test_index"), row.get("objective_score"),
            row.get("total_profit_percent"), row.get("total_profit"),
            row.get("maximum_drawdown"), row.get("long_profit"),
            row.get("short_profit"), row.get("stronger_side"),
        ])
    add_dashboard_detail_table(
        2, 14, "Top 10 exact results - score, profit and risk",
        ["Rank", "Test", "Score", "Profit %", "Net Profit", "Drawdown %",
         "Long Profit", "Short Profit", "Stronger Side"],
        top_rows,
    )

    directional_rows = []
    for _, row in ranked.head(10).iterrows():
        directional_rows.append([
            row.get("rank"), row.get("long_profit"), row.get("short_profit"),
            row.get("directional_profit_gap"), row.get("stronger_side"),
        ])
    add_dashboard_detail_table(
        18, 14, "Top 10 directional breakdown - exact net profit",
        ["Rank", "Long Profit", "Short Profit", "Gap", "Stronger Side"],
        directional_rows,
    )

    ranking_headers = {cell.value: cell.column for cell in rankings_ws[1]}
    category_column = ranking_headers.get("rank")
    score_column = ranking_headers.get("objective_score")
    if category_column and score_column:
        last_row = min(rankings_ws.max_row, 11)
        chart = BarChart()
        chart.type = "col"
        chart.style = 10
        chart.title = "Top 10 candidates by objective score"
        chart.y_axis.title = "Objective score"
        chart.y_axis.numFmt = "0.000"
        chart.height = 7
        chart.width = 14
        chart.add_data(
            Reference(rankings_ws, min_col=score_column, min_row=1, max_row=last_row),
            titles_from_data=True,
        )
        chart.set_categories(
            Reference(rankings_ws, min_col=category_column, min_row=2, max_row=last_row)
        )
        chart.legend = None
        chart.dLbls = DataLabelList()
        chart.dLbls.showVal = True
        chart.dLbls.numFmt = "0.000"
        chart.series[0].graphicalProperties.solidFill = "8064A2"
        chart.series[0].graphicalProperties.line.solidFill = "5F497A"
        add_report_chart(workbook, chart)

    directional_ws = workbook["Directional Metrics"]
    directional_headers = {cell.value: cell.column for cell in directional_ws[1]}
    rank_column = directional_headers.get("rank")
    long_column = directional_headers.get("long_profit")
    short_column = directional_headers.get("short_profit")
    if rank_column and long_column and short_column:
        last_row = min(directional_ws.max_row, 11)
        chart = BarChart()
        chart.type = "col"
        chart.style = 11
        chart.title = "Top candidates: Long vs Short net profit"
        chart.y_axis.title = "Net profit ($)"
        chart.y_axis.numFmt = '$#,##0'
        chart.height = 7
        chart.width = 14
        for column in (long_column, short_column):
            chart.add_data(
                Reference(directional_ws, min_col=column, min_row=1, max_row=last_row),
                titles_from_data=True,
            )
        chart.set_categories(
            Reference(directional_ws, min_col=rank_column, min_row=2, max_row=last_row)
        )
        chart.dLbls = DataLabelList()
        chart.dLbls.showVal = True
        chart.dLbls.numFmt = '$#,##0'
        for series, color in zip(chart.series, ("70AD47", "C0504D")):
            series.graphicalProperties.solidFill = color
            series.graphicalProperties.line.solidFill = color
        add_report_chart(workbook, chart)
    workbook.save(output_path)
    return output_path


def _write_best_params_manifest(
    output_dir,
    *,
    status,
    selection_basis,
    metrics=None,
    candidate_id=None,
    rank=1,
):
    """Publish an unambiguous pointer without polluting strategy parameter JSON."""
    metrics = metrics or {}
    key_metrics = {
        key: metrics.get(key) for key in (
            "score", "objective_score", "total_profit_percent", "maximum_drawdown",
            "closed_trades", "win_rate", "profit_factor", "stronger_side",
            "long_profit", "long_win_rate", "long_profit_factor",
            "short_profit", "short_win_rate", "short_profit_factor",
        ) if key in metrics
    }
    _write_json(Path(output_dir) / "best_params_manifest.json", {
        "schema_version": 1,
        "recommended_file": "best_params.json",
        "instruction": "Use best_params.json for the selected winner in this directory.",
        "status": status,
        "rank": rank,
        "candidate_id": candidate_id,
        "selection_basis": selection_basis,
        "key_metrics": key_metrics,
        "file_roles": {
            "best_params.json": "canonical parameters to load",
            "best_candidate_summary.json": "human/audit details when present",
            "best_training_params.json": "training-only reference; not preferred over validated best_params.json",
            "cycles/*/best_params.json": "intermediate Auto-cycle winners; not preferred over root best_params.json",
            "cycles/*/checkpoints/*/best_params.json": "provisional checkpoints; not a final winner",
        },
        "updated_at": _timestamp_now(),
    })


def _write_best_files(output_dir, best, mode, requested_tests, completed, elapsed, seed,
                      metadata=None, final=False):
    if best is None:
        return
    _write_json(Path(output_dir) / "best_params.json", best["params"])
    summary = {
        "mode": mode,
        "requested_tests": requested_tests,
        "completed_tests": completed,
        "seed": seed,
        "elapsed_seconds": round(elapsed, 3),
        "best_test_index": best["index"],
        "best_duration_seconds": round(best["duration"], 4),
        "recommended_params_file": "best_params.json",
        "best_params_manifest": "best_params_manifest.json",
        "best_params": best["params"],
        "best_metrics": best["result"],
    }
    if metadata:
        summary.update(metadata)
    _write_json(Path(output_dir) / "optimization_summary.json", summary)
    validated = bool(metadata and metadata.get("best_robust_score") is not None)
    _write_best_params_manifest(
        output_dir,
        status="final" if final else "provisional_checkpoint",
        selection_basis=(
            "rank #1 by validation-adjusted robust score"
            if validated else "rank #1 by optimizer objective score"
        ),
        metrics={
            **best["result"],
            "objective_score": (
                metadata.get("best_robust_score")
                if validated else _score(best["result"])
            ),
        },
        candidate_id=f"test-{best['index']}",
    )


def _result_row(keys, index, params, result, duration, objective_score=None):
    row = {"test_index": index, "duration_s": round(duration, 4)}
    row.update({key: params[key] for key in keys})
    row.update({key: result.get(key) for key in RESULT_COLUMNS})
    closed_trades = int(result.get("closed_trades", 0) or 0)
    realized_profit = result.get("realized_profit")
    if realized_profit is None:
        realized_profit = result.get("total_profit", 0)
    realized_profit = float(realized_profit or 0)
    row["objective_score"] = objective_score
    row["profit_per_trade"] = (
        realized_profit / closed_trades if closed_trades else None
    )
    return row


def _result_fieldnames(keys):
    """Put outcome and trade metrics before the usually much wider parameter set."""
    metrics = [
        column for column in RESULT_COLUMNS
        if column != "score" and column not in IMPORTANT_RESULT_COLUMNS
    ]
    return [
        "test_index", "objective_score", "score",
        *IMPORTANT_RESULT_COLUMNS, *metrics,
        "profit_per_trade", "duration_s", *keys,
    ]


def _parse_grid_csv_value(raw, values):
    for value in values:
        if str(value) == raw:
            return value
    raise ValueError(f"saved value {raw!r} is not present in the current grid")


def _parse_metric(raw):
    if raw in (None, ""):
        return None
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return raw
    return int(number) if number.is_integer() else number


def _read_resume_records(path, grid, base_tune):
    records = []
    with Path(path).open(newline="", encoding="utf-8") as csv_file:
        reader = csv.DictReader(csv_file)
        # New metric columns are optional so older compatible checkpoints can
        # still resume after the reporting schema grows.
        required = {"test_index", "duration_s", "score", *grid}
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(
                "cannot resume because CSV columns do not match this profile: "
                + ", ".join(sorted(missing))
            )
        for row in reader:
            selected = {
                key: _parse_grid_csv_value(row[key], grid[key]) for key in grid
            }
            result = {key: _parse_metric(row.get(key)) for key in RESULT_COLUMNS}
            records.append({
                "index": int(row["test_index"]),
                "params": {**base_tune, **selected},
                "result": result,
                "duration": float(row["duration_s"]),
            })
    return records


def _load_base_tune(args):
    if getattr(args, '_resume_base_tune', None) is not None:
        return dict(args._resume_base_tune), 'frozen campaign baseline'
    source = getattr(args, "base_source", "config")
    if source == "config":
        adapter = _adapter_from_args(args)
        return {}, f"defaults exposed by {adapter.module_name}"
    path = Path(getattr(args, "base_params", "outputs/optimize/best_params.json"))
    if not path.is_file():
        raise FileNotFoundError(
            f"base parameter file not found: {path}. Run an optimization first or "
            "use --base-source=config."
        )
    return _adapter_from_args(args).load_tune(path), str(path)


def _validate_top_candidates(records, args, workers, chunksize):
    validation_start = getattr(args, "validation_start", None)
    validation_end = getattr(args, "validation_end", None)
    if not validation_start and not validation_end:
        return None, []
    if not validation_start or not validation_end:
        raise ValueError("--validation-start and --validation-end must be used together")

    top_count = min(getattr(args, "validation_top", 20), len(records))
    candidates = records[:top_count]
    start = _parse_bound(validation_start)
    end = _parse_bound(validation_end)
    train_candles = _range_candle_count(args.start, args.end)
    validation_candles = _range_candle_count(validation_start, validation_end)
    tasks = [(record["index"], record["params"]) for record in candidates]
    strategy_spec = _adapter_from_args(args).identifier
    fixed_warmup = _maximum_candidate_warmup(strategy_spec, {}, tasks)
    if workers > 1:
        pool = multiprocessing.Pool(
            workers,
            initializer=_init_worker,
            initargs=(
                start, end, {}, True, True, strategy_spec, True, fixed_warmup,
            ),
        )
        try:
            evaluated = list(pool.imap_unordered(_evaluate_task, tasks, chunksize=chunksize))
        except KeyboardInterrupt:
            pool.terminate()
            pool.join()
            raise
        else:
            pool.close()
            pool.join()
    else:
        evaluated = [
            _evaluate_candidate(
                index, params, {}, start, end,
                strategy_spec=strategy_spec, research=True,
                indicator_warmup_candles=fixed_warmup,
            )
            for index, params in tasks
        ]

    train_by_index = {record["index"]: record for record in candidates}
    validated = []
    for index, params, result, duration, error in evaluated:
        train = train_by_index[index]
        validation_qualified = (
            not error and math.isfinite(_objective_score(
                result,
                min_trades=getattr(args, "min_trades", 0),
                max_drawdown=getattr(args, "max_drawdown", None),
            ))
        )
        robust_score = (
            -math.inf if not validation_qualified else _robust_validation_score(
                train["result"], result, getattr(args, "overfit_penalty", 0.25),
                train_candles=train_candles,
                validation_candles=validation_candles,
            )
        )
        validated.append({
            "index": index,
            "params": params,
            "result": result,
            "duration": duration,
            "error": error,
            "training_result": train["result"],
            "training_time_normalized_score": _time_normalized_score(
                _score(train["result"]), train_candles
            ),
            "validation_time_normalized_score": _time_normalized_score(
                _score(result), validation_candles
            ),
            "robust_score": robust_score,
        })
    validated.sort(key=lambda item: item["robust_score"], reverse=True)
    return (validated[0] if validated else None), validated


def _auto_ranges(args, resolved_end):
    stress_end = getattr(args, "auto_stress_end", None) or args.auto_validation_start
    return {
        "discovery": [args.auto_discovery_start, resolved_end],
        "validation": [args.auto_validation_start, args.auto_discovery_start],
        "stress": [args.auto_stress_start, stress_end],
        "final": [args.auto_validation_start, resolved_end],
    }


def _bound_index(value):
    parsed = _parse_bound(value)
    if isinstance(parsed, int):
        return parsed
    if _ACTIVE_MARKET_DATA_SOURCE is not None:
        return _ACTIVE_MARKET_DATA_SOURCE.resolve_index(parsed)
    from get_candle_index import get_candle_index

    return int(get_candle_index(parsed))


def _format_range_bound(value):
    """Return a human-readable market timestamp for a date or candle bound."""
    parsed = _parse_bound(value)
    if not isinstance(parsed, int):
        try:
            return parsed.strftime("%Y-%m-%d %H:%M")
        except (AttributeError, ValueError):
            return str(value)

    try:
        if _ACTIVE_MARKET_DATA_SOURCE is not None:
            return _ACTIVE_MARKET_DATA_SOURCE.format_bound(parsed)
        from get_candle_index import _open_times

        open_times = _open_times().dropna()
        if open_times.empty:
            return str(value)
        if 0 <= parsed < len(open_times):
            timestamp = open_times.iloc[parsed]
        elif parsed == len(open_times):
            recent = open_times.tail(100).sort_values()
            deltas = recent.diff().dropna()
            candle_delta = deltas.median() if not deltas.empty else timedelta(minutes=15)
            if candle_delta <= timedelta(0):
                candle_delta = timedelta(minutes=15)
            timestamp = open_times.iloc[-1] + candle_delta
        else:
            return str(value)
        return timestamp.strftime("%Y-%m-%d %H:%M")
    except (IndexError, OSError, TypeError, ValueError):
        return str(value)


def _validate_auto_ranges(ranges):
    discovery_start = _bound_index(ranges["discovery"][0])
    validation_start = _bound_index(ranges["validation"][0])
    stress_start = _bound_index(ranges["stress"][0])
    stress_end = _bound_index(ranges["stress"][1])
    final_start = _bound_index(ranges["final"][0])
    final_end = _bound_index(ranges["final"][1])
    if not (
        stress_start < stress_end <= validation_start
        and final_start == validation_start < discovery_start < final_end
    ):
        raise ValueError(
            "auto ranges must satisfy: stress-start < stress-end <= recent-start "
            "< discovery-start < auto-end"
        )


def _auto_configuration(args, profile, grid, base_tune, base_description, resolved_end):
    ranges = _auto_ranges(args, resolved_end)
    _validate_auto_ranges(ranges)
    adapter = _adapter_from_args(args)
    data_file = _strategy_data_file(args, adapter)
    return {
        "strategy": adapter.identifier,
        "data_file": str(data_file.resolve()) if data_file is not None else None,
        "timeframe": getattr(args, "_detected_timeframe", adapter.timeframe),
        "parameter_grid_source": getattr(args, "param_grid", None),
        "profile": profile,
        "parameter_grid": {key: list(values) for key, values in grid.items()},
        "base_source": base_description,
        "base_tune": base_tune,
        "tests_per_cycle": args.auto_tests,
        "validation_top": args.auto_validation_top,
        "stress_top": args.auto_stress_top,
        "final_top": args.auto_final_top,
        "hall_size": args.auto_hall_size,
        "snapshot_cycles": int(getattr(args, "snapshot_cycles", 50)),
        "snapshot_top": int(getattr(args, "snapshot_top", 100)),
        "importance_target": args.auto_importance_target,
        "seed": args.seed,
        "minimum_trades": args.min_trades,
        "maximum_allowed_drawdown": args.max_drawdown,
        "indicator_warmup": bool(
            getattr(args, "_indicator_warmup_enabled", True)
        ),
        "ranges": ranges,
    }


def _auto_feature_configuration(args):
    """Settings for the version-2 search engine, stored outside legacy config."""
    return {
        "learning_target": getattr(args, 'auto_learning_target', 'rank'),
        "minimum_trades_policy": getattr(args, 'auto_trade_count_policy', 'fixed'),
        "advanced_min_candidates": int(args.auto_advanced_min_candidates),
        "halving_rungs": int(args.auto_halving_rungs),
        "halving_keep": float(args.auto_halving_keep),
        "surrogate_min_samples": int(args.auto_surrogate_min_samples),
        "surrogate_pool_multiplier": int(args.auto_surrogate_pool),
        "surrogate_trees": int(args.auto_surrogate_trees),
        "surrogate_max_samples": int(
            getattr(args, "auto_surrogate_max_samples", SURROGATE_MAX_TRAINING_SAMPLES)
        ),
        "walk_forward_folds": int(args.auto_walk_forward_folds),
        "walk_forward_top": int(args.auto_walk_forward_top),
        "walk_forward_stability_penalty": float(
            args.auto_walk_forward_stability_penalty
        ),
    }


def _restore_auto_resume_args(args, state):
    """Restore persisted search settings before validating a legacy checkpoint."""
    config = state.get("config") or {}
    saved_strategy = config.get("strategy", "ma_strategy:ma_strategy")
    requested_strategy = _adapter_from_args(args).identifier
    if saved_strategy != requested_strategy:
        raise ValueError(
            f"checkpoint strategy is {saved_strategy!r}, not {requested_strategy!r}"
        )
    args.strategy = saved_strategy
    args._strategy_adapter = _adapter_from_spec(saved_strategy)
    if "parameter_grid_source" in config:
        args.param_grid = config.get("parameter_grid_source")
    if "indicator_warmup" not in config:
        # Checkpoints created before historical warm-up existed must retain their
        # original strategy semantics; otherwise old and new scores get mixed.
        config["indicator_warmup"] = False
        state["strategy_semantics_migration"] = {
            "indicator_warmup": False,
            "reason": "legacy checkpoint created before indicator warm-up",
        }
    args._indicator_warmup_enabled = bool(config["indicator_warmup"])
    profile = config.get("profile")
    requested_profile = getattr(args, "profile", None) or profile
    if profile and requested_profile != profile:
        raise ValueError(
            f"checkpoint profile is {profile!r}, not {requested_profile!r}"
        )
    if profile:
        args.profile = profile
    config_mapping = {
        "tests_per_cycle": "auto_tests",
        "validation_top": "auto_validation_top",
        "stress_top": "auto_stress_top",
        "final_top": "auto_final_top",
        "hall_size": "auto_hall_size",
        "snapshot_cycles": "snapshot_cycles",
        "snapshot_top": "snapshot_top",
        "importance_target": "auto_importance_target",
        "seed": "seed",
        "minimum_trades": "min_trades",
        "maximum_allowed_drawdown": "max_drawdown",
    }
    for saved_key, argument_name in config_mapping.items():
        if saved_key in config:
            setattr(args, argument_name, config[saved_key])
    ranges = config.get("ranges") or {}
    if ranges:
        args.auto_discovery_start = ranges["discovery"][0]
        args.auto_validation_start = ranges["validation"][0]
        args.auto_stress_start = ranges["stress"][0]
        args.auto_stress_end = ranges["stress"][1]
        args.auto_end = ranges["final"][1]

    features = state.get("optimizer_features") or {}
    feature_mapping = {
        "learning_target": "auto_learning_target",
        "advanced_min_candidates": "auto_advanced_min_candidates",
        "halving_rungs": "auto_halving_rungs",
        "halving_keep": "auto_halving_keep",
        "surrogate_min_samples": "auto_surrogate_min_samples",
        "surrogate_pool_multiplier": "auto_surrogate_pool",
        "surrogate_trees": "auto_surrogate_trees",
        "walk_forward_folds": "auto_walk_forward_folds",
        "walk_forward_top": "auto_walk_forward_top",
        "walk_forward_stability_penalty": "auto_walk_forward_stability_penalty",
    }
    for saved_key, argument_name in feature_mapping.items():
        if saved_key in features:
            setattr(args, argument_name, features[saved_key])
    args.auto_learning_target = features.get('learning_target', 'rank')
    args.auto_trade_count_policy = features.get('minimum_trades_policy', 'fixed')
    # Version-2 checkpoints predate the configurable history cap. Preserve the
    # exact 1,024-sample behavior they were trained with instead of silently
    # changing their tree model during resume.
    args.auto_surrogate_max_samples = int(
        features.get("surrogate_max_samples", 1024)
    )
    return config


def _auto_bootstrap(args, grid):
    """Use an existing standard-optimizer winner to warm-start a new campaign."""
    if getattr(args, "base_source", "config") != "config":
        return None
    path = Path(getattr(args, "base_params", "outputs/optimize/best_params.json"))
    if not path.is_file():
        return None
    saved = _adapter_from_args(args).load_tune(path)
    compatible = {key: saved[key] for key in grid if key in saved}
    if not compatible:
        return None
    return {
        "source": str(path),
        "compatible_parameter_count": len(compatible),
        "params": compatible,
    }


def _load_json(path, default=None):
    path = Path(path)
    if not path.is_file():
        return default
    with path.open(encoding="utf-8") as file:
        return json.load(file)


def _candidate_signature(params, keys):
    return tuple(params[key] for key in keys)


def _load_auto_seen(output_dir, keys):
    seen = set()
    paths = sorted((Path(output_dir) / "cycles").glob("cycle_*/*_candidates.json"))
    for index, path in enumerate(paths, start=1):
        for candidate in _read_candidate_plan(path):
            params = candidate.get("params", {})
            if all(key in params for key in keys):
                seen.add(_candidate_signature(params, keys))
        _show_loading_progress("Loading previous candidates", index, len(paths))
    return seen


def _load_discovery_history(output_dir, keys):
    """Load a compact persistent surrogate history and increment it as needed."""
    output_dir = Path(output_dir)
    cache_path = output_dir / "surrogate_history_cache.json.gz"
    cycles_dir = Path(output_dir) / "cycles"
    saved_state = _load_json(output_dir / 'auto_state.json', {})
    evidence_enabled = (saved_state.get('optimizer_features') or {}).get('learning_target') == 'profit-evidence'
    plan_paths = sorted(cycles_dir.glob("cycle_*/discovery_candidates.json"))
    cached_history, cached_cycle = _read_surrogate_history_cache(cache_path, keys)
    if cached_history is None:
        history = []
        cached_cycle = 0
        selected_paths = _surrogate_bootstrap_paths(
            plan_paths, SURROGATE_CACHE_BOOTSTRAP_CYCLES
        )
        if len(selected_paths) < len(plan_paths):
            print(
                f"Building compact surrogate cache from {len(selected_paths):,}/"
                f"{len(plan_paths):,} time-balanced/recent cycles."
            )
    else:
        history = cached_history
        selected_paths = [
            path for path in plan_paths
            if _cycle_number_from_path(path) > cached_cycle
        ]
        print(
            f"Loaded compact surrogate cache: {len(history):,} samples through "
            f"cycle {cached_cycle:,}."
        )

    for index, plan_path in enumerate(selected_paths, start=1):
        results_path = _resolve_csv_path(plan_path.with_name("discovery_results.csv"))
        candidates = _read_candidate_plan(plan_path)
        cycle_records = _read_auto_stage_records(results_path, candidates)
        _annotate_discovery_learning_scores(cycle_records)
        if evidence_enabled:
            stages = {'discovery': cycle_records}
            for stage in AUTO_STAGE_ORDER:
                if stage in ('discovery', 'walk_forward'):
                    continue
                stage_plan = plan_path.with_name(f'{stage}_candidates.json')
                if stage_plan.is_file():
                    stages[stage] = _read_auto_stage_records(
                        _resolve_csv_path(plan_path.with_name(f'{stage}_results.csv')),
                        _read_candidate_plan(stage_plan))
            walk = _load_json(plan_path.with_name('walk_forward_summary.json'), {})
            if walk.get('records'):
                stages['walk_forward'] = walk['records']
            _apply_funnel_learning_scores(cycle_records, stages)
            apply_profit_learning(cycle_records, stages, AUTO_STAGE_WEIGHTS)
        for record in cycle_records:
            score = _surrogate_target(record)
            if score is not None and all(key in record["params"] for key in keys):
                history.append(record)
        _show_loading_progress("Loading surrogate history", index, len(selected_paths))

    latest_cycle = max(
        [cached_cycle] + [_cycle_number_from_path(path) for path in selected_paths]
    )
    if selected_paths or cached_history is None:
        history = _write_surrogate_history_cache(
            cache_path, history, keys, latest_cycle
        )
    return history


def _cycle_number_from_path(path):
    try:
        return int(Path(path).parent.name.removeprefix("cycle_"))
    except ValueError:
        return 0


def _evenly_spaced_items(items, limit):
    items = list(items)
    limit = max(1, int(limit))
    if len(items) <= limit:
        return items
    if limit == 1:
        return [items[-1]]
    indices = {
        round(position * (len(items) - 1) / (limit - 1))
        for position in range(limit)
    }
    return [items[index] for index in sorted(indices)]


def _surrogate_bootstrap_paths(paths, limit):
    """Blend whole-history coverage with extra weight on recent refinements."""
    paths = list(paths)
    limit = max(1, int(limit))
    if len(paths) <= limit:
        return paths
    broad_count = max(1, limit // 2)
    broad = _evenly_spaced_items(paths, broad_count)
    selected = {path: None for path in broad}
    for path in reversed(paths):
        selected[path] = None
        if len(selected) >= limit:
            break
    return sorted(selected)


def _read_surrogate_history_cache(path, keys):
    path = Path(path)
    if not path.is_file():
        return None, 0
    try:
        with gzip.open(path, "rt", encoding="utf-8") as cache_file:
            payload = json.load(cache_file)
        version = int(payload.get("version", 1) or 1)
        if version not in (1, SURROGATE_CACHE_VERSION) or tuple(
            payload.get("parameter_keys", ())
        ) != tuple(keys):
            return None, 0
        rows = payload.get("rows", [])
        if version == 1:
            history = [
                {
                    "candidate_id": row[0],
                    "objective_score": row[1],
                    "params": dict(zip(keys, row[2:])),
                }
                for row in rows
            ]
            # Legacy caches stored incomparable raw scores. Preserve every sample
            # but migrate its target to a bounded rank without reopening cycles.
            _annotate_discovery_learning_scores(history)
        else:
            history = [
                {
                    "candidate_id": row[0],
                    "learning_score": row[1],
                    "learning_source": row[2],
                    "params": dict(zip(keys, row[3:])),
                }
                for row in rows
            ]
        return history, int(payload.get("latest_cycle", 0) or 0)
    except (
        OSError, EOFError, UnicodeError, ValueError, TypeError, IndexError,
        json.JSONDecodeError,
    ):
        return None, 0


def _write_surrogate_history_cache(path, history, keys, latest_cycle):
    usable = [
        record for record in history
        if _surrogate_target(record) is not None
        and all(key in record.get("params", {}) for key in keys)
    ]
    selected = [
        {
            "candidate_id": record.get("candidate_id"),
            "learning_score": _surrogate_target(record),
            "learning_source": record.get("learning_source", "legacy_fallback"),
            "params": {key: record["params"][key] for key in keys},
        }
        for record in _representative_surrogate_history(
            usable, SURROGATE_MAX_TRAINING_SAMPLES
        )
    ]
    payload = {
        "version": SURROGATE_CACHE_VERSION,
        "latest_cycle": int(latest_cycle),
        "parameter_keys": list(keys),
        "sample_count": len(selected),
        "sample_method": "deterministic_learning_quantiles",
        "rows": [
            [
                record.get("candidate_id"),
                record["learning_score"],
                record["learning_source"],
                *(record["params"][key] for key in keys),
            ]
            for record in selected
        ],
    }
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with gzip.open(
            temporary, "wt", encoding="utf-8", compresslevel=6
        ) as cache_file:
            json.dump(_json_safe(payload), cache_file, separators=(",", ":"))
        os.replace(temporary, path)
    except BaseException:
        if temporary.is_file():
            temporary.unlink()
        raise
    return selected


def _merge_surrogate_history(history, new_records):
    """Merge resumable history by candidate id; newer robust labels win."""
    merged = {
        record.get("candidate_id"): record
        for record in history
        if record.get("candidate_id") is not None
    }
    for record in new_records:
        candidate_id = record.get("candidate_id")
        if candidate_id is not None:
            merged[candidate_id] = record
    return list(merged.values())


def _surrogate_feature_row(params, keys, grid):
    row = []
    for key in keys:
        value = params[key]
        if isinstance(value, bool):
            row.append(float(value))
        elif isinstance(value, (int, float)):
            row.append(float(value))
        else:
            values = list(grid[key])
            row.append(float(values.index(value)) if value in values else -1.0)
    return row


def _representative_surrogate_history(history, limit):
    """Select deterministic score quantiles while retaining the complete history on disk."""
    history = list(history)
    limit = max(2, int(limit))
    if len(history) <= limit:
        return history
    ranked = sorted(history, key=lambda record: float(_surrogate_target(record)))
    indices = {
        round(position * (len(ranked) - 1) / (limit - 1))
        for position in range(limit)
    }
    return [ranked[index] for index in sorted(indices)]


def _diversity_fingerprint(params, keys, grid, bins=8):
    fingerprint = []
    for key in keys:
        value = params[key]
        values = list(grid[key])
        if isinstance(value, bool):
            bucket = int(value)
        elif isinstance(value, (int, float)) and values:
            lower, upper = min(values), max(values)
            ratio = 0.0 if upper == lower else (value - lower) / (upper - lower)
            bucket = min(bins - 1, max(0, int(ratio * bins)))
        else:
            index = values.index(value) if value in values else 0
            bucket = round(index * (bins - 1) / max(1, len(values) - 1))
        fingerprint.append(bucket)
    return tuple(fingerprint)


def _rank_fractions(values, reverse=False):
    order = sorted(range(len(values)), key=values.__getitem__, reverse=reverse)
    denominator = max(1, len(values) - 1)
    output = [0.0] * len(values)
    rank = 0
    while rank < len(order):
        end = rank + 1
        while end < len(order) and values[order[end]] == values[order[rank]]:
            end += 1
        fraction = 1.0 - ((rank + end - 1) / 2) / denominator
        for index in order[rank:end]:
            output[index] = fraction
        rank = end
    return output


def _select_surrogate_candidates(
    candidates, history, count, keys, grid, features, seed, parameter_importance=None,
):
    """Use quality, uncertainty, novelty and bounded diversity to select a pool."""
    candidates = list(candidates)
    usable_history = [
        record for record in history
        if _surrogate_target(record) is not None
    ]
    minimum = features["surrogate_min_samples"]
    if len(usable_history) < minimum or len(candidates) <= count:
        return candidates[:count], {
            "enabled": False,
            "reason": "insufficient_history" if len(usable_history) < minimum else "small_pool",
            "history_samples": len(usable_history),
            "minimum_samples": minimum,
            "candidate_pool": len(candidates),
        }

    training_history = _representative_surrogate_history(
        usable_history, features.get(
            "surrogate_max_samples", SURROGATE_MAX_TRAINING_SAMPLES
        )
    )
    if len(training_history) < len(usable_history):
        print(
            f"Surrogate training set: {len(training_history):,} representative "
            f"samples from {len(usable_history):,} stored results."
        )
    training_features = [
        _surrogate_feature_row(record["params"], keys, grid)
        for record in training_history
    ]
    targets = [float(_surrogate_target(record)) for record in training_history]
    # A deterministic held-back part of search history tests whether the model
    # can rank unseen configurations. This never consumes reporting OOS data.
    probe_indices = list(range(len(training_history)))
    random.Random(seed + 104729).shuffle(probe_indices)
    validation_count = max(1, min(len(probe_indices)-3, max(8, len(probe_indices) // 5)))
    validation_indices, fit_indices = probe_indices[:validation_count], probe_indices[validation_count:]
    probe_scope = 'held-back search configurations, never reporting OOS'
    if features.get('learning_target') == 'profit-evidence':
        chronological = chronological_probe_indices(training_history)
        if chronological:
            validation_indices, fit_indices = chronological
            validation_count = len(validation_indices)
            probe_scope = 'latest complete search cycles, never market OOS or holdout'
    probe = ExtraTreesSurrogate(n_trees=min(16, features['surrogate_trees']),
                                max_depth=8, min_leaf=3, seed=seed + 17).fit(
        [training_features[i] for i in fit_indices], [targets[i] for i in fit_indices])
    probe_predictions = [v[0] for v in probe.predict_mean_std([training_features[i] for i in validation_indices])]
    predicted_ranks = _rank_fractions(probe_predictions)
    actual_ranks = _rank_fractions([targets[i] for i in validation_indices])
    pred_mean, actual_mean = statistics.fmean(predicted_ranks), statistics.fmean(actual_ranks)
    covariance = sum((p-pred_mean)*(a-actual_mean) for p,a in zip(predicted_ranks, actual_ranks))
    variance = math.sqrt(sum((p-pred_mean)**2 for p in predicted_ranks) * sum((a-actual_mean)**2 for a in actual_ranks))
    rank_correlation = covariance / variance if variance else 0.0
    model_trust = min(1.0, max(0.0, rank_correlation))
    quality_fraction = 0.25 + 0.25 * model_trust
    model = ExtraTreesSurrogate(
        n_trees=features["surrogate_trees"],
        max_depth=max(6, min(12, round(math.log2(len(training_history) + 1)) + 1)),
        min_leaf=max(3, min(12, len(training_history) // 40)),
        seed=seed,
    ).fit(training_features, targets, progress_label="Training surrogate model")
    predictions = model.predict_mean_std(
        [_surrogate_feature_row(candidate, keys, grid) for candidate in candidates],
        progress_label="Scoring candidate pool",
    )
    mean_ranks = _rank_fractions([item[0] for item in predictions], reverse=True)
    uncertainty_ranks = _rank_fractions(
        [item[1] for item in predictions], reverse=True
    )
    acquisition = [
        0.82 * mean_ranks[index] + 0.18 * uncertainty_ranks[index]
        for index in range(len(candidates))
    ]
    ranked_quality = sorted(
        range(len(candidates)), key=acquisition.__getitem__, reverse=True
    )
    ranked_uncertainty = sorted(
        range(len(candidates)), key=lambda index: predictions[index][1], reverse=True
    )
    supplied_importance = parameter_importance or {}
    diversity_keys = sorted(
        keys,
        key=lambda key: float(supplied_importance.get(key, {}).get("weight", 0.0)),
        reverse=True,
    )[:min(8, len(keys))]
    history_buckets = {}
    for record in training_history:
        fingerprint = _diversity_fingerprint(
            record["params"], diversity_keys, grid
        )
        history_buckets[fingerprint] = history_buckets.get(fingerprint, 0) + 1
    fingerprints = [
        _diversity_fingerprint(candidate, diversity_keys, grid)
        for candidate in candidates
    ]
    novelty = [
        1.0 / math.sqrt(1.0 + history_buckets.get(fingerprint, 0))
        for fingerprint in fingerprints
    ]
    diversity_value = [
        0.65 * acquisition[index] + 0.35 * novelty[index]
        for index in range(len(candidates))
    ]
    ranked_diverse = sorted(
        range(len(candidates)), key=diversity_value.__getitem__, reverse=True
    )
    selected_indices = []
    selected_set = set()
    selected_buckets = {}
    bucket_limit = max(2, math.ceil(count * 0.01))

    def add(indices, limit, enforce_bucket_limit=True):
        for index in indices:
            if len(selected_indices) >= limit:
                break
            if index not in selected_set:
                fingerprint = fingerprints[index]
                if (
                    enforce_bucket_limit
                    and selected_buckets.get(fingerprint, 0) >= bucket_limit
                ):
                    continue
                selected_set.add(index)
                selected_indices.append(index)
                selected_buckets[fingerprint] = selected_buckets.get(fingerprint, 0) + 1

    quality_target = max(1, round(count * quality_fraction))
    uncertainty_target = min(count, quality_target + round(count * 0.20))
    diversity_target = min(count, uncertainty_target + round(count * 0.20))
    add(ranked_quality, quality_target)
    add(ranked_uncertainty, uncertainty_target)
    add(ranked_diverse, diversity_target)
    remaining = [index for index in range(len(candidates)) if index not in selected_set]
    random.Random(seed + 7919).shuffle(remaining)
    add(remaining, count)
    if len(selected_indices) < count:
        add(ranked_quality, count, enforce_bucket_limit=False)
    selected = [candidates[index] for index in selected_indices[:count]]
    selected_predictions = [predictions[index] for index in selected_indices[:count]]
    return selected, {
        "enabled": True,
        "model": "dependency_free_extra_trees",
        "validation_rank_correlation": rank_correlation,
        "validation_samples": validation_count,
        "validation_scope": probe_scope,
        "history_samples": len(usable_history),
        "training_samples": len(training_history),
        "training_target": ("profit_evidence_v1" if features.get('learning_target') == 'profit-evidence'
                            else "normalized_and_robust_funnel_rank"),
        "training_sample_method": "deterministic_learning_quantiles",
        "candidate_pool": len(candidates),
        "selected_candidates": len(selected),
        "trees": features["surrogate_trees"],
        "predicted_score_min": min(item[0] for item in selected_predictions),
        "predicted_score_max": max(item[0] for item in selected_predictions),
        "mean_uncertainty": statistics.fmean(item[1] for item in selected_predictions),
        "diversity_keys": diversity_keys,
        "diversity_buckets": len(set(fingerprints[index] for index in selected_indices)),
        "selection_mix": {
            "quality_acquisition": quality_fraction, "uncertainty": 0.20,
            "diversity_novelty": 0.20, "random": 0.60-quality_fraction,
        },
    }


def _promote_candidates(records, keep_count):
    ranked = sorted(
        records,
        key=lambda record: (
            _finite_number(record.get("objective_score"))
            if _finite_number(record.get("objective_score")) is not None
            else -math.inf
        ),
        reverse=True,
    )
    return [
        {"candidate_id": record["candidate_id"], "params": record["params"]}
        for record in ranked[:keep_count]
        if _finite_number(record.get("objective_score")) is not None
    ]


def _expanding_discovery_ranges(range_start, range_end, rung_count):
    start_index = _bound_index(range_start)
    end_index = _bound_index(range_end)
    width = end_index - start_index
    if width <= rung_count + 1:
        return []
    ranges = []
    for rung in range(1, rung_count + 1):
        fraction = rung / (rung_count + 1)
        rung_start = end_index - max(1, round(width * fraction))
        ranges.append((rung_start, end_index))
    return ranges


def _walk_forward_ranges(ranges, fold_count):
    # The historical slice is only a Stress sanity check; walk-forward belongs
    # to the main recent optimization window.
    start_index = _bound_index(ranges["final"][0])
    end_index = _bound_index(ranges["discovery"][0])
    width = end_index - start_index
    fold_count = min(max(0, fold_count), max(0, width))
    if fold_count < 2:
        return []
    boundaries = [
        start_index + round(width * index / fold_count)
        for index in range(fold_count + 1)
    ]
    return [
        (boundaries[index], boundaries[index + 1])
        for index in range(fold_count)
        if boundaries[index] < boundaries[index + 1]
    ]


def _count_auto_evaluations(output_dir):
    completed = 0
    cycles_dir = Path(output_dir) / "cycles"
    paths = list(cycles_dir.glob("cycle_*/*_results.csv"))
    paths.extend(
        path for path in cycles_dir.glob("cycle_*/*_results.csv.gz")
        if not path.with_suffix("").is_file()
    )
    for index, path in enumerate(paths, start=1):
        with _open_csv_text(path) as csv_file:
            reader = csv.DictReader(csv_file)
            completed += sum(1 for _ in reader)
        _show_loading_progress("Counting legacy results", index, len(paths))
    return completed


def _resolve_csv_path(path):
    """Prefer a writable/plain CSV and otherwise use its completed gzip archive."""
    path = Path(path)
    if path.is_file():
        return path
    compressed = path.with_suffix(path.suffix + ".gz")
    return compressed if compressed.is_file() else path


def _open_csv_text(path):
    path = Path(path)
    if path.suffix == ".gz":
        return gzip.open(path, "rt", newline="", encoding="utf-8")
    return path.open(newline="", encoding="utf-8")


def _count_csv_rows(path):
    path = _resolve_csv_path(path)
    if not path.is_file():
        return 0
    with _open_csv_text(path) as csv_file:
        return sum(1 for _ in csv.DictReader(csv_file))


def _reconcile_auto_evaluations(output_dir, state):
    """Trust persisted totals and only reconcile the active stage after a crash."""
    saved_total = state.get("total_evaluations")
    if saved_total is None:
        return _count_auto_evaluations(output_dir)
    cycle_dir = (
        Path(output_dir) / "cycles" / f"cycle_{int(state.get('cycle', 1)):06d}"
    )
    stage = state.get("stage")
    if not stage:
        return int(saved_total)
    actual_stage_rows = _count_csv_rows(cycle_dir / f"{stage}_results.csv")
    saved_stage_rows = int(state.get("stage_completed", 0) or 0)
    recovered_rows = max(0, actual_stage_rows - saved_stage_rows)
    if recovered_rows:
        print(
            f"Resume reconciliation: recovered {recovered_rows:,} completed "
            f"{stage} result(s) written after the last state flush."
        )
    return int(saved_total) + recovered_rows


def _write_candidate_plan(path, cycle, stage, candidates, source=None):
    """Write a compact, backwards-compatible candidate plan.

    Parameter names are stored once instead of once per candidate.  A stage that
    reuses an unchanged plan can point at its source rather than duplicating it.
    """
    path = Path(path)
    if source is not None:
        payload = {
            "version": 2,
            "cycle": cycle,
            "stage": stage,
            "candidate_count": len(candidates),
            "source": os.path.relpath(Path(source), path.parent),
        }
    else:
        parameter_keys = list(candidates[0].get("params", {})) if candidates else []
        payload = {
            "version": 2,
            "cycle": cycle,
            "stage": stage,
            "candidate_count": len(candidates),
            "parameter_keys": parameter_keys,
            "candidate_rows": [
                [candidate["candidate_id"], *(
                    candidate.get("params", {}).get(key) for key in parameter_keys
                )]
                for candidate in candidates
            ],
        }
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(_json_safe(payload), file, separators=(",", ":"), ensure_ascii=False)
        file.write("\n")
    os.replace(temporary, path)


def _read_candidate_plan(path):
    payload = _load_json(path, {}) or {}
    if payload.get("source"):
        source_path = Path(path).parent / payload["source"]
        return _read_candidate_plan(source_path.resolve())
    if "candidate_rows" in payload:
        keys = payload.get("parameter_keys", [])
        return [
            {
                "candidate_id": row[0],
                "params": dict(zip(keys, row[1:])),
            }
            for row in payload["candidate_rows"]
        ]
    return payload.get("candidates", [])


def _compact_auto_candidate_plans(output_dir, show_progress=False):
    """Upgrade verbose legacy plans and replace exact rung-1 copies in place."""
    rewritten = 0
    bytes_before = 0
    bytes_after = 0
    cycles_dir = Path(output_dir) / "cycles"
    paths = sorted(cycles_dir.glob("cycle_*/*_candidates.json"))
    for index, path in enumerate(paths, start=1):
        payload = _load_json(path, {}) or {}
        needs_upgrade = payload.get("version") != 2
        source_path = path.parent / "discovery_pool_candidates.json"
        use_source = path.name == "discovery_rung_01_candidates.json" and source_path.is_file()
        if not needs_upgrade and (not use_source or payload.get("source")):
            if show_progress:
                _show_loading_progress("Checking candidate plans", index, len(paths))
            continue
        candidates = _read_candidate_plan(path)
        if use_source and candidates != _read_candidate_plan(source_path):
            use_source = False
        old_size = path.stat().st_size
        _write_candidate_plan(
            path,
            payload.get("cycle"),
            payload.get("stage", path.stem.removesuffix("_candidates")),
            candidates,
            source=source_path if use_source else None,
        )
        rewritten += 1
        bytes_before += old_size
        bytes_after += path.stat().st_size
        if show_progress:
            _show_loading_progress("Checking candidate plans", index, len(paths))
    return rewritten, max(0, bytes_before - bytes_after)


def _compress_completed_cycle_results(cycle_dir):
    """Losslessly archive completed CSVs while keeping their columns easy to scan."""
    compressed_count = 0
    reclaimed = 0
    known_fields = _auto_stage_fieldnames(())
    known_set = set(known_fields)
    for path in sorted(Path(cycle_dir).glob("*_results.csv")):
        destination = path.with_suffix(path.suffix + ".gz")
        original_size = path.stat().st_size
        if destination.is_file():
            path.unlink()
            compressed_count += 1
            reclaimed += original_size
            continue
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        try:
            with path.open(newline="", encoding="utf-8") as source:
                reader = csv.DictReader(source)
                existing_fields = reader.fieldnames or []
                parameter_fields = [
                    field for field in existing_fields if field not in known_set
                ]
                fieldnames = [
                    field for field in known_fields if field != "error" and field in existing_fields
                ]
                fieldnames.extend(parameter_fields)
                if "error" in existing_fields:
                    fieldnames.append("error")
                with gzip.open(
                    temporary, "wt", newline="", encoding="utf-8", compresslevel=6
                ) as target:
                    writer = csv.DictWriter(target, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(reader)
            os.replace(temporary, destination)
            compressed_size = destination.stat().st_size
            path.unlink()
            compressed_count += 1
            reclaimed += max(0, original_size - compressed_size)
        except BaseException:
            if temporary.is_file():
                temporary.unlink()
            raise
    return compressed_count, reclaimed


def _cleanup_completed_cycle(cycle_dir):
    """Compress and prune exactly one completed cycle."""
    removed = 0
    reclaimed = 0
    cycle_dir = Path(cycle_dir)
    try:
        compressed, cycle_reclaimed = _compress_completed_cycle_results(cycle_dir)
    except (OSError, UnicodeError, csv.Error) as error:
        # Storage optimization is optional; a locked or unusual legacy CSV must
        # not prevent the actual optimization campaign from continuing.
        print(f"Storage warning: kept legacy files in {cycle_dir.name}: {error}")
        compressed = 0
    else:
        reclaimed += cycle_reclaimed
    keep = {"discovery_candidates.json", "discovery_pool_candidates.json"}
    for path in cycle_dir.glob("*_candidates.json"):
        if path.name not in keep:
            reclaimed += path.stat().st_size
            path.unlink()
            removed += 1
    checkpoints_dir = cycle_dir / "checkpoints"
    if checkpoints_dir.is_dir():
        for checkpoint_dir in checkpoints_dir.iterdir():
            if not checkpoint_dir.is_dir():
                continue
            for name in ("best_params.json", "checkpoint.json"):
                path = checkpoint_dir / name
                if path.is_file():
                    reclaimed += path.stat().st_size
                    path.unlink()
                    removed += 1
            if not any(checkpoint_dir.iterdir()):
                checkpoint_dir.rmdir()
        if not any(checkpoints_dir.iterdir()):
            checkpoints_dir.rmdir()
    return removed, compressed, reclaimed


def _cleanup_completed_auto_cycles(output_dir, current_cycle, show_progress=False):
    """Upgrade every older cycle once when a campaign process starts."""
    removed = compressed = reclaimed = 0
    cycles_dir = Path(output_dir) / "cycles"
    cycle_dirs = sorted(cycles_dir.glob("cycle_*"))
    eligible = []
    for cycle_dir in cycle_dirs:
        try:
            cycle_number = int(cycle_dir.name.removeprefix("cycle_"))
        except ValueError:
            continue
        if cycle_number >= int(current_cycle):
            continue
        eligible.append(cycle_dir)
    for index, cycle_dir in enumerate(eligible, start=1):
        cycle_removed, cycle_compressed, cycle_reclaimed = _cleanup_completed_cycle(
            cycle_dir
        )
        removed += cycle_removed
        compressed += cycle_compressed
        reclaimed += cycle_reclaimed
        if show_progress:
            _show_loading_progress("Preparing stored cycles", index, len(eligible))
    return removed, compressed, reclaimed


def _safe_cleanup_completed_auto_cycles(output_dir, current_cycle, show_progress=False):
    """Keep optional storage cleanup from blocking a compatible resume."""
    try:
        return _cleanup_completed_auto_cycles(
            output_dir, current_cycle, show_progress=show_progress
        )
    except OSError as error:
        print(f"Storage warning: cleanup was deferred: {error}")
        return 0, 0, 0


def _safe_cleanup_completed_cycle(cycle_dir):
    """Clean only the cycle that just completed; older gzip files are untouched."""
    try:
        return _cleanup_completed_cycle(cycle_dir)
    except OSError as error:
        print(f"Storage warning: cleanup was deferred for {Path(cycle_dir).name}: {error}")
        return 0, 0, 0


def _auto_stage_fieldnames(keys):
    metrics = [
        column for column in RESULT_COLUMNS
        if column != "score" and column not in IMPORTANT_RESULT_COLUMNS
    ]
    return [
        "candidate_id", "cycle", "stage", "range_start", "range_end",
        "objective_score", "time_normalized_score", "score",
        *IMPORTANT_RESULT_COLUMNS, *metrics,
        "profit_per_trade", "range_candles", "duration_s", *keys, "error",
        "monthly_returns_json", "required_trades",
    ]


def _upgrade_auto_stage_csv(path, keys):
    """Add time-normalized columns before appending to a version-1 checkpoint."""
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        return
    with path.open(newline="", encoding="utf-8") as csv_file:
        reader = csv.DictReader(csv_file)
        existing_fields = reader.fieldnames or []
        target_fields = _auto_stage_fieldnames(keys)
        if existing_fields == target_fields:
            return
        rows = list(reader)
    for row in rows:
        range_candles = _range_candle_count(row["range_start"], row["range_end"])
        row["range_candles"] = range_candles
        row["time_normalized_score"] = _time_normalized_score(
            row.get("objective_score"), range_candles
        )
    _write_rows_atomic(path, _auto_stage_fieldnames(keys), rows)


def _auto_result_row(keys, candidate_id, cycle, stage, range_start, range_end,
                     params, result, duration, objective_score, error,
                     time_normalized_score=None, range_candles=None, required_trades=None):
    row = {
        "candidate_id": candidate_id,
        "cycle": cycle,
        "stage": stage,
        "range_start": range_start,
        "range_end": range_end,
        "duration_s": round(duration, 4),
        "objective_score": objective_score,
        "time_normalized_score": time_normalized_score,
        "range_candles": range_candles,
        "error": error,
        "required_trades": required_trades,
        "monthly_returns_json": (
            json.dumps(result.get("monthly_returns", []), separators=(",", ":"))
            if result and result.get("monthly_returns") is not None else ""
        ),
    }
    row.update({key: params[key] for key in keys})
    if result:
        row.update({key: result.get(key) for key in RESULT_COLUMNS})
        closed_trades = int(result.get("closed_trades", 0) or 0)
        realized_profit = result.get("realized_profit")
        if realized_profit is None:
            realized_profit = result.get("total_profit", 0)
        row["profit_per_trade"] = (
            float(realized_profit or 0) / closed_trades if closed_trades else None
        )
    return row


def _read_auto_stage_records(path, candidates):
    path = _resolve_csv_path(path)
    if not path.is_file():
        return []
    candidate_map = {candidate["candidate_id"]: candidate for candidate in candidates}
    records = {}
    with _open_csv_text(path) as csv_file:
        reader = csv.DictReader(csv_file)
        if "candidate_id" not in (reader.fieldnames or ()):
            raise ValueError(f"invalid auto checkpoint: {path}")
        for row in reader:
            candidate = candidate_map.get(row["candidate_id"])
            if candidate is None:
                continue
            result = {key: _parse_metric(row.get(key)) for key in RESULT_COLUMNS}
            monthly_json = row.get("monthly_returns_json")
            if monthly_json:
                try:
                    result["monthly_returns"] = [
                        float(value) for value in json.loads(monthly_json)
                    ]
                except (TypeError, ValueError, json.JSONDecodeError):
                    result["monthly_returns"] = []
            raw_score = _parse_metric(row.get("objective_score"))
            range_candles = _parse_metric(row.get("range_candles"))
            if range_candles is None:
                try:
                    range_candles = _range_candle_count(
                        row.get("range_start"), row.get("range_end")
                    )
                except (TypeError, ValueError):
                    range_candles = None
            normalized_score = _parse_metric(row.get("time_normalized_score"))
            if normalized_score is None:
                normalized_score = _time_normalized_score(raw_score, range_candles)
            records[row["candidate_id"]] = {
                "candidate_id": row["candidate_id"],
                "params": candidate["params"],
                "result": result,
                "objective_score": raw_score,
                "time_normalized_score": normalized_score,
                "range_candles": range_candles,
                "range_start": row.get("range_start"),
                "range_end": row.get("range_end"),
                "duration": float(row.get("duration_s") or 0),
                "error": row.get("error") or None,
            }
    return [
        records[candidate["candidate_id"]]
        for candidate in candidates
        if candidate["candidate_id"] in records
    ]


def _auto_stage_best(records):
    records = list(records)

    def rank(record):
        objective = _record_comparable_score(record)
        if objective is not None:
            return 1, objective
        score = _finite_number((record.get("result") or {}).get("score"))
        return 0, score if score is not None else -math.inf

    return max(records, key=rank) if records else None


def _write_auto_stage_checkpoint(
    cycle_dir, stage, records, base_tune, completed, total, state
):
    """Keep a best_params file beside every resumable auto-stage checkpoint."""
    best = _auto_stage_best(records)
    if best is None:
        return None
    selected_tune = {**base_tune, **best["params"]}
    try:
        checkpoint_adapter = resolve_strategy(
            (state.get("config") or {}).get("strategy", "ma")
        )
    except (ModuleNotFoundError, ValueError):
        checkpoint_adapter = None
    effective_params = (
        _freeze_strategy_tune(checkpoint_adapter, selected_tune)
        if checkpoint_adapter is not None
        else selected_tune
    )
    checkpoint_dir = Path(cycle_dir) / "checkpoints" / stage
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    _write_json(checkpoint_dir / "best_params.json", effective_params)
    _write_json(Path(cycle_dir) / "best_params.json", effective_params)
    checkpoint = {
        "cycle": state["cycle"],
        "stage": stage,
        "completed": completed,
        "total": total,
        "best_candidate_id": best["candidate_id"],
        "best_objective_score": best.get("objective_score"),
        "best_time_normalized_score": best.get("time_normalized_score"),
        "best_params": effective_params,
        "updated_at": _timestamp_now(),
    }
    _write_json(checkpoint_dir / "checkpoint.json", checkpoint)
    state.update({
        "checkpoint_best_stage": stage,
        "checkpoint_best_candidate_id": best["candidate_id"],
        "checkpoint_best_params": effective_params,
    })
    return best


def _clear_auto_stage_checkpoint(cycle_dir, stage):
    """Remove an ephemeral stage checkpoint after its result CSV is complete."""
    checkpoint_dir = Path(cycle_dir) / "checkpoints" / stage
    for name in ("best_params.json", "checkpoint.json"):
        path = checkpoint_dir / name
        if path.is_file():
            path.unlink()
    if checkpoint_dir.is_dir() and not any(checkpoint_dir.iterdir()):
        checkpoint_dir.rmdir()
    parent = checkpoint_dir.parent
    if parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()


def duration_trade_requirement(reference_trades, stage_candles, reference_candles):
    if reference_trades <= 0:
        return 0
    return max(1, math.ceil(reference_trades * stage_candles / max(1, reference_candles)))


def _run_auto_stage(
    args,
    cycle,
    stage,
    candidates,
    range_start,
    range_end,
    base_tune,
    cycle_dir,
    state,
    state_path,
    min_trades_override=None,
):
    """Evaluate one auto stage and persist every yielded result for exact resume."""
    keys = tuple(state["config"]["parameter_grid"])
    results_path = Path(cycle_dir) / f"{stage}_results.csv"
    _upgrade_auto_stage_csv(results_path, keys)
    existing = _read_auto_stage_records(results_path, candidates)
    records = {record["candidate_id"]: record for record in existing}
    pending = [
        candidate for candidate in candidates
        if candidate["candidate_id"] not in records
    ]
    total = len(candidates)
    print(
        f"Cycle {cycle} | {stage}: {len(existing):,}/{total:,} complete | "
        f"range {_format_range_bound(range_start)} -> "
        f"{_format_range_bound(range_end)}"
    )
    if not pending:
        _write_auto_stage_checkpoint(
            cycle_dir, stage, existing, base_tune, len(existing), total, state
        )
        _clear_auto_stage_checkpoint(cycle_dir, stage)
        return existing, False

    fieldnames = _auto_stage_fieldnames(keys)
    append = results_path.is_file() and results_path.stat().st_size > 0
    workers = min(max(1, args.workers), len(pending))
    batch_size = args.batch_size or max(32, workers * 8)
    chunksize = args.chunksize or max(1, batch_size // (workers * 4))
    started = time.perf_counter()
    pool = None
    pool_terminated = False
    interrupted = False
    effective_min_trades = (
        args.min_trades if min_trades_override is None else min_trades_override
    )
    if (state.get('optimizer_features') or {}).get('minimum_trades_policy') == 'duration':
        reference = state['config']['ranges']['discovery']
        effective_min_trades = duration_trade_requirement(
            args.min_trades, _range_candle_count(range_start, range_end),
            _range_candle_count(*reference))
    range_candles = _range_candle_count(range_start, range_end)
    current_best = _auto_stage_best(records.values())
    use_indicator_warmup = bool(
        state["config"].get("indicator_warmup", False)
    )
    strategy_spec = state["config"].get("strategy", "ma")
    capture_monthly_detail = stage == "final"
    tasks = [
        (candidate["candidate_id"], candidate["params"])
        for candidate in pending
    ]
    fixed_warmup = _maximum_candidate_warmup(
        strategy_spec, base_tune, tasks, use_indicator_warmup
    )

    with results_path.open("a" if append else "w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        if not append:
            writer.writeheader()
        if workers > 1:
            pool = multiprocessing.Pool(
                workers,
                initializer=_init_worker,
                initargs=(
                    _parse_bound(range_start),
                    _parse_bound(range_end),
                    base_tune,
                    True,
                     use_indicator_warmup,
                     strategy_spec,
                     capture_monthly_detail,
                     fixed_warmup,
                 ),
            )
        else:
            _init_worker(
                _parse_bound(range_start),
                _parse_bound(range_end),
                base_tune,
                False,
                use_indicator_warmup,
                strategy_spec,
                capture_monthly_detail,
                fixed_warmup,
            )

        if pool is not None:
            evaluated = pool.imap_unordered(_evaluate_task, tasks, chunksize=chunksize)
        else:
            evaluated = (
                _evaluate_candidate(
                    candidate_id,
                    params,
                    base_tune,
                    _parse_bound(range_start),
                    _parse_bound(range_end),
                    use_indicator_warmup,
                    strategy_spec,
                    capture_monthly_detail,
                    fixed_warmup,
                )
                for candidate_id, params in tasks
            )

        try:
            for candidate_id, params, result, duration, error in evaluated:
                objective_score = None
                normalized_score = None
                if not error:
                    closed_trades = int(result.get("closed_trades", 0) or 0)
                    if closed_trades > 0:
                        value = _objective_score(
                            result,
                            min_trades=effective_min_trades,
                            max_drawdown=args.max_drawdown,
                        )
                        objective_score = value if math.isfinite(value) else None
                        normalized_score = _time_normalized_score(
                            objective_score, range_candles
                        )
                writer.writerow(_auto_result_row(
                    keys, candidate_id, cycle, stage, range_start, range_end,
                    params, result, duration, objective_score, error,
                    time_normalized_score=normalized_score,
                    range_candles=range_candles,
                    required_trades=effective_min_trades,
                ))
                csv_file.flush()
                records[candidate_id] = {
                    "candidate_id": candidate_id,
                    "params": params,
                    "result": result or {},
                    "objective_score": objective_score,
                    "time_normalized_score": normalized_score,
                    "range_candles": range_candles,
                    "range_start": range_start,
                    "range_end": range_end,
                    "duration": duration,
                    "error": error,
                }
                state["total_evaluations"] += 1
                state["stage_completed"] = len(records)
                new_record = records[candidate_id]
                previous_best_score = (
                    _record_comparable_score(current_best)
                    if current_best is not None else None
                )
                new_score = _record_comparable_score(new_record)
                is_new_best = new_score is not None and (
                    previous_best_score is None or new_score > previous_best_score
                )
                if is_new_best:
                    current_best = new_record
                    _write_auto_stage_checkpoint(
                        cycle_dir, stage, records.values(), base_tune,
                        len(records), total, state,
                    )
                if args.log_every and (
                    len(records) % args.log_every == 0 or len(records) == total
                ):
                    elapsed = time.perf_counter() - started
                    processed = len(records) - len(existing)
                    speed = processed / elapsed if elapsed > 0 else 0.0
                    remaining = len(pending) - processed
                    eta = remaining / speed if speed > 0 else 0.0
                    best_score = (
                        _finite_number(current_best.get("objective_score"))
                        if current_best else None
                    )
                    shown = "n/a" if best_score is None else f"{best_score:.4f}"
                    current_shown = (
                        "n/a" if objective_score is None else f"{objective_score:.4f}"
                    )
                    rate_shown = (
                        "n/a" if normalized_score is None else f"{normalized_score:.4f}"
                    )
                    print(
                        f"  [{len(records):,}/{total:,}] best={shown} "
                        f"score={current_shown} rate={rate_shown} | "
                        f"{speed:.2f} tests/s | elapsed={elapsed:.1f}s eta={eta:.1f}s"
                    )
                if len(records) % max(1, min(25, args.log_every or 25)) == 0:
                    _write_auto_stage_checkpoint(
                        cycle_dir, stage, records.values(), base_tune,
                        len(records), total, state,
                    )
                    state["updated_at"] = _timestamp_now()
                    _write_json(state_path, state)
        except KeyboardInterrupt:
            interrupted = True
            state.update({
                "status": "interrupted",
                "stage": stage,
                "stage_completed": len(records),
                "updated_at": _timestamp_now(),
            })
            _write_auto_stage_checkpoint(
                cycle_dir, stage, records.values(), base_tune,
                len(records), total, state,
            )
            _write_json(state_path, state)
            if pool is not None:
                pool.terminate()
                pool_terminated = True
            print(
                f"\nAuto mode stopped during {stage} after {len(records):,}/{total:,} "
                "stage tests. Checkpoint saved; use --auto --resume."
            )
        finally:
            if pool is not None:
                if not pool_terminated:
                    pool.close()
                pool.join()

    ordered = [
        records[candidate["candidate_id"]]
        for candidate in candidates
        if candidate["candidate_id"] in records
    ]
    _write_auto_stage_checkpoint(
        cycle_dir, stage, ordered, base_tune, len(ordered), total, state
    )
    if not interrupted:
        _clear_auto_stage_checkpoint(cycle_dir, stage)
    return ordered, interrupted


def _aggregate_walk_forward_records(fold_records, candidates, stability_penalty):
    record_maps = {
        fold_name: {record["candidate_id"]: record for record in records}
        for fold_name, records in fold_records.items()
    }
    aggregated = []
    for candidate in candidates:
        candidate_id = candidate["candidate_id"]
        records = [
            record_maps[fold_name].get(candidate_id)
            for fold_name in sorted(record_maps)
        ]
        if not records or any(record is None for record in records):
            continue
        raw_scores = [_finite_number(record.get("objective_score")) for record in records]
        scores = [_record_comparable_score(record) for record in records]
        qualified = all(score is not None for score in scores)
        valid_scores = [score for score in scores if score is not None]
        if qualified:
            mean_score = statistics.fmean(valid_scores)
            median_score = statistics.median(valid_scores)
            worst_score = min(valid_scores)
            score_std = statistics.pstdev(valid_scores) if len(valid_scores) > 1 else 0.0
            composite = (
                0.50 * median_score
                + 0.25 * mean_score
                + 0.25 * worst_score
                - stability_penalty * score_std
            )
        else:
            mean_score = median_score = worst_score = score_std = None
            composite = -math.inf

        result = {}
        for metric in RESULT_COLUMNS:
            values = [
                _finite_number((record.get("result") or {}).get(metric))
                for record in records
            ]
            numeric = [value for value in values if value is not None]
            if not numeric:
                result[metric] = None
            elif metric in ("closed_trades", "wins", "losses", "liquidations"):
                result[metric] = sum(numeric)
            elif metric == "maximum_drawdown":
                result[metric] = max(numeric, key=abs)
            else:
                result[metric] = statistics.median(numeric)
        result["score"] = composite if math.isfinite(composite) else None
        aggregated.append({
            "candidate_id": candidate_id,
            "params": candidate["params"],
            "result": result,
            "objective_score": composite if math.isfinite(composite) else None,
            "time_normalized_score": composite if math.isfinite(composite) else None,
            "range_candles": sum(record.get("range_candles", 0) or 0 for record in records),
            "duration": sum(record.get("duration", 0) for record in records),
            "error": None if qualified else "candidate failed one or more walk-forward folds",
            "walk_forward": {
                "fold_raw_scores": raw_scores,
                "fold_time_normalized_scores": scores,
                "mean_score": mean_score,
                "median_score": median_score,
                "worst_score": worst_score,
                "score_std": score_std,
                "stability_penalty": stability_penalty,
            },
        })
    return aggregated


def _run_walk_forward_stage(
    args, cycle, candidates, folds, base_tune, cycle_dir, state, state_path,
):
    """Evaluate fixed candidates on disjoint chronological folds with exact resume."""
    fold_records = {}
    for fold_index, (range_start, range_end) in enumerate(folds, start=1):
        fold_name = f"walk_forward_fold_{fold_index:02d}"
        state.update({
            "stage": fold_name,
            "stage_completed": 0,
            "updated_at": _timestamp_now(),
        })
        _write_json(state_path, state)
        records, interrupted = _run_auto_stage(
            args, cycle, fold_name, candidates, range_start, range_end,
            base_tune, cycle_dir, state, state_path,
            min_trades_override=(
                0 if not args.min_trades else max(
                    1, math.ceil(args.min_trades / max(1, len(folds)))
                )
            ),
        )
        fold_records[fold_name] = records
        if interrupted:
            return [], True

    aggregated = _aggregate_walk_forward_records(
        fold_records,
        candidates,
        state["optimizer_features"]["walk_forward_stability_penalty"],
    )
    summary = {
        "cycle": cycle,
        "stage": "walk_forward",
        "folds": [
            {"fold": index, "start": start, "end": end}
            for index, (start, end) in enumerate(folds, start=1)
        ],
        "candidate_count": len(candidates),
        "ranking_formula": (
            "0.50*median + 0.25*mean + 0.25*worst "
            "- stability_penalty*population_stddev"
        ),
        "records": aggregated,
    }
    _write_json(Path(cycle_dir) / "walk_forward_summary.json", summary)
    state.update({
        "stage": "walk_forward",
        "stage_completed": len(aggregated),
        "updated_at": _timestamp_now(),
    })
    _write_auto_stage_checkpoint(
        cycle_dir, "walk_forward", aggregated, base_tune,
        len(aggregated), len(candidates), state,
    )
    _clear_auto_stage_checkpoint(cycle_dir, "walk_forward")
    _write_json(state_path, state)
    return aggregated, False


def _write_rows_atomic(path, fieldnames, rows):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    _replace_with_retry(temporary, path)


def _compound_returns(values):
    compounded = 1.0
    for value in values:
        compounded *= 1.0 + value
    return (compounded - 1.0) * 100.0


def _monthly_performance_summary(monthly_returns):
    """Return decision-friendly monthly statistics from decimal returns."""
    values = []
    for value in monthly_returns or ():
        numeric = _finite_number(value)
        if numeric is not None:
            values.append(numeric)
    recent = values[-12:]

    def describe(sample, prefix=""):
        positive = sum(value > 0 for value in sample)
        negative = sum(value < 0 for value in sample)
        return {
            f"{prefix}months_observed": len(sample),
            f"{prefix}profitable_months": positive,
            f"{prefix}losing_months": negative,
            f"{prefix}flat_months": len(sample) - positive - negative,
            f"{prefix}months_ge_8pct": sum(value >= 0.08 for value in sample),
            f"{prefix}positive_month_ratio": (
                positive / len(sample) if sample else None
            ),
            f"{prefix}compound_return_pct": (
                _compound_returns(sample) if sample else None
            ),
            f"{prefix}average_month_pct": (
                statistics.fmean(sample) * 100.0 if sample else None
            ),
            f"{prefix}best_month_pct": max(sample) * 100.0 if sample else None,
            f"{prefix}worst_month_pct": min(sample) * 100.0 if sample else None,
        }

    summary = describe(values)
    summary.update(describe(recent, "last_12_"))
    return summary


def _range_reporting_summary(state):
    ranges = (state.get("config") or {}).get("ranges", {}) or {}
    final_range = ranges.get("final") or [None, None]
    stress_range = ranges.get("stress") or [None, None]
    created_at = state.get("created_at")
    updated_at = state.get("updated_at")
    elapsed_seconds = None
    try:
        elapsed_seconds = max(
            0.0,
            (datetime.fromisoformat(updated_at) - datetime.fromisoformat(created_at)).total_seconds(),
        )
    except (TypeError, ValueError):
        pass
    return {
        "campaign_started_at": created_at,
        "campaign_updated_at": updated_at,
        "campaign_elapsed_hours": (
            elapsed_seconds / 3600.0 if elapsed_seconds is not None else None
        ),
        "test_start": final_range[0] if len(final_range) > 0 else None,
        "test_end": final_range[1] if len(final_range) > 1 else None,
        "stability_start": stress_range[0] if len(stress_range) > 0 else None,
        "stability_end": stress_range[1] if len(stress_range) > 1 else None,
    }


def _monthly_return_rows(monthly_returns, range_start=None):
    values = [
        value for value in (_finite_number(item) for item in (monthly_returns or ()))
        if value is not None
    ]
    start_date = None
    try:
        start_date = datetime.fromisoformat(str(range_start)).replace(day=1)
    except (TypeError, ValueError):
        pass
    rows = []
    for index, value in enumerate(values):
        label = (
            _shift_calendar_months(start_date, index).strftime("%Y-%m")
            if start_date is not None else f"Month {index + 1}"
        )
        rows.append({
            "Month": label,
            "Return %": value * 100.0,
            "Profitable": value > 0,
            "Profit >= 8%": value >= 0.08,
        })
    return rows


def _flatten_hall_record(record, keys, rank, state=None):
    final_metrics = record.get("stage_metrics", {}).get("final", {}) or {}
    row = {
        "rank": rank,
        "decision": record.get("decision", "WATCH"),
        "decision_reasons": "; ".join(record.get("decision_reasons", []) or []),
        "decision_scope": record.get("decision_scope"),
        "robust_score": record["robust_score"],
        "recency_score": record.get("recency_score"),
        "stage_consistency_score": record.get("stage_consistency_score"),
        "worst_stage_percentile": record.get("worst_stage_percentile"),
        "cycle": record["cycle"],
        "candidate_id": record["candidate_id"],
    }
    metric_names = (
        "score", "time_normalized_score", "total_profit", "total_profit_percent", "closed_trades",
        "win_rate", "maximum_drawdown", "profit_factor", "expectancy_percent",
        "calmar_ratio", "liquidations", "stronger_side", "directional_profit_gap",
        "long_profit", "long_trades", "long_wins", "long_losses", "long_win_rate",
        "long_profit_factor", "long_expectancy", "long_maximum_drawdown",
        "long_total_fees", "long_liquidations", "short_profit", "short_trades",
        "short_wins", "short_losses", "short_win_rate", "short_profit_factor",
        "short_expectancy", "short_maximum_drawdown", "short_total_fees",
        "short_liquidations",
    )
    for metric in metric_names:
        row[f"final_{metric}"] = final_metrics.get(metric)
    row.update(_monthly_performance_summary(final_metrics.get("monthly_returns")))
    row.update({key: final_metrics.get(key) for key in MONTHLY_COLUMNS})
    if state is not None:
        row.update(_range_reporting_summary(state))
    for stage in AUTO_STAGE_ORDER:
        metrics = record.get("stage_metrics", {}).get(stage, {})
        for metric in metric_names:
            if stage == "final":
                continue
            row[f"{stage}_{metric}"] = metrics.get(metric)
    effective_params = record.get('effective_params') or record.get('params') or {}
    row.update({key: effective_params.get(key) for key in keys})
    return row


def _candidate_summary(record, rank, parameter_file, state=None):
    final_metrics = record.get("stage_metrics", {}).get("final", {}) or {}
    summary = {
        "rank": rank,
        "candidate_id": record.get("candidate_id"),
        "decision": record.get("decision", "WATCH"),
        "decision_scope": record.get("decision_scope"),
        "decision_reasons": record.get("decision_reasons", []),
        "robust_score": record.get("robust_score"),
        "recency_score": record.get("recency_score"),
        "stage_consistency_score": record.get("stage_consistency_score"),
        "worst_stage_percentile": record.get("worst_stage_percentile"),
        "cycle": record.get("cycle"),
        "parameter_file": str(parameter_file).replace("\\", "/"),
        "stage_scores": record.get("stage_scores", {}),
        "stage_metrics": record.get("stage_metrics", {}),
        "effective_params": record.get("effective_params") or record.get("params") or {},
    }
    summary["monthly_performance"] = _monthly_performance_summary(
        final_metrics.get("monthly_returns")
    )
    if state is not None:
        summary["time_summary"] = _range_reporting_summary(state)
    return summary


def _save_auto_workbook(
    output_dir, hall_rows, importance_rows, state=None, filename="auto_report.xlsx",
    best_monthly_returns=None,
):
    if not hall_rows:
        return None
    try:
        import pandas as pd
        from openpyxl import load_workbook
        from openpyxl.chart import BarChart, Reference
        from openpyxl.chart.label import DataLabelList
        from openpyxl.formatting.rule import ColorScaleRule
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter
        from openpyxl.worksheet.table import Table, TableStyleInfo
    except ImportError:
        return None

    state = state or {}
    time_summary = _range_reporting_summary(state)
    best = hall_rows[0]
    dashboard_rows = [
        {"Section": "Campaign", "Metric": "Status", "Value": state.get("status")},
        {"Section": "Campaign", "Metric": "Cycles completed", "Value": state.get("cycles_completed")},
        {"Section": "Campaign", "Metric": "Total evaluations", "Value": state.get("total_evaluations")},
        {"Section": "Campaign", "Metric": "Started at", "Value": time_summary["campaign_started_at"]},
        {"Section": "Campaign", "Metric": "Last updated at", "Value": time_summary["campaign_updated_at"]},
        {"Section": "Campaign", "Metric": "Elapsed hours", "Value": time_summary["campaign_elapsed_hours"]},
        {"Section": "Test window", "Metric": "Main test start", "Value": time_summary["test_start"]},
        {"Section": "Test window", "Metric": "Main test end (exclusive)", "Value": time_summary["test_end"]},
        {"Section": "Test window", "Metric": "Historical stability start", "Value": time_summary["stability_start"]},
        {"Section": "Test window", "Metric": "Historical stability end", "Value": time_summary["stability_end"]},
        {"Section": "Best candidate", "Metric": "Candidate", "Value": best.get("candidate_id")},
        {"Section": "Best candidate", "Metric": "Recommended parameter file", "Value": "best_params.json"},
        {"Section": "Best candidate", "Metric": "Decision", "Value": best.get("decision")},
        {"Section": "Best candidate", "Metric": "Robust score", "Value": best.get("robust_score")},
        {"Section": "Best candidate", "Metric": "Total profit %", "Value": best.get("final_total_profit_percent")},
        {"Section": "Best candidate", "Metric": "Maximum drawdown %", "Value": best.get("final_maximum_drawdown")},
        {"Section": "Best candidate", "Metric": "Closed trades", "Value": best.get("final_closed_trades")},
        {"Section": "Best candidate", "Metric": "Win rate %", "Value": best.get("final_win_rate")},
        {"Section": "Direction", "Metric": "Stronger side", "Value": best.get("final_stronger_side")},
        {"Section": "Long", "Metric": "Long net profit", "Value": best.get("final_long_profit")},
        {"Section": "Long", "Metric": "Long trades", "Value": best.get("final_long_trades")},
        {"Section": "Long", "Metric": "Long win rate %", "Value": best.get("final_long_win_rate")},
        {"Section": "Long", "Metric": "Long profit factor", "Value": best.get("final_long_profit_factor")},
        {"Section": "Long", "Metric": "Long maximum drawdown %", "Value": best.get("final_long_maximum_drawdown")},
        {"Section": "Short", "Metric": "Short net profit", "Value": best.get("final_short_profit")},
        {"Section": "Short", "Metric": "Short trades", "Value": best.get("final_short_trades")},
        {"Section": "Short", "Metric": "Short win rate %", "Value": best.get("final_short_win_rate")},
        {"Section": "Short", "Metric": "Short profit factor", "Value": best.get("final_short_profit_factor")},
        {"Section": "Short", "Metric": "Short maximum drawdown %", "Value": best.get("final_short_maximum_drawdown")},
        {"Section": "Last 12 months", "Metric": "Months observed", "Value": best.get("last_12_months_observed")},
        {"Section": "Last 12 months", "Metric": "Profitable months", "Value": best.get("last_12_profitable_months")},
        {"Section": "Last 12 months", "Metric": "Months with profit >= 8%", "Value": best.get("last_12_months_ge_8pct")},
        {"Section": "Last 12 months", "Metric": "Losing months", "Value": best.get("last_12_losing_months")},
        {"Section": "Last 12 months", "Metric": "Compound return %", "Value": best.get("last_12_compound_return_pct")},
        {"Section": "Last 12 months", "Metric": "Average month %", "Value": best.get("last_12_average_month_pct")},
        {"Section": "Last 12 months", "Metric": "Best month %", "Value": best.get("last_12_best_month_pct")},
        {"Section": "Last 12 months", "Metric": "Worst month %", "Value": best.get("last_12_worst_month_pct")},
    ]
    monthly_columns = [
        "rank", "decision", "candidate_id", "test_start", "test_end",
        "months_observed", "profitable_months", "months_ge_8pct", "losing_months",
        "positive_month_ratio", "compound_return_pct", "average_month_pct",
        "best_month_pct", "worst_month_pct", "last_12_months_observed",
        "last_12_profitable_months", "last_12_months_ge_8pct",
        "last_12_losing_months", "last_12_positive_month_ratio",
        "last_12_compound_return_pct", "last_12_average_month_pct",
        "last_12_best_month_pct", "last_12_worst_month_pct",
    ]
    monthly_rows = [
        {key: row.get(key) for key in monthly_columns} for row in hall_rows
    ]
    best_monthly_rows = _monthly_return_rows(
        best_monthly_returns or [], best.get("test_start")
    )
    directional_columns = [
        column for column in hall_rows[0]
        if column in {"rank", "decision", "candidate_id"}
        or column.startswith("final_long_")
        or column.startswith("final_short_")
        or column in {"final_stronger_side", "final_directional_profit_gap"}
    ]
    directional_rows = [
        {column: row.get(column) for column in directional_columns}
        for row in hall_rows
    ]
    output_path = Path(output_dir) / filename
    staging_path = output_path.with_name(
        f".{output_path.stem}.building{output_path.suffix}"
    )
    with pd.ExcelWriter(staging_path, engine="openpyxl") as writer:
        pd.DataFrame([
            {key: row.get(key) for key in (*OVERVIEW_COLUMNS, *MONTHLY_COLUMNS)} for row in hall_rows[:5]
        ]).to_excel(writer, sheet_name="Quick Compare", index=False)
        comparison_keys = list((state.get('config') or {}).get('parameter_grid') or {})
        if comparison_keys:
            pd.DataFrame(comparison_rows(hall_rows, comparison_keys)).to_excel(
                writer, sheet_name="Parameter Compare", index=False)
        pd.DataFrame(dashboard_rows).to_excel(writer, sheet_name="Dashboard", index=False)
        pd.DataFrame(hall_rows).to_excel(writer, sheet_name="Hall of Fame", index=False)
        pd.DataFrame(directional_rows).to_excel(
            writer, sheet_name="Directional Metrics", index=False
        )
        pd.DataFrame(monthly_rows).to_excel(
            writer, sheet_name="Monthly Analysis", index=False
        )
        pd.DataFrame(
            best_monthly_rows,
            columns=("Month", "Return %", "Profitable", "Profit >= 8%"),
        ).to_excel(writer, sheet_name="Best Monthly Returns", index=False)
        pd.DataFrame(importance_rows).to_excel(
            writer, sheet_name="Parameter Importance", index=False
        )
    workbook = load_workbook(staging_path)
    header_fill = PatternFill("solid", fgColor="17365D")
    header_font = Font(color="FFFFFF", bold=True)
    section_colors = {
        "Campaign": "D9EAF7", "Test window": "E2F0D9",
        "Best candidate": "FFF2CC", "Last 12 months": "E4DFEC",
        "Direction": "E4DFEC", "Long": "E2F0D9", "Short": "FCE4D6",
    }
    thin_gray = Side(style="thin", color="D9E1F2")
    for index, worksheet in enumerate(workbook.worksheets, start=1):
        worksheet.freeze_panes = (
            "D2" if worksheet.title in {"Hall of Fame", "Directional Metrics"}
            else "A2"
        )
        worksheet.sheet_view.showGridLines = False
        for cell in worksheet[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center")
        worksheet.auto_filter.ref = worksheet.dimensions
        for column_index, cells in enumerate(worksheet.columns, start=1):
            width = min(38, max(10, max(len(str(cell.value or "")) for cell in cells[:200]) + 2))
            worksheet.column_dimensions[get_column_letter(column_index)].width = width
        if worksheet.max_row >= 2:
            table = Table(displayName=f"AutoOptimizerTable{index}", ref=worksheet.dimensions)
            table.tableStyleInfo = TableStyleInfo(
                name="TableStyleMedium2", showRowStripes=True,
                showFirstColumn=False, showLastColumn=False,
                showColumnStripes=False,
            )
            worksheet.add_table(table)
        headers = {cell.value: cell.column for cell in worksheet[1]}
        for metric in (
            "robust_score", "recency_score", "stage_consistency_score",
            "worst_stage_percentile", "final_total_profit_percent",
            "final_total_profit", "final_long_profit", "final_short_profit",
            "final_maximum_drawdown", "final_win_rate", "weight", "effect",
        ):
            column = headers.get(metric)
            if column and worksheet.max_row >= 3:
                letter = get_column_letter(column)
                worksheet.conditional_formatting.add(
                    f"{letter}2:{letter}{worksheet.max_row}",
                    ColorScaleRule(
                        start_type="min", start_color="F8696B",
                        mid_type="percentile", mid_value=50, mid_color="FFEB84",
                        end_type="max", end_color="63BE7B",
                    ),
                )
        for header, column in headers.items():
            header_text = str(header or "")
            if "ratio" in header_text:
                for cell in worksheet.iter_rows(
                    min_row=2, max_row=worksheet.max_row,
                    min_col=column, max_col=column,
                ):
                    cell[0].number_format = "0.0%"
            elif header_text.endswith("_pct") or header_text.endswith("_percent"):
                for cell in worksheet.iter_rows(
                    min_row=2, max_row=worksheet.max_row,
                    min_col=column, max_col=column,
                ):
                    cell[0].number_format = "0.00"
        if worksheet.title == "Dashboard":
            worksheet.sheet_properties.tabColor = "4472C4"
            worksheet.column_dimensions["A"].width = 20
            worksheet.column_dimensions["B"].width = 30
            worksheet.column_dimensions["C"].width = 24
            for row in range(2, worksheet.max_row + 1):
                section = worksheet.cell(row, 1).value
                fill = PatternFill("solid", fgColor=section_colors.get(section, "FFFFFF"))
                for column in range(1, 4):
                    cell = worksheet.cell(row, column)
                    cell.fill = fill
                    cell.border = Border(bottom=thin_gray)
                worksheet.cell(row, 1).font = Font(bold=True, color="17365D")
                if worksheet.cell(row, 2).value in {
                    "Total profit %", "Maximum drawdown %", "Win rate %",
                    "Compound return %", "Average month %", "Best month %", "Worst month %",
                }:
                    worksheet.cell(row, 3).number_format = "0.00"
        elif worksheet.title == "Hall of Fame":
            worksheet.sheet_properties.tabColor = "70AD47"
            rank_column = headers.get("rank")
            if rank_column:
                medal_colors = {2: "FFD966", 3: "D9E1F2", 4: "F4B183"}
                for row, color in medal_colors.items():
                    if row <= worksheet.max_row:
                        cell = worksheet.cell(row, rank_column)
                        cell.fill = PatternFill("solid", fgColor=color)
                        cell.font = Font(bold=True, color="17365D")
            decision_column = headers.get("decision")
            if decision_column:
                decision_fills = {
                    "ACCEPT": PatternFill("solid", fgColor="C6EFCE"),
                    "WATCH": PatternFill("solid", fgColor="FFEB9C"),
                    "REJECT": PatternFill("solid", fgColor="FFC7CE"),
                }
                for row in range(2, worksheet.max_row + 1):
                    decision = worksheet.cell(row, decision_column).value
                    if decision in decision_fills:
                        worksheet.cell(row, decision_column).fill = decision_fills[decision]
                        worksheet.cell(row, decision_column).font = Font(bold=True)
        elif worksheet.title == "Monthly Analysis":
            worksheet.sheet_properties.tabColor = "8064A2"
        elif worksheet.title == "Directional Metrics":
            worksheet.sheet_properties.tabColor = "A5A5A5"
            for header, column in headers.items():
                header_text = str(header or "")
                if header_text.startswith("final_long_"):
                    worksheet.cell(1, column).fill = PatternFill("solid", fgColor="548235")
                elif header_text.startswith("final_short_"):
                    worksheet.cell(1, column).fill = PatternFill("solid", fgColor="C0504D")
        elif worksheet.title == "Best Monthly Returns":
            worksheet.sheet_properties.tabColor = "5B9BD5"
            return_column = headers.get("Return %")
            if return_column and worksheet.max_row >= 2:
                letter = get_column_letter(return_column)
                worksheet.conditional_formatting.add(
                    f"{letter}2:{letter}{worksheet.max_row}",
                    ColorScaleRule(
                        start_type="min", start_color="F8696B",
                        mid_type="num", mid_value=0, mid_color="FFEB84",
                        end_type="max", end_color="63BE7B",
                    ),
                )
        else:
            worksheet.sheet_properties.tabColor = "F4B183"

    dashboard_sheet = workbook["Dashboard"]

    def add_auto_dashboard_table(start_row, start_column, title, headers, rows):
        end_column = start_column + len(headers) - 1
        dashboard_sheet.merge_cells(
            start_row=start_row, start_column=start_column,
            end_row=start_row, end_column=end_column,
        )
        title_cell = dashboard_sheet.cell(start_row, start_column, title)
        title_cell.fill = PatternFill("solid", fgColor="17365D")
        title_cell.font = Font(color="FFFFFF", bold=True, size=11)
        title_cell.alignment = Alignment(horizontal="left", vertical="center")
        dashboard_sheet.row_dimensions[start_row].height = 22
        for offset, header in enumerate(headers):
            cell = dashboard_sheet.cell(start_row + 1, start_column + offset, header)
            cell.fill = PatternFill("solid", fgColor="D9EAF7")
            cell.font = Font(color="17365D", bold=True)
            cell.alignment = Alignment(horizontal="center", vertical="center")
        for row_offset, row_values in enumerate(rows, start=2):
            for column_offset, value in enumerate(row_values):
                cell = dashboard_sheet.cell(
                    start_row + row_offset, start_column + column_offset, value
                )
                cell.border = Border(bottom=thin_gray)
                cell.alignment = Alignment(
                    horizontal="right" if isinstance(value, (int, float)) else "left"
                )
                header = headers[column_offset]
                if header in {"Profit %", "Drawdown %"}:
                    cell.number_format = '0.00"%";[Red]-0.00"%"'
                elif "Profit" in header or header == "Gap":
                    cell.number_format = '$#,##0.00;[Red]-$#,##0.00'
                elif header == "Score":
                    cell.number_format = "0.000"
                if header == "Long Profit":
                    cell.fill = PatternFill("solid", fgColor="E2F0D9")
                elif header == "Short Profit":
                    cell.fill = PatternFill("solid", fgColor="FCE4D6")
                elif header == "Decision":
                    decision_colors = {
                        "ACCEPT": "C6EFCE", "WATCH": "FFEB9C", "REJECT": "FFC7CE"
                    }
                    if value in decision_colors:
                        cell.fill = PatternFill("solid", fgColor=decision_colors[value])
                        cell.font = Font(bold=True)
        widths = {
            "Rank": 8, "Decision": 11, "Candidate": 18, "Score": 12,
            "Profit %": 12, "Net Profit": 15, "Drawdown %": 13,
            "Long Profit": 15, "Short Profit": 15, "Gap": 14,
            "Stronger Side": 14,
        }
        for offset, header in enumerate(headers):
            dashboard_sheet.column_dimensions[
                get_column_letter(start_column + offset)
            ].width = widths.get(header, 13)

    add_auto_dashboard_table(
        2, 14, "Top 10 exact results - snapshot / Hall of Fame",
        ["Rank", "Decision", "Candidate", "Score", "Profit %", "Net Profit",
         "Drawdown %", "Long Profit", "Short Profit", "Stronger Side"],
        [[
            row.get("rank"), row.get("decision"), row.get("candidate_id"),
            row.get("robust_score"), row.get("final_total_profit_percent"),
            row.get("final_total_profit"), row.get("final_maximum_drawdown"),
            row.get("final_long_profit"), row.get("final_short_profit"),
            row.get("final_stronger_side"),
        ] for row in hall_rows[:10]],
    )
    add_auto_dashboard_table(
        18, 14, "Top 10 directional breakdown - exact net profit",
        ["Rank", "Long Profit", "Short Profit", "Gap", "Stronger Side"],
        [[
            row.get("rank"), row.get("final_long_profit"),
            row.get("final_short_profit"), row.get("final_directional_profit_gap"),
            row.get("final_stronger_side"),
        ] for row in hall_rows[:10]],
    )

    hall_sheet = workbook["Hall of Fame"]
    hall_headers = {cell.value: cell.column for cell in hall_sheet[1]}
    profit_column = hall_headers.get("final_total_profit_percent")
    if profit_column and hall_sheet.max_row >= 2:
        chart = BarChart()
        chart.title = "Top candidates: total profit %"
        chart.y_axis.title = "Profit %"
        chart.y_axis.numFmt = '0.00"%"'
        chart.height = 7
        chart.width = 13
        last_row = min(hall_sheet.max_row, 11)
        chart.add_data(
            Reference(hall_sheet, min_col=profit_column, min_row=1, max_row=last_row),
            titles_from_data=True,
        )
        chart.set_categories(
            Reference(hall_sheet, min_col=1, min_row=2, max_row=last_row)
        )
        chart.legend = None
        chart.dLbls = DataLabelList()
        chart.dLbls.showVal = True
        chart.dLbls.numFmt = '0.00"%"'
        chart.series[0].graphicalProperties.solidFill = "4472C4"
        chart.series[0].graphicalProperties.line.solidFill = "2F5597"
        add_report_chart(workbook, chart)
    directional_sheet = workbook["Directional Metrics"]
    directional_headers = {cell.value: cell.column for cell in directional_sheet[1]}
    rank_column = directional_headers.get("rank")
    long_profit_column = directional_headers.get("final_long_profit")
    short_profit_column = directional_headers.get("final_short_profit")
    if rank_column and long_profit_column and short_profit_column:
        chart = BarChart()
        chart.type = "col"
        chart.style = 11
        chart.title = "Top candidates: Long vs Short net profit"
        chart.y_axis.title = "Net profit ($)"
        chart.y_axis.numFmt = '$#,##0'
        chart.height = 7
        chart.width = 13
        last_row = min(directional_sheet.max_row, 11)
        for column in (long_profit_column, short_profit_column):
            chart.add_data(
                Reference(
                    directional_sheet, min_col=column, min_row=1, max_row=last_row
                ),
                titles_from_data=True,
            )
        chart.set_categories(
            Reference(
                directional_sheet, min_col=rank_column, min_row=2, max_row=last_row
            )
        )
        chart.dLbls = DataLabelList()
        chart.dLbls.showVal = True
        chart.dLbls.numFmt = '$#,##0'
        for series, color in zip(chart.series, ("70AD47", "C0504D")):
            series.graphicalProperties.solidFill = color
            series.graphicalProperties.line.solidFill = color
        add_report_chart(workbook, chart)
    workbook.save(staging_path)
    try:
        _replace_with_retry(staging_path, output_path)
        return output_path
    except PermissionError:
        fallback_path = output_path.with_name(
            f"{output_path.stem}_latest{output_path.suffix}"
        )
        _replace_with_retry(staging_path, fallback_path)
        print(
            f"Warning: {output_path.name} is open or locked; wrote the newest "
            f"workbook to {fallback_path.name}. Close the workbook before the next "
            "cycle to restore normal in-place updates."
        )
        return fallback_path


def _write_auto_reports(output_dir, hall, importance, state, keys, excel_enabled=True):
    output_dir = Path(output_dir)
    for record in hall:
        if not record.get("decision"):
            record.update(_auto_candidate_decision(record))
    ranked_hall = sorted(hall, key=_auto_candidate_rank_key, reverse=True)
    _write_json(output_dir / "hall_of_fame.json", ranked_hall)
    _write_json(output_dir / "parameter_importance.json", importance)
    if ranked_hall:
        _write_json(output_dir / "best_params.json", ranked_hall[0]["effective_params"])
    elif state.get("checkpoint_best_params"):
        # Before the first full finalist exists, expose the best provisional
        # checkpoint so even an early interruption has a usable best_params.json.
        _write_json(output_dir / "best_params.json", state["checkpoint_best_params"])
    candidates_dir = output_dir / "candidates"
    candidates_dir.mkdir(parents=True, exist_ok=True)
    hall_rows = []
    candidate_catalog = []
    for rank, record in enumerate(ranked_hall, start=1):
        if not record.get("decision"):
            record.update(_auto_candidate_decision(record))
        parameter_name = f"rank_{rank:03d}_params.json"
        summary_name = f"rank_{rank:03d}_summary.json"
        parameter_path = candidates_dir / parameter_name
        summary_path = candidates_dir / summary_name
        _write_json(parameter_path, record["effective_params"])
        summary = _candidate_summary(
            record, rank, Path("candidates") / parameter_name, state=state
        )
        _write_json(summary_path, summary)
        row = _flatten_hall_record(record, keys, rank, state=state)
        row["parameter_file"] = str(Path("candidates") / parameter_name).replace("\\", "/")
        row["summary_file"] = str(Path("candidates") / summary_name).replace("\\", "/")
        # Keep reusable file locations with the decision columns, before metrics/params.
        row = {
            key: row[key] for key in (
                "rank", "decision", "decision_reasons", "decision_scope",
                "parameter_file", "summary_file", "robust_score", "recency_score",
                "stage_consistency_score", "worst_stage_percentile", "cycle",
                "candidate_id",
            )
        } | {
            key: value for key, value in row.items()
            if key not in {
                "rank", "decision", "decision_reasons", "decision_scope",
                "parameter_file", "summary_file", "robust_score", "recency_score",
                "stage_consistency_score", "worst_stage_percentile", "cycle",
                "candidate_id",
            }
        }
        hall_rows.append(row)
        candidate_catalog.append(summary)
    _write_json(output_dir / "candidate_catalog.json", candidate_catalog)
    write_overview(output_dir, hall_rows, keys, state)
    if candidate_catalog:
        _write_json(output_dir / "best_candidate_summary.json", candidate_catalog[0])
    importance_rows = [
        {"rank": rank, "parameter": key, **item}
        for rank, (key, item) in enumerate(
            sorted(importance.items(), key=lambda pair: pair[1]["weight"], reverse=True),
            start=1,
        )
    ]
    if hall_rows:
        _write_rows_atomic(output_dir / "hall_of_fame.csv", list(hall_rows[0]), hall_rows)
        _write_rows_atomic(
            output_dir / "candidate_catalog.csv", list(hall_rows[0]), hall_rows
        )
    if importance_rows:
        _write_rows_atomic(
            output_dir / "parameter_importance.csv",
            list(importance_rows[0]),
            importance_rows,
        )
    workbook = (
        _save_auto_workbook(
            output_dir,
            hall_rows,
            importance_rows,
            state=state,
            best_monthly_returns=(
                (ranked_hall[0].get("stage_metrics", {}).get("final", {}) or {}).get(
                    "monthly_returns"
                )
                if ranked_hall else None
            ),
        )
        if excel_enabled else None
    )
    summary = {
        "mode": "auto",
        "optimizer_version": state.get("version", 1),
        "status": state["status"],
        "cycles_completed": state["cycles_completed"],
        "current_cycle": state["cycle"],
        "current_stage": state["stage"],
        "total_evaluations": state["total_evaluations"],
        "hall_of_fame_size": len(ranked_hall),
        "best_robust_score": ranked_hall[0]["robust_score"] if ranked_hall else None,
        "best_decision": ranked_hall[0].get("decision") if ranked_hall else None,
        "best_recency_score": ranked_hall[0].get("recency_score") if ranked_hall else None,
        "best_params": ranked_hall[0]["effective_params"] if ranked_hall else None,
        "recommended_params_file": "best_params.json",
        "best_params_manifest": "best_params_manifest.json",
        "time_summary": _range_reporting_summary(state),
        "best_monthly_performance": (
            _monthly_performance_summary(
                (ranked_hall[0].get("stage_metrics", {}).get("final", {}) or {}).get(
                    "monthly_returns"
                )
            )
            if ranked_hall else None
        ),
        "date_protocol": state.get("date_protocol"),
        "time_summary": _range_reporting_summary(state),
        "optimizer_features": state.get("optimizer_features"),
        "advanced_from_cycle": state.get("advanced_from_cycle"),
        "bootstrap": state.get("bootstrap"),
        "excel_report": str(workbook) if workbook else None,
        "updated_at": state["updated_at"],
    }
    _write_json(output_dir / "auto_summary.json", summary)
    publish_campaign(output_dir, state, excel=excel_enabled, cycle=max(1, int(state.get('cycles_completed', 0))))
    if not ranked_hall:
        fields = list(_flatten_hall_record({'robust_score': None, 'cycle': None,
                      'candidate_id': None}, keys, 1))
        _write_rows_atomic(output_dir / 'hall_of_fame.csv', fields, [])
        _write_rows_atomic(output_dir / 'candidate_catalog.csv', fields, [])
        if excel_enabled:
            shutil.copy2(output_dir / 'campaign_report.xlsx', output_dir / 'auto_report.xlsx')
            summary['excel_report'] = str(output_dir / 'auto_report.xlsx')
        summary['recommended_params_file'] = None
        summary['outcome'] = 'NO_QUALIFIED_FINALIST'
        _write_json(output_dir / 'auto_summary.json', summary)
    if ranked_hall:
        best_record = ranked_hall[0]
        best_metrics = (best_record.get("stage_metrics", {}).get("final", {}) or {})
        _write_best_params_manifest(
            output_dir,
            status=(
                "final" if state.get("status") == "completed"
                else "best_available_auto_checkpoint"
            ),
            selection_basis=(
                "rank #1 robust Hall-of-Fame candidate across Auto stages; "
                "root best_params.json supersedes cycle/checkpoint copies"
            ),
            metrics={**best_metrics, "objective_score": best_record.get("robust_score")},
            candidate_id=best_record.get("candidate_id"),
        )
    elif state.get("checkpoint_best_params"):
        _write_best_params_manifest(
            output_dir,
            status="provisional_checkpoint",
            selection_basis="best available Auto checkpoint; no Hall-of-Fame finalist yet",
        )


def refresh_auto_report_monthly(output_dir, top_n=None, workers=1):
    """Backfill rich monthly analytics for an existing completed campaign."""
    output_dir = Path(output_dir)
    state = _load_json(output_dir / "auto_state.json", {}) or {}
    hall = _load_json(output_dir / "hall_of_fame.json", []) or []
    if not state or not hall:
        raise ValueError("auto_state.json and hall_of_fame.json are required")
    ranges = (state.get("config") or {}).get("ranges", {}) or {}
    final_range = ranges.get("final")
    if not isinstance(final_range, (list, tuple)) or len(final_range) != 2:
        raise ValueError("campaign does not contain a valid final range")
    strategy_spec = (state.get("config") or {}).get("strategy", "ma")
    limit = len(hall) if top_n is None else min(len(hall), max(1, int(top_n)))
    selected = hall[:limit]
    record_by_id = {str(record.get("candidate_id")): record for record in selected}
    tasks = [
        (
            index,
            str(record.get("candidate_id")),
            "monthly_refresh",
            record.get("effective_params") or record.get("params") or {},
            _parse_bound(final_range[0]),
            _parse_bound(final_range[1]),
            strategy_spec,
        )
        for index, record in enumerate(selected, start=1)
    ]
    worker_count = min(max(1, int(workers)), len(tasks))
    if worker_count > 1:
        pool = multiprocessing.Pool(worker_count)
        evaluated = pool.imap_unordered(_evaluate_random_window_task, tasks, chunksize=1)
    else:
        pool = None
        evaluated = map(_evaluate_random_window_task, tasks)
    completed = 0
    for (
        _test_index, candidate_id, _window_id, _params, _start, _end,
        result, _duration, error,
    ) in evaluated:
        if error or result is None:
            if pool is not None:
                pool.terminate()
                pool.join()
            raise RuntimeError(f"monthly refresh failed for {candidate_id}: {error}")
        record = record_by_id[candidate_id]
        final_metrics = record.setdefault("stage_metrics", {}).setdefault("final", {})
        monthly_returns = [
            float(value) for value in result.get("monthly_returns", [])
            if _finite_number(value) is not None
        ]
        final_metrics.update({
            "monthly_returns": monthly_returns,
            "profit_more_than_8%": sum(value >= 0.08 for value in monthly_returns),
            "range_start": final_range[0],
            "range_end": final_range[1],
        })
        completed += 1
        _show_loading_progress("Refreshing monthly analytics", completed, limit)
    if pool is not None:
        pool.close()
        pool.join()
    state["report_enrichment"] = {
        "monthly_candidates": limit,
        "monthly_threshold": 0.08,
        "updated_at": _timestamp_now(),
    }
    _write_json(output_dir / "auto_state.json", state)
    importance = _load_json(output_dir / "parameter_importance.json", {}) or {}
    keys = tuple((state.get("config") or {}).get("parameter_grid", {}))
    _write_auto_reports(output_dir, hall, importance, state, keys, excel_enabled=True)
    cycles_completed = int(state.get("cycles_completed", 0) or 0)
    if cycles_completed > 0:
        _write_auto_cycle_snapshot(
            output_dir,
            hall,
            cycles_completed,
            (state.get("config") or {}).get("snapshot_top", len(hall)),
            (state.get("config") or {}).get("parameter_grid", {}),
            state,
        )
    return output_dir / "auto_report.xlsx"


def _merge_hall_of_fame(
    hall, finalists, cycle, keys, base_tune, limit, strategy_adapter=None
):
    by_signature = {
        _candidate_signature(record["params"], keys): record for record in hall
    }
    for finalist in finalists:
        if not math.isfinite(finalist["robust_score"]):
            continue
        record = {
            **finalist,
            "cycle": cycle,
            "effective_params": (
                _freeze_strategy_tune(
                    strategy_adapter, {**base_tune, **finalist["params"]}
                )
                if strategy_adapter is not None
                else {**base_tune, **finalist["params"]}
            ),
        }
        signature = _candidate_signature(record["params"], keys)
        previous = by_signature.get(signature)
        if previous is None or record["robust_score"] > previous["robust_score"]:
            by_signature[signature] = record
    return sorted(by_signature.values(), key=_auto_candidate_rank_key, reverse=True)[:limit]


def run_auto_optimization(args, grid=None):
    """Run an unlimited, staged, importance-guided optimization campaign."""
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / "auto_state.json"
    resume_existing = bool(args.resume or state_path.is_file())
    saved_state = _load_json(state_path) if resume_existing and state_path.is_file() else None
    saved_config = _restore_auto_resume_args(args, saved_state) if saved_state else None
    profiles = _profiles_from_args(args)
    profile = getattr(args, "profile", None) or (
        "full" if "full" in profiles else next(iter(profiles))
    )
    if saved_config and saved_config.get("parameter_grid"):
        grid = {
            key: list(values)
            for key, values in saved_config["parameter_grid"].items()
        }
    else:
        if grid is None:
            if profile not in profiles:
                raise ValueError(
                    f"profile {profile!r} is not available for strategy "
                    f"{_adapter_from_args(args).identifier}; choose from {', '.join(profiles)}"
                )
            grid = profiles[profile]
    keys = tuple(grid)
    if saved_config:
        base_tune = dict(saved_config.get("base_tune") or {})
        base_description = saved_config.get("base_source", "saved checkpoint")
    else:
        base_tune, base_description = _load_base_tune(args)
        if getattr(args, 'auto_learning_target', 'rank') == 'profit-evidence':
            base_tune = _freeze_strategy_tune(_adapter_from_args(args), base_tune)
    resolved_end = _latest_market_end() if args.auto_end == "latest" else args.auto_end
    config = _auto_configuration(
        args, profile, grid, base_tune, base_description, resolved_end
    )
    optimizer_features = _auto_feature_configuration(args)
    bootstrap = _auto_bootstrap(args, grid)
    seed_elites = list(getattr(args, "_seed_elites", ()) or ())
    seed_history = list(getattr(args, "_seed_history", ()) or ())

    if resume_existing:
        state = saved_state or _load_json(state_path)
        if state is None:
            raise FileNotFoundError(
                f"auto checkpoint not found: {state_path}. Start without --resume first."
            )
        if state.get("version", 1) not in LEGACY_AUTO_STATE_VERSIONS:
            raise ValueError("auto checkpoint version is not compatible")
        saved_runtime_config = state.get("config") or {}
        saved_runtime_config.setdefault("strategy", "ma_strategy:ma_strategy")
        saved_runtime_config.setdefault("parameter_grid_source", None)
        # Legacy checkpoints predate strategy-owned data metadata.  Their saved
        # ranges remain authoritative; adding current equivalent metadata keeps
        # them resumable without weakening checks for all newly created runs.
        saved_runtime_config.setdefault("data_file", config.get("data_file"))
        saved_runtime_config.setdefault("timeframe", config.get("timeframe"))
        saved_runtime_config.setdefault(
            "snapshot_cycles", int(getattr(args, "snapshot_cycles", 50))
        )
        saved_runtime_config.setdefault(
            "snapshot_top", int(getattr(args, "snapshot_top", 100))
        )
        state["config"] = saved_runtime_config
        if state.get("config") != config:
            raise ValueError(
                "auto checkpoint settings differ from this command; use the original "
                "profile, ranges, test counts, base parameters, and constraints"
            )
        saved_features = state.get("optimizer_features")
        if saved_features is not None:
            saved_features.setdefault('learning_target', 'rank')
            saved_features.setdefault('minimum_trades_policy', 'fixed')
        if saved_features is not None and "surrogate_max_samples" not in saved_features:
            saved_features["surrogate_max_samples"] = optimizer_features[
                "surrogate_max_samples"
            ]
            state["resume_migration"] = {
                "restored_saved_cli_settings": True,
                "legacy_surrogate_max_samples": optimizer_features[
                    "surrogate_max_samples"
                ],
                "migrated_at": _timestamp_now(),
            }
        if saved_features is not None and saved_features != optimizer_features:
            raise ValueError(
                "auto search-engine settings differ from this checkpoint; use the "
                "original halving, surrogate, and walk-forward options"
            )
        if saved_features is None:
            # Version-1 campaigns may already be halfway through a stage. Finish
            # that exact plan first and activate the new engine next cycle.
            current_cycle_dir = (
                output_dir / "cycles" / f"cycle_{int(state['cycle']):06d}"
            )
            state["optimizer_features"] = optimizer_features
            state["advanced_from_cycle"] = int(state["cycle"]) + int(
                current_cycle_dir.exists()
            )
            state["migrated_from_version"] = state.get("version", 1)
            state["version"] = AUTO_STATE_VERSION
        hall = _load_json(output_dir / "hall_of_fame.json", []) or []
        resume_adapter = _adapter_from_args(args)
        for record in hall:
            record["effective_params"] = _freeze_strategy_tune(
                resume_adapter,
                record.get("effective_params")
                or {**base_tune, **record.get("params", {})},
            )
        if hall:
            _write_json(output_dir / "hall_of_fame.json", hall)
        importance = _load_json(output_dir / "parameter_importance.json", {}) or {}
        mutation_guidance = _load_json(
            output_dir / "mutation_guidance.json", {}
        ) or {}
        state.update({
            "status": "running",
            "total_evaluations": _reconcile_auto_evaluations(output_dir, state),
            "updated_at": _timestamp_now(),
        })
        saved_ranges = state.get("config", {}).get("ranges", {}) or {}
        state.setdefault("date_protocol", {
            "policy": "frozen_checkpoint",
            "development_start": (saved_ranges.get("final") or [None, None])[0],
            "validation_start": (saved_ranges.get("validation") or [None, None])[0],
            "discovery_start": (saved_ranges.get("discovery") or [None, None])[0],
            "development_end": (saved_ranges.get("final") or [None, None])[1],
        })
        print(
            f"Resuming auto cycle {state['cycle']} at {state['stage']} | "
            f"{state['total_evaluations']:,} total evaluations"
        )
        if not args.resume:
            print("Existing auto checkpoint detected; resume was selected automatically.")
    else:
        if state_path.exists():
            raise FileExistsError(
                f"an auto campaign already exists in {output_dir}; use --auto --resume "
                "or choose another --output-dir"
            )
        equal_weight = 1.0 / max(1, len(keys))
        importance = {
            key: {"weight": equal_weight, "effect": 0.0, "groups": 0, "samples": 0}
            for key in keys
        }
        hall = []
        mutation_guidance = {}
        state = {
            "version": AUTO_STATE_VERSION,
            "status": "running",
            "cycle": 1,
            "cycles_completed": 0,
            "stage": "discovery",
            "stage_completed": 0,
            "total_evaluations": 0,
            "importance_cycle": 0,
            "created_at": _timestamp_now(),
            "updated_at": _timestamp_now(),
            "config": config,
            "optimizer_features": optimizer_features,
            "date_protocol": dict(getattr(args, "_date_protocol", {}) or {}),
            "advanced_from_cycle": 1,
            "bootstrap": bootstrap,
        }
    removed_files, compressed_csvs, removed_bytes = _safe_cleanup_completed_auto_cycles(
        output_dir, state["cycle"], show_progress=resume_existing
    )
    try:
        compacted_plans, compacted_bytes = _compact_auto_candidate_plans(
            output_dir, show_progress=resume_existing
        )
    except OSError as error:
        print(f"Storage warning: legacy candidate plans were kept unchanged: {error}")
        compacted_plans = compacted_bytes = 0
    reclaimed_bytes = removed_bytes + compacted_bytes
    if removed_files or compressed_csvs or compacted_plans:
        print(
            f"Storage cleanup: removed {removed_files:,} redundant files, "
            f"compressed {compressed_csvs:,} result CSVs, "
            f"compacted {compacted_plans:,} candidate plans | "
            f"reclaimed {reclaimed_bytes / (1024 * 1024):.1f} MiB"
        )
    _write_json(state_path, state)

    ranges = config["ranges"]
    print(
        f"Mode: auto | {args.auto_tests:,} discovery tests/cycle | "
        f"workers: {max(1, args.workers)}"
    )
    if args.auto_tests >= optimizer_features["advanced_min_candidates"]:
        print(
            f"Funnel: {args.auto_tests:,} -> {args.auto_validation_top} -> "
            f"{args.auto_stress_top} -> WF {args.auto_walk_forward_top} -> "
            f"{args.auto_final_top} | profile: {profile}"
        )
    else:
        print(
            f"Funnel (legacy-small-run): {args.auto_tests:,} -> "
            f"{args.auto_validation_top} -> {args.auto_stress_top} -> "
            f"{args.auto_final_top} | profile: {profile}"
        )
    print(f"Latest market end: {resolved_end}")
    print(f"Learning target: {optimizer_features.get('learning_target', 'rank')}")
    print(
        "Search intelligence: normalized funnel learning | "
        "quality + uncertainty + diversity selection | directed mutation"
    )
    if not resume_existing and bootstrap:
        print(
            f"Warm start: {bootstrap['compatible_parameter_count']} compatible "
            f"parameters loaded from {bootstrap['source']}"
        )

    completed_at_resume = int(state.get("cycles_completed", 0) or 0)
    snapshot_every = max(1, int(getattr(args, "snapshot_cycles", 50)))
    if (
        resume_existing
        and hall
        and completed_at_resume > 0
        and completed_at_resume % snapshot_every == 0
    ):
        expected_snapshot = (
            output_dir / "snapshots" / f"cycles_{completed_at_resume:06d}"
        )
        if not (expected_snapshot / "manifest.json").is_file():
            recovered_snapshot = _write_auto_cycle_snapshot(
                output_dir,
                hall,
                completed_at_resume,
                getattr(args, "snapshot_top", 100),
                grid,
                state,
            )
            print(f"Recovered missing cycle-boundary snapshot: {recovered_snapshot}")

    cycle_limit = max(0, int(args.auto_cycles))
    # Disk history is loaded once per process. Subsequent cycles extend these
    # caches with only their new plans/results instead of rescanning everything.
    seen_cache = None
    history_cache = seed_history or None
    try:
        while cycle_limit == 0 or state["cycles_completed"] < cycle_limit:
            cycle = int(state["cycle"])
            cycle_dir = output_dir / "cycles" / f"cycle_{cycle:06d}"
            cycle_dir.mkdir(parents=True, exist_ok=True)
            continuation_parent = hall[0] if hall else None
            continuation_base = (
                continuation_parent.get("effective_params", continuation_parent["params"])
                if continuation_parent
                else (state.get("bootstrap") or {}).get("params", base_tune)
            )
            training_parent = {
                "source": "hall_of_fame" if continuation_parent else "base_configuration",
                "candidate_id": (
                    continuation_parent.get("candidate_id") if continuation_parent else None
                ),
                "robust_score": (
                    continuation_parent.get("robust_score") if continuation_parent else None
                ),
                "params": continuation_base,
            }
            state["training_parent"] = training_parent
            _write_json(cycle_dir / "training_parent.json", training_parent)
            advanced = (
                args.auto_tests >= optimizer_features["advanced_min_candidates"]
                and cycle >= int(state.get("advanced_from_cycle", 1))
            )
            source_plan_path = cycle_dir / (
                "discovery_pool_candidates.json" if advanced
                else "discovery_candidates.json"
            )
            if source_plan_path.is_file():
                discovery_pool = _read_candidate_plan(source_plan_path)
            else:
                if seen_cache is None:
                    seen_cache = _load_auto_seen(output_dir, keys)
                else:
                    print(
                        f"Using in-memory candidate history: "
                        f"{len(seen_cache):,} tested configurations."
                    )
                elite_records = [
                    {
                        "params": {key: record["params"][key] for key in keys},
                        "result": record.get("stage_metrics", {}).get("final", {}),
                    }
                    for record in hall
                ]
                if not elite_records and seed_elites:
                    elite_records = [
                        {
                            "params": {
                                key: record["params"][key] for key in keys
                            },
                            "result": record.get("result", {}),
                        }
                        for record in seed_elites
                        if all(key in record.get("params", {}) for key in keys)
                    ]
                if advanced:
                    if history_cache is None:
                        history_cache = _load_discovery_history(output_dir, keys)
                    else:
                        print(
                            f"Using in-memory surrogate history: "
                            f"{len(history_cache):,} stored results."
                        )
                    history = history_cache
                    if not mutation_guidance and history:
                        mutation_guidance = _learn_mutation_guidance(
                            history, keys, grid
                        )
                else:
                    history = []
                generator = SmartCandidateGenerator(
                    grid,
                    seed=args.seed + cycle - 1,
                    baseline_params=continuation_base,
                    continuous_refinement=bool(elite_records) and getattr(
                        _adapter_from_args(args).module, 'ALLOW_CONTINUOUS_REFINEMENT', True),
                    parameter_importance={
                        key: item.get("weight", 1.0) for key, item in importance.items()
                    },
                    mutation_guidance=mutation_guidance,
                    refinement_round=max(1, cycle - 1),
                    strategy_adapter=_adapter_from_args(args),
                )
                generator.seen.update(seen_cache)
                surrogate_ready = (
                    len(history) >= optimizer_features["surrogate_min_samples"]
                )
                pool_target = args.auto_tests * (
                    optimizer_features["surrogate_pool_multiplier"]
                    if advanced and surrogate_ready else 1
                )
                generated_pool = (
                    generator.generate_auto(
                        pool_target,
                        elites=elite_records,
                        progress_label="Generating candidate pool",
                    )
                    if elite_records else generator.generate(
                        pool_target, progress_label="Generating candidate pool"
                    )
                )
                if len(generated_pool) < pool_target:
                    print(
                        f"\nCandidate pool reached {len(generated_pool):,}/{pool_target:,} "
                        "unique valid configurations; continuing with the available pool."
                    )
                generated, surrogate_metadata = _select_surrogate_candidates(
                    generated_pool,
                    history,
                    args.auto_tests,
                    keys,
                    grid,
                    optimizer_features,
                    args.seed + cycle - 1,
                    parameter_importance=importance,
                )
                seen_cache.update(
                    _candidate_signature(params, keys) for params in generated
                )
                discovery_pool = [
                    {
                        "candidate_id": f"c{cycle:06d}-{index:06d}",
                        "params": params,
                    }
                    for index, params in enumerate(generated, start=1)
                ]
                _write_candidate_plan(
                    source_plan_path,
                    cycle,
                    "discovery_pool" if advanced else "discovery",
                    discovery_pool,
                )
                if advanced:
                    surrogate_metadata.update({
                        "cycle": cycle,
                        "created_at": _timestamp_now(),
                        "uses_previous_discovery_results": True,
                    })
                    _write_json(cycle_dir / "surrogate_search.json", surrogate_metadata)
                    if surrogate_metadata.get("enabled"):
                        mix = surrogate_metadata["selection_mix"]
                        print(
                            "Candidate selection: "
                            f"quality {mix['quality_acquisition']:.0%} | "
                            f"uncertainty {mix['uncertainty']:.0%} | "
                            f"diversity {mix['diversity_novelty']:.0%} | "
                            f"random {mix['random']:.0%} | "
                            f"{surrogate_metadata['diversity_buckets']:,} diversity buckets"
                        )
            if not discovery_pool:
                state.update({"status": "exhausted", "updated_at": _timestamp_now()})
                _write_json(state_path, state)
                print("Auto search space is exhausted; no unique discovery candidates remain.")
                break

            stage_results = {}
            discovery_candidates = discovery_pool
            if advanced and optimizer_features["halving_rungs"]:
                rung_ranges = _expanding_discovery_ranges(
                    *ranges["discovery"], optimizer_features["halving_rungs"]
                )
                previous_records = None
                for rung_index, rung_range in enumerate(rung_ranges, start=1):
                    rung_stage = f"discovery_rung_{rung_index:02d}"
                    rung_plan_path = cycle_dir / f"{rung_stage}_candidates.json"
                    if rung_plan_path.is_file():
                        rung_candidates = _read_candidate_plan(rung_plan_path)
                    elif rung_index == 1:
                        rung_candidates = discovery_pool
                        _write_candidate_plan(
                            rung_plan_path, cycle, rung_stage, rung_candidates,
                            source=source_plan_path,
                        )
                    else:
                        keep_count = max(
                            args.auto_validation_top,
                            math.ceil(
                                len(discovery_candidates)
                                * optimizer_features["halving_keep"]
                            ),
                        )
                        rung_candidates = _promote_candidates(
                            previous_records, keep_count
                        )
                        _write_candidate_plan(
                            rung_plan_path, cycle, rung_stage, rung_candidates
                        )
                    discovery_candidates = rung_candidates
                    state.update({
                        "status": "running", "stage": rung_stage,
                        "stage_completed": 0, "updated_at": _timestamp_now(),
                    })
                    _write_json(state_path, state)
                    previous_records, interrupted = _run_auto_stage(
                        args, cycle, rung_stage, rung_candidates,
                        *rung_range, base_tune, cycle_dir, state, state_path,
                        min_trades_override=(
                            0 if not args.min_trades else max(
                                1,
                                math.ceil(
                                    args.min_trades
                                    * (rung_range[1] - rung_range[0])
                                    / max(
                                        1,
                                        _bound_index(ranges["discovery"][1])
                                        - _bound_index(ranges["discovery"][0]),
                                    )
                                ),
                            )
                        ),
                    )
                    if interrupted:
                        _write_auto_reports(
                            output_dir, hall, importance, state, keys,
                            excel_enabled=bool(args.excel_top),
                        )
                        return hall[0] if hall else None

                discovery_plan_path = cycle_dir / "discovery_candidates.json"
                if discovery_plan_path.is_file():
                    discovery_candidates = _read_candidate_plan(discovery_plan_path)
                else:
                    keep_count = max(
                        args.auto_validation_top,
                        math.ceil(
                            len(discovery_candidates)
                            * optimizer_features["halving_keep"]
                        ),
                    )
                    discovery_candidates = _promote_candidates(
                        previous_records, keep_count
                    )
                    _write_candidate_plan(
                        discovery_plan_path, cycle, "discovery", discovery_candidates
                    )

            discovery_plan_path = cycle_dir / "discovery_candidates.json"
            if advanced and not discovery_plan_path.is_file():
                _write_candidate_plan(
                    discovery_plan_path, cycle, "discovery", discovery_candidates
                )

            state.update({
                "status": "running", "stage": "discovery", "stage_completed": 0,
                "updated_at": _timestamp_now(),
            })
            _write_json(state_path, state)
            discovery_records, interrupted = _run_auto_stage(
                args, cycle, "discovery", discovery_candidates,
                *ranges["discovery"], base_tune, cycle_dir, state, state_path,
            )
            stage_results["discovery"] = discovery_records
            if interrupted:
                _write_auto_reports(
                    output_dir, hall, importance, state, keys,
                    excel_enabled=bool(args.excel_top),
                )
                return hall[0] if hall else None
            _annotate_discovery_learning_scores(discovery_records)
            if optimizer_features.get('learning_target') == 'profit-evidence':
                apply_profit_learning(discovery_records, {'discovery': discovery_records}, AUTO_STAGE_WEIGHTS)
            if advanced and history_cache is None:
                history_cache = _load_discovery_history(output_dir, keys)
            if history_cache is not None:
                history_cache = _merge_surrogate_history(
                    history_cache, discovery_records
                )
                history_cache = _write_surrogate_history_cache(
                    output_dir / "surrogate_history_cache.json.gz",
                    history_cache,
                    keys,
                    cycle,
                )
                mutation_guidance = _learn_mutation_guidance(
                    history_cache, keys, grid
                )
                _write_json(
                    output_dir / "mutation_guidance.json", mutation_guidance
                )
            else:
                mutation_guidance = _learn_mutation_guidance(
                    discovery_records, keys, grid
                )
                _write_json(
                    output_dir / "mutation_guidance.json", mutation_guidance
                )

            if int(state.get("importance_cycle", 0)) < cycle:
                learned = _learn_parameter_importance(
                    discovery_records, keys, target=args.auto_importance_target
                )
                importance = _smooth_parameter_importance(importance, learned)
                state["importance_cycle"] = cycle
                state["updated_at"] = _timestamp_now()
                _write_json(output_dir / "parameter_importance.json", importance)
                _write_json(state_path, state)

            stage_plan = [
                ("validation", args.auto_validation_top),
                ("stress", args.auto_stress_top),
            ]
            if not advanced:
                stage_plan.append(("final", args.auto_final_top))
            for stage, keep_count in stage_plan:
                plan_path = cycle_dir / f"{stage}_candidates.json"
                if plan_path.is_file():
                    candidates = _read_candidate_plan(plan_path)
                else:
                    ranked = _combine_auto_stage_records(stage_results)
                    candidates = [
                        {
                            "candidate_id": record["candidate_id"],
                            "params": record["params"],
                        }
                        for record in ranked[:keep_count]
                        if math.isfinite(record["robust_score"])
                    ]
                    _write_candidate_plan(plan_path, cycle, stage, candidates)
                state.update({
                    "stage": stage, "stage_completed": 0, "updated_at": _timestamp_now(),
                })
                _write_json(state_path, state)
                records, interrupted = _run_auto_stage(
                    args, cycle, stage, candidates, *ranges[stage], base_tune,
                    cycle_dir, state, state_path,
                )
                stage_results[stage] = records
                if interrupted:
                    _write_auto_reports(
                        output_dir, hall, importance, state, keys,
                        excel_enabled=bool(args.excel_top),
                    )
                    return hall[0] if hall else None

            if advanced:
                walk_folds = _walk_forward_ranges(
                    ranges, optimizer_features["walk_forward_folds"]
                )
                if walk_folds:
                    walk_plan_path = cycle_dir / "walk_forward_candidates.json"
                    if walk_plan_path.is_file():
                        walk_candidates = _read_candidate_plan(walk_plan_path)
                    else:
                        ranked = _combine_auto_stage_records(stage_results)
                        walk_candidates = [
                            {
                                "candidate_id": record["candidate_id"],
                                "params": record["params"],
                            }
                            for record in ranked[
                                :optimizer_features["walk_forward_top"]
                            ]
                            if math.isfinite(record["robust_score"])
                        ]
                        _write_candidate_plan(
                            walk_plan_path, cycle, "walk_forward", walk_candidates
                        )
                    walk_records, interrupted = _run_walk_forward_stage(
                        args, cycle, walk_candidates, walk_folds, base_tune,
                        cycle_dir, state, state_path,
                    )
                    stage_results["walk_forward"] = walk_records
                    if interrupted:
                        _write_auto_reports(
                            output_dir, hall, importance, state, keys,
                            excel_enabled=bool(args.excel_top),
                        )
                        return hall[0] if hall else None

                final_plan_path = cycle_dir / "final_candidates.json"
                if final_plan_path.is_file():
                    final_candidates = _read_candidate_plan(final_plan_path)
                else:
                    ranked = _combine_auto_stage_records(stage_results)
                    final_candidates = [
                        {
                            "candidate_id": record["candidate_id"],
                            "params": record["params"],
                        }
                        for record in ranked[:args.auto_final_top]
                        if math.isfinite(record["robust_score"])
                    ]
                    _write_candidate_plan(
                        final_plan_path, cycle, "final", final_candidates
                    )
                state.update({
                    "stage": "final", "stage_completed": 0,
                    "updated_at": _timestamp_now(),
                })
                _write_json(state_path, state)
                final_records, interrupted = _run_auto_stage(
                    args, cycle, "final", final_candidates, *ranges["final"],
                    base_tune, cycle_dir, state, state_path,
                )
                stage_results["final"] = final_records
                if interrupted:
                    _write_auto_reports(
                        output_dir, hall, importance, state, keys,
                        excel_enabled=bool(args.excel_top),
                    )
                    return hall[0] if hall else None

            finalists = _combine_auto_stage_records(stage_results)
            if history_cache is not None:
                _apply_funnel_learning_scores(history_cache, stage_results)
                if optimizer_features.get('learning_target') == 'profit-evidence':
                    apply_profit_learning(history_cache, stage_results, AUTO_STAGE_WEIGHTS)
                history_cache = _write_surrogate_history_cache(
                    output_dir / "surrogate_history_cache.json.gz",
                    history_cache,
                    keys,
                    cycle,
                )
                mutation_guidance = _learn_mutation_guidance(
                    history_cache, keys, grid
                )
                _write_json(
                    output_dir / "mutation_guidance.json", mutation_guidance
                )
            hall = _merge_hall_of_fame(
                hall,
                finalists,
                cycle,
                keys,
                base_tune,
                args.auto_hall_size,
                _adapter_from_args(args),
            )
            state.update({
                "cycles_completed": state["cycles_completed"] + 1,
                "cycle": cycle + 1,
                "stage": "discovery",
                "stage_completed": 0,
                "status": "running",
                "updated_at": _timestamp_now(),
            })
            _write_json(state_path, state)
            _write_auto_reports(
                output_dir, hall, importance, state, keys,
                excel_enabled=bool(args.excel_top),
            )
            completed_cycles = int(state["cycles_completed"])
            print_leaders([
                _flatten_hall_record(record, keys, rank)
                for rank, record in enumerate(
                    sorted(hall, key=_auto_candidate_rank_key, reverse=True)[:3], 1)
            ], output_dir)
            snapshot_every = max(1, int(getattr(args, "snapshot_cycles", 50)))
            if completed_cycles % snapshot_every == 0:
                snapshot_dir = _write_auto_cycle_snapshot(
                    output_dir,
                    hall,
                    completed_cycles,
                    getattr(args, "snapshot_top", 100),
                    grid,
                    state,
                )
                print(
                    f"Snapshot published: top {min(len(hall), int(getattr(args, 'snapshot_top', 100)))} "
                    f"at {snapshot_dir}"
                )
            _safe_cleanup_completed_cycle(cycle_dir)
            best_text = (
                f"{hall[0]['robust_score']:.4f}" if hall else "no qualified finalist"
            )
            important = sorted(
                importance.items(), key=lambda pair: pair[1]["weight"], reverse=True
            )[:5]
            print(
                f"Cycle {cycle} complete | Hall of Fame: {len(hall)} | best: {best_text}"
            )
            print(
                "Most influential parameters: "
                + ", ".join(
                    f"{key}={hall[0].get('effective_params', {}).get(key)!r} "
                    f"(influence {item['weight']:.1%})" if hall
                    else f"{key} ({item['weight']:.1%})"
                    for key, item in important)
            )
            directed = sorted(
                (
                    (key, item) for key, item in mutation_guidance.items()
                    if item.get("direction") in (-1, 1)
                ),
                key=lambda pair: pair[1].get("confidence", 0.0),
                reverse=True,
            )[:5]
            if directed:
                print(
                    "Mutation guidance: "
                    + ", ".join(
                        f"{key} {'up' if item['direction'] > 0 else 'down'} "
                        f"x{item['step_multiplier']} ({item['confidence']:.0%})"
                        for key, item in directed
                    )
                )
    except KeyboardInterrupt:
        state.update({"status": "interrupted", "updated_at": _timestamp_now()})
        _write_json(state_path, state)
        _write_auto_reports(
            output_dir, hall, importance, state, keys,
            excel_enabled=bool(args.excel_top),
        )
        print("\nAuto mode stopped safely. Use --auto --resume to continue.")
        if hall and (output_dir / "best_params.json").is_file():
            print(f"BEST AVAILABLE PARAMETER FILE: {output_dir / 'best_params.json'}")
            print(f"Winner guide: {output_dir / 'best_params_manifest.json'}")
        if not hall:
            print("No qualified final winner. Checkpoint parameters are provisional.")
        print(f"Campaign report: {output_dir / 'CAMPAIGN.md'}")
        return hall[0] if hall else None

    state.update({"status": "completed", "updated_at": _timestamp_now()})
    _write_json(state_path, state)
    _write_auto_reports(
        output_dir, hall, importance, state, keys,
        excel_enabled=bool(args.excel_top),
    )
    print(
        f"Auto campaign stopped after {state['cycles_completed']} completed cycle(s). "
        f"Resume with --auto --resume."
    )
    if hall and (output_dir / "best_params.json").is_file():
        print(f"USE THIS PARAMETER FILE: {output_dir / 'best_params.json'}")
        print(f"Winner guide: {output_dir / 'best_params_manifest.json'}")
    if not hall:
        print("No qualified final winner. Checkpoint parameters are provisional.")
    print(f"Campaign report: {output_dir / 'CAMPAIGN.md'}")
    return hall[0] if hall else None


def _merge_staged_archive(archive, phase_hall, block, phase_name, limit):
    """Keep the strongest unique full configurations across staged phases."""
    merged = {}
    sources = [(record, False) for record in archive]
    sources.extend((record, True) for record in phase_hall)
    for record, is_current_phase in sources:
        effective = record.get("effective_params") or record.get("params") or {}
        if not effective:
            continue
        signature = json.dumps(effective, sort_keys=True, separators=(",", ":"))
        enriched = dict(record)
        enriched["effective_params"] = dict(effective)
        if is_current_phase:
            enriched["block"] = block
            enriched["phase"] = phase_name
            original_id = record.get("candidate_id")
            enriched["phase_candidate_id"] = original_id
            enriched["candidate_id"] = f"b{block:04d}-{phase_name}-{original_id}"
        previous = merged.get(signature)
        current_score = _finite_number(enriched.get("robust_score"))
        previous_score = _finite_number(previous.get("robust_score")) if previous else None
        if current_score is None:
            continue
        if previous is None or previous_score is None or current_score > previous_score:
            merged[signature] = enriched
    return sorted(
        merged.values(),
        key=_auto_candidate_rank_key,
        reverse=True,
    )[:limit]


def _staged_seed_data(records, grid, baseline, strategy_adapter=None):
    """Project full historical winners onto one phase for trees and parent selection."""
    keys = tuple(grid)
    defaults = (strategy_adapter or _adapter_from_spec('ma')).default_values(baseline)
    seeds = []
    seen = set()
    def seed_rank(record):
        audit_score = _finite_number(record.get("random_audit_score"))
        robust_score = _finite_number(record.get("robust_score"))
        return (
            int(audit_score is not None),
            audit_score if audit_score is not None else -math.inf,
            robust_score if robust_score is not None else -math.inf,
        )

    ranked = sorted(records, key=seed_rank, reverse=True)
    denominator = max(1, len(ranked) - 1)
    for rank, record in enumerate(ranked):
        effective = record.get("effective_params") or record.get("params") or {}
        def inherited_value(key):
            value = effective.get(key, defaults.get(key))
            if value is None and key.startswith(('long_', 'short_')):
                common = key.split('_', 1)[1]
                value = effective.get(common, defaults.get(common, grid[key][0]))
            return value
        params = {
            key: _nearest_value(grid[key], inherited_value(key))
            for key in keys
        }
        signature = _candidate_signature(params, keys)
        if signature in seen:
            continue
        seen.add(signature)
        seeds.append({
            "candidate_id": f"staged-seed-{rank + 1:04d}",
            "params": params,
            "learning_score": 1.0 - rank / denominator,
            "learning_source": "previous_staged_top",
            "result": record.get("stage_metrics", {}).get("final", {}),
        })
    return seeds


def _staged_report_rows(records):
    parameter_keys = tuple(dict.fromkeys(
        key for record in records
        for key in (record.get('effective_params') or record.get('params') or {})))
    rows = []
    for rank, record in enumerate(records, start=1):
        effective = record.get("effective_params") or {}
        final_metrics = record.get("stage_metrics", {}).get("final", {})
        row = {
            "rank": rank,
            "decision": record.get("decision", "WATCH"),
            "decision_reasons": "; ".join(record.get("decision_reasons", []) or []),
            "decision_scope": record.get("decision_scope"),
            "robust_score": record.get("robust_score"),
            "recency_score": record.get("recency_score"),
            "stage_consistency_score": record.get("stage_consistency_score"),
            "worst_stage_percentile": record.get("worst_stage_percentile"),
            "block": record.get("block"),
            "phase": record.get("phase"),
            "cycle": record.get("cycle"),
            "candidate_id": record.get("candidate_id"),
            "random_audit_score": record.get("random_audit_score"),
            "random_audit_valid_ratio": record.get("random_audit_valid_ratio"),
            "random_audit_positive_ratio": record.get("random_audit_positive_ratio"),
        }
        for metric in (
            "score", "total_profit", "total_profit_percent", "closed_trades",
            "win_rate", "maximum_drawdown", "profit_factor",
            "expectancy_percent", "calmar_ratio", "liquidations", "stronger_side",
            "directional_profit_gap", "long_profit", "long_trades", "long_wins",
            "long_losses", "long_win_rate", "long_profit_factor", "long_expectancy",
            "long_maximum_drawdown", "long_total_fees", "long_liquidations",
            "short_profit", "short_trades", "short_wins", "short_losses",
            "short_win_rate", "short_profit_factor", "short_expectancy",
            "short_maximum_drawdown", "short_total_fees", "short_liquidations",
        ):
            row[metric] = final_metrics.get(metric)
        row.update({key: effective.get(key) for key in parameter_keys})
        rows.append(row)
    return rows


def _write_staged_ranking(output_dir, records, top_n, *, snapshot_dir=None,
                          excel_enabled=True, state=None):
    ranked = list(records)[:max(1, int(top_n))]
    if not ranked:
        return
    rows = _staged_report_rows(ranked)
    fieldnames = list(rows[0])
    output_dir = Path(output_dir)
    _write_rows_atomic(output_dir / "top_100.csv", fieldnames, rows)
    _write_rows_atomic(output_dir / "candidate_catalog.csv", fieldnames, rows)
    _write_json(output_dir / f"top_{max(1, int(top_n))}.json", ranked)
    _write_json(output_dir / "best_params.json", ranked[0]["effective_params"])
    _write_best_params_manifest(
        output_dir,
        status="final",
        selection_basis="rank #1 robust winner across completed staged optimization phases",
        metrics=ranked[0].get("stage_metrics", {}).get("final", {}),
        candidate_id=ranked[0].get("candidate_id"),
    )
    _write_json(
        output_dir / "best_candidate_summary.json",
        _candidate_summary(ranked[0], 1, Path("best_params.json")),
    )
    parameter_keys = tuple(dict.fromkeys(key for record in ranked for key in record['effective_params']))
    workbook_rows = [_flatten_hall_record(record, parameter_keys, rank, state=state)
                     for rank, record in enumerate(ranked, 1)]
    write_overview(output_dir, workbook_rows, parameter_keys, state or {})
    if excel_enabled:
        _save_auto_workbook(output_dir, workbook_rows, [], state=state)
        if snapshot_dir is not None:
            Path(snapshot_dir).mkdir(parents=True, exist_ok=True)
            _save_auto_workbook(snapshot_dir, workbook_rows, [], state=state, filename='snapshot_report.xlsx')
    if snapshot_dir is not None:
        snapshot_dir = Path(snapshot_dir)
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        _write_rows_atomic(snapshot_dir / "top_100.csv", fieldnames, rows)
        _write_json(
            snapshot_dir / f"top_{max(1, int(top_n))}.json", ranked
        )
        _write_json(snapshot_dir / "best_params.json", ranked[0]["effective_params"])
        _write_best_params_manifest(
            snapshot_dir,
            status="immutable_snapshot",
            selection_basis="rank #1 staged winner captured by this snapshot",
            metrics=ranked[0].get("stage_metrics", {}).get("final", {}),
            candidate_id=ranked[0].get("candidate_id"),
        )
        _write_json(
            snapshot_dir / "best_candidate_summary.json",
            _candidate_summary(ranked[0], 1, Path("best_params.json")),
        )
        params_dir = snapshot_dir / "params"
        summaries_dir = snapshot_dir / "summaries"
        params_dir.mkdir(parents=True, exist_ok=True)
        summaries_dir.mkdir(parents=True, exist_ok=True)
        for rank, record in enumerate(ranked, 1):
            parameter_file = Path("params") / f"rank_{rank:03d}_params.json"
            _write_json(
                snapshot_dir / parameter_file,
                record["effective_params"],
            )
            _write_json(
                summaries_dir / f"rank_{rank:03d}_summary.json",
                _candidate_summary(record, rank, parameter_file),
            )


def _write_auto_cycle_snapshot(output_dir, hall, cycle, top_n, grid, state):
    """Publish reusable top candidates at a deterministic cycle boundary."""
    output_dir = Path(output_dir)
    snapshot_dir = output_dir / "snapshots" / f"cycles_{int(cycle):06d}"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    top_n = max(1, int(top_n))
    plateau = parameter_plateau_scores(hall, grid)
    enriched = []
    for record in hall:
        candidate_id = str(record.get("candidate_id", ""))
        enriched.append({**record, **plateau.get(candidate_id, {})})
    enriched.sort(
        key=lambda item: (
            _auto_candidate_rank_key(item)[0],
            _finite_number(item.get("plateau_adjusted_score"))
            if _finite_number(item.get("plateau_adjusted_score")) is not None
            else -math.inf,
            *_auto_candidate_rank_key(item)[1:],
        ),
        reverse=True,
    )
    selected = enriched[:top_n]
    _write_json(snapshot_dir / f"top_{top_n}.json", selected)
    if selected:
        rows = [
            _flatten_hall_record(record, tuple(grid), rank, state=state)
            for rank, record in enumerate(selected, 1)
        ]
        for row, record in zip(rows, selected):
            plateau_values = {}
            for metric in (
                "neighbour_count", "neighbour_method", "median_grid_distance",
                "median_neighbour_score",
                "plateau_stability", "plateau_adjusted_score",
            ):
                plateau_values[metric] = record.get(metric)
            core_keys = (
                "rank", "decision", "decision_reasons", "decision_scope",
                "robust_score", "recency_score", "stage_consistency_score",
                "worst_stage_percentile", "cycle", "candidate_id",
            )
            original = dict(row)
            row.clear()
            row.update({key: original.get(key) for key in core_keys})
            row.update(plateau_values)
            row.update({key: value for key, value in original.items() if key not in core_keys})
        _write_rows_atomic(snapshot_dir / f"top_{top_n}.csv", list(rows[0]), rows)
        _write_json(snapshot_dir / "best_params.json", selected[0]["effective_params"])
        _write_best_params_manifest(
            snapshot_dir,
            status="immutable_snapshot",
            selection_basis="rank #1 plateau-adjusted Auto winner at this cycle boundary",
            metrics=selected[0].get("stage_metrics", {}).get("final", {}),
            candidate_id=selected[0].get("candidate_id"),
        )
        _write_json(
            snapshot_dir / "best_candidate_summary.json",
            _candidate_summary(selected[0], 1, Path("best_params.json"), state=state),
        )
        params_dir = snapshot_dir / "params"
        summaries_dir = snapshot_dir / "summaries"
        params_dir.mkdir(parents=True, exist_ok=True)
        summaries_dir.mkdir(parents=True, exist_ok=True)
        for rank, record in enumerate(selected, 1):
            parameter_file = Path("params") / f"rank_{rank:03d}_params.json"
            _write_json(
                snapshot_dir / parameter_file,
                record["effective_params"],
            )
            _write_json(
                summaries_dir / f"rank_{rank:03d}_summary.json",
                _candidate_summary(record, rank, parameter_file, state=state),
            )
        snapshot_importance = _load_json(
            output_dir / "parameter_importance.json", {}
        ) or {}
        importance_rows = [
            {"rank": rank, "parameter": key, **item}
            for rank, (key, item) in enumerate(
                sorted(
                    snapshot_importance.items(),
                    key=lambda pair: pair[1].get("weight", 0),
                    reverse=True,
                ),
                start=1,
            )
        ]
        _save_auto_workbook(
            snapshot_dir,
            rows,
            importance_rows,
            state=state,
            filename="snapshot_report.xlsx",
            best_monthly_returns=(
                (selected[0].get("stage_metrics", {}).get("final", {}) or {}).get(
                    "monthly_returns"
                )
            ),
        )
    run_manifest = _load_json(output_dir / "research_manifest.json", {}) or {}
    auto_ranges = state.get("config", {}).get("ranges", {}) or {}
    development_range = auto_ranges.get("final")
    manifest = {
        "snapshot_schema_version": 1,
        "cycle": int(cycle),
        "requested_top": top_n,
        "saved_candidates": len(selected),
        "strategy": state.get("config", {}).get(
            "strategy", "ma_strategy:ma_strategy"
        ),
        "ranking": "plateau_adjusted_score, then robust_score",
        "date_protocol": state.get("date_protocol"),
        "development_range": development_range,
        "development_end_exclusive": (
            development_range[1]
            if isinstance(development_range, (list, tuple)) and len(development_range) == 2
            else None
        ),
        "holdout_status": "development results; sealed holdout not consumed",
        "best_candidate": (
            {
                "candidate_id": selected[0].get("candidate_id"),
                "decision": selected[0].get("decision"),
                "robust_score": selected[0].get("robust_score"),
                "final_metrics": selected[0].get("stage_metrics", {}).get("final", {}),
                "monthly_performance": _monthly_performance_summary(
                    (selected[0].get("stage_metrics", {}).get("final", {}) or {}).get(
                        "monthly_returns"
                    )
                ),
            }
            if selected else None
        ),
        "colored_workbook": "snapshot_report.xlsx" if selected else None,
        "run_fingerprints": run_manifest.get("fingerprints"),
        "created_at": _timestamp_now(),
    }
    if not selected:
        publish_campaign(snapshot_dir, state, excel=True)
        shutil.copy2(snapshot_dir / 'campaign_report.xlsx', snapshot_dir / 'snapshot_report.xlsx')
        manifest['colored_workbook'] = 'snapshot_report.xlsx'
    _write_json(snapshot_dir / "manifest.json", manifest)
    return snapshot_dir


def _generate_random_audit_windows(
    earliest, recent_start, end, count, recent_ratio, seed,
    min_months=6, max_months=12,
):
    """Build deterministic, unique random windows with a recent-data quota."""
    earliest_index = _bound_index(earliest)
    recent_index = max(earliest_index, _bound_index(recent_start))
    end_index = _bound_index(end)
    if end_index <= earliest_index:
        raise ValueError("random audit end must be after its earliest allowed date")
    min_months = max(1, int(min_months))
    max_months = max(min_months, int(max_months))
    count = max(1, int(count))
    recent_target = min(count, max(0, round(count * float(recent_ratio))))
    randomizer = random.Random(seed)
    candles_per_month = round(_ACTIVE_CANDLES_PER_YEAR / 12)
    windows = []
    seen = set()

    def add_windows(tier, target, lower, upper):
        attempts = 0
        while sum(window["tier"] == tier for window in windows) < target:
            attempts += 1
            if attempts > max(1000, target * 200):
                break
            months = randomizer.randint(min_months, max_months)
            duration = months * candles_per_month
            latest_start = upper - duration
            if latest_start < lower:
                continue
            start_index = randomizer.randint(lower, latest_start)
            signature = (start_index, start_index + duration)
            if signature in seen:
                continue
            seen.add(signature)
            windows.append({
                "window_id": len(windows) + 1,
                "tier": tier,
                "months": months,
                "start": start_index,
                "end": start_index + duration,
            })

    add_windows("recent", recent_target, recent_index, end_index)
    older_target = count - len(windows)
    add_windows("historical", older_target, earliest_index, min(recent_index, end_index))
    if len(windows) < count:
        # Short datasets may not have room for the requested historical quota.
        add_windows("fallback", count - len(windows), earliest_index, end_index)
    if len(windows) < count:
        raise ValueError(
            f"could only construct {len(windows)}/{count} unique random audit windows"
        )
    randomizer.shuffle(windows)
    for index, window in enumerate(windows, start=1):
        window["window_id"] = index
        window["start_label"] = _format_range_bound(window["start"])
        window["end_label"] = _format_range_bound(window["end"])
    return windows


def _aggregate_random_audit(records, candidates, expected_windows):
    """Rank candidates by cross-window percentile, downside, and consistency."""
    candidate_map = {record["candidate_id"]: record for record in candidates}
    by_window = {}
    for record in records:
        by_window.setdefault(record["window_id"], []).append(record)
    for window_records in by_window.values():
        percentiles = _score_percentiles(
            window_records,
            score_getter=lambda item: item.get("time_normalized_score"),
        )
        for record in window_records:
            record["window_percentile"] = percentiles.get(record["candidate_id"])

    grouped = {candidate_id: [] for candidate_id in candidate_map}
    for record in records:
        grouped.setdefault(record["candidate_id"], []).append(record)
    summaries = []
    for candidate_id, candidate_records in grouped.items():
        valid = [
            record for record in candidate_records
            if record.get("window_percentile") is not None
        ]
        percentiles = sorted(record["window_percentile"] for record in valid)
        valid_ratio = len(valid) / max(1, expected_windows)
        if percentiles:
            tail_count = max(1, math.ceil(len(percentiles) * 0.10))
            median_percentile = statistics.median(percentiles)
            mean_percentile = statistics.fmean(percentiles)
            worst_decile = statistics.fmean(percentiles[:tail_count])
            positive_ratio = statistics.fmean(
                float((record.get("result") or {}).get("total_profit", 0) > 0)
                for record in valid
            )
            stability = statistics.pstdev(percentiles) if len(percentiles) > 1 else 0.0
            robust_score = 100.0 * valid_ratio * (
                0.45 * median_percentile
                + 0.25 * mean_percentile
                + 0.20 * worst_decile
                + 0.10 * positive_ratio
            ) - 10.0 * stability
        else:
            median_percentile = mean_percentile = worst_decile = None
            positive_ratio = stability = 0.0
            robust_score = -math.inf
        drawdowns = [
            _finite_number((record.get("result") or {}).get("maximum_drawdown"))
            for record in valid
        ]
        drawdowns = [value for value in drawdowns if value is not None]
        profits = [
            _finite_number((record.get("result") or {}).get("total_profit_percent"))
            for record in valid
        ]
        profits = [value for value in profits if value is not None]
        summaries.append({
            "candidate_id": candidate_id,
            "random_audit_score": robust_score,
            "valid_windows": len(valid),
            "expected_windows": expected_windows,
            "valid_ratio": valid_ratio,
            "median_percentile": median_percentile,
            "mean_percentile": mean_percentile,
            "worst_decile_percentile": worst_decile,
            "positive_window_ratio": positive_ratio,
            "percentile_std": stability,
            "median_profit_percent": statistics.median(profits) if profits else None,
            "worst_drawdown": min(drawdowns) if drawdowns else None,
            "effective_params": dict(
                candidate_map[candidate_id].get("effective_params", {})
            ),
        })
    summaries.sort(key=lambda item: item["random_audit_score"], reverse=True)
    return summaries


def _run_random_window_audit(args, candidates, output_dir, block, final_end):
    """Evaluate staged finalists on shared random periods and publish robust winner."""
    candidate_count = min(int(args.random_audit_top), len(candidates))
    if candidate_count <= 0 or int(args.random_audit_tests) <= 0:
        return None
    selected = list(candidates)[:candidate_count]
    strategy_spec = _adapter_from_args(args).identifier
    window_count = max(1, int(args.random_audit_tests) // candidate_count)
    windows = _generate_random_audit_windows(
        args.random_audit_earliest,
        args.random_audit_recent_start,
        final_end,
        window_count,
        args.random_audit_recent_ratio,
        args.seed + block * 1_000_003,
        args.random_audit_min_months,
        args.random_audit_max_months,
    )
    tasks = []
    windows_by_id = {window["window_id"]: window for window in windows}
    for window in windows:
        for candidate in selected:
            tasks.append((
                len(tasks) + 1,
                candidate["candidate_id"],
                window["window_id"],
                candidate["effective_params"],
                window["start"],
                window["end"],
                strategy_spec,
            ))
    tasks = tasks[:int(args.random_audit_tests)]
    workers = max(1, int(args.workers))
    chunksize = args.chunksize or max(1, len(tasks) // max(1, workers * 20))
    print(
        f"Random-window audit: {len(selected)} finalists x {len(windows)} windows "
        f"= {len(tasks)} tests | {args.random_audit_recent_ratio:.0%} recent target"
    )
    if workers == 1:
        evaluated = map(_evaluate_random_window_task, tasks)
        pool = None
    else:
        pool = multiprocessing.Pool(processes=workers)
        evaluated = pool.imap_unordered(
            _evaluate_random_window_task, tasks, chunksize=chunksize
        )
    records = []
    pool_terminated = False
    try:
        for completed, item in enumerate(evaluated, start=1):
            (
                test_index, candidate_id, window_id, params, start, end,
                result, duration, error,
            ) = item
            window = windows_by_id[window_id]
            audit_min_trades = (
                0 if not args.min_trades else max(
                    1,
                    math.ceil(
                        args.min_trades * window["months"]
                        / max(1, args.random_audit_max_months)
                    ),
                )
            )
            objective = _objective_score(
                result, min_trades=audit_min_trades, max_drawdown=args.max_drawdown
            )
            comparable = _time_normalized_score(objective, end - start)
            records.append({
                "test_index": test_index,
                "candidate_id": candidate_id,
                "window_id": window_id,
                "tier": window["tier"],
                "months": window["months"],
                "start": start,
                "end": end,
                "start_label": window["start_label"],
                "end_label": window["end_label"],
                "objective_score": objective if math.isfinite(objective) else None,
                "time_normalized_score": comparable,
                "minimum_trades_required": audit_min_trades,
                "duration_s": round(duration, 4),
                "error": error,
                "result": result or {},
            })
            if args.log_every and (
                completed == len(tasks) or completed % args.log_every == 0
            ):
                print(f"  Random audit: {completed:,}/{len(tasks):,} complete")
    except BaseException:
        if pool is not None:
            pool.terminate()
            pool_terminated = True
        raise
    finally:
        if pool is not None:
            if not pool_terminated:
                pool.close()
            pool.join()

    summaries = _aggregate_random_audit(records, selected, len(windows))
    output_dir = Path(output_dir)
    result_rows = []
    for record in sorted(records, key=lambda item: item["test_index"]):
        row = {key: value for key, value in record.items() if key != "result"}
        row.update({
            metric: record["result"].get(metric)
            for metric in (
                "total_profit", "total_profit_percent", "closed_trades", "win_rate",
                "maximum_drawdown", "profit_factor", "expectancy_percent",
                "calmar_ratio", "liquidations", "stronger_side", "directional_profit_gap",
                "long_profit", "long_trades", "long_win_rate", "long_profit_factor",
                "long_expectancy", "long_maximum_drawdown", "long_total_fees",
                "long_liquidations", "short_profit", "short_trades", "short_win_rate",
                "short_profit_factor", "short_expectancy", "short_maximum_drawdown",
                "short_total_fees", "short_liquidations",
            )
        })
        result_rows.append(row)
    if result_rows:
        _write_rows_atomic(
            output_dir / "random_window_results.csv", list(result_rows[0]), result_rows
        )
    summary_rows = [
        {key: value for key, value in record.items() if key != "effective_params"}
        for record in summaries
    ]
    if summary_rows:
        _write_rows_atomic(
            output_dir / "random_window_summary.csv", list(summary_rows[0]), summary_rows
        )
    window_rows = list(windows)
    _write_rows_atomic(
        output_dir / "random_windows.csv", list(window_rows[0]), window_rows
    )
    _write_json(output_dir / "random_window_summary.json", summaries)
    if summaries:
        _write_json(output_dir / "best_params.json", summaries[0]["effective_params"])
        _write_best_params_manifest(
            output_dir,
            status="final_random_window_audit",
            selection_basis="rank #1 finalist by development random-window robustness audit",
            metrics=summaries[0],
            candidate_id=summaries[0].get("candidate_id"),
        )
        # Export already-computed winner windows automatically; no duplicate
        # backtests and no separate terminal command are needed.
        if getattr(args, 'excel_top', 5000):
            from evaluate_params import summarize, write_workbook
            winner_records = [dict(record, start_date=record['start_label'],
                                   end_exclusive=record['end_label']) for record in records
                              if record['candidate_id'] == summaries[0]['candidate_id']]
            report, frame = summarize(winner_records, len(windows), args.min_trades or 0,
                                      args.max_drawdown or 40, .7, .7)
            write_workbook(output_dir / 'random_window_report.xlsx', frame, report,
                           summaries[0]['effective_params'],
                           dict(source='Auto snapshot audit (selection data)',
                                min_trades=args.min_trades or 0, max_drawdown=args.max_drawdown or 40,
                                min_positive_ratio=.7, min_pass_ratio=.7,
                                selection_warning='Winner selected using these windows; descriptive results only'),
                           winner_records)
    return summaries[0] if summaries else None


def _first_primes(count):
    primes = []
    candidate = 2
    while len(primes) < count:
        if all(candidate % prime for prime in primes if prime * prime <= candidate):
            primes.append(candidate)
        candidate += 1
    return primes


def _van_der_corput(index, base):
    value = 0.0
    denominator = 1.0
    while index:
        index, remainder = divmod(index, base)
        denominator *= base
        value += remainder / denominator
    return value


def _space_filling_candidates(grid, count, baseline, adapter, seed=42):
    """Deterministic Halton coverage for the unbiased research candidate pool."""
    generator = SmartCandidateGenerator(
        grid,
        seed=seed,
        baseline_params=baseline,
        strategy_adapter=adapter,
    )
    keys = tuple(grid)
    primes = _first_primes(len(keys))
    candidates = []
    seen = set()
    baseline_candidate = generator._canonicalize(dict(generator.baseline))
    if is_valid_candidate(baseline_candidate, adapter):
        candidates.append(baseline_candidate)
        seen.add(_candidate_signature(baseline_candidate, keys))
    index = max(1, int(seed))
    maximum = max(1000, count * 100)
    attempts = 0
    while len(candidates) < count and attempts < maximum:
        attempts += 1
        candidate = {}
        for dimension, key in enumerate(keys):
            values = list(grid[key])
            fraction = _van_der_corput(index, primes[dimension])
            position = min(len(values) - 1, int(fraction * len(values)))
            candidate[key] = values[position]
        index += 1
        candidate = generator._canonicalize(candidate)
        signature = _candidate_signature(candidate, keys)
        if signature in seen or not is_valid_candidate(candidate, adapter):
            continue
        seen.add(signature)
        candidates.append(candidate)
    return candidates


def _load_research_seed_candidates(source, grid, baseline, adapter):
    """Load reusable Auto/snapshot winners into a fixed research candidate pool."""
    if not source:
        return []
    path = Path(source)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("candidates"), list):
        rows = payload["candidates"]
    elif isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = [payload]
    else:
        raise ValueError("--research-seeds must contain a parameter object or list")
    generator = SmartCandidateGenerator(
        grid,
        baseline_params=baseline,
        strategy_adapter=adapter,
    )
    seeds = []
    seen = set()
    keys = tuple(grid)
    for row in rows:
        if not isinstance(row, dict):
            continue
        source_params = (
            row.get("effective_params")
            or row.get("params")
            or row
        )
        if not isinstance(source_params, dict):
            continue
        candidate = {
            key: source_params.get(key, generator.baseline[key])
            for key in keys
        }
        candidate = generator._canonicalize(candidate)
        signature = _candidate_signature(candidate, keys)
        if signature in seen or not is_valid_candidate(candidate, adapter):
            continue
        seen.add(signature)
        seeds.append(candidate)
    if not seeds:
        raise ValueError(
            f"no compatible candidates were found in research seed file: {path}"
        )
    return seeds


def _research_seed_provenance(source, folds, strategy_identifier):
    """Verify that performance-selected seeds stopped before the first OOS bar."""
    first_oos_start = folds[0]["test"][0] if folds else None
    if not source:
        return {
            "status": "no_performance_selected_seeds",
            "safe_for_oos_claim": True,
            "source": None,
            "manifest": None,
            "seed_development_end": None,
            "seed_development_end_index": None,
            "first_oos_start_index": first_oos_start,
            "strategy_matches": True,
        }

    source_path = Path(source)
    manifest_candidates = [
        source_path.parent / "manifest.json",
        source_path.parent / "research_manifest.json",
    ]
    # Cycle snapshots live under <campaign>/snapshots/cycles_NNNNNN/.
    if len(source_path.parents) >= 3:
        manifest_candidates.append(source_path.parents[2] / "research_manifest.json")
    manifest_path = next((path for path in manifest_candidates if path.is_file()), None)
    manifest = _load_json(manifest_path, {}) if manifest_path else {}
    manifest = manifest or {}
    manifest_strategy = manifest.get("strategy")
    strategy_matches = (
        manifest_strategy in (None, strategy_identifier)
    )
    development_range = manifest.get("development_range")
    if not development_range:
        resolved_config = manifest.get("resolved_config", {}) or {}
        development_range = (
            (resolved_config.get("ranges", {}) or {}).get("final")
            or (resolved_config.get("auto_ranges", {}) or {}).get("final")
        )
    development_end = manifest.get("development_end_exclusive")
    if development_end is None and isinstance(development_range, (list, tuple)):
        if len(development_range) == 2:
            development_end = development_range[1]

    development_end_index = None
    if development_end is not None:
        try:
            development_end_index = _bound_index(development_end)
        except (IndexError, OSError, TypeError, ValueError):
            development_end_index = None

    if not strategy_matches:
        status = "strategy_mismatch"
        safe = False
    elif development_end_index is None or first_oos_start is None:
        status = "unverifiable_seed_history"
        safe = False
    elif development_end_index <= first_oos_start:
        status = "verified_pre_oos"
        safe = True
    else:
        status = "overlaps_reporting_oos"
        safe = False
    return {
        "status": status,
        "safe_for_oos_claim": safe,
        "source": str(source_path),
        "manifest": str(manifest_path) if manifest_path else None,
        "manifest_strategy": manifest_strategy,
        "seed_development_range": development_range,
        "seed_development_end": development_end,
        "seed_development_end_index": development_end_index,
        "first_oos_start_index": first_oos_start,
        "strategy_matches": strategy_matches,
    }


def _nested_walk_forward_ranges(
    start,
    end,
    *,
    train_months=30,
    validation_months=6,
    test_months=6,
    step_months=6,
    rolling=False,
    purge_candles=0,
):
    """Build chronological train -> validation -> untouched OOS folds."""
    start_index = _bound_index(start)
    end_index = _bound_index(end)
    candles_per_month = round(_ACTIVE_CANDLES_PER_YEAR / 12)
    train_width = max(1, round(float(train_months) * candles_per_month))
    validation_width = max(1, round(float(validation_months) * candles_per_month))
    test_width = max(1, round(float(test_months) * candles_per_month))
    step_width = max(1, round(float(step_months) * candles_per_month))
    purge_candles = max(0, int(purge_candles))
    test_start = start_index + train_width + validation_width + 2 * purge_candles
    folds = []
    while test_start + test_width <= end_index:
        validation_end = test_start - purge_candles
        validation_start = validation_end - validation_width
        train_end = validation_start - purge_candles
        train_start = max(start_index, train_end - train_width) if rolling else start_index
        if train_start < train_end and validation_start < validation_end:
            folds.append({
                "fold": len(folds) + 1,
                "train": [train_start, train_end],
                "validation": [validation_start, validation_end],
                "test": [test_start, test_start + test_width],
                "purge_candles": purge_candles,
            })
        test_start += step_width
    return folds


def _evaluate_research_candidates(
    args, candidates, start, end, base_tune, *, research=False
):
    if not candidates:
        return []
    adapter = _adapter_from_args(args)
    strategy_spec = adapter.identifier
    workers = min(max(1, int(args.workers)), len(candidates))
    chunksize = args.chunksize or max(1, len(candidates) // max(1, workers * 8))
    tasks = [(candidate["candidate_id"], candidate["params"]) for candidate in candidates]
    fixed_warmup = _maximum_candidate_warmup(
        strategy_spec, base_tune, tasks, enabled=True
    )
    if workers > 1:
        pool = multiprocessing.Pool(
            workers,
            initializer=_init_worker,
            initargs=(
                _parse_bound(start), _parse_bound(end), base_tune, True, True,
                strategy_spec, research, fixed_warmup,
            ),
        )
        try:
            evaluated = list(pool.imap_unordered(_evaluate_task, tasks, chunksize=chunksize))
        except BaseException:
            pool.terminate()
            pool.join()
            raise
        else:
            pool.close()
            pool.join()
    else:
        _init_worker(
            _parse_bound(start), _parse_bound(end), base_tune,
            strategy_spec=strategy_spec, research=research,
            indicator_warmup_candles=fixed_warmup,
        )
        evaluated = [
            _evaluate_candidate(
                candidate_id, params, base_tune, _parse_bound(start), _parse_bound(end),
                strategy_spec=strategy_spec, research=research,
                indicator_warmup_candles=fixed_warmup,
            )
            for candidate_id, params in tasks
        ]
    candidate_map = {candidate["candidate_id"]: candidate for candidate in candidates}
    range_candles = _range_candle_count(start, end)
    records = []
    for candidate_id, params, result, duration, error in evaluated:
        objective = None
        if not error:
            research_min_trades = int(
                getattr(args, "min_fold_trades", 0) or 0
            )
            drawdown_limits = [
                value
                for value in (
                    getattr(args, "max_drawdown", None),
                    getattr(args, "research_max_drawdown", None),
                )
                if value is not None
            ]
            value = _objective_score(
                result,
                min_trades=max(
                    int(getattr(args, "min_trades", 0) or 0),
                    research_min_trades,
                ),
                max_drawdown=min(drawdown_limits) if drawdown_limits else None,
            )
            objective = value if math.isfinite(value) else None
        records.append({
            "candidate_id": candidate_id,
            "params": candidate_map[candidate_id]["params"],
            "result": dict(result or {}),
            "objective_score": objective,
            "range_candles": range_candles,
            "duration": duration,
            "error": error,
        })
    return records


def _research_selection_records(
    train_records, validation_records, parameter_grid=None
):
    train_percentiles = _score_percentiles(
        train_records, score_getter=lambda row: row.get("objective_score")
    )
    validation_percentiles = _score_percentiles(
        validation_records, score_getter=lambda row: row.get("objective_score")
    )
    train_map = {record["candidate_id"]: record for record in train_records}
    selected = []
    for record in validation_records:
        candidate_id = record["candidate_id"]
        if candidate_id not in train_percentiles or candidate_id not in validation_percentiles:
            continue
        train_rank = train_percentiles[candidate_id]
        validation_rank = validation_percentiles[candidate_id]
        gap = max(0.0, train_rank - validation_rank)
        robust = (
            0.30 * train_rank
            + 0.50 * validation_rank
            + 0.20 * min(train_rank, validation_rank)
            - 0.15 * gap
        )
        selected.append({
            "candidate_id": candidate_id,
            "params": record["params"],
            "robust_score": robust,
            "train_percentile": train_rank,
            "validation_percentile": validation_rank,
            "training_result": train_map[candidate_id]["result"],
            "validation_result": record["result"],
        })
    if parameter_grid and selected:
        plateau = parameter_plateau_scores(
            selected, parameter_grid, score_key="robust_score"
        )
        for record in selected:
            record.update(plateau.get(record["candidate_id"], {}))
    selected.sort(
        key=lambda row: row.get("plateau_adjusted_score", row["robust_score"]),
        reverse=True,
    )
    return selected


def _parameter_stability_report(folds, parameter_grid):
    """Summarize how often fold selection changes each optimized parameter."""
    selections = [
        fold.get("selected_params", {})
        for fold in folds
        if fold.get("selected_params")
    ]
    parameters = {}
    for key, configured_values in parameter_grid.items():
        values = [selection.get(key) for selection in selections if key in selection]
        if not values:
            continue
        counts = {}
        originals = {}
        for value in values:
            token = json.dumps(value, sort_keys=True, ensure_ascii=False)
            counts[token] = counts.get(token, 0) + 1
            originals[token] = value
        ordered = sorted(
            counts,
            key=lambda token: (-counts[token], token),
        )
        mode_token = ordered[0]
        switches = sum(left != right for left, right in zip(values, values[1:]))
        positions = [
            grid_ordinal_position(configured_values, value)
            for value in values
        ]
        finite_positions = [value for value in positions if value is not None]
        normalized_spread = None
        if finite_positions and len(configured_values) > 1:
            normalized_spread = statistics.pstdev(finite_positions) / (
                len(configured_values) - 1
            )
        parameters[key] = {
            "selection_count": len(values),
            "consensus_value": originals[mode_token],
            "consensus_ratio": counts[mode_token] / len(values),
            "unique_values": len(counts),
            "switches_between_folds": switches,
            "normalized_grid_spread": normalized_spread,
            "value_counts": [
                {"value": originals[token], "count": counts[token]}
                for token in ordered
            ],
        }
    consensus = [record["consensus_ratio"] for record in parameters.values()]
    spreads = [
        record["normalized_grid_spread"]
        for record in parameters.values()
        if record["normalized_grid_spread"] is not None
    ]
    mutable_records = [
        parameters[key]
        for key, configured_values in parameter_grid.items()
        if len(configured_values) > 1 and key in parameters
    ]
    mutable_consensus = [
        record["consensus_ratio"] for record in mutable_records
    ]
    mutable_spreads = [
        record["normalized_grid_spread"]
        for record in mutable_records
        if record["normalized_grid_spread"] is not None
    ]
    return {
        "selected_fold_count": len(selections),
        "mean_parameter_consensus": statistics.fmean(consensus) if consensus else None,
        "mean_normalized_grid_spread": statistics.fmean(spreads) if spreads else None,
        "mutable_parameter_count": len(mutable_records),
        "mean_mutable_parameter_consensus": (
            statistics.fmean(mutable_consensus) if mutable_consensus else None
        ),
        "mean_mutable_normalized_grid_spread": (
            statistics.fmean(mutable_spreads) if mutable_spreads else None
        ),
        "parameters": parameters,
    }


def _monthly_sharpe(result):
    returns = [
        float(value) for value in (result or {}).get("monthly_returns", [])
        if _finite_number(value) is not None
    ]
    if len(returns) < 2:
        return None
    deviation = statistics.stdev(returns)
    return statistics.fmean(returns) / deviation if deviation > 0 else None


def run_nested_walk_forward(args, grid=None):
    """Run nested chronological selection and publish a stitched OOS report."""
    profiles = _profiles_from_args(args)
    profile = args.profile if getattr(args, "profile", None) in profiles else next(iter(profiles))
    grid = profiles[profile] if grid is None else grid
    base_tune, base_description = _load_base_tune(args)
    adapter = _adapter_from_args(args)
    resolved_end = (
        _latest_market_end() if args.wf_end == "latest" else args.wf_end
    )
    folds = _nested_walk_forward_ranges(
        args.wf_start,
        resolved_end,
        train_months=args.wf_train_months,
        validation_months=args.wf_validation_months,
        test_months=args.wf_test_months,
        step_months=args.wf_step_months,
        rolling=args.wf_rolling,
        purge_candles=args.wf_purge_candles,
    )
    if not folds:
        raise ValueError("walk-forward range is too short for the requested windows")
    output_dir = Path(args.output_dir) / "walk_forward_research"
    output_dir.mkdir(parents=True, exist_ok=True)
    requested_candidate_count = max(1, int(args.research_tests))
    seed_params = _load_research_seed_candidates(
        getattr(args, "research_seeds", None),
        grid,
        base_tune,
        adapter,
    )
    seed_provenance = _research_seed_provenance(
        getattr(args, "research_seeds", None), folds, adapter.identifier
    )
    if seed_params and seed_provenance["status"] == "strategy_mismatch":
        raise ValueError(
            "research seed snapshot belongs to another strategy; choose a matching "
            "snapshot instead of mixing strategy parameter histories"
        )
    if seed_params and not seed_provenance["safe_for_oos_claim"]:
        if not getattr(args, "allow_research_seed_overlap", False):
            raise ValueError(
                "research seed history is not verified to end before the first OOS "
                f"window ({seed_provenance['status']}); use a pre-OOS snapshot or "
                "--allow-research-seed-overlap for a diagnostic run that can never "
                "be accepted"
            )
        print(
            "WARNING: research seeds are not independent of OOS; this run is "
            "marked contaminated and cannot pass all acceptance gates."
        )
    halton_params = _space_filling_candidates(
        grid,
        min(requested_candidate_count, grid_size(grid)),
        base_tune,
        adapter,
        seed=args.seed,
    )
    candidate_params = []
    seen_candidate_params = set()
    for params in [*seed_params, *halton_params]:
        signature = _candidate_signature(params, tuple(grid))
        if signature in seen_candidate_params:
            continue
        seen_candidate_params.add(signature)
        candidate_params.append(params)
        if len(candidate_params) >= requested_candidate_count:
            break
    seed_signatures = {
        _candidate_signature(params, tuple(grid)) for params in seed_params
    }
    candidates = [
        {
            "candidate_id": f"research-{index:06d}",
            "params": params,
            "source": (
                "snapshot_seed"
                if _candidate_signature(params, tuple(grid)) in seed_signatures
                else "halton"
            ),
        }
        for index, params in enumerate(candidate_params, 1)
    ]
    if not candidates:
        raise ValueError("research candidate pool is empty after validation")
    used_seed_count = sum(
        candidate["source"] == "snapshot_seed" for candidate in candidates
    )
    preflight_manifest = _load_json(
        Path(args.output_dir) / "research_manifest.json", {}
    ) or {}
    environment_fingerprints = preflight_manifest.get("fingerprints", {})
    plan = {
        "strategy": adapter.identifier,
        "profile": profile,
        "base_source": base_description,
        "base_tune": base_tune,
        "parameter_grid": {key: list(values) for key, values in grid.items()},
        "candidate_count": len(candidates),
        "candidate_pool_sha256": fingerprint_config(candidates),
        "candidate_generation": (
            "snapshot seeds followed by deterministic Halton space filling"
            if seed_params
            else "deterministic Halton space filling"
        ),
        "research_seed_source": getattr(args, "research_seeds", None),
        "research_seed_sha256": (
            fingerprint_config(seed_params) if seed_params else None
        ),
        "research_seed_loaded_count": len(seed_params),
        "research_seed_count": used_seed_count,
        "research_seed_provenance": seed_provenance,
        "environment_fingerprints": {
            "data_sha256": environment_fingerprints.get("data_sha256"),
            "code_sha256": environment_fingerprints.get("code_sha256"),
        },
        "folds": folds,
        "selection_feedback": "train and inner validation only; OOS tests never feed selection",
        "settings": {
            "research_tests": args.research_tests,
            "research_validation_top": args.research_validation_top,
            "research_pbo_candidates": args.research_pbo_candidates,
            "min_fold_trades": args.min_fold_trades,
            "min_total_oos_trades": args.min_total_oos_trades,
            "max_oos_liquidations": args.max_oos_liquidations,
            "research_max_drawdown": args.research_max_drawdown,
            "max_oos_drawdown": args.max_oos_drawdown,
            "min_oos_folds": args.min_oos_folds,
            "min_positive_fold_ratio": args.min_positive_fold_ratio,
            "min_dsr_probability": args.min_dsr_probability,
            "max_pbo": args.max_pbo,
            "min_parameter_consensus": args.min_parameter_consensus,
            "max_parameter_spread": args.max_parameter_spread,
            "bootstrap_samples": args.bootstrap_samples,
            "bootstrap_confidence": args.bootstrap_confidence,
            "require_positive_ci": args.require_positive_ci,
            "allow_research_seed_overlap": bool(
                getattr(args, "allow_research_seed_overlap", False)
            ),
            "seed": args.seed,
        },
    }
    plan["run_fingerprint"] = fingerprint_config(plan)
    plan_path = output_dir / "walk_forward_plan.json"
    saved_plan = _load_json(plan_path)
    if saved_plan is not None:
        if not getattr(args, "resume", False):
            raise FileExistsError(
                f"walk-forward research already exists in {output_dir}; use "
                "--resume for the identical plan or choose a new --output-dir"
            )
        if saved_plan.get("run_fingerprint") != plan["run_fingerprint"]:
            raise ValueError(
                "walk-forward checkpoint settings differ from this command; "
                "resume with the original strategy, grid, folds, baseline, and gates"
            )
    elif getattr(args, "resume", False):
        raise FileNotFoundError(
            f"walk-forward checkpoint not found: {plan_path}"
        )
    else:
        _write_json(plan_path, plan)
        _write_json(output_dir / "candidate_pool.json", candidates)
    checkpoint_dir = output_dir / "checkpoints"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / "research_state.json"
    _write_json(state_path, {
        "status": "running",
        "run_fingerprint": plan["run_fingerprint"],
        "updated_at": _timestamp_now(),
    })

    fold_summaries = []
    pbo_ids = {candidate["candidate_id"] for candidate in candidates[:args.research_pbo_candidates]}
    pbo_by_candidate = {candidate_id: [] for candidate_id in pbo_ids}
    validation_sharpes = {}
    winner_counts = {}
    winner_selection_scores = {}
    trial_ledger = {
        "unique_candidate_configurations": len(candidates),
        "fold_count": len(folds),
        "train_evaluations": 0,
        "inner_validation_evaluations": 0,
        "reporting_only_oos_evaluations": 0,
        "failed_evaluations": 0,
        "oos_evaluations_used_for_selection": 0,
    }
    checkpoint_counter_keys = (
        "train_evaluations",
        "inner_validation_evaluations",
        "reporting_only_oos_evaluations",
        "failed_evaluations",
    )

    def save_fold_checkpoint(
        fold, summary, fold_pbo_values, fold_validation_sharpes, before
    ):
        trial_delta = {
            key: trial_ledger[key] - before[key]
            for key in checkpoint_counter_keys
        }
        payload = {
            "run_fingerprint": plan["run_fingerprint"],
            "fold": fold["fold"],
            "summary": summary,
            "pbo_values": fold_pbo_values,
            "validation_sharpes": fold_validation_sharpes,
            "trial_delta": trial_delta,
            "completed_at": _timestamp_now(),
        }
        _write_json(
            checkpoint_dir / f"fold_{fold['fold']:02d}.json", payload
        )
        _write_json(state_path, {
            "status": "running",
            "run_fingerprint": plan["run_fingerprint"],
            "completed_folds": len(fold_summaries),
            "updated_at": _timestamp_now(),
        })

    for fold in folds:
        fold_checkpoint_path = checkpoint_dir / f"fold_{fold['fold']:02d}.json"
        if getattr(args, "resume", False) and fold_checkpoint_path.is_file():
            checkpoint = _load_json(fold_checkpoint_path)
            if checkpoint.get("run_fingerprint") != plan["run_fingerprint"]:
                raise ValueError(
                    f"fold {fold['fold']} checkpoint belongs to another research plan"
                )
            summary = checkpoint["summary"]
            fold_summaries.append(summary)
            for candidate_id in pbo_ids:
                value = checkpoint.get("pbo_values", {}).get(candidate_id)
                pbo_by_candidate[candidate_id].append(
                    float(value) if _finite_number(value) is not None else math.nan
                )
            for candidate_id, value in checkpoint.get(
                "validation_sharpes", {}
            ).items():
                if _finite_number(value) is not None:
                    validation_sharpes.setdefault(candidate_id, []).append(
                        float(value)
                    )
            selected_id = summary.get("selected_candidate_id")
            if selected_id:
                winner_counts[selected_id] = winner_counts.get(selected_id, 0) + 1
                selection_score = _finite_number(summary.get("selection_score"))
                if selection_score is not None:
                    winner_selection_scores.setdefault(selected_id, []).append(
                        selection_score
                    )
            for key in checkpoint_counter_keys:
                trial_ledger[key] += int(
                    checkpoint.get("trial_delta", {}).get(key, 0) or 0
                )
            print(
                f"Nested WF fold {fold['fold']}/{len(folds)} restored from checkpoint"
            )
            continue
        trial_before = {
            key: trial_ledger[key] for key in checkpoint_counter_keys
        }
        fold_pbo_values = {}
        fold_validation_sharpes = {}
        print(
            f"Nested WF fold {fold['fold']}/{len(folds)} | "
            f"train {fold['train'][0]}->{fold['train'][1]} | "
            f"validation {fold['validation'][0]}->{fold['validation'][1]} | "
            f"OOS {fold['test'][0]}->{fold['test'][1]}"
        )
        train_records = _evaluate_research_candidates(
            args, candidates, *fold["train"], base_tune, research=False
        )
        trial_ledger["train_evaluations"] += len(train_records)
        trial_ledger["failed_evaluations"] += sum(
            bool(record.get("error")) for record in train_records
        )
        train_records.sort(
            key=lambda row: row["objective_score"] if row["objective_score"] is not None else -math.inf,
            reverse=True,
        )
        top_ids = {
            record["candidate_id"]
            for record in train_records[: int(args.research_validation_top)]
        } | pbo_ids
        validation_candidates = [candidate for candidate in candidates if candidate["candidate_id"] in top_ids]
        validation_records = _evaluate_research_candidates(
            args, validation_candidates, *fold["validation"], base_tune, research=True
        )
        trial_ledger["inner_validation_evaluations"] += len(validation_records)
        trial_ledger["failed_evaluations"] += sum(
            bool(record.get("error")) for record in validation_records
        )
        for record in validation_records:
            candidate_id = record["candidate_id"]
            if candidate_id in pbo_ids:
                value = (record.get("result") or {}).get("total_profit_percent")
                value = float(value) if _finite_number(value) is not None else math.nan
                pbo_by_candidate[candidate_id].append(value)
                fold_pbo_values[candidate_id] = value
            sharpe = _monthly_sharpe(record.get("result"))
            if sharpe is not None:
                validation_sharpes.setdefault(candidate_id, []).append(sharpe)
                fold_validation_sharpes[candidate_id] = sharpe
        ranked = _research_selection_records(
            train_records, validation_records, parameter_grid=grid
        )
        if not ranked:
            summary = {**fold, "status": "no_qualified_candidate"}
            fold_summaries.append(summary)
            _write_json(output_dir / f"fold_{fold['fold']:02d}.json", summary)
            save_fold_checkpoint(
                fold,
                summary,
                fold_pbo_values,
                fold_validation_sharpes,
                trial_before,
            )
            continue
        winner = ranked[0]
        winner_counts[winner["candidate_id"]] = winner_counts.get(winner["candidate_id"], 0) + 1
        winner_selection_scores.setdefault(winner["candidate_id"], []).append(
            float(winner["robust_score"])
        )
        test_record = _evaluate_research_candidates(
            args,
            [{"candidate_id": winner["candidate_id"], "params": winner["params"]}],
            *fold["test"],
            base_tune,
            research=True,
        )[0]
        trial_ledger["reporting_only_oos_evaluations"] += 1
        trial_ledger["failed_evaluations"] += bool(test_record.get("error"))
        summary = {
            **fold,
            "status": "complete" if test_record.get("objective_score") is not None else "oos_failed_gates",
            "selected_candidate_id": winner["candidate_id"],
            "selected_params": {**base_tune, **winner["params"]},
            "selection_score": winner["robust_score"],
            "plateau_adjusted_selection_score": winner.get(
                "plateau_adjusted_score", winner["robust_score"]
            ),
            "plateau_stability": winner.get("plateau_stability"),
            "plateau_neighbour_count": winner.get("neighbour_count", 0),
            "plateau_neighbour_method": winner.get("neighbour_method"),
            "plateau_median_grid_distance": winner.get("median_grid_distance"),
            "train_percentile": winner["train_percentile"],
            "validation_percentile": winner["validation_percentile"],
            "training_result": winner["training_result"],
            "validation_result": winner["validation_result"],
            "oos_result": test_record["result"],
            "oos_objective_score": test_record["objective_score"],
            "oos_error": test_record.get("error"),
            "oos_duration_seconds": test_record.get("duration"),
        }
        fold_summaries.append(summary)
        _write_json(output_dir / f"fold_{fold['fold']:02d}.json", summary)
        save_fold_checkpoint(
            fold,
            summary,
            fold_pbo_values,
            fold_validation_sharpes,
            trial_before,
        )

    completed = [
        fold for fold in fold_summaries
        if fold.get("oos_result") and not fold.get("oos_error")
    ]
    stitched_monthly = [
        float(value)
        for fold in completed
        for value in fold["oos_result"].get("monthly_returns", [])
        if _finite_number(value) is not None
    ]
    if not stitched_monthly:
        stitched_monthly = [
            float(fold["oos_result"].get("total_profit_percent", 0.0)) / 100.0
            for fold in completed
        ]
        periods_per_year = 12.0 / max(1.0, float(args.wf_test_months))
    else:
        periods_per_year = 12.0
    oos_metrics = performance_from_returns(stitched_monthly, periods_per_year)
    bootstrap = moving_block_bootstrap_ci(
        stitched_monthly,
        samples=args.bootstrap_samples,
        confidence=args.bootstrap_confidence,
        seed=args.seed,
    )
    observed_monthly_sharpe = _monthly_sharpe({"monthly_returns": stitched_monthly})
    # One aggregate validation Sharpe per unique configuration is a more honest
    # multiple-testing count than treating every fold repeat as a new strategy.
    trial_sharpes = [
        statistics.fmean(values)
        for values in validation_sharpes.values()
        if values
    ]
    dsr = (
        deflated_sharpe_ratio(
            observed_monthly_sharpe,
            len(stitched_monthly),
            trial_sharpes,
            skewness=(
                float(oos_metrics["skewness"])
                if _finite_number(oos_metrics.get("skewness")) is not None
                else 0.0
            ),
            kurtosis=(
                float(oos_metrics["kurtosis"])
                if _finite_number(oos_metrics.get("kurtosis")) is not None
                else 3.0
            ),
        )
        if observed_monthly_sharpe is not None
        else {"deflated_sharpe_probability": None, "trial_count": len(trial_sharpes)}
    )
    pbo_rows = [
        values for values in pbo_by_candidate.values() if len(values) == len(folds)
    ]
    # CSCV requires an even block count. The newest unmatched fold is withheld.
    if pbo_rows and len(folds) % 2:
        pbo_rows = [values[:-1] for values in pbo_rows]
    pbo = probability_of_backtest_overfitting(pbo_rows) if pbo_rows else {
        "pbo": None, "combinations": 0, "configurations": 0, "blocks": 0
    }
    positive_ratio = (
        statistics.fmean(
            float(fold["oos_result"].get("total_profit_percent", 0.0) > 0)
            for fold in completed
        )
        if completed else 0.0
    )
    fold_returns = [
        float(fold["oos_result"].get("total_profit_percent", 0.0))
        for fold in completed
    ]
    total_oos_trades = sum(
        int(fold["oos_result"].get("closed_trades", 0) or 0)
        for fold in completed
    )
    total_oos_liquidations = sum(
        int(fold["oos_result"].get("liquidations", 0) or 0)
        for fold in completed
    )
    qualified_oos = [fold for fold in completed if fold.get("status") == "complete"]
    trial_ledger.update({
        "total_backtest_evaluations": (
            trial_ledger["train_evaluations"]
            + trial_ledger["inner_validation_evaluations"]
            + trial_ledger["reporting_only_oos_evaluations"]
        ),
        "effective_dsr_trials": len(trial_sharpes),
        "pbo_configuration_count": len(pbo_rows),
        "completed_oos_evaluations": len(completed),
        "qualified_oos_evaluations": len(qualified_oos),
        "selection_sources": ["train", "inner_validation"],
        "reporting_only_sources": ["oos"],
    })
    parameter_stability = _parameter_stability_report(completed, grid)
    gates = {
        "research_seed_provenance": bool(
            seed_provenance.get("safe_for_oos_claim")
        ),
        "minimum_completed_folds": len(completed) >= args.min_oos_folds,
        "minimum_qualified_folds": len(qualified_oos) >= args.min_oos_folds,
        "minimum_total_oos_trades": (
            total_oos_trades >= args.min_total_oos_trades
        ),
        "maximum_oos_liquidations": (
            total_oos_liquidations <= args.max_oos_liquidations
        ),
        "positive_fold_ratio": positive_ratio >= args.min_positive_fold_ratio,
        "maximum_stitched_oos_drawdown": (
            oos_metrics.get("maximum_drawdown") is not None
            and abs(float(oos_metrics["maximum_drawdown"])) * 100.0
            <= args.max_oos_drawdown
        ),
        "bootstrap_mean_lower_positive": (
            bootstrap.get("lower") is not None and bootstrap["lower"] > 0
        ),
        "deflated_sharpe": (
            dsr.get("deflated_sharpe_probability") is not None
            and dsr["deflated_sharpe_probability"] >= args.min_dsr_probability
        ),
        "pbo": pbo.get("pbo") is not None and pbo["pbo"] <= args.max_pbo,
        "parameter_consensus": (
            parameter_stability["mutable_parameter_count"] == 0
            or (
                parameter_stability["mean_mutable_parameter_consensus"] is not None
                and parameter_stability["mean_mutable_parameter_consensus"]
                >= args.min_parameter_consensus
            )
        ),
        "parameter_spread": (
            parameter_stability["mutable_parameter_count"] == 0
            or (
                parameter_stability["mean_mutable_normalized_grid_spread"] is not None
                and parameter_stability["mean_mutable_normalized_grid_spread"]
                <= args.max_parameter_spread
            )
        ),
    }
    if not args.require_positive_ci:
        gates["bootstrap_mean_lower_positive"] = True
    recommendation_id = max(
        winner_counts,
        key=lambda candidate_id: (
            winner_counts[candidate_id],
            statistics.median(winner_selection_scores.get(candidate_id, [-math.inf])),
            -int(candidate_id.rsplit("-", 1)[-1]),
        ),
    ) if winner_counts else None
    recommendation = next(
        (candidate for candidate in candidates if candidate["candidate_id"] == recommendation_id),
        None,
    )
    recommended_params = (
        _freeze_strategy_tune(
            adapter, {**base_tune, **recommendation["params"]}
        )
        if recommendation
        else None
    )
    passed_gate_count = sum(bool(value) for value in gates.values())
    if recommendation and all(gates.values()):
        research_decision = "ACCEPT"
    elif (
        recommendation
        and passed_gate_count >= math.ceil(0.70 * len(gates))
        and gates.get("minimum_completed_folds")
        and gates.get("maximum_oos_liquidations")
    ):
        research_decision = "WATCH"
    else:
        research_decision = "REJECT"
    failed_gates = [name for name, passed in gates.items() if not passed]
    report = {
        "protocol": "nested chronological walk-forward",
        "date_protocol": dict(getattr(args, "_date_protocol", {}) or {}),
        "strategy": adapter.identifier,
        "fold_count": len(folds),
        "completed_oos_folds": len(completed),
        "qualified_oos_folds": len(qualified_oos),
        "positive_oos_fold_ratio": positive_ratio,
        "total_oos_trades": total_oos_trades,
        "total_oos_liquidations": total_oos_liquidations,
        "oos_fold_return_percent": {
            "values": fold_returns,
            "median": statistics.median(fold_returns) if fold_returns else None,
            "worst": min(fold_returns) if fold_returns else None,
            "best": max(fold_returns) if fold_returns else None,
        },
        "oos_metrics": oos_metrics,
        "oos_bootstrap": bootstrap,
        "deflated_sharpe": dsr,
        "probability_of_backtest_overfitting": pbo,
        "research_seed_provenance": seed_provenance,
        "trial_ledger": trial_ledger,
        "parameter_stability": parameter_stability,
        "acceptance_gates": gates,
        "accepted": all(gates.values()),
        "decision": research_decision,
        "decision_reasons": (
            ["all Research acceptance gates passed"]
            if research_decision == "ACCEPT"
            else [f"failed gate: {name}" for name in failed_gates]
        ),
        "holdout_ready": bool(recommendation and all(gates.values())),
        "recommended_candidate_id": recommendation_id,
        "recommended_candidate_source": (
            recommendation.get("source") if recommendation else None
        ),
        "recommended_selection_frequency": (
            winner_counts.get(recommendation_id, 0) if recommendation_id else 0
        ),
        "recommended_median_selection_score": (
            statistics.median(winner_selection_scores[recommendation_id])
            if recommendation_id and winner_selection_scores.get(recommendation_id)
            else None
        ),
        "recommended_params": recommended_params,
        "folds": fold_summaries,
        "warning": "OOS results are reporting-only and were not used to select fold winners.",
    }
    _write_json(output_dir / "walk_forward_report.json", report)
    _write_json(output_dir / "trial_ledger.json", trial_ledger)
    _write_json(output_dir / "parameter_stability.json", parameter_stability)
    _write_json(output_dir / "research_decision.json", {
        "decision": research_decision,
        "decision_scope": (
            "Research validation; ACCEPT permits one sealed Holdout test, not live trading"
        ),
        "decision_reasons": report["decision_reasons"],
        "accepted": report["accepted"],
        "holdout_ready": report["holdout_ready"],
        "candidate_params": (
            str(output_dir / "walk_forward_candidate_params.json")
            if recommendation
            else None
        ),
        "recommended_params": (
            str(output_dir / "walk_forward_recommended_params.json")
            if report["holdout_ready"]
            else None
        ),
        "failed_gates": failed_gates,
    })
    research_ranked = sorted(
        (candidate for candidate in candidates if winner_counts.get(candidate["candidate_id"], 0)),
        key=lambda candidate: (
            winner_counts[candidate["candidate_id"]],
            statistics.median(
                winner_selection_scores.get(candidate["candidate_id"], [-math.inf])
            ),
        ),
        reverse=True,
    )
    research_candidates_dir = output_dir / "candidates"
    research_candidates_dir.mkdir(parents=True, exist_ok=True)
    research_rows = []
    research_catalog = []
    for rank, candidate in enumerate(research_ranked, 1):
        candidate_id = candidate["candidate_id"]
        effective_params = _freeze_strategy_tune(
            adapter, {**base_tune, **candidate["params"]}
        )
        parameter_file = Path("candidates") / f"research_rank_{rank:03d}_params.json"
        _write_json(output_dir / parameter_file, effective_params)
        candidate_decision = (
            research_decision if candidate_id == recommendation_id else "WATCH"
        )
        candidate_reasons = (
            report["decision_reasons"]
            if candidate_id == recommendation_id
            else ["selected in fewer train/validation folds than the recommendation"]
        )
        summary = {
            "rank": rank,
            "candidate_id": candidate_id,
            "decision": candidate_decision,
            "decision_reasons": candidate_reasons,
            "selection_frequency": winner_counts[candidate_id],
            "median_selection_score": statistics.median(
                winner_selection_scores.get(candidate_id, [-math.inf])
            ),
            "source": candidate.get("source"),
            "parameter_file": str(parameter_file).replace("\\", "/"),
            "effective_params": effective_params,
        }
        research_catalog.append(summary)
        research_rows.append({
            "rank": rank,
            "decision": candidate_decision,
            "decision_reasons": "; ".join(candidate_reasons),
            "selection_frequency": summary["selection_frequency"],
            "median_selection_score": summary["median_selection_score"],
            "source": summary["source"],
            "candidate_id": candidate_id,
            "parameter_file": summary["parameter_file"],
            **effective_params,
        })
    _write_json(output_dir / "research_candidate_catalog.json", research_catalog)
    if research_rows:
        _write_rows_atomic(
            output_dir / "research_candidate_catalog.csv",
            list(research_rows[0]), research_rows,
        )
    if recommendation:
        _write_json(
            output_dir / "walk_forward_candidate_params.json",
            recommended_params,
        )
        if report["accepted"]:
            _write_json(
                output_dir / "walk_forward_recommended_params.json",
                recommended_params,
            )
    _write_json(state_path, {
        "status": "complete",
        "run_fingerprint": plan["run_fingerprint"],
        "completed_folds": len(fold_summaries),
        "accepted": report["accepted"],
        "report": str(output_dir / "walk_forward_report.json"),
        "updated_at": _timestamp_now(),
    })
    print(
        f"Nested walk-forward complete | accepted={report['accepted']} | "
        f"positive OOS folds={positive_ratio:.1%} | report={output_dir / 'walk_forward_report.json'}"
    )
    return report


DEFAULT_EXECUTION_SCENARIOS = {
    "base": {},
    "adverse": {
        "fee_rate": 0.0007,
        "slippage_rate": 0.0002,
        "funding_rate_per_8h": 0.0001,
        "maintenance_margin_rate": 0.005,
        "liquidation_fee_rate": 0.002,
    },
    "severe": {
        "fee_rate": 0.0010,
        "slippage_rate": 0.0005,
        "funding_rate_per_8h": 0.0003,
        "maintenance_margin_rate": 0.010,
        "liquidation_fee_rate": 0.005,
    },
}


def _load_execution_scenarios(args, adapter):
    source = getattr(args, "cost_scenarios", None)
    if source:
        payload = json.loads(Path(source).read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not all(
            isinstance(value, dict) for value in payload.values()
        ):
            raise ValueError("cost scenario JSON must map scenario names to tune objects")
        return {str(name): dict(values) for name, values in payload.items()}
    module_scenarios = getattr(adapter.module, "EXECUTION_SCENARIOS", None)
    if isinstance(module_scenarios, dict):
        return {str(name): dict(values) for name, values in module_scenarios.items()}
    if adapter.identifier == "ma_strategy:ma_strategy":
        return {name: dict(values) for name, values in DEFAULT_EXECUTION_SCENARIOS.items()}
    return {"base": {}}


def _default_holdout_params_path(output_dir):
    output_dir = Path(output_dir)
    return output_dir / "walk_forward_research" / "walk_forward_recommended_params.json"


def run_sealed_holdout(args):
    """Consume a frozen date range once without feeding any optimizer state."""
    adapter = _adapter_from_args(args)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    params_path = Path(
        args.holdout_params or _default_holdout_params_path(output_dir)
    )
    if not params_path.is_file():
        raise FileNotFoundError(
            f"frozen holdout params not found: {params_path}; run --research or supply "
            "--holdout-params"
        )
    params = _freeze_strategy_tune(adapter, adapter.load_tune(params_path))
    holdout_end = (
        _latest_market_end() if args.holdout_end == "latest" else args.holdout_end
    )
    scenarios = _load_execution_scenarios(args, adapter)
    data_file = _strategy_data_file(args, adapter)
    data_sha = fingerprint_data(data_file) if data_file is not None else "unavailable"
    range_payload = {
        "strategy": adapter.identifier,
        "data_sha256": data_sha,
        "start": args.holdout_start,
        "end": holdout_end,
    }
    holdout_key = fingerprint_config({
        "strategy": adapter.identifier,
        "start": args.holdout_start,
        "end": holdout_end,
    })
    range_fingerprint = fingerprint_config(range_payload)
    run_payload = {
        **range_payload,
        "params": params,
        "scenarios": scenarios,
    }
    run_fingerprint = fingerprint_config(run_payload)
    ledger_path = output_dir / "holdout_ledger.json"
    ledger = _load_json(ledger_path, []) or []
    prior = [
        entry for entry in ledger
        if entry.get("holdout_key") == holdout_key
        or entry.get("range_fingerprint") == range_fingerprint
    ]
    contaminated = bool(prior)
    if contaminated and not args.allow_holdout_repeat:
        raise PermissionError(
            "this data range has already been consumed as a sealed holdout; changing "
            "parameters after seeing it makes it development data. Use new future data, "
            "or --allow-holdout-repeat to run an explicitly contaminated diagnostic."
        )

    scenario_results = {}
    for scenario_name, overrides in scenarios.items():
        effective_tune = {**params, **overrides}
        _, _, result, duration, error = _evaluate_candidate(
            f"holdout-{scenario_name}",
            {},
            effective_tune,
            _parse_bound(args.holdout_start),
            _parse_bound(holdout_end),
            True,
            adapter.identifier,
            True,
        )
        objective = _objective_score(
            result,
            min_trades=args.holdout_min_trades,
            max_drawdown=args.holdout_max_drawdown,
        ) if not error else -math.inf
        monthly_returns = list((result or {}).get("monthly_returns", []))
        scenario_results[scenario_name] = {
            "overrides": overrides,
            "effective_params": effective_tune,
            "result": result or {},
            "objective_score": objective if math.isfinite(objective) else None,
            "monthly_statistics": performance_from_returns(monthly_returns, 12.0),
            "monthly_bootstrap": moving_block_bootstrap_ci(
                monthly_returns,
                samples=args.bootstrap_samples,
                confidence=args.bootstrap_confidence,
                seed=args.seed,
            ),
            "duration_seconds": duration,
            "error": error,
        }
    base = scenario_results.get("base") or next(iter(scenario_results.values()))
    scenario_gates = {
        name: (
            record["error"] is None
            and record["objective_score"] is not None
            and int(record["result"].get("closed_trades", 0) or 0)
            >= int(args.holdout_min_trades)
            and float(record["result"].get("total_profit_percent", 0.0)) > 0
            and int(record["result"].get("liquidations", 0) or 0) == 0
            and (
                not args.require_positive_ci
                or (
                    record["monthly_bootstrap"].get("lower") is not None
                    and record["monthly_bootstrap"]["lower"] > 0
                )
            )
        )
        for name, record in scenario_results.items()
    }
    report = {
        "protocol": "sealed holdout",
        "date_protocol": dict(getattr(args, "_date_protocol", {}) or {}),
        "status": "contaminated_repeat" if contaminated else "first_and_only_peek",
        "created_at": _timestamp_now(),
        "strategy": adapter.identifier,
        "params_source": str(params_path),
        "range": [args.holdout_start, holdout_end],
        "data_sha256": data_sha,
        "range_fingerprint": range_fingerprint,
        "holdout_key": holdout_key,
        "run_fingerprint": run_fingerprint,
        "scenario_gates": scenario_gates,
        "accepted": bool(
            not contaminated
            and base["objective_score"] is not None
            and all(scenario_gates.values())
        ),
        "acceptance_requirements": {
            "minimum_trades_per_scenario": int(args.holdout_min_trades),
            "maximum_drawdown_percent": float(args.holdout_max_drawdown),
            "positive_total_return": True,
            "zero_liquidations": True,
            "positive_bootstrap_lower_bound": bool(args.require_positive_ci),
            "first_peek_only": True,
        },
        "scenario_results": scenario_results,
        "feedback_policy": "results are never written to Hall of Fame, surrogate history, or seeds",
    }
    report["decision"] = "ACCEPT" if report["accepted"] else "REJECT"
    report["decision_reasons"] = (
        ["all sealed Holdout cost scenarios passed on the first peek"]
        if report["accepted"]
        else (
            ["sealed range was previously consumed; this repeat is contaminated"]
            if contaminated
            else [
                f"failed cost scenario: {name}"
                for name, passed in scenario_gates.items() if not passed
            ]
        )
    )
    run_dir = output_dir / "sealed_holdout" / run_fingerprint[:16]
    if run_dir.exists() and contaminated:
        run_dir = output_dir / "sealed_holdout" / (
            run_fingerprint[:12]
            + "_repeat_"
            + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        )
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_json(run_dir / "holdout_report.json", report)
    ledger.append({
        "created_at": report["created_at"],
        "range_fingerprint": range_fingerprint,
        "holdout_key": holdout_key,
        "run_fingerprint": run_fingerprint,
        "status": report["status"],
        "accepted": report["accepted"],
        "report": str(run_dir / "holdout_report.json"),
    })
    _write_json(ledger_path, ledger)
    print(
        f"Sealed holdout consumed | accepted={report['accepted']} | "
        f"status={report['status']} | report={run_dir / 'holdout_report.json'}"
    )
    return report


def run_staged_optimization(args):
    """Run repeatable 50-cycle blocks with one locked parameter group per phase."""
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / "staged_state.json"
    archive_path = output_dir / "staged_hall_of_fame.json"
    staged_phases = _strategy_staged_phases(args)
    strategy_adapter = _adapter_from_args(args)
    parameter_profiles = getattr(args, "_parameter_profiles", None) or strategy_adapter.discovered_profiles()
    phase_count = len(staged_phases)
    block_cycles = int(args.stage_cycles) * phase_count
    if int(args.snapshot_cycles) != block_cycles:
        raise ValueError(
            f"--snapshot-cycles must equal --stage-cycles x {phase_count} "
            f"({block_cycles} with the current schedule)"
        )

    persisted_state = _load_json(state_path, {}) if state_path.is_file() else {}
    if persisted_state:
        _restore_frozen_date_protocol(args, persisted_state.get("date_protocol"))

    staged_config = {
        "strategy": _adapter_from_args(args).identifier,
        "phases": [list(item) for item in staged_phases],
        "stage_cycles": int(args.stage_cycles),
        "snapshot_cycles": int(args.snapshot_cycles),
        "snapshot_top": int(args.snapshot_top),
        "random_audit_tests": int(args.random_audit_tests),
        "random_audit_top": int(args.random_audit_top),
        "random_audit_earliest": args.random_audit_earliest,
        "random_audit_recent_start": args.random_audit_recent_start,
        "random_audit_recent_ratio": float(args.random_audit_recent_ratio),
        "random_audit_min_months": int(args.random_audit_min_months),
        "random_audit_max_months": int(args.random_audit_max_months),
    }
    if state_path.is_file():
        state = persisted_state or {}
        saved_staged_config = state.get("config") or {}
        saved_staged_config.setdefault(
            "strategy", _adapter_from_args(args).identifier
        )
        state["config"] = saved_staged_config
        if state.get("config") != staged_config:
            raise ValueError(
                "staged checkpoint settings differ from this command; use the original "
                "phase, snapshot, and random-audit settings"
            )
        archive = _load_json(archive_path, []) or []
        baseline = state.get("baseline_params", {})
        print(
            f"Resuming staged campaign after {state.get('cycles_completed', 0):,} "
            "completed cycles."
        )
    else:
        base_tune, base_description = _load_base_tune(args)
        baseline = dict(base_tune)
        archive = []
        legacy_auto_dir = output_dir.parent / "auto"
        legacy_best_path = legacy_auto_dir / "best_params.json"
        legacy_hall_path = legacy_auto_dir / "hall_of_fame.json"
        if (
            _adapter_from_args(args).identifier == "ma_strategy:ma_strategy"
            and getattr(args, "base_source", "config") == "config"
            and legacy_best_path.is_file()
        ):
            baseline = strategy_adapter.load_tune(legacy_best_path)
            base_description = f"legacy auto winner: {legacy_best_path}"
            legacy_hall = _load_json(legacy_hall_path, []) or []
            for rank, record in enumerate(legacy_hall, start=1):
                migrated = dict(record)
                migrated["candidate_id"] = (
                    f"legacy-auto-{record.get('candidate_id', rank)}"
                )
                migrated["phase_candidate_id"] = record.get("candidate_id")
                migrated["block"] = 0
                migrated["phase"] = "legacy_auto"
                migrated["effective_params"] = dict(
                    record.get("effective_params") or record.get("params") or baseline
                )
                archive.append(migrated)
            if archive:
                _write_json(archive_path, archive)
            print(
                f"Staged bootstrap: loaded {len(archive)} legacy winners and "
                f"baseline from {legacy_auto_dir}."
            )
        state = {
            "version": 1,
            "status": "running",
            "cycles_completed": 0,
            "block": 1,
            "phase_index": 0,
            "baseline_source": base_description,
            "baseline_params": baseline,
            "config": staged_config,
            "date_protocol": dict(getattr(args, "_date_protocol", {}) or {}),
            "created_at": _timestamp_now(),
            "updated_at": _timestamp_now(),
        }
        _write_json(state_path, state)

    total_limit = max(0, int(args.auto_cycles))
    internal_limit = max(500, int(args.snapshot_top) * 5)
    while total_limit == 0 or state["cycles_completed"] < total_limit:
        phase_index = int(state.get("phase_index", 0)) % phase_count
        phase_name, profile_name = staged_phases[phase_index]
        phase_grid = parameter_profiles[profile_name]
        block = int(state.get("block", 1))
        phase_dir = (
            output_dir / "blocks" / f"block_{block:04d}" /
            "phases" / f"{phase_index + 1:02d}_{phase_name}"
        )
        phase_dir.mkdir(parents=True, exist_ok=True)
        baseline_path = phase_dir / "baseline_params.json"
        if not baseline_path.is_file():
            _write_json(baseline_path, baseline)

        phase_args = argparse.Namespace(**vars(args))
        phase_args.staged = False
        phase_args.profile = profile_name
        phase_args.output_dir = str(phase_dir)
        phase_args.auto_cycles = int(args.stage_cycles)
        phase_args.base_source = "file"
        phase_args.base_params = str(baseline_path)
        phase_args.resume = (phase_dir / "auto_state.json").is_file()
        seeds = _staged_seed_data(
            archive[:int(args.snapshot_top)], phase_grid, baseline, strategy_adapter
        )
        phase_args._seed_elites = seeds
        # Projected winners are useful parents, but their old scores were
        # measured with different fixed settings. Re-evaluate before learning.
        phase_args._seed_history = []

        print(
            f"\nStaged block {block} | phase {phase_index + 1}/{phase_count}: "
            f"{phase_name.upper()} | {args.stage_cycles} cycles | "
            f"{len(phase_grid)} active parameters"
        )
        run_auto_optimization(phase_args, grid=phase_grid)
        phase_state = _load_json(phase_dir / "auto_state.json", {}) or {}
        if int(phase_state.get("cycles_completed", 0)) < int(args.stage_cycles):
            state.update({
                "status": "interrupted",
                "active_phase": phase_name,
                "updated_at": _timestamp_now(),
            })
            _write_json(state_path, state)
            return archive[0] if archive else None

        phase_hall = _load_json(phase_dir / "hall_of_fame.json", []) or []
        if phase_hall:
            baseline = dict(phase_hall[0]["effective_params"])
            archive = _merge_staged_archive(
                archive, phase_hall, block, phase_name, internal_limit
            )
            _write_json(archive_path, archive)

        state["cycles_completed"] = int(state.get("cycles_completed", 0)) + int(
            args.stage_cycles
        )
        state["phase_index"] = phase_index + 1
        state["baseline_params"] = baseline
        state["status"] = "running"
        state["updated_at"] = _timestamp_now()

        if state['phase_index'] < phase_count:
            _write_staged_ranking(output_dir, archive, args.snapshot_top,
                                  excel_enabled=bool(args.excel_top), state=state)
        if state["phase_index"] >= phase_count:
            snapshot_dir = output_dir / "snapshots" / f"cycles_{state['cycles_completed']:06d}"
            _write_staged_ranking(
                output_dir, archive, args.snapshot_top, snapshot_dir=snapshot_dir,
                excel_enabled=False, state=state,
            )
            audit_best = _run_random_window_audit(
                args, archive[:args.random_audit_top], output_dir, block,
                _latest_market_end() if args.auto_end == "latest" else args.auto_end,
            )
            if audit_best:
                audit_summaries = _load_json(
                    output_dir / "random_window_summary.json", []
                ) or []
                audit_by_id = {
                    record["candidate_id"]: record for record in audit_summaries
                }
                for record in archive:
                    audit = audit_by_id.get(record.get("candidate_id"))
                    if audit:
                        record["random_audit_score"] = audit.get("random_audit_score")
                        record["random_audit_valid_ratio"] = audit.get("valid_ratio")
                        record["random_audit_positive_ratio"] = audit.get(
                            "positive_window_ratio"
                        )
                archive.sort(
                    key=lambda record: (
                        int(_finite_number(record.get("random_audit_score")) is not None),
                        (
                            _finite_number(record.get("random_audit_score"))
                            if _finite_number(record.get("random_audit_score")) is not None
                            else -math.inf
                        ),
                        (
                            _finite_number(record.get("robust_score"))
                            if _finite_number(record.get("robust_score")) is not None
                            else -math.inf
                        ),
                    ),
                    reverse=True,
                )
                _write_json(archive_path, archive)
                _write_staged_ranking(
                    output_dir, archive, args.snapshot_top, snapshot_dir=snapshot_dir,
                    excel_enabled=bool(args.excel_top), state=state,
                )
                for filename in (
                    "random_window_results.csv", "random_window_summary.csv",
                    "random_windows.csv", "random_window_summary.json", "best_params.json",
                    "random_window_report.xlsx",
                ):
                    source = output_dir / filename
                    if source.is_file():
                        shutil.copy2(source, snapshot_dir / filename)
                state["random_audit_best"] = {
                    key: value for key, value in audit_best.items()
                    if key != "effective_params"
                }
            development_end = (
                _latest_market_end() if args.auto_end == "latest" else args.auto_end
            )
            run_manifest = _load_json(
                output_dir / "research_manifest.json", {}
            ) or {}
            _write_json(snapshot_dir / "manifest.json", {
                "snapshot_schema_version": 1,
                "cycle": int(state["cycles_completed"]),
                "requested_top": int(args.snapshot_top),
                "saved_candidates": min(len(archive), int(args.snapshot_top)),
                "strategy": _adapter_from_args(args).identifier,
                "ranking": "random-window audit when available, then robust score",
                "date_protocol": state.get("date_protocol"),
                "development_range": [args.auto_validation_start, development_end],
                "development_end_exclusive": development_end,
                "holdout_status": (
                    "recency-first optimization consumed data through the latest candle"
                ),
                "run_fingerprints": run_manifest.get("fingerprints"),
                "created_at": _timestamp_now(),
            })
            state["block"] = block + 1
            state["phase_index"] = 0
            if audit_best:
                baseline = dict(audit_best["effective_params"])
                state["baseline_params"] = baseline
            elif archive:
                baseline = dict(archive[0]["effective_params"])
                state["baseline_params"] = baseline
            print(
                f"Completed {state['cycles_completed']:,} staged cycles | "
                f"top {min(len(archive), args.snapshot_top)} snapshot: {snapshot_dir}"
            )
        _write_json(state_path, state)

    state.update({"status": "completed", "updated_at": _timestamp_now()})
    _write_json(state_path, state)
    _write_staged_ranking(output_dir, archive, args.snapshot_top,
                          excel_enabled=bool(args.excel_top), state=state)
    if (output_dir / "best_params.json").is_file():
        print(f"USE THIS PARAMETER FILE: {output_dir / 'best_params.json'}")
        print(f"Winner guide: {output_dir / 'best_params_manifest.json'}")
    return archive[0] if archive else None


def run_optimization(args, grid=None):
    profiles = _profiles_from_args(args)
    profile = getattr(args, "profile", None) or (
        "focused" if "focused" in profiles else next(iter(profiles))
    )
    if grid is None:
        if profile not in profiles:
            raise ValueError(
                f"profile {profile!r} is unavailable; choose from {', '.join(profiles)}"
            )
        grid = profiles[profile]
    keys = tuple(grid)
    base_tune, base_description = _load_base_tune(args)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    results_path = output_dir / "optimization_results.csv"
    fieldnames = _result_fieldnames(keys)

    if args.mode == "grid":
        requested_tests = grid_size(grid)
        candidate_source = None
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
    min_trades = getattr(args, "min_trades", 0)
    max_drawdown = getattr(args, "max_drawdown", None)
    strategy_adapter = _adapter_from_args(args)
    strategy_spec = strategy_adapter.identifier
    fixed_warmup = strategy_adapter.maximum_optimizer_warmup(grid, base_tune)

    def objective(result):
        return _objective_score(result, min_trades=min_trades, max_drawdown=max_drawdown)

    resume = bool(getattr(args, "resume", False))
    resume_signatures = set()
    if resume and results_path.is_file():
        ranked = _read_resume_records(results_path, grid, base_tune)
        for record in ranked:
            record["params"] = _freeze_strategy_tune(
                strategy_adapter, record["params"]
            )
        completed = len(ranked)
        resume_signatures = {
            tuple(record["params"][key] for key in keys) for record in ranked
        }
        if ranked:
            next_index = max(record["index"] for record in ranked) + 1
        ranked.sort(key=lambda item: objective(item["result"]), reverse=True)
        best = next(
            (record for record in ranked if math.isfinite(objective(record["result"]))),
            None,
        )
        del ranked[max(
            getattr(args, "top_n", 20), args.elite_size,
            getattr(args, "validation_top", 20),
        ):]
        print(f"Resuming from {completed:,} completed candidates in {results_path}")

    seen_signatures = resume_signatures
    if args.mode == "grid":
        candidate_source = (
            candidate for candidate in iter_grid_candidates(grid)
            if tuple(candidate[key] for key in keys) not in seen_signatures
            and is_valid_candidate({**base_tune, **candidate}, strategy_adapter)
        )

    run_metadata = {
        "strategy": strategy_spec,
        "parameter_grid_source": getattr(args, "param_grid", None),
        "profile": profile,
        "optimized_parameters": list(keys),
        "base_source": base_description,
        "minimum_trades": min_trades,
        "maximum_allowed_drawdown": max_drawdown,
        "resumed": resume,
    }

    print(f"Mode: {args.mode} | tests: {requested_tests:,} | workers: {workers}")
    print(f"Profile: {profile} ({len(keys)} parameters) | base: {base_description}")
    print(f"Range: {start} -> {end}")

    append_results = resume and results_path.is_file() and completed > 0
    with results_path.open("a" if append_results else "w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        if not append_results:
            writer.writeheader()

        pool = None
        if workers > 1:
            pool = multiprocessing.Pool(
                workers,
                initializer=_init_worker,
                initargs=(
                    start, end, base_tune, True, True, strategy_spec, False,
                    fixed_warmup,
                ),
            )
        else:
            # Warm cached market data once in the parent for serial searches.
            _init_worker(
                start, end, base_tune,
                strategy_spec=strategy_spec,
                indicator_warmup_candles=fixed_warmup,
            )

        def evaluate(batch):
            tasks = [(offset, candidate) for offset, candidate in batch]
            if pool is not None:
                return pool.imap_unordered(_evaluate_task, tasks, chunksize=chunksize)
            return (
                _evaluate_candidate(
                    index, params, base_tune, start, end,
                    strategy_spec=strategy_spec,
                    indicator_warmup_candles=fixed_warmup,
                )
                for index, params in tasks
            )

        def consume(batch):
            nonlocal completed, failed, best, ranked
            for index, params, result, duration, error in evaluate(batch):
                completed += 1
                if error:
                    failed += 1
                    print(f"[{completed}/{requested_tests}] test {index} failed: {error}")
                    continue
                effective_params = _freeze_strategy_tune(
                    strategy_adapter, {**base_tune, **params}
                )
                result_objective = objective(result)
                writer.writerow(_result_row(
                    keys, index, effective_params, result, duration,
                    objective_score=(result_objective if math.isfinite(result_objective) else None),
                ))
                record = {
                    "index": index,
                    "params": effective_params,
                    "result": result,
                    "duration": duration,
                }
                ranked.append(record)
                ranked.sort(key=lambda item: objective(item["result"]), reverse=True)
                del ranked[max(
                    getattr(args, "top_n", 20), args.elite_size,
                    getattr(args, "validation_top", 20),
                ):]
                if math.isfinite(objective(result)) and (
                    best is None or objective(result) > objective(best["result"])
                ):
                    best = record
                if args.log_every and (completed % args.log_every == 0 or completed == requested_tests):
                    best_score = _score(best["result"]) if best else -math.inf
                    current_score = _score(result)
                    elapsed = time.perf_counter() - started
                    print(
                        f"[{completed:,}/{requested_tests:,}] best_score={best_score:.4f} "
                        f"score={current_score:.4f} elapsed={elapsed:.1f}s"
                    )
            csv_file.flush()
            _write_best_files(
                output_dir, best, args.mode, requested_tests, completed,
                time.perf_counter() - started, args.seed, run_metadata,
            )

        interrupted = False
        pool_terminated = False
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
                generator = SmartCandidateGenerator(
                    grid, seed=args.seed, baseline_params=base_tune,
                    strategy_adapter=_adapter_from_args(args),
                )
                generator.seen.update(seen_signatures)
                while completed < requested_tests:
                    count = min(batch_size, requested_tests - completed)
                    elites = [
                        record for record in ranked[:args.elite_size]
                        if math.isfinite(objective(record["result"]))
                    ]
                    progress = completed / max(1, requested_tests)
                    candidates = generator.generate(count, elites=elites, progress=progress)
                    if not candidates:
                        break
                    batch = list(enumerate(candidates, start=next_index))
                    next_index += len(batch)
                    consume(batch)
        except KeyboardInterrupt:
            interrupted = True
            csv_file.flush()
            _write_best_files(
                output_dir, best, args.mode, requested_tests, completed,
                time.perf_counter() - started, args.seed,
                {**run_metadata, "interrupted": True},
            )
            if pool is not None:
                pool.terminate()
                pool_terminated = True
            print(
                f"\nStopped by user after {completed:,} completed tests. "
                "Checkpoint saved; use --resume to continue."
            )
        finally:
            if pool is not None:
                if not pool_terminated:
                    pool.close()
                pool.join()

    if interrupted:
        return best

    elapsed = time.perf_counter() - started
    training_best = best
    validated_best, validation_records = _validate_top_candidates(
        ranked, args, workers, chunksize,
    )
    if validation_records:
        _write_json(output_dir / "validation_results.json", validation_records)
        run_metadata.update({
            "validation_range": [args.validation_start, args.validation_end],
            "validation_candidates": len(validation_records),
            "overfit_penalty": args.overfit_penalty,
        })
        if training_best:
            _write_json(output_dir / "best_training_params.json", training_best["params"])
        if validated_best and math.isfinite(validated_best["robust_score"]):
            best = {
                "index": validated_best["index"],
                "params": validated_best["params"],
                "result": validated_best["result"],
                "duration": validated_best["duration"],
                "selection_score": validated_best["robust_score"],
            }
            run_metadata.update({
                "best_robust_score": validated_best["robust_score"],
                "best_training_metrics": validated_best["training_result"],
            })
            print(
                f"Validated {len(validation_records)} finalists | "
                f"best robust score={validated_best['robust_score']:.4f}"
            )
        else:
            run_metadata["validation_warning"] = "no finalist passed validation constraints"
            print("Validation warning: no finalist passed the requested constraints")
    elapsed = time.perf_counter() - started
    excel_top = max(0, int(getattr(args, "excel_top", 5000)))
    workbook_path = (
        _save_optimizer_workbook(
            results_path,
            output_dir / "optimization_results.xlsx",
            keys,
            max_rows=excel_top,
            selected_best=best,
        )
        if excel_top else None
    )
    if workbook_path is not None:
        run_metadata["excel_report"] = str(workbook_path)
    _write_best_files(
        output_dir, best, args.mode, requested_tests, completed, elapsed,
        args.seed, run_metadata, final=True,
    )
    top_n = max(1, int(getattr(args, "top_n", 20)))
    _write_json(output_dir / "top_results.json", [
        {"rank": rank, **record}
        for rank, record in enumerate(ranked[:top_n], start=1)
    ])
    print(f"Finished {completed:,} tests ({failed} failed) in {elapsed:.1f}s")
    print(f"Results: {results_path}")
    if workbook_path is not None:
        print(f"Excel report: {workbook_path}")
    if best:
        print(f"Best score: {_score(best['result']):.4f}")
        print(f"USE THIS PARAMETER FILE: {output_dir / 'best_params.json'}")
        print(f"Winner guide: {output_dir / 'best_params_manifest.json'}")
    return best


class _OptimizerHelpFormatter(
    argparse.ArgumentDefaultsHelpFormatter,
    argparse.RawDescriptionHelpFormatter,
):
    """Keep command examples readable while still showing option defaults."""

    def _get_help_string(self, action):
        help_text = action.help
        if (
            "%(default)" not in help_text
            and action.default not in (None, False, argparse.SUPPRESS)
        ):
            help_text += " (default: %(default)s)"
        return help_text


def build_parser():
    parser = argparse.ArgumentParser(
        prog="optimize.py",
        formatter_class=_OptimizerHelpFormatter,
        description="""Search and validate robust strategy parameters.

Choose one search path:
  smart  Budgeted adaptive search (recommended for normal experiments).
  grid   Every combination in a profile; usually only practical for tiny grids.
  auto   Continuous model-guided development search with successive halving,
         walk-forward validation, stress tests, and development-period finalists. Existing state is
         resumed automatically. It runs until Ctrl+C unless --auto-cycles is set.

Dates are inclusive at START and exclusive at END. A candle index may be used
instead of a YYYY-MM-DD date.""",
        epilog="""recommended examples:
  Inspect profiles and estimate a run without starting it:
    python optimize.py --list-profiles
    python optimize.py --mode smart --profile focused --tests 5000 --dry-run

  Start a fast adaptive search from ma_strategy_config.py:
    python optimize.py --mode smart --profile focused --base-source config `
      --tests 5000 -w 8 --output-dir outputs/optimize/focused_run

  Train on one period and use the next period as inner validation:
    python optimize.py --mode smart --profile signal --tests 10000 -w 8 --date-policy fixed `
      --start 2023-01-01 --end 2024-01-01 `
      --validation-start 2024-01-01 --validation-end 2025-04-01 `
      --validation-top 30 --min-trades 50 --max-drawdown 35 `
      --output-dir outputs/optimize/validated_signal

  Refine an existing winner, then resume the same interrupted run:
    python optimize.py --mode smart --profile exit --base-source best `
      --base-params outputs/optimize/best_params.json --tests 5000 -w 8 `
      --output-dir outputs/optimize/refine_exit
    python optimize.py --mode smart --profile exit --base-source best `
      --base-params outputs/optimize/best_params.json --tests 5000 -w 8 `
      --output-dir outputs/optimize/refine_exit --resume

  Run two auto cycles (omit --auto-cycles to run until Ctrl+C):
    python optimize.py --auto --auto-cycles 2 -w 16 `
      --output-dir outputs/optimize/auto_two_cycles

  Resume the auto campaign with exactly the same settings (--resume is optional
  when the checkpoint already exists):
    python optimize.py --auto --auto-cycles 2 -w 16 `
      --output-dir outputs/optimize/auto_two_cycles --resume

Tips:
  * Start with --dry-run. Full built-in grids can contain enormous combinations.
  * Use a new --output-dir for a new experiment; use --resume only for the same run.
  * Plain --auto detects and resumes a compatible checkpoint in --output-dir.
  * A new campaign warm-starts from a compatible existing --base-params winner.
  * --date-policy auto derives recent leak-resistant ranges from the latest candle.
  * Use --date-policy fixed when explicit date flags must be preserved exactly.
  * For trustworthy selection, use --research, freeze its recommendation, then peek once with --sealed-holdout.
  * Raw score is preserved; cross-range comparisons use a candle-count annualized score.
  * Auto learns from normalized Discovery ranks and later funnel outcomes, not raw scale.
  * Candidate selection balances predicted quality, uncertainty, diversity, and randomness.
  * Numeric mutations learn a preferred direction and local step inside the configured bounds.
  * Auto mode defaults to profile=full; non-auto mode defaults to profile=focused.""",
    )

    search = parser.add_argument_group("search mode and parameter scope")
    search.add_argument(
        "--strategy", default="ma", metavar="NAME|MODULE:FUNCTION",
        help=(
            "strategy callable (built-in alias 'ma', or module:function; the callable "
            "must accept tune/start/end and return result metrics)"
        ),
    )
    search.add_argument(
        "--param-grid", metavar="JSON|MODULE:ATTRIBUTE",
        help=(
            "external parameter grid or profile collection; otherwise the strategy's "
            "param_grid/PARAMETER_PROFILES is discovered"
        ),
    )
    search.add_argument(
        "--auto", action="store_true",
        help="use the resumable staged auto campaign (overrides --mode)",
    )
    search.add_argument(
        "--mode", choices=("smart", "grid"), default="smart",
        help="non-auto search algorithm",
    )
    search.add_argument(
        "--tests", type=int, default=5000, metavar="N",
        help="candidate budget in smart mode; ignored by grid and auto",
    )
    search.add_argument(
        "--profile", default=None, metavar="NAME",
        help="parameter profile name (default: full in auto mode, focused otherwise)",
    )
    search.add_argument(
        "--base-source", choices=("config", "best", "file"), default="config",
        help="fixed/base values come from the selected strategy config or --base-params JSON",
    )
    search.add_argument(
        "--base-params", default=os.path.join("outputs", "optimize", "best_params.json"),
        metavar="PATH", help="JSON read when --base-source is best or file",
    )

    execution = parser.add_argument_group("execution and reproducibility")
    execution.add_argument(
        "-w", "--workers", type=int, default=min(8, os.cpu_count() or 1), metavar="N",
        help="parallel worker processes",
    )
    execution.add_argument(
        "--batch-size", type=int, default=0, metavar="N",
        help="candidates evaluated before adapting/checkpointing; 0 selects automatically",
    )
    execution.add_argument(
        "--chunksize", type=int, default=0, metavar="N",
        help="tasks sent to each worker at once; 0 selects automatically",
    )
    execution.add_argument(
        "--elite-size", type=int, default=20, metavar="N",
        help="top candidates that guide smart search",
    )
    execution.add_argument(
        "--seed", type=int, default=42, metavar="N",
        help="random seed for reproducible smart/auto candidate generation",
    )
    execution.add_argument(
        "--data-file", metavar="PATH",
        help=(
            "fallback market CSV for audit/date discovery when a strategy does not "
            "expose DATA_FILE; strategy-owned DATA_FILE is authoritative"
        ),
    )
    execution.add_argument(
        "--data-audit", choices=("strict", "warn", "off"), default="strict",
        help="pre-run market-data gate; gaps/zero volume remain warnings in strict mode",
    )
    execution.add_argument(
        "--refresh-auto-report", action="store_true",
        help=(
            "re-evaluate saved Auto finalists for monthly analytics and rebuild "
            "the colored report/snapshot"
        ),
    )

    ranges = parser.add_argument_group("standard search ranges and robustness")
    ranges.add_argument(
        "--date-policy", choices=("auto", "fixed"), default="auto",
        help=(
            "auto derives and freezes recent ranges from the latest candle; "
            "fixed uses the explicit date options below"
        ),
    )
    ranges.add_argument(
        "--rolling-development-months", type=int,
        default=DEFAULT_ROLLING_DEVELOPMENT_MONTHS, metavar="N",
        help="recent history ending at the latest candle (default: 24 months)",
    )
    ranges.add_argument(
        "--rolling-oos-months", type=int, default=DEFAULT_ROLLING_OOS_MONTHS,
        metavar="N", help="reporting-only OOS reservation before the embargo",
    )
    ranges.add_argument(
        "--rolling-embargo-months", type=int,
        default=DEFAULT_ROLLING_EMBARGO_MONTHS, metavar="N",
        help="unused calendar months between research OOS and sealed holdout",
    )
    ranges.add_argument(
        "--rolling-holdout-months", type=int,
        default=DEFAULT_ROLLING_HOLDOUT_MONTHS, metavar="N",
        help="latest calendar months reserved as the sealed holdout",
    )
    ranges.add_argument(
        "--rolling-stress-months", type=int,
        default=DEFAULT_ROLLING_STRESS_MONTHS, metavar="N",
        help="small historical stability slice immediately before the recent window",
    )
    ranges.add_argument(
        "--rolling-validation-months", type=int,
        default=DEFAULT_ROLLING_VALIDATION_MONTHS, metavar="N",
        help="rolling Validation duration after Stress and before Discovery",
    )
    ranges.add_argument(
        "--start", default=DEFAULT_DEVELOPMENT_START, metavar="DATE|INDEX",
        help="inclusive training start",
    )
    ranges.add_argument(
        "--end", default=DEFAULT_DEVELOPMENT_END, metavar="DATE|INDEX",
        help="exclusive search end; later dates are reserved for walk-forward/holdout",
    )
    ranges.add_argument(
        "--validation-start", metavar="DATE|INDEX",
        help="inclusive inner-validation start; requires --validation-end",
    )
    ranges.add_argument(
        "--validation-end", metavar="DATE|INDEX",
        help="exclusive inner-validation end; requires --validation-start",
    )
    ranges.add_argument(
        "--validation-top", type=int, default=20, metavar="N",
        help="training finalists re-tested on inner validation (used for selection)",
    )
    ranges.add_argument(
        "--overfit-penalty", type=float, default=0.25, metavar="FLOAT",
        help="penalty when training score exceeds validation score",
    )
    ranges.add_argument(
        "--min-trades", type=int, default=0, metavar="N",
        help="disqualify candidates with fewer closed trades (0 disables)",
    )
    ranges.add_argument(
        "--max-drawdown", type=float, metavar="PERCENT",
        help="disqualify candidates above this absolute drawdown percentage",
    )

    output = parser.add_argument_group("output, checkpoints, and planning")
    output.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, metavar="PATH",
                        help="directory for reports; CLI default: outputs/<strategy>/optimize (research/holdout for those modes)")
    output.add_argument("--resume", nargs="?", const=True, default=False, metavar="FOLDER",
                        help="resume with saved settings; optionally give a campaign path or unique folder name")
    output.add_argument("--log-every", type=int, default=10, metavar="N",
                        help="print progress every N completed tests (0 is silent)")
    output.add_argument("--top-n", type=int, default=20, metavar="N",
                        help="ranked candidates saved to top_results.json")
    output.add_argument(
        "--excel-top", type=int, default=5000,
        metavar="N", help="top candidates included in XLSX (0 disables XLSX)",
    )
    output.add_argument("--list-profiles", action="store_true",
                        help="show profile parameter counts/grid sizes and exit")
    output.add_argument("--dry-run", action="store_true",
                        help="print the resolved plan without running backtests")

    research = parser.add_argument_group(
        "nested walk-forward research (reporting-only OOS validation)"
    )
    research.add_argument(
        "--research", action="store_true",
        help="run nested chronological walk-forward instead of a normal/auto search",
    )
    research.add_argument(
        "--wf-start", default=DEFAULT_DEVELOPMENT_START, metavar="DATE|INDEX",
        help="earliest nested walk-forward training candle",
    )
    research.add_argument(
        "--wf-end", default=DEFAULT_RESEARCH_END, metavar="DATE|INDEX|latest",
        help=(
            "exclusive development end, not the dataset end; later candles "
            "through --holdout-end stay sealed"
        ),
    )
    research.add_argument("--wf-train-months", type=float, default=24.0, metavar="N")
    research.add_argument("--wf-validation-months", type=float, default=3.0, metavar="N")
    research.add_argument("--wf-test-months", type=float, default=2.0, metavar="N")
    research.add_argument("--wf-step-months", type=float, default=2.0, metavar="N")
    research.add_argument(
        "--wf-rolling", action="store_true",
        help="use a fixed rolling train window instead of anchored expanding history",
    )
    research.add_argument(
        "--wf-purge-candles", type=int, default=0, metavar="N",
        help="unused candles between train/validation/test boundaries",
    )
    research.add_argument(
        "--research-tests", type=int, default=500, metavar="N",
        help="fixed candidate pool evaluated independently inside every fold",
    )
    research.add_argument(
        "--research-seeds", metavar="JSON",
        help=(
            "optional Auto snapshot/top-results JSON; compatible winners are "
            "inserted before deterministic Halton candidates"
        ),
    )
    research.add_argument(
        "--allow-research-seed-overlap", action="store_true",
        help=(
            "allow unverifiable/overlapping seed history for diagnostics; the "
            "research_seed_provenance gate remains false"
        ),
    )
    research.add_argument(
        "--research-validation-top", type=int, default=50, metavar="N",
        help="training finalists evaluated on each inner validation window",
    )
    research.add_argument(
        "--research-pbo-candidates", type=int, default=20, metavar="N",
        help="fixed candidates retained across validation blocks for CSCV/PBO",
    )
    research.add_argument("--bootstrap-samples", type=int, default=1000, metavar="N")
    research.add_argument("--bootstrap-confidence", type=float, default=0.95, metavar="RATIO")
    research.add_argument("--min-oos-folds", type=int, default=4, metavar="N")
    research.add_argument(
        "--min-fold-trades", type=int, default=5, metavar="N",
        help="minimum closed trades required in every train/validation/OOS evaluation",
    )
    research.add_argument(
        "--min-total-oos-trades", type=int, default=30, metavar="N",
        help="minimum closed trades across the stitched reporting-only OOS folds",
    )
    research.add_argument(
        "--max-oos-liquidations", type=int, default=0, metavar="N",
        help="maximum liquidations allowed across all reporting-only OOS folds",
    )
    research.add_argument(
        "--research-max-drawdown", type=float, default=40.0, metavar="PERCENT",
        help="per-window drawdown gate used during nested selection",
    )
    research.add_argument(
        "--max-oos-drawdown", type=float, default=40.0, metavar="PERCENT",
        help="maximum drawdown allowed on the stitched OOS return series",
    )
    research.add_argument("--min-positive-fold-ratio", type=float, default=0.60, metavar="RATIO")
    research.add_argument("--min-dsr-probability", type=float, default=0.95, metavar="RATIO")
    research.add_argument("--max-pbo", type=float, default=0.20, metavar="RATIO")
    research.add_argument(
        "--min-parameter-consensus", type=float, default=0.50, metavar="RATIO",
        help="minimum mean modal frequency across mutable parameters and fold winners",
    )
    research.add_argument(
        "--max-parameter-spread", type=float, default=0.35, metavar="RATIO",
        help="maximum mean normalized grid spread across mutable parameters",
    )
    research.add_argument(
        "--require-positive-ci", action="store_true",
        help="require the bootstrap lower bound of periodic OOS return to exceed zero",
    )
    research.add_argument(
        "--allow-nonpositive-ci", dest="require_positive_ci", action="store_false",
        help="diagnostic override: do not reject a result whose bootstrap lower bound is non-positive",
    )
    research.set_defaults(require_positive_ci=True)
    research.add_argument(
        "--sealed-holdout", action="store_true",
        help="evaluate frozen parameters once on the sealed range and record its consumption",
    )
    research.add_argument("--holdout-params", metavar="PATH")
    research.add_argument(
        "--holdout-start", default=DEFAULT_HOLDOUT_START, metavar="DATE|INDEX",
        help="inclusive sealed range start; by default this is also --wf-end",
    )
    research.add_argument(
        "--holdout-end", default="latest", metavar="DATE|INDEX|latest",
        help="exclusive sealed range end; latest means one interval after the final candle",
    )
    research.add_argument(
        "--holdout-min-trades", type=int, default=20, metavar="N",
        help="minimum closed trades required in every sealed cost scenario",
    )
    research.add_argument(
        "--holdout-max-drawdown", type=float, default=30.0, metavar="PERCENT",
        help="maximum absolute drawdown allowed in every sealed cost scenario",
    )
    research.add_argument(
        "--cost-scenarios", metavar="JSON",
        help="scenario-name to strategy-tune overrides; MA gets base/adverse/severe defaults",
    )
    research.add_argument(
        "--allow-holdout-repeat", action="store_true",
        help="allow a repeat but mark it contaminated and never call it unseen",
    )

    auto = parser.add_argument_group("auto campaign (used only with --auto)")
    auto.add_argument(
        "--auto-tests", type=int, default=2000,
        metavar="N",
        help="new discovery candidates generated in every auto cycle",
    )
    auto.add_argument(
        "--auto-validation-top", type=int, default=500,
        metavar="N",
        help="discovery finalists sent to the independent validation range",
    )
    auto.add_argument(
        "--auto-stress-top", type=int, default=250,
        metavar="N",
        help="validation finalists sent to the older stress range",
    )
    auto.add_argument(
        "--auto-final-top", type=int, default=100,
        metavar="N",
        help="stress finalists tested on the complete development range",
    )
    auto.add_argument(
        "--auto-hall-size", type=int, default=100,
        metavar="N",
        help="maximum robust winners retained across all auto cycles",
    )
    auto.add_argument(
        "--auto-cycles", "--cycles", type=int, default=0,
        metavar="N",
        help="stop after N completed cycles (0 runs until Ctrl+C)",
    )
    auto.add_argument(
        "--auto-discovery-start", default=DEFAULT_AUTO_DISCOVERY_START,
        metavar="DATE|INDEX",
        help="start of the recent discovery range",
    )
    auto.add_argument(
        "--auto-validation-start", default=DEFAULT_AUTO_VALIDATION_START,
        metavar="DATE|INDEX",
        help="start of validation; it ends at auto-discovery-start",
    )
    auto.add_argument(
        "--auto-stress-start", default=DEFAULT_DEVELOPMENT_START,
        metavar="DATE|INDEX",
        help="inclusive start of the older stability-only stress slice",
    )
    auto.add_argument(
        "--auto-stress-end", default=None, metavar="DATE|INDEX",
        help=(
            "exclusive end of the older stability slice; in fixed mode defaults "
            "to --auto-validation-start"
        ),
    )
    auto.add_argument(
        "--auto-end", default=DEFAULT_DEVELOPMENT_END,
        metavar="DATE|INDEX|latest",
        help=(
            "exclusive candidate-search end; auto policy sets this immediately "
            "after the latest candle"
        ),
    )
    auto.add_argument(
        "--auto-importance-target",
        choices=("objective_score", "total_profit", "total_profit_percent"),
        default="objective_score",
        help="metric used to learn which parameters deserve more mutations",
    )
    auto.add_argument(
        "--auto-advanced-min-candidates", type=int, default=64, metavar="N",
        help="minimum cycle size that activates halving/surrogate/walk-forward",
    )
    auto.add_argument(
        "--auto-halving-rungs", type=int, default=2, metavar="N",
        help="cheap expanding Discovery rungs before full Discovery (0 disables)",
    )
    auto.add_argument(
        "--auto-halving-keep", type=float, default=0.25, metavar="RATIO",
        help="fraction promoted after each cheap Discovery rung",
    )
    auto.add_argument(
        "--auto-surrogate-min-samples", type=int, default=64, metavar="N",
        help="historical full-Discovery samples required before Extra Trees is used",
    )
    auto.add_argument(
        "--auto-surrogate-pool", type=int, default=8, metavar="MULTIPLIER",
        help="unevaluated quality/uncertainty/diversity pool relative to --auto-tests",
    )
    auto.add_argument(
        "--auto-surrogate-trees", type=int, default=64, metavar="N",
        help="trees learning normalized ranks and robust funnel outcomes",
    )
    auto.add_argument(
        "--auto-surrogate-max-samples", type=int, default=10_000, metavar="N",
        help="representative historical samples retained for tree training",
    )
    auto.add_argument(
        '--auto-learning-target', choices=('rank', 'profit-evidence'), default='rank',
        help='profit-evidence learns net return, monthly downside and failed candidates; persists across resume',
    )
    auto.add_argument('--auto-trade-count-policy', choices=('fixed', 'duration'), default='fixed',
                      help='duration scales the trade gate to each stage relative to Discovery')
    auto.add_argument(
        "--auto-walk-forward-folds", type=int, default=3, metavar="N",
        help="disjoint pre-Discovery time folds (0 disables; minimum enabled value is 2)",
    )
    auto.add_argument(
        "--auto-walk-forward-top", type=int, default=150, metavar="N",
        help="Stress finalists evaluated on every walk-forward fold",
    )
    auto.add_argument(
        "--auto-walk-forward-stability-penalty", type=float, default=0.15,
        metavar="FLOAT",
        help="penalty multiplier applied to score variation across folds",
    )
    auto.add_argument(
        "--staged", action="store_true",
        help="optimize Signal, Exit, Risk, RSI, and Scale in consecutive phases",
    )
    auto.add_argument(
        "--stage-cycles", type=int, default=10, metavar="N",
        help="completed auto cycles allocated to each staged parameter phase",
    )
    auto.add_argument(
        "--snapshot-cycles", type=int, default=50, metavar="N",
        help="completed auto cycles between ranked Top-N snapshots (default: 50)",
    )
    auto.add_argument(
        "--snapshot-top", type=int, default=100, metavar="N",
        help="ranked candidates and standalone parameter JSON files per snapshot",
    )
    auto.add_argument(
        "--random-audit-tests", type=int, default=500, metavar="N",
        help="total random-window backtests after every staged snapshot",
    )
    auto.add_argument(
        "--random-audit-top", type=int, default=10, metavar="N",
        help="staged finalists compared on identical random windows",
    )
    auto.add_argument(
        "--random-audit-earliest", default=DEFAULT_DEVELOPMENT_START, metavar="DATE|INDEX",
        help="earliest allowed random-window candle",
    )
    auto.add_argument(
        "--random-audit-recent-start", default=DEFAULT_AUTO_DISCOVERY_START, metavar="DATE|INDEX",
        help="start boundary used for the recent-window quota",
    )
    auto.add_argument(
        "--random-audit-recent-ratio", type=float, default=0.70, metavar="RATIO",
        help="fraction of random windows starting on recent data",
    )
    auto.add_argument(
        "--random-audit-min-months", type=int, default=6, metavar="N",
        help="minimum random audit window duration",
    )
    auto.add_argument(
        "--random-audit-max-months", type=int, default=12, metavar="N",
        help="maximum random audit window duration",
    )
    parser.add_argument('--directional', action='store_true',
                        help='MA: optimize long/short separately; use side phases with --staged')
    parser.add_argument('--autopilot', action='store_true',
                        help='MA: start staged directional Auto with automatic snapshot and audit workbooks')
    add_runtime_arguments(parser, data_file=False)
    return parser


@runtime_session
@output_session
def main(argv=None):
    from optimize_resume import restore_campaign
    from optimize_commands import expand_arguments, report_command
    parser = build_parser()
    arguments = sys.argv[1:] if argv is None else list(argv)
    simple_start = bool(arguments and not arguments[0].startswith('-')
                        and arguments[0] not in ('resume', 'status', 'report'))
    try:
        if report_command(arguments):
            return
        arguments = expand_arguments(arguments)
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from error
    args = parser.parse_args(arguments)
    if any(item == '--cycles' or item.startswith('--cycles=') for item in arguments):
        args.auto = True
    if simple_start and args.auto and not args.resume:
        module = _adapter_from_spec(args.strategy).module
        parser.set_defaults(**getattr(module, 'OPTIMIZER_DEFAULTS', {}))
        args = parser.parse_args(arguments)
        args.auto = True
    try:
        arguments = restore_campaign(parser, args, arguments)
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from error
    try:
        configure_runtime(args, arguments)
    except (ValueError, OSError) as error:
        raise SystemExit(str(error)) from error
    if args.autopilot:
        args.auto = args.staged = args.directional = True
    if args.directional:
        if args.strategy not in ('ma', 'ma_strategy', 'ma_strategy:ma_strategy'):
            raise SystemExit('--directional/--autopilot currently require the MA strategy')
        if args.profile is None:
            args.profile = 'directional'
        if args.staged:
            from ma_strategy_config import STAGED_AUTO_PHASES
            args.snapshot_cycles = args.stage_cycles * (2 * len(STAGED_AUTO_PHASES) + 1)
    try:
        args._strategy_adapter = _adapter_from_spec(args.strategy)
        if getattr(args, '_resume_grid', None):
            args._parameter_profiles = {args.profile: args._resume_grid}
        else:
            args._parameter_profiles = _profiles_from_args(args)
        if getattr(args, '_resume_state', None):
            _restore_auto_resume_args(args, args._resume_state)
    except (FileNotFoundError, ModuleNotFoundError, ValueError) as error:
        raise SystemExit(str(error)) from error
    default_profile = "full" if args.auto else "focused"
    if default_profile not in args._parameter_profiles:
        default_profile = next(iter(args._parameter_profiles))
    args.profile = args.profile or default_profile
    if args.profile not in args._parameter_profiles:
        raise SystemExit(
            f"unknown profile {args.profile!r}; available profiles: "
            + ", ".join(args._parameter_profiles)
        )
    explicit = lambda flag: any(a == flag or a.startswith(flag + '=') for a in arguments)
    _apply_strategy_date_defaults(args, args._strategy_adapter.module, arguments)
    workflow = 'research' if args.research else 'holdout' if args.sealed_holdout else 'optimize'
    identifier = args._strategy_adapter.identifier
    if not explicit('--output-dir'):
        args.output_dir = str(output_path(identifier, workflow))
    if not explicit('--base-params'):
        args.base_params = str(output_path(identifier, 'optimize') / 'best_params.json')
    assert_strategy_path(args.output_dir, identifier)
    assert_strategy_path(args.base_params, identifier)
    if args.list_profiles:
        print(f"Strategy: {args._strategy_adapter.identifier}")
        for name, grid in args._parameter_profiles.items():
            print(f"{name}: {len(grid)} parameters | {grid_size(grid):,} grid combinations")
        return
    try:
        _activate_strategy_market_data(args, args._strategy_adapter)
        print(f'Performance: {args.performance} | workers: {args.workers} | timeframe: {getattr(args, "_detected_timeframe", "unknown")}')
        frozen_protocol = (
            _load_frozen_date_protocol(args.output_dir)
            if args.date_policy == "auto" else None
        )
        if frozen_protocol:
            if _market_data_coverage()['end_exclusive'] != frozen_protocol.get('dataset_end_exclusive'):
                print('Dataset coverage changed. Existing campaign dates remain frozen; '
                      'use a new --output-dir to optimize the updated data.')
            _restore_frozen_date_protocol(args, frozen_protocol)
            date_protocol = frozen_protocol
        else:
            date_protocol = _apply_date_policy(args)
    except (IndexError, OSError, TypeError, ValueError) as error:
        raise SystemExit(f"cannot resolve date policy: {error}") from error
    rolling_counts = (
        args.rolling_development_months,
        args.rolling_oos_months,
        args.rolling_embargo_months,
        args.rolling_holdout_months,
        args.rolling_stress_months,
        args.rolling_validation_months,
    )
    if min(rolling_counts) <= 0:
        raise SystemExit("all rolling date-policy month counts must be greater than zero")
    if args.tests <= 0:
        raise SystemExit("--tests must be greater than zero")
    if args.elite_size <= 0:
        raise SystemExit("--elite-size must be greater than zero")
    if args.validation_top <= 0:
        raise SystemExit("--validation-top must be greater than zero")
    if args.min_trades < 0:
        raise SystemExit("--min-trades cannot be negative")
    if args.max_drawdown is not None and args.max_drawdown <= 0:
        raise SystemExit("--max-drawdown must be greater than zero")
    if args.overfit_penalty < 0:
        raise SystemExit("--overfit-penalty cannot be negative")
    if args.top_n <= 0:
        raise SystemExit("--top-n must be greater than zero")
    if args.excel_top < 0:
        raise SystemExit("--excel-top cannot be negative")
    if sum(bool(value) for value in (args.research, args.sealed_holdout, args.auto)) > 1:
        raise SystemExit("--research, --sealed-holdout, and --auto are separate workflows")
    if args.research:
        if args.research_seeds and not Path(args.research_seeds).is_file():
            raise SystemExit(f"research seed JSON not found: {args.research_seeds}")
        if min(
            args.wf_train_months, args.wf_validation_months,
            args.wf_test_months, args.wf_step_months,
        ) <= 0:
            raise SystemExit("walk-forward month windows must be greater than zero")
        if args.wf_purge_candles < 0:
            raise SystemExit("--wf-purge-candles cannot be negative")
        if min(
            args.research_tests, args.research_validation_top,
            args.research_pbo_candidates, args.bootstrap_samples,
            args.min_oos_folds,
        ) <= 0:
            raise SystemExit("research counts must be greater than zero")
        if min(
            args.min_fold_trades,
            args.min_total_oos_trades,
            args.max_oos_liquidations,
        ) < 0:
            raise SystemExit("research trade-count gates cannot be negative")
        if min(args.research_max_drawdown, args.max_oos_drawdown) <= 0:
            raise SystemExit("research drawdown gates must be greater than zero")
        for name in (
            "bootstrap_confidence", "min_positive_fold_ratio",
            "min_dsr_probability", "max_pbo", "min_parameter_consensus",
            "max_parameter_spread",
        ):
            if not 0 < getattr(args, name) <= 1:
                raise SystemExit(f"--{name.replace('_', '-')} must be in (0, 1]")
    if args.holdout_min_trades < 0:
        raise SystemExit("--holdout-min-trades cannot be negative")
    if args.holdout_max_drawdown <= 0:
        raise SystemExit("--holdout-max-drawdown must be greater than zero")
    if args.auto_tests <= 0:
        raise SystemExit("--auto-tests must be greater than zero")
    if min(
        args.auto_validation_top, args.auto_stress_top,
        args.auto_final_top, args.auto_hall_size,
    ) <= 0:
        raise SystemExit("auto funnel and Hall of Fame sizes must be greater than zero")
    if not (
        args.auto_tests >= args.auto_validation_top
        >= args.auto_stress_top >= args.auto_final_top
    ):
        raise SystemExit(
            "auto funnel must satisfy: auto-tests >= auto-validation-top >= "
            "auto-stress-top >= auto-final-top"
        )
    if args.auto_cycles < 0:
        raise SystemExit("--auto-cycles cannot be negative")
    if args.auto_advanced_min_candidates <= 0:
        raise SystemExit("--auto-advanced-min-candidates must be greater than zero")
    if not 0 <= args.auto_halving_rungs <= 4:
        raise SystemExit("--auto-halving-rungs must be between 0 and 4")
    if not 0 < args.auto_halving_keep <= 1:
        raise SystemExit("--auto-halving-keep must be greater than 0 and at most 1")
    if args.auto_surrogate_min_samples < 4:
        raise SystemExit("--auto-surrogate-min-samples must be at least 4")
    if args.auto_surrogate_pool <= 0 or args.auto_surrogate_trees <= 0:
        raise SystemExit("auto surrogate pool and tree counts must be greater than zero")
    if args.auto_surrogate_max_samples < args.auto_surrogate_min_samples:
        raise SystemExit(
            "--auto-surrogate-max-samples must be at least --auto-surrogate-min-samples"
        )
    if args.auto_walk_forward_folds == 1 or args.auto_walk_forward_folds < 0:
        raise SystemExit("--auto-walk-forward-folds must be 0 or at least 2")
    if args.auto_walk_forward_top <= 0:
        raise SystemExit("--auto-walk-forward-top must be greater than zero")
    if (
        args.auto_tests >= args.auto_advanced_min_candidates
        and not args.auto_stress_top >= args.auto_walk_forward_top >= args.auto_final_top
    ):
        raise SystemExit(
            "advanced auto funnel must satisfy: auto-stress-top >= "
            "auto-walk-forward-top >= auto-final-top"
        )
    if args.auto_walk_forward_stability_penalty < 0:
        raise SystemExit("--auto-walk-forward-stability-penalty cannot be negative")
    if args.auto and min(args.snapshot_cycles, args.snapshot_top) <= 0:
        raise SystemExit("--snapshot-cycles and --snapshot-top must be greater than zero")
    if args.staged:
        if not args.auto:
            raise SystemExit("--staged requires --auto")
        try:
            staged_phases = _strategy_staged_phases(args)
        except ValueError as error:
            raise SystemExit(str(error)) from error
        if not staged_phases:
            raise SystemExit(
                f"strategy {args._strategy_adapter.identifier} does not expose "
                "STAGED_PHASES; use normal --auto or define staged profile groups"
            )
        if min(args.stage_cycles, args.snapshot_cycles, args.snapshot_top) <= 0:
            raise SystemExit("staged cycle and snapshot settings must be greater than zero")
        if min(args.random_audit_tests, args.random_audit_top) <= 0:
            raise SystemExit("random-audit test and finalist counts must be greater than zero")
        if args.random_audit_tests < args.random_audit_top:
            raise SystemExit("--random-audit-tests must be at least --random-audit-top")
        if not 0 <= args.random_audit_recent_ratio <= 1:
            raise SystemExit("--random-audit-recent-ratio must be between 0 and 1")
        if (
            args.random_audit_min_months <= 0
            or args.random_audit_max_months < args.random_audit_min_months
        ):
            raise SystemExit("random-audit month limits must satisfy 0 < min <= max")
        expected_snapshot = args.stage_cycles * len(staged_phases)
        if args.snapshot_cycles != expected_snapshot:
            raise SystemExit(
                f"--snapshot-cycles must equal {expected_snapshot} for the current "
                "five-phase schedule"
            )
        if args.auto_cycles and args.auto_cycles % args.stage_cycles:
            raise SystemExit("--auto-cycles must be divisible by --stage-cycles in staged mode")
    if bool(args.validation_start) != bool(args.validation_end):
        raise SystemExit("--validation-start and --validation-end must be used together")
    if args.dry_run:
        selected_grid = args._parameter_profiles[args.profile]
        if args.sealed_holdout:
            coverage = _market_data_coverage()
            holdout_end = (
                coverage["end_exclusive"] if args.holdout_end == "latest"
                else args.holdout_end
            )
            params_path = Path(
                args.holdout_params
                or _default_holdout_params_path(args.output_dir)
            )
            scenarios = _load_execution_scenarios(
                args, args._strategy_adapter
            )
            print(
                f"Mode: sealed holdout | strategy: "
                f"{args._strategy_adapter.identifier}"
            )
            print(
                f"Dataset candles: {coverage['first_candle']} -> "
                f"{coverage['last_candle']} (inclusive)"
            )
            print(f"Dataset end: {coverage['end_exclusive']} (exclusive)")
            print(f"Frozen params: {params_path}")
            print(f"Holdout range: {args.holdout_start} -> {holdout_end} (exclusive)")
            print("Execution scenarios: " + ", ".join(scenarios))
            print(
                f"Gates per scenario: trades >= {args.holdout_min_trades}, "
                f"drawdown <= {args.holdout_max_drawdown:.1f}%, positive return, "
                "zero liquidations, bootstrap lower bound "
                + ("must be positive" if args.require_positive_ci else "diagnostic override")
            )
            print(
                "Repeat policy: "
                + (
                    "allowed but marked contaminated"
                    if args.allow_holdout_repeat
                    else "blocked after the first peek"
                )
            )
            return
        if args.research:
            coverage = _market_data_coverage()
            resolved_end = (
                coverage["end_exclusive"] if args.wf_end == "latest" else args.wf_end
            )
            folds = _nested_walk_forward_ranges(
                args.wf_start, resolved_end,
                train_months=args.wf_train_months,
                validation_months=args.wf_validation_months,
                test_months=args.wf_test_months,
                step_months=args.wf_step_months,
                rolling=args.wf_rolling,
                purge_candles=args.wf_purge_candles,
            )
            if not folds:
                raise SystemExit(
                    "walk-forward range is too short for the requested train, "
                    "validation, and OOS windows"
                )
            base_tune, _ = _load_base_tune(args)
            seed_params = _load_research_seed_candidates(
                args.research_seeds,
                selected_grid,
                base_tune,
                args._strategy_adapter,
            )
            seed_provenance = _research_seed_provenance(
                args.research_seeds, folds, args._strategy_adapter.identifier
            )
            if seed_params and seed_provenance["status"] == "strategy_mismatch":
                raise SystemExit(
                    "research seed snapshot belongs to another strategy"
                )
            if seed_params and not seed_provenance["safe_for_oos_claim"]:
                if not args.allow_research_seed_overlap:
                    raise SystemExit(
                        "research seed history is not verified to end before the "
                        f"first OOS window ({seed_provenance['status']})"
                    )
            halton_params = _space_filling_candidates(
                selected_grid,
                min(args.research_tests, grid_size(selected_grid)),
                base_tune,
                args._strategy_adapter,
                seed=args.seed,
            )
            pool_signatures = []
            seen_pool = set()
            for params in [*seed_params, *halton_params]:
                signature = _candidate_signature(params, tuple(selected_grid))
                if signature in seen_pool:
                    continue
                seen_pool.add(signature)
                pool_signatures.append(signature)
                if len(pool_signatures) >= args.research_tests:
                    break
            print(f"Mode: nested walk-forward research | strategy: {args._strategy_adapter.identifier}")
            print(
                f"Dataset candles: {coverage['first_candle']} -> "
                f"{coverage['last_candle']} (inclusive)"
            )
            print(f"Research end: {resolved_end} (exclusive)")
            try:
                has_reserved_holdout = (
                    _bound_index(resolved_end)
                    < _bound_index(coverage["end_exclusive"])
                )
            except (IndexError, OSError, TypeError, ValueError):
                has_reserved_holdout = False
            if has_reserved_holdout:
                print(
                    f"Embargo: {date_protocol.get('embargo_start', resolved_end)} -> "
                    f"{date_protocol.get('holdout_start', resolved_end)}"
                )
                print(
                    f"Reserved sealed holdout: "
                    f"{date_protocol.get('holdout_start', resolved_end)} -> "
                    f"{coverage['end_exclusive']} (exclusive; last candle "
                    f"{coverage['last_candle']})"
                )
            print(
                f"Candidate pool: {len(pool_signatures):,} | "
                f"snapshot seeds loaded: {len(seed_params):,} | "
                "remaining capacity: deterministic Halton"
            )
            print(
                "Research-seed provenance: "
                f"{seed_provenance['status']} | acceptance-safe: "
                f"{seed_provenance['safe_for_oos_claim']}"
            )
            print(f"Folds: {len(folds)} | OOS feedback: disabled")
            for fold in folds:
                print(
                    f"Fold {fold['fold']}: train {fold['train']} -> "
                    f"validation {fold['validation']} -> OOS {fold['test']}"
                )
            return
        if args.auto:
            resolved_end = _latest_market_end() if args.auto_end == "latest" else args.auto_end
            ranges = _auto_ranges(args, resolved_end)
            _validate_auto_ranges(ranges)
            print("Mode: staged auto" if args.staged else "Mode: auto")
            print(
                f"Date policy: {date_protocol['policy']} | latest candle: "
                f"{date_protocol.get('dataset_last_candle', 'manual')}"
            )
            if args.staged:
                staged_phases = _strategy_staged_phases(args)
                print(
                    "Phase schedule: "
                    + " -> ".join(
                        f"{name} ({len(args._parameter_profiles[profile])} params, "
                        f"{args.stage_cycles} cycles)"
                        for name, profile in staged_phases
                    )
                )
                audit_windows = args.random_audit_tests // args.random_audit_top
                print(
                    f"Random audit: {args.random_audit_top} finalists x "
                    f"{audit_windows} shared windows = "
                    f"{audit_windows * args.random_audit_top} tests | "
                    f"{args.random_audit_recent_ratio:.0%} starting after "
                    f"{args.random_audit_recent_start}"
                )
            else:
                print(f"Profile: {args.profile} ({len(selected_grid)} parameters)")
            print(
                f"Snapshot: top {args.snapshot_top} every "
                f"{args.snapshot_cycles} completed cycles"
            )
            print(
                f"Funnel per cycle: {args.auto_tests:,} -> "
                f"{args.auto_validation_top} -> {args.auto_stress_top} -> "
                f"WF {args.auto_walk_forward_top} x {args.auto_walk_forward_folds} -> "
                f"{args.auto_final_top}"
            )
            print(
                f"Discovery halving: {args.auto_halving_rungs} rung(s), "
                f"keep {args.auto_halving_keep:.0%} per rung"
            )
            print(
                f"Surrogate: {args.auto_surrogate_trees} Extra Trees after "
                f"{args.auto_surrogate_min_samples} historical samples"
            )
            print(f"Cycles: {'unlimited' if args.auto_cycles == 0 else args.auto_cycles}")
            print(f"Workers: {args.workers}")
            for stage in ("discovery", "validation", "stress", "final"):
                print(f"{stage.title()}: {ranges[stage][0]} -> {ranges[stage][1]}")
            for index, fold in enumerate(
                _walk_forward_ranges(ranges, args.auto_walk_forward_folds), start=1
            ):
                print(f"Walk-forward fold {index}: {fold[0]} -> {fold[1]}")
            return
        planned = (
            min(args.tests, grid_size(selected_grid))
            if args.mode == "smart"
            else grid_size(selected_grid)
        )
        print(f"Mode: {args.mode}")
        print(f"Profile: {args.profile} ({len(selected_grid)} parameters)")
        print(f"Planned candidates: {planned:,}")
        print(f"Workers: {args.workers}")
        print(f"Range: {args.start} -> {args.end}")
        return
    multiprocessing.freeze_support()
    if args.refresh_auto_report:
        claim_output(args.output_dir, identifier, workflow)
        report_path = refresh_auto_report_monthly(
            args.output_dir,
            top_n=getattr(args, "snapshot_top", 100),
            workers=args.workers,
        )
        print(f"Refreshed Auto report: {report_path}")
        return
    claim_output(args.output_dir, identifier, workflow)
    resolved_cli_config = {
        key: value for key, value in vars(args).items() if not key.startswith("_")
    }
    resolved_cli_config["resolved_strategy"] = args._strategy_adapter.identifier
    resolved_cli_config["resolved_parameter_profiles"] = args._parameter_profiles
    campaign_config_path = Path(args.output_dir) / 'campaign_config.json'
    if not campaign_config_path.exists():
        if not args.auto and not args.research and not args.sealed_holdout:
            baseline, _ = _load_base_tune(args)
            resolved_cli_config['frozen_base_tune'] = _freeze_strategy_tune(
                args._strategy_adapter, baseline)
        _write_json(campaign_config_path, resolved_cli_config)
    if args.sealed_holdout:
        _run_research_preflight(args, args.output_dir, resolved_cli_config)
        run_sealed_holdout(args)
    elif args.research:
        _run_research_preflight(args, args.output_dir, resolved_cli_config)
        run_nested_walk_forward(args)
    elif args.auto:
        if not args.staged:
            saved_auto_state = _load_json(
                Path(args.output_dir) / "auto_state.json"
            )
            if saved_auto_state is not None:
                saved_auto_config = _restore_auto_resume_args(
                    args, saved_auto_state
                )
                resolved_cli_config = {
                    key: value
                    for key, value in vars(args).items()
                    if not key.startswith("_")
                }
                resolved_cli_config["resolved_strategy"] = (
                    args._strategy_adapter.identifier
                )
                resolved_cli_config["resolved_parameter_profiles"] = {
                    saved_auto_config.get("profile", args.profile):
                    saved_auto_config.get("parameter_grid", {})
                }
        _run_research_preflight(args, args.output_dir, resolved_cli_config)
        if args.staged:
            run_staged_optimization(args)
        else:
            run_auto_optimization(args)
    else:
        _run_research_preflight(args, args.output_dir, resolved_cli_config)
        run_optimization(args)


if __name__ == "__main__":
    main()
