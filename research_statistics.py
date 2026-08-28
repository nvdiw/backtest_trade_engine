"""Statistical diagnostics for strategy research.

The functions here are intentionally optimizer-independent.  They operate on
return series or a candidate-by-time-block matrix, which makes them reusable by
any strategy adapter.
"""

from __future__ import annotations

import itertools
import math
import random
import statistics
from statistics import NormalDist
from typing import Any, Iterable, Mapping, Sequence

import numpy as np


def _finite_array(values: Iterable[float]) -> np.ndarray:
    array = np.asarray(list(values), dtype=float)
    return array[np.isfinite(array)]


def equity_returns(equity: Sequence[float]) -> np.ndarray:
    values = _finite_array(equity)
    if values.size < 2:
        return np.asarray([], dtype=float)
    previous = values[:-1]
    valid = previous > 0
    return (values[1:][valid] / previous[valid]) - 1.0


def performance_from_returns(
    returns: Sequence[float], periods_per_year: float = 365.25
) -> dict[str, float | int | None]:
    """Calculate interpretable, time-aware metrics from periodic returns."""
    values = _finite_array(returns)
    count = int(values.size)
    if count == 0:
        return {
            "observations": 0,
            "total_return": None,
            "cagr": None,
            "annualized_volatility": None,
            "sharpe": None,
            "sortino": None,
            "maximum_drawdown": None,
            "calmar": None,
            "skewness": None,
            "kurtosis": None,
        }
    growth = np.cumprod(1.0 + values)
    total_return = float(growth[-1] - 1.0)
    years = count / float(periods_per_year)
    cagr = (
        float(growth[-1] ** (1.0 / years) - 1.0)
        if years > 0 and growth[-1] > 0
        else -1.0
    )
    mean = float(np.mean(values))
    volatility = float(np.std(values, ddof=1)) if count > 1 else 0.0
    annualized_volatility = volatility * math.sqrt(periods_per_year)
    sharpe = (
        mean / volatility * math.sqrt(periods_per_year)
        if volatility > 0
        else None
    )
    downside = values[values < 0]
    downside_deviation = (
        float(math.sqrt(np.mean(downside**2))) if downside.size else 0.0
    )
    sortino = (
        mean / downside_deviation * math.sqrt(periods_per_year)
        if downside_deviation > 0
        else None
    )
    peaks = np.maximum.accumulate(np.concatenate(([1.0], growth)))
    curve = np.concatenate(([1.0], growth))
    drawdowns = curve / peaks - 1.0
    maximum_drawdown = float(np.min(drawdowns))
    calmar = cagr / abs(maximum_drawdown) if maximum_drawdown < 0 else None

    centered = values - mean
    moment2 = float(np.mean(centered**2))
    if moment2 > 0:
        skewness = float(np.mean(centered**3) / moment2**1.5)
        kurtosis = float(np.mean(centered**4) / moment2**2)
    else:
        skewness = 0.0
        kurtosis = 3.0
    return {
        "observations": count,
        "total_return": total_return,
        "cagr": cagr,
        "annualized_volatility": annualized_volatility,
        "sharpe": sharpe,
        "sortino": sortino,
        "maximum_drawdown": maximum_drawdown,
        "calmar": calmar,
        "skewness": skewness,
        "kurtosis": kurtosis,
    }


def moving_block_bootstrap_ci(
    values: Sequence[float],
    *,
    statistic: str = "mean",
    confidence: float = 0.95,
    samples: int = 1000,
    block_size: int | None = None,
    seed: int = 42,
) -> dict[str, float | int | None]:
    """Dependence-preserving moving-block bootstrap confidence interval."""
    array = _finite_array(values)
    count = int(array.size)
    if count == 0:
        return {"estimate": None, "lower": None, "upper": None, "samples": 0}
    if not 0 < confidence < 1:
        raise ValueError("confidence must be between zero and one")
    samples = max(1, int(samples))
    block_size = max(1, min(count, int(block_size or round(count ** (1 / 3)))))

    def calculate(sample: np.ndarray) -> float:
        if statistic == "mean":
            return float(np.mean(sample))
        if statistic == "median":
            return float(np.median(sample))
        if statistic == "sharpe":
            deviation = float(np.std(sample, ddof=1)) if sample.size > 1 else 0.0
            return float(np.mean(sample) / deviation) if deviation > 0 else 0.0
        raise ValueError("statistic must be mean, median, or sharpe")

    randomizer = random.Random(seed)
    estimates: list[float] = []
    for _ in range(samples):
        pieces: list[float] = []
        while len(pieces) < count:
            start = randomizer.randrange(count)
            pieces.extend(array[(start + offset) % count] for offset in range(block_size))
        estimates.append(calculate(np.asarray(pieces[:count], dtype=float)))
    alpha = (1.0 - confidence) / 2.0
    return {
        "estimate": calculate(array),
        "lower": float(np.quantile(estimates, alpha)),
        "upper": float(np.quantile(estimates, 1.0 - alpha)),
        "samples": samples,
        "block_size": block_size,
        "confidence": confidence,
    }


