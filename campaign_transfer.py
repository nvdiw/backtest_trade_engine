"""Read-only campaign transfer: reuse evidence only in an identical environment."""
import gzip
import csv
import json
import math
import random
from itertools import zip_longest
from pathlib import Path


def read_json(path, default=None):
    path = Path(path)
    if not path.is_file():
        return default
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rt', encoding='utf-8') as handle:
        return json.load(handle)


def prepare_transfer(source, config, features, grid, adapter, fingerprints, limit=256):
    source = Path(source).resolve()
    state = read_json(source / 'auto_state.json', {})
    previous = state.get('config', {})
    if previous.get('strategy') != adapter.identifier:
        raise ValueError('Seed campaign belongs to another strategy')
    if previous.get('timeframe') != config.get('timeframe'):
        raise ValueError('Seed campaign timeframe differs from the new campaign')
    if Path(previous.get('data_file', '')).resolve() != Path(config.get('data_file', '')).resolve():
        raise ValueError('Seed campaign market/data file differs from the new campaign')
    hall = read_json(source / 'hall_of_fame.json', [])
    cache = read_json(source / 'surrogate_history_cache.json.gz', {})
    old_keys = cache.get('parameter_keys', [])
    history = []
    for row in cache.get('rows', []):
        if len(row) != len(old_keys) + 3 or not isinstance(row[1], (int, float)) or not math.isfinite(row[1]):
            continue
        history.append(dict(candidate_id='transfer-' + str(row[0]), learning_score=row[1],
                            learning_source=row[2], params=dict(zip(old_keys, row[3:]))))
    cached_evidence = bool(history) and cache.get('version') == 2
    if not history:
        old_keys = list(previous.get('parameter_grid', {}))
        files = sorted((source / 'cycles').glob('cycle_*/discovery_results.csv*'))
        # Read representative complete cycles without modifying/rebuilding the source cache.
        if len(files) > 64:
            files = [files[round(i * (len(files)-1) / 63)] for i in range(64)]
        for path in files:
            opener = gzip.open if path.suffix == '.gz' else open
            with opener(path, 'rt', encoding='utf-8', newline='') as handle:
                for row in csv.DictReader(handle):
                    if row.get('error') or not all(row.get(key) not in (None, '') for key in old_keys):
                        continue
                    def decode(value):
                        try: return json.loads(value.lower() if value in ('True', 'False') else value)
                        except (ValueError, TypeError): return value
                    score = decode(row.get('objective_score', ''))
                    if not isinstance(score, (int, float)) or not math.isfinite(score):
                        score = -1e30
                    history.append(dict(candidate_id='transfer-' + str(row.get('candidate_id')),
                                        learning_score=score, learning_source='proposal_only_csv',
                                        params={key: decode(row[key]) for key in old_keys}))
    ordered = sorted(history, key=lambda row: row['learning_score'])
    # Cover poor, middle and strong outcomes, not just the Hall of Fame.
    sample_count = min(max(1, limit - min(len(hall), limit // 2)), len(ordered))
    sample = [ordered[round(i * (len(ordered)-1) / max(1, sample_count-1))]
              for i in range(sample_count)] if ordered else []
    proposals, seen = [], set()
    random.Random(0).shuffle(sample)
    candidates = [record for pair in zip_longest(hall[:limit // 2], sample) for record in pair if record is not None]
    for record in candidates:
        original = {**previous.get('base_tune', {}), **(record.get('effective_params') or record.get('params', {}))}
        params = {}
        for key, values in grid.items():
            value = original.get(key)
            if value is None and key.startswith(('long_', 'short_')):
                value = original.get(key.split('_', 1)[1])
            if value in values:
                params[key] = value
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                params[key] = min(values, key=lambda candidate: abs(candidate-value))
            else:
                params[key] = values[0]
        signature = tuple(params.values())
        if signature not in seen and adapter.validate_candidate({**config.get('base_tune', {}), **params}):
            seen.add(signature)
            proposals.append(params)
    old_fp = read_json(source / 'research_manifest.json', {}).get('fingerprints', {})
    same_fingerprints = all(fingerprints.get(key) and fingerprints[key] == old_fp.get(key)
                            for key in ('data_sha256', 'code_sha256'))
    compatible = (cached_evidence and same_fingerprints and previous == config and state.get('optimizer_features') == features
                  and set(old_keys) == set(grid))
    usable_history = [row for row in history if all(row['params'].get(key) in values for key, values in grid.items())]
    summary = dict(source=str(source), source_cycles=state.get('cycles_completed', 0),
                   history_through_cycle=cache.get('latest_cycle'),
                   source_hall_candidates=len(hall), source_history_samples=len(history),
                   proposal_count=len(proposals), training_samples=len(usable_history) if compatible else 0,
                   mode='compatible_history' if compatible else 'retest_proposals',
                   explanation='Old scores are never copied into the new Hall of Fame; proposals run through the new funnel.')
    return dict(summary=summary, proposals=proposals, history=usable_history if compatible else [],
                source_importance=read_json(source / 'parameter_importance.json', {}),
                source_mutation_guidance=read_json(source / 'mutation_guidance.json', {}))


def save_transfer(path, payload):
    path = Path(path)
    temporary = path.with_suffix('.tmp')
    with gzip.open(temporary, 'wt', encoding='utf-8') as handle:
        json.dump(payload, handle)
    temporary.replace(path)
