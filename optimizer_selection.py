"""Auto stage combination, directional evidence and candidate ranking."""
import math
import statistics
from optimizer_defaults import AUTO_STAGE_ORDER, AUTO_STAGE_WEIGHTS
from optimizer_learning import _finite_number, _record_comparable_score


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
    from optimizer_evidence import directional_evidence
    side_evidence = directional_evidence(final_metrics, record.get('effective_params') or record.get('params'))
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
    side_reasons = []
    for side, evidence in side_evidence.items():
        if evidence['status'] == 'DISABLED':
            continue
        if evidence['status'] != 'SUFFICIENT':
            side_reasons.append(f"{side.upper()} evidence {evidence['status'].lower()}: "
                                f"{evidence['trades']} closed trades; requires {evidence['minimum_trades']} in Final")
        elif not evidence['profitable']:
            side_reasons.append(f"{side.upper()} has non-positive net profit in Final")
    if side_reasons:
        if decision == 'ACCEPT':
            decision = 'WATCH'
            reasons = [reason for reason in reasons if reason != 'eligible for independent Research validation']
        reasons += side_reasons
    return {
        "decision": decision,
        "directional_evidence": side_evidence,
        "decision_scope": "Auto triage only; Research and sealed Holdout still required",
        "decision_reasons": reasons,
    }

def _auto_candidate_rank_key(record):
    """Prefer research-eligible evidence before raw score magnitude."""
    decision_priority = {"ACCEPT": 2, "WATCH": 1, "REJECT": 0}
    decision = _auto_candidate_decision(record)
    evidence = decision['directional_evidence']
    coverage = min((min(1.0, (item['trades'] or 0) / item['minimum_trades'])
                    for item in evidence.values() if item['status'] != 'DISABLED'), default=1.0)
    def score(key):
        value = _finite_number(record.get(key))
        return value if value is not None else -math.inf
    return (
        decision_priority[decision['decision']],
        coverage,
        score('robust_score'),
        score('recency_score'),
        score('stage_consistency_score'),
    )
