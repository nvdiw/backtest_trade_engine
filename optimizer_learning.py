"""Learning targets, parameter importance and the surrogate model."""
import math
import random
import statistics
from optimizer_defaults import AUTO_STAGE_WEIGHTS
from optimizer_progress import _show_loading_progress


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
