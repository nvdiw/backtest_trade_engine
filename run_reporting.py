"""One output contract for every strategy, market and candle interval."""
import json
from pathlib import Path

import numpy as np
import pandas as pd

from check_monthly_data import write_monthly_summary
from market_data import MarketDataSource


def market_metadata(market, strategy, symbol=None):
    source = MarketDataSource(market['data_file'], 'auto')
    coverage = source.coverage()
    return {'strategy_name': strategy, 'symbol': symbol or source.data_file.stem.split('_')[0].upper(),
            'timeframe': coverage['timeframe'], 'data_file': str(source.data_file)}


def write_metadata(directory, result, parameters):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name, payload in (('result.json', result), ('params.json', parameters)):
        temporary = directory / (name + '.tmp')
        temporary.write_text(json.dumps(payload, indent=2,
            default=lambda value: value.item() if isinstance(value, np.generic) else str(value)) + '\n', encoding='utf-8')
        temporary.replace(directory / name)


def publish_backtest(engine, result, parameters, start, end, first_balance, monthly, output_file=None):
    """Both account adapters call this; strategy code does not serialize reports."""
    result.update(engine.market_metadata)
    result['report_schema_version'] = 1
    result['backtest_start'] = str(start)
    result['backtest_end_exclusive'] = str(end)
    summary, months = monthly
    sections = {
        'Run': {'Strategy': result.get('strategy_name', 'Strategy'),
                'Symbol': result.get('symbol', 'unspecified'), 'Timeframe': result.get('timeframe', 'unspecified'),
                'Start time': str(start), 'End time': str(end), **summary},
        'Capital': {'Starting balance': first_balance, 'Final balance': result['final_balance'],
                    'Final balance without fees': result.get('final_balance_without_fee'),
                    'Static balance': result.get('final_balance_static'),
                    'Dynamic marked balance': result.get('final_balance_dynamic'),
                    'Total profit': result['total_profit'], 'Total profit %': result['total_profit_percent'],
                    'Realized profit': result.get('realized_profit'), 'Unrealized profit': result.get('unrealized_profit'),
                    'Total fees': result['total_fees'], 'Saved money': result.get('saved_money')},
        'Performance': {'Optimizer score': result['score'], 'Maximum drawdown %': result['maximum_drawdown'],
                        'Win rate %': result['win_rate'], 'Profit factor': result.get('profit_factor'),
                        'Expectancy %': result.get('expectancy_percent'), 'Calmar ratio': result.get('calmar_ratio'),
                        'Profitable months': result.get('profit_months'), 'Losing months': result.get('loss_months'),
                        'Stronger side': result.get('stronger_side'), 'Net-profit gap': result.get('directional_profit_gap')},
        'Trades': {key: result.get(key) for key in ('closed_trades', 'wins', 'losses', 'open_positions', 'liquidations',
                   'long_trades', 'long_wins', 'long_losses', 'short_trades', 'short_wins', 'short_losses')},
        'Long': engine._overview_side_metrics('Long', result),
        'Short': engine._overview_side_metrics('Short', result),
        'Strategy': {'Name': result.get('strategy_name', 'Strategy'), **parameters},
    }
    for prefix in ('rsi', 'scale'):
        values = {key: value for key, value in result.items() if key.startswith(prefix + '_') or '_' + prefix + '_' in key}
        if values:
            sections[prefix.upper()] = values
    sections['Trades']['Realized profit / trade'] = (result.get('realized_profit', 0) / result['closed_trades']
                                                    if result['closed_trades'] else 0)
    lines = [f"{section} | {key}: {value}" for section, values in sections.items()
             for key, value in values.items()]
    if engine.verbose:
        print('BACKTEST FINISHED')
        print(f"Final Balance: {result['final_balance']} | Total Fees Paid: {result['total_fees']}")
        print(f"Maximum Drawdown: {result['maximum_drawdown']} | Total score: {result['score']}")
        print('\n'.join(lines))
    if not engine.write_trades:
        return
    directory = Path(engine.output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = str(output_file or directory / 'trades/data_orders.csv')
    elapsed_minutes = int((pd.Timestamp(end) - pd.Timestamp(start)).total_seconds() / 60)
    days, remainder = divmod(elapsed_minutes, 1440)
    hours, minutes = divmod(remainder, 60)
    engine.csv_logger.save_csv(first_balance=first_balance, final_balance=result['final_balance'],
        total_profit=result['total_profit'], total_profit_percent=result['total_profit_percent'],
        total_fee=result['total_fees'], start_time=start, end_time=end,
        days=days, hours=hours, minutes=minutes, overview_metrics=sections,
        file_name=path, monthly_report=monthly, run_metadata=engine.market_metadata)
    write_monthly_summary(in_file=path, out_file=str(directory / 'monthly/monthly_data_orders.csv'), quiet=True)
    numeric = {key: value for key, value in result.items() if not isinstance(value, (dict, list, tuple))}
    pd.DataFrame([numeric]).to_csv(directory / 'run_summary.csv', index=False)
    (directory / 'run.log').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    write_metadata(directory, result, parameters)
