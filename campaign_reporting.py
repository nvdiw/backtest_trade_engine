"""Persistent campaign status and rejection evidence, including zero-finalist runs."""
import csv
import gzip
import json
import math
from pathlib import Path

import pandas as pd

DIRECTION_METRICS = [f'{side}_{metric}' for side in ('long', 'short')
                     for metric in ('trades', 'profit', 'win_rate', 'profit_factor', 'maximum_drawdown')]


def observation(row, profit, trades, score):
    return {'candidate_id': row.get('candidate_id'), 'net_return_percent': profit,
            'closed_trades': trades, 'eligible': score is not None,
            **{key: number(row.get(key)) for key in DIRECTION_METRICS},
            **{key: row.get(key) for key in ('enable_long', 'enable_short')}}


def read_json(path, default=None):
    return json.loads(Path(path).read_text(encoding='utf-8')) if Path(path).is_file() else default


def write_json(path, payload):
    path = Path(path)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def collect_stage_evidence(directory, cycle=None):
    """Read stored outcomes; never rerun trades or change candidate eligibility."""
    directory = Path(directory)
    state = read_json(directory / 'auto_state.json', {})
    config = state.get('config', {})
    minimum = config.get('minimum_trades', 0)
    max_dd = config.get('maximum_allowed_drawdown')
    summaries = read_json(directory / 'stage_evidence.json', {})
    pattern = f'cycle_{cycle:06d}/*results.csv*' if cycle is not None else 'cycle_*/*results.csv*'
    for path in sorted((directory / 'cycles').glob(pattern)):
        stage = path.name.split('_results.csv')[0]
        key = path.parent.name + '/' + stage
        info = dict(cycle=int(path.parent.name.split('_')[1]), stage=stage, evaluations=0,
                    eligible=0, rejected=0, errors=0, no_trades=0, insufficient_trades=0,
                    insufficient_directional_trades=0,
                    excessive_drawdown=0, other_rejections=0, positive_net_return=0)
        opener = gzip.open if path.suffix == '.gz' else open
        best = None
        direction_best = {}
        with opener(path, 'rt', encoding='utf-8', newline='') as handle:
            for row in csv.DictReader(handle):
                info['evaluations'] += 1
                trades = number(row.get('closed_trades')) or 0
                profit = number(row.get('total_profit_percent'))
                score = number(row.get('objective_score'))
                info['positive_net_return'] += profit is not None and profit > 0
                if row.get('error'):
                    info['errors'] += 1
                elif score is not None:
                    info['eligible'] += 1
                else:
                    info['rejected'] += 1
                    # Older files have no gate value. The legacy rule is recoverable.
                    gate = number(row.get('required_trades'))
                    if gate is None:
                        folds = (state.get('optimizer_features') or {}).get('walk_forward_folds', 3)
                        gate = math.ceil(minimum / max(1, folds)) if stage.startswith('walk_forward_fold') else minimum
                    dd = number(row.get('maximum_drawdown'))
                    if trades == 0:
                        info['no_trades'] += 1
                    elif trades < gate:
                        info['insufficient_trades'] += 1
                    elif (number(row.get('required_directional_trades')) or 0) > 0 and any(
                        str(row.get('enable_' + side, 'True')).lower() != 'false'
                        and (number(row.get(side + '_trades')) or 0) < number(row['required_directional_trades'])
                        for side in ('long', 'short')
                    ):
                        info['insufficient_directional_trades'] += 1
                    elif max_dd is not None and dd is not None and abs(dd) > max_dd:
                        info['excessive_drawdown'] += 1
                    else:
                        info['other_rejections'] += 1
                if profit is not None and not row.get('error') and (best is None or profit > best['net_return_percent']):
                    best = observation(row, profit, trades, score)
                if profit is not None and not row.get('error'):
                    for side in ('long', 'short'):
                        side_profit = number(row.get(side + '_profit'))
                        if (number(row.get(side + '_trades')) or 0) > 0 and side_profit is not None:
                            previous = direction_best.get(side)
                            if previous is None or side_profit > previous[side + '_profit']:
                                direction_best[side] = observation(row, profit, trades, score)
        info['best_observed'] = best
        info['direction_best'] = direction_best
        summaries[key] = info
    write_json(directory / 'stage_evidence.json', summaries)
    return summaries