def probabilistic_sharpe_ratio(
    observed_sharpe: float,
    benchmark_sharpe: float,
    observations: int,
    *,
    skewness: float = 0.0,
    kurtosis: float = 3.0,
) -> float | None:
    """Probability that a per-period Sharpe exceeds the benchmark."""
    observations = int(observations)
    if observations < 2:
        return None
    denominator_squared = (
        1.0
        - skewness * observed_sharpe
        + ((kurtosis - 1.0) / 4.0) * observed_sharpe**2
    )
    if denominator_squared <= 0:
        return None
    statistic = (
        (observed_sharpe - benchmark_sharpe)
        * math.sqrt(observations - 1)
        / math.sqrt(denominator_squared)
    )
    return NormalDist().cdf(statistic)


def deflated_sharpe_ratio(
    observed_sharpe: float,
    observations: int,
    trial_sharpes: Sequence[float],
    *,
    skewness: float = 0.0,
    kurtosis: float = 3.0,
) -> dict[str, float | int | None]:
    """Selection-adjust a per-period Sharpe using the effective trial set.

    The returned probability is a PSR against the expected maximum Sharpe under
    repeated trials.  Correlated configurations should be clustered before this
    function when an effective rather than literal trial count is desired.
    """
    trials = _finite_array(trial_sharpes)
    trial_count = max(1, int(trials.size))
    trial_std = float(np.std(trials, ddof=1)) if trial_count > 1 else 0.0
    if trial_count <= 1 or trial_std == 0:
        expected_maximum = 0.0
    else:
        euler_gamma = 0.5772156649015329
        normal = NormalDist()
        first = normal.inv_cdf(max(1e-12, 1.0 - 1.0 / trial_count))
        second = normal.inv_cdf(
            max(1e-12, 1.0 - 1.0 / (trial_count * math.e))
        )
        expected_maximum = trial_std * (
            (1.0 - euler_gamma) * first + euler_gamma * second
        )
    probability = probabilistic_sharpe_ratio(
        float(observed_sharpe),
        expected_maximum,
        observations,
        skewness=skewness,
        kurtosis=kurtosis,
    )
    return {
        "deflated_sharpe_probability": probability,
        "observed_sharpe": float(observed_sharpe),
        "expected_maximum_sharpe": expected_maximum,
        "trial_count": trial_count,
        "trial_sharpe_std": trial_std,
        "observations": int(observations),
    }


def probability_of_backtest_overfitting(
    performance_matrix: Sequence[Sequence[float]],
    *,
    max_combinations: int = 5000,
    seed: int = 42,
) -> dict[str, Any]:
    """Estimate PBO with combinatorially symmetric cross-validation.

    Rows are configurations and columns are chronological, non-overlapping
    return/performance blocks.  At least two configurations and four even-numbered
    blocks are required.
    """
    matrix = np.asarray(performance_matrix, dtype=float)
    if matrix.ndim != 2:
        raise ValueError("performance_matrix must be two-dimensional")
    configuration_count, block_count = matrix.shape
    if configuration_count < 2 or block_count < 4 or block_count % 2:
        return {
            "pbo": None,
            "combinations": 0,
            "configurations": configuration_count,
            "blocks": block_count,
            "logits": [],
        }
    half = block_count // 2
    combinations = list(itertools.combinations(range(block_count), half))
    # Train/test complements are symmetric, so keep a canonical half only.
    combinations = [combo for combo in combinations if 0 in combo]
    if len(combinations) > max_combinations:
        combinations = random.Random(seed).sample(combinations, max_combinations)

    logits: list[float] = []
    all_columns = set(range(block_count))
    for train_columns in combinations:
        test_columns = sorted(all_columns - set(train_columns))
        train_scores = np.nanmean(matrix[:, train_columns], axis=1)
        if not np.isfinite(train_scores).any():
            continue
        winner = int(np.nanargmax(train_scores))
        test_scores = np.nanmean(matrix[:, test_columns], axis=1)
        finite = np.isfinite(test_scores)
        if not finite[winner] or finite.sum() < 2:
            continue
        # Percentile rank: zero is worst and one is best, ties share a midpoint.
        below = int(np.sum(test_scores[finite] < test_scores[winner]))
        equal = int(np.sum(test_scores[finite] == test_scores[winner]))
        percentile = (below + 0.5 * max(0, equal - 1)) / max(1, int(finite.sum()) - 1)
        percentile = min(1.0 - 1e-9, max(1e-9, percentile))
        logits.append(math.log(percentile / (1.0 - percentile)))
    pbo = (
        sum(logit <= 0 for logit in logits) / len(logits) if logits else None
    )
    return {
        "pbo": pbo,
        "combinations": len(logits),
        "configurations": configuration_count,
        "blocks": block_count,
        "median_logit": statistics.median(logits) if logits else None,
        "logits": logits,
    }


