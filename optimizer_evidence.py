"""Bounded learning targets using measured net results on selection data only."""
import math
import statistics


def finite(value, default=0.0):
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def profit_evidence(result):
    """0..1 target; losers stay below neutral even when best in a weak batch.

    Net return already includes execution costs: do not subtract fees twice.
    Monthly downside and trade count reduce trust in fragile positive returns.
    This is a search heuristic, not a probability or an independent validation.
    """
    trades = max(0, finite(result.get('closed_trades')))
    if not trades or finite(result.get('liquidations')) > 0:
        return 0.0
    profit = finite(result.get('total_profit_percent'))
    drawdown = abs(finite(result.get('maximum_drawdown')))
    months = [finite(value) * 100 for value in result.get('monthly_returns', []) or []]
    downside = math.sqrt(statistics.fmean([min(0, value) ** 2 for value in months])) if months else 0
    signal = math.tanh(profit / max(1, drawdown + downside))
    confidence = min(1.0, math.sqrt(trades / 100))
    if profit > 0:
        # Missing monthly evidence cannot receive the maximum confidence.
        consistency = sum(value > 0 for value in months) / len(months) if months else .5
        signal *= confidence * (.5 + .5 * consistency)
    return .5 + .5 * signal


def apply_profit_learning(history, stage_records, weights):
    """Combine absolute economic evidence with existing relative funnel ranks."""
    observations = {}
    for stage, records in stage_records.items():
        if stage not in weights:
            continue
        for record in records:
            if record.get('error') or not record.get('result'):
                continue  # Infrastructure failures are not economic observations.
            qualified = ('objective_score' not in record or
                         math.isfinite(finite(record['objective_score'], float('nan'))))
            observations.setdefault(record['candidate_id'], []).append(
                (weights[stage], profit_evidence(record['result']) if qualified else 0.0))
    for record in history:
        evidence = observations.get(record.get('candidate_id'))
        if not evidence:
            continue
        weighted = sum(weight * value for weight, value in evidence) / sum(weight for weight, _ in evidence)
        # Emphasize the weakest observed regime as well as the weighted average.
        quality = .7 * weighted + .3 * min(value for _, value in evidence)
        rank = finite(record.get('learning_score'))
        record['learning_score'] = .35 * rank + .65 * quality
        record['learning_source'] = 'profit_evidence_v1'
    return history


def chronological_probe_indices(history):
    """Hold out whole latest search cycles; never use market OOS/holdout data."""
    cycles = {}
    for index, row in enumerate(history):
        candidate = str(row.get('candidate_id', ''))
        prefix = candidate.split('-', 1)[0]
        if not prefix.startswith('c') or not prefix[1:].isdigit():
            return None
        cycles.setdefault(int(prefix[1:]), []).append(index)
    if len(cycles) < 3:
        return None
    held_cycles = sorted(cycles)[-max(1, len(cycles) // 5):]
    validation = [index for cycle in held_cycles for index in cycles[cycle]]
    fit = [index for cycle in sorted(cycles) if cycle not in held_cycles for index in cycles[cycle]]
    return (validation, fit) if len(validation) >= 4 and len(fit) >= 8 else None
