"""Pulse quality search: 200 resumable cycles, 1m default, 15m supported."""
import argparse
import hashlib
import json
from pathlib import Path

from market_data import MarketDataSource

ROOT = Path(__file__).resolve().parent


def search_grid(timeframe):
    minute = timeframe == '1m'
    return {
        'breakout_lookback_bars': [30, 60, 120, 240] if minute else [10, 20, 30, 60],
        'stop_atr_mult': [1.5, 2.0, 3.0, 4.0],
        'max_hold_bars': [30, 60, 120, 240, 480] if minute else [4, 8, 16, 32],
        'breakout_buffer_atr': [0., .1, .25, .5],
        'trend_ma_bars': [0, 60, 120, 240, 480] if minute else [0, 16, 32, 64],
        'min_efficiency_ratio': [0., .15, .3, .5],
        'min_volume_ratio': [0., 1., 1.5, 2.],
        'min_atr_cost_ratio': [0., .5, 1., 2., 3.],
        'max_signal_range_atr': [0., 2., 3., 4.],
        'max_entry_gap_atr': [0., .25, .5, 1.],
        'trailing_stop_atr_mult': [0., 1.5, 2., 3.],
        'trailing_activation_r': [1., 2., 3.],
        'cooldown_bars': [0, 5, 15, 30] if minute else [0, 1, 2, 4],
        'enable_long': [True, False],
        'enable_short': [True, False],
    }


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeframe', choices=('1m', '15m'), default='1m')
    parser.add_argument('--data-file')
    parser.add_argument('--output-dir')
    parser.add_argument('--resume', metavar='FOLDER')
    parser.add_argument('--cycles', type=int, default=200, help='additional completed cycles for this invocation')
    parser.add_argument('--tests', type=int, default=128, help='Discovery candidates per cycle (minimum 32)')
    parser.add_argument('-w', '--workers', type=int)
    parser.add_argument('--performance', choices=('normal', 'power_saving', 'boost'), default='normal')
    parser.add_argument('--dry-run', action='store_true')
    return parser


def campaign_arguments(args):
    if args.cycles <= 0 or args.tests < 32:
        raise ValueError('--cycles must be positive and --tests must be at least 32')
    controls = ['--auto-cycles', str(args.cycles), '--performance', args.performance]
    if args.workers is not None:
        controls += ['--workers', str(args.workers)]
    if args.dry_run:
        controls += ['--dry-run']
    if args.resume:
        return ['--resume', args.resume, *controls]
    data_file = Path(args.data_file) if args.data_file else ROOT / 'data_candle' / (
        'btc_1m_data_2025_to_2026.csv' if args.timeframe == '1m' else 'btc_15m_data_2018_to_2026.csv')
    source = MarketDataSource(data_file, args.timeframe)
    source.interval()  # Reject a mislabeled dataset before creating outputs.
    stat = source.data_file.stat()
    recipe = json.dumps({'grid': search_grid(args.timeframe), 'tests': args.tests,
                         'version': 1}, sort_keys=True)
    revision = hashlib.sha256(
        f'{source.data_file}|{stat.st_size}|{stat.st_mtime_ns}|{recipe}'.encode()).hexdigest()[:12]
    output = Path(args.output_dir) if args.output_dir else (
        ROOT / 'outputs/pulse/optimize' / f'quality_{args.timeframe}_v1_{revision}')
    # Module-backed grids make dry-run read-only and require no generated script.
    grid_spec = f'run_pulse_200:GRID_{args.timeframe.upper()}'
    return [
        '--strategy', 'pulse', '--auto', '--profile', 'full', '--param-grid', grid_spec,
        '--data-file', str(source.data_file), '--timeframe', args.timeframe,
        '--date-policy', 'auto', '--output-dir', str(output),
        '--base-source', 'config', '--base-params', str(output / 'best_params.json'),
        '--auto-tests', str(args.tests), '--auto-validation-top', '24',
        '--auto-stress-top', '12', '--auto-walk-forward-top', '8',
        '--auto-walk-forward-folds', '3', '--auto-final-top', '4',
        '--auto-hall-size', '100', '--auto-advanced-min-candidates', '32',
        '--auto-halving-rungs', '0', '--auto-surrogate-min-samples', '64',
        '--auto-surrogate-max-samples', '10000', '--auto-learning-target', 'profit-evidence',
        '--min-trades', '30', '--max-drawdown', '25',
        '--snapshot-cycles', '50', '--snapshot-top', '100', *controls,
    ]


GRID_1M = search_grid('1m')
GRID_15M = search_grid('15m')


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        command = campaign_arguments(args)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print('Pulse quality campaign | net costs, filters, exits and side selection | fixed default sizing')
    print(f'{args.cycles} cycles requested; snapshots every 50 for new campaigns.')
    import optimize
    optimize.main(command)


if __name__ == '__main__':
    main()
