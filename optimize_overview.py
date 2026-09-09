"""Compact views of complete candidate combinations, without dropping raw reports."""
import json
from pathlib import Path


OVERVIEW_COLUMNS = ('rank', 'decision', 'robust_score', 'final_total_profit_percent',
                    'final_maximum_drawdown', 'final_closed_trades', 'final_win_rate',
                    'final_liquidations', 'parameter_file')


def comparison_rows(rows, keys):
    top = rows[:5]
    # Put parameters that distinguish finalists first; retain shared values too.
    keys = sorted(keys, key=lambda key: (
        len({json.dumps(row.get(key), sort_keys=True) for row in top}) <= 1, key))
    return [dict(parameter=key, **{f'Rank {i}': row.get(key)
                                 for i, row in enumerate(top, 1)}) for key in keys]


def write_overview(output_dir, rows, keys, state):
    def value(item):
        if item is None:
            return '-'
        if isinstance(item, float):
            return f'{item:.3f}'
        return str(item).replace('|', '\\|').replace('\n', ' ')
    lines = ['# Best candidates now', '',
             f"Completed cycles: {state.get('cycles_completed', 0)} | Updated: {state.get('updated_at', '-')}", '',
             'Ranking compares complete parameter combinations across Auto stages. '
             'It does not establish which individual parameter value is universally best.', '']
    if rows:
        lines += [f"**Current leader: rank 1 — {rows[0].get('decision', 'WATCH')}**. "
                  'Parameters: [best_params.json](best_params.json).', '',
                  'Scores use campaign selection data; see decision reasons before choosing a candidate.', '',
                  '| Rank | Decision | Robust score | Profit % | Drawdown % | Trades | Win % | Liquidations | Parameters |',
                  '|---|---|---|---|---|---|---|---|---|']
        for row in rows[:5]:
            lines.append('| ' + ' | '.join(value(row.get(key)) for key in OVERVIEW_COLUMNS) + ' |')
        lines += ['', '## Parameter comparison', '',
                  'Differing values appear first. Columns refer to the ranked combinations above.', '']
        comparisons = comparison_rows(rows, keys)
        if comparisons:
            headers = list(comparisons[0])
            lines += ['| ' + ' | '.join(headers) + ' |',
                      '| ' + ' | '.join('---' for _ in headers) + ' |']
            lines += ['| ' + ' | '.join(value(row[key]) for key in headers) + ' |'
                      for row in comparisons]
        lines += ['', '## Decision details', '']
        lines += [f"- Rank {row['rank']}: {value(row.get('decision_reasons'))}" for row in rows[:5]]
    else:
        lines += ['No fully evaluated finalist yet. Any checkpoint parameters are provisional.']
    lines += ['', 'Full detail: hall_of_fame.csv, candidate_catalog.json and candidates/.', '']
    path = Path(output_dir) / 'OVERVIEW.md'
    temporary = path.with_suffix('.tmp')
    temporary.write_text('\n'.join(lines), encoding='utf-8')
    temporary.replace(path)


def print_leaders(rows, output_dir):
    if not rows:
        return
    print('\nBEST COMBINATIONS NOW | ranked across Auto stages')
    for row in rows[:3]:
        def metric(key):
            value = row.get(key)
            return f'{value:.2f}' if isinstance(value, (int, float)) else '-'
        print(f"  #{row['rank']} {row.get('decision', 'WATCH')} | "
              f"score {metric('robust_score')} | profit {metric('final_total_profit_percent')}% | "
              f"DD {metric('final_maximum_drawdown')}% | trades {metric('final_closed_trades')}")
    print(f'  Parameters + comparison: {Path(output_dir) / "OVERVIEW.md"}\n')