def publish_campaign(directory, state=None, excel=True, rebuild=False, cycle=None):
    directory = Path(directory)
    state = state or read_json(directory / 'auto_state.json', {})
    if not state:
        raise ValueError(f'No Auto checkpoint found in {directory}')
    evidence = (collect_stage_evidence(directory, cycle) if rebuild or cycle is not None
                else read_json(directory / 'stage_evidence.json', {}))
    hall = read_json(directory / 'hall_of_fame.json', [])
    from optimizer_selection import _auto_candidate_decision
    accepted = [record for record in hall if _auto_candidate_decision(record)['decision'] == 'ACCEPT']
    result = dict(status=state.get('status'), cycles_completed=state.get('cycles_completed', 0),
                  current_cycle=state.get('cycle'), current_stage=state.get('stage'),
                  evaluations=state.get('total_evaluations', 0), qualified_finalists=len(accepted),
                  observed_finalists=len(hall),
                  strategy=state.get('config', {}).get('strategy'), timeframe=state.get('config', {}).get('timeframe'),
                  outcome='QUALIFIED_FINALISTS' if accepted else 'NO_QUALIFIED_FINALIST',
                  recommended_params='best_params.json' if accepted and hall[0] in accepted else None,
                  note='Checkpoint parameters are provisional; no approved winner.' if not accepted else 'Auto selection; independent validation still required.')
    stages = []
    leaders = []
    direction_leaders = []
    for _, item in sorted(evidence.items()):
        stages.append({key: value for key, value in item.items() if key not in ('best_observed', 'direction_best')})
        if item.get('best_observed'):
            leaders.append({'cycle': item['cycle'], 'stage': item['stage'], **item['best_observed'],
                            'scope': 'stage observation, not a final winner'})
        for side, leader in item.get('direction_best', {}).items():
            direction_leaders.append({'cycle': item['cycle'], 'stage': item['stage'],
                                      'selected_by': side + ' net profit', **leader,
                                      'scope': 'side contribution within tested portfolio; not a standalone backtest or final winner'})
    stage_columns = ['cycle', 'stage', 'evaluations', 'eligible', 'rejected', 'errors', 'no_trades',
                     'insufficient_trades', 'insufficient_directional_trades', 'excessive_drawdown', 'other_rejections', 'positive_net_return']
    stage_frame = pd.DataFrame(stages, columns=stage_columns)
    aggregate = stage_frame.groupby('stage', sort=False).sum(numeric_only=True).drop(columns='cycle', errors='ignore').reset_index()
    status_frame = pd.DataFrame(list(result.items()), columns=['Metric', 'Value'])
    leader_columns = ['cycle', 'stage', 'candidate_id', 'net_return_percent', 'closed_trades',
                      'eligible', 'enable_long', 'enable_short', *DIRECTION_METRICS, 'scope']
    leader_frame = pd.DataFrame(leaders, columns=leader_columns)
    direction_frame = pd.DataFrame(direction_leaders, columns=['selected_by', *leader_columns])
    status_frame.to_csv(directory / 'campaign_status.csv', index=False)
    aggregate.to_csv(directory / 'stage_summary.csv', index=False)
    leader_frame.to_csv(directory / 'stage_observations.csv', index=False)
    direction_frame.to_csv(directory / 'direction_observations.csv', index=False)
    write_json(directory / 'campaign_status.json', result)
    text = '\n'.join(f'{key}: {value}' for key, value in result.items()) + '\n\n' + aggregate.to_string(index=False) + '\n'
    (directory / 'campaign.log').write_text(text, encoding='utf-8')
    (directory / 'CAMPAIGN.md').write_text('# Campaign result\n\n```text\n' + text + '```\n', encoding='utf-8')
    if excel:
        from openpyxl.styles import Font, PatternFill
        path = directory / 'campaign_report.xlsx'
        temp = directory / '.campaign_report.building.xlsx'
        with pd.ExcelWriter(temp, engine='openpyxl') as writer:
            for name, frame in [('Campaign', status_frame), ('Direction Leaders', direction_frame),
                                ('Stage Observations', leader_frame), ('Stage Summary', aggregate),
                                ('Per Cycle', stage_frame)]:
                frame.to_excel(writer, sheet_name=name, index=False)
                sheet = writer.sheets[name]
                sheet.freeze_panes = 'A2'
                sheet.sheet_view.showGridLines = False
                for cell in sheet[1]:
                    cell.fill = PatternFill('solid', fgColor='17365D')
                    cell.font = Font(bold=True, color='FFFFFF')
                sheet.auto_filter.ref = sheet.dimensions
                for column in sheet.columns:
                    sheet.column_dimensions[column[0].column_letter].width = min(65, max(16, max(len(str(cell.value or '')) for cell in column[:200]) + 2))
            from report_style import style_report
            style_report(writer.book)
        temp.replace(path)
    return result
