"""Grid enumeration and adaptive candidate generation, independent of execution."""
import itertools
import math
import random
from optimizer_progress import _show_loading_progress

def build_ma_strategy_config(tune=None):
    from ma_strategy_config import build_ma_strategy_config as build
    return build(tune)

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
