"""Replay a stored Auto candidate with its actual parameters, window and market."""
import json
from pathlib import Path


def load_campaign_replay(directory, rank=1, stage='final'):
    directory = Path(directory).resolve()
    state = json.loads((directory / 'auto_state.json').read_text(encoding='utf-8'))
    config = state['config']
    if config.get('strategy') != 'pulse_strategy:pulse_strategy':
        raise ValueError('Replay folder is not a Pulse campaign')
    hall = json.loads((directory / 'hall_of_fame.json').read_text(encoding='utf-8'))
    if rank < 1 or rank > len(hall):
        raise ValueError('Replay rank is outside this campaign Hall of Fame')
    record = hall[rank-1]
    start, end = config['ranges'][stage]
    params = record.get('effective_params') or {**config.get('base_tune', {}), **record['params']}
    return dict(params=params, start=str(start), end=str(end), data_file=config['data_file'],
                timeframe=config['timeframe'], use_indicator_warmup=bool(config.get('indicator_warmup', False)),
                candidate_id=record['candidate_id'], expected=record.get('stage_metrics', {}).get(stage, {}),
                source=str(directory), stage=stage)
