"""Selection constraints; these never alter strategy signals or position sizing."""
import math

from optimizer_evidence import DIRECTIONAL_MIN_TRADES, finite


def required_directional_trades(strategy, stage_candles, final_candles):
    """Pulse needs 30 trades per enabled side over Final, scaled for short stages."""
    if strategy not in ('pulse', 'pulse_strategy:pulse_strategy'):
        return 0
    return max(1, math.ceil(DIRECTIONAL_MIN_TRADES * stage_candles / max(1, final_candles)))


def apply_directional_gate(record, minimum, base_params=None):
    """Recheck new and resumed evidence identically, retaining raw trade metrics."""
    if not minimum or record.get('error'):
        return record
    params = {**(base_params or {}), **record.get('params', {})}
    result = record.get('result') or {}
    missing = [side for side in ('long', 'short')
               if params.get('enable_' + side, True) is not False
               and finite(result.get(side + '_trades')) < minimum]
    record['required_directional_trades'] = minimum
    record['insufficient_directions'] = missing
    if missing:
        record['objective_score'] = None
        record['time_normalized_score'] = None
        record['learning_score'] = 0.0
        record['learning_source'] = 'insufficient_directional_trades'
    return record