def grid_ordinal_position(configured_values: Sequence[Any], value: Any) -> float | None:
    """Map an exact or refined numeric value onto an ordinal grid coordinate."""
    try:
        exact_positions = {
            configured: index for index, configured in enumerate(configured_values)
        }
        exact = exact_positions.get(value)
    except TypeError:
        exact = None
    if exact is not None:
        return float(exact)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric_points = []
    for index, configured in enumerate(configured_values):
        if isinstance(configured, bool) or not isinstance(configured, (int, float)):
            return None
        numeric_points.append((float(configured), float(index)))
    if not numeric_points or not math.isfinite(float(value)):
        return None
    numeric_points.sort(key=lambda point: point[0])
    numeric_value = float(value)
    if numeric_value <= numeric_points[0][0]:
        return numeric_points[0][1]
    if numeric_value >= numeric_points[-1][0]:
        return numeric_points[-1][1]
    for (left_value, left_index), (right_value, right_index) in zip(
        numeric_points, numeric_points[1:]
    ):
        if left_value <= numeric_value <= right_value:
            if right_value == left_value:
                return (left_index + right_index) / 2.0
            fraction = (numeric_value - left_value) / (right_value - left_value)
            return left_index + fraction * (right_index - left_index)
    return None


def parameter_plateau_scores(
    records: Sequence[Mapping[str, Any]],
    parameter_grid: Mapping[str, Sequence[Any]],
    *,
    score_key: str = "robust_score",
) -> dict[str, dict[str, Any]]:
    """Measure whether each candidate lives on a good local plateau.

    Neighbours differ by at most one configured step in exactly one parameter.
    A sharp isolated maximum therefore receives low neighbour coverage/stability.
    """
    keys = tuple(parameter_grid)
    positions = {
        key: {value: index for index, value in enumerate(values)}
        for key, values in parameter_grid.items()
    }

    rows = []
    for record in records:
        score = record.get(score_key)
        candidate_id = str(record.get("candidate_id", len(rows)))
        try:
            score = float(score)
        except (TypeError, ValueError):
            continue
        if math.isfinite(score):
            rows.append((candidate_id, record.get("params", {}), score))
    output: dict[str, dict[str, Any]] = {}
    for candidate_id, params, score in rows:
        neighbours: list[float] = []
        neighbour_distances: list[float] = []
        for other_id, other, other_score in rows:
            if other_id == candidate_id:
                continue
            differences = 0
            adjacent = True
            for key in keys:
                if params.get(key) == other.get(key):
                    continue
                differences += 1
                left = positions[key].get(params.get(key))
                right = positions[key].get(other.get(key))
                if left is None or right is None or abs(left - right) > 1:
                    adjacent = False
                    break
            if adjacent and differences == 1:
                neighbours.append(other_score)
                neighbour_distances.append(0.0)
        neighbour_method = "one_grid_step"
        if not neighbours:
            # Space-filling research designs rarely contain an exact one-axis
            # neighbour in a high-dimensional grid.  Fall back to the closest
            # few configurations, while explicitly discounting their stability
            # by normalized grid distance.
            nearby = []
            for other_id, other, other_score in rows:
                if other_id == candidate_id:
                    continue
                distances = []
                for key, configured_values in parameter_grid.items():
                    left = grid_ordinal_position(configured_values, params.get(key))
                    right = grid_ordinal_position(configured_values, other.get(key))
                    if left is None or right is None:
                        distances = []
                        break
                    scale = max(1, len(configured_values) - 1)
                    distances.append(abs(left - right) / scale)
                if distances:
                    distance = math.sqrt(
                        sum(value * value for value in distances) / len(distances)
                    )
                    nearby.append((distance, other_score))
            nearby.sort(key=lambda item: item[0])
            nearby = nearby[: min(5, len(nearby))]
            neighbours = [other_score for _, other_score in nearby]
            neighbour_distances = [distance for distance, _ in nearby]
            neighbour_method = "nearest_grid_configurations"
        if neighbours:
            median_neighbour = float(statistics.median(neighbours))
            downside_gap = max(0.0, score - median_neighbour)
            scale = max(1.0, abs(score))
            score_support = max(0.0, 1.0 - downside_gap / scale)
            median_distance = float(statistics.median(neighbour_distances))
            proximity = max(0.0, 1.0 - median_distance)
            stability = score_support * proximity
        else:
            median_neighbour = None
            median_distance = None
            stability = 0.0
        plateau_penalty = 0.25 * (1.0 - stability) * max(1.0, abs(score))
        output[candidate_id] = {
            "neighbour_count": len(neighbours),
            "neighbour_method": neighbour_method,
            "median_grid_distance": median_distance,
            "median_neighbour_score": median_neighbour,
            "plateau_stability": stability,
            "plateau_adjusted_score": score - plateau_penalty,
        }
    return output


__all__ = [
    "deflated_sharpe_ratio",
    "equity_returns",
    "grid_ordinal_position",
    "moving_block_bootstrap_ci",
    "parameter_plateau_scores",
    "performance_from_returns",
    "probabilistic_sharpe_ratio",
    "probability_of_backtest_overfitting",
]
