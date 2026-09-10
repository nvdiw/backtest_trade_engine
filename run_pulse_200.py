"""Pulse quality search: 200 resumable cycles, 1m default, 15m supported."""
import argparse
import hashlib
import json
from pathlib import Path

from market_data import MarketDataSource

ROOT = Path(__file__).resolve().parent


def search_grid(timeframe):
    from pulse_strategy_config import PARAMETER_PROFILES
    grid = {key: list(values) for key, values in PARAMETER_PROFILES['quality'].items()}
    if timeframe == '15m':
        for side in ('long', 'short'):
            grid.update({f'{side}_{key}': values for key, values in dict(
                breakout_lookback_bars=[10, 20, 30, 60], max_hold_bars=[4, 8, 16, 32],
                trend_ma_bars=[0, 16, 32, 64], cooldown_bars=[0, 1, 2, 4]).items()})
    elif timeframe != '1m':
        raise ValueError('Pulse quality search supports 1m or 15m')
    return grid


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeframe', choices=('1m', '15m'), default='1m')
    parser.add_argument('--data-file')
    parser.add_argument('--output-dir')
    parser.add_argument('--resume', metavar='FOLDER')
    parser.add_argument('--seed-campaign', metavar='FOLDER')
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
        if args.seed_campaign:
            raise ValueError('--seed-campaign starts a new campaign; do not combine it with --resume')
        return ['--resume', args.resume, *controls]
    if args.seed_campaign:
        controls += ['--seed-campaign', args.seed_campaign]
    data_file = Path(args.data_file) if args.data_file else ROOT / 'data_candle' / (
        'btc_1m_data_2025_to_2026.csv' if args.timeframe == '1m' else 'btc_15m_data_2018_to_2026.csv')
    source = MarketDataSource(data_file, args.timeframe)
    source.interval()  # Reject a mislabeled dataset before creating outputs.
    stat = source.data_file.stat()
    recipe = json.dumps({'grid': search_grid(args.timeframe), 'tests': args.tests,
                         'seed_campaign': str(Path(args.seed_campaign).resolve()) if args.seed_campaign else None,
                         'version': 2}, sort_keys=True)
    revision = hashlib.sha256(
        f'{source.data_file}|{stat.st_size}|{stat.st_mtime_ns}|{recipe}'.encode()).hexdigest()[:12]
    output = Path(args.output_dir) if args.output_dir else (
        ROOT / 'outputs/pulse/optimize' / f'quality_{args.timeframe}_v2_{revision}')
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
    print('Pulse quality campaign | LONG + SHORT enabled | independent signals, exits and sizing per side')
    print(f'{args.cycles} cycles requested; snapshots every 50 for new campaigns.')
    import optimize
    optimize.main(command)


if __name__ == '__main__':
    main()
