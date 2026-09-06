"""Run a bounded Pulse Phase A campaign; resume only the same dataset revision."""
import argparse
from datetime import datetime
import hashlib
from pathlib import Path

from market_data import MarketDataSource
import pulse_strategy as pulse


def campaign_arguments(args, coverage, stat):
    source = Path(args.data_file).resolve()
    revision = hashlib.sha256(
        f'{source}|{stat.st_size}|{stat.st_mtime_ns}'.encode()
    ).hexdigest()[:10]
    end = datetime.fromisoformat(coverage['end_exclusive']).strftime('%Y%m%dT%H%M%S')
    output = Path(__file__).resolve().parent / 'outputs/pulse/optimize' / f'phase_a_{end}_{revision}'
    command = [
        '--strategy', 'pulse', '--profile', 'signal', '--auto',
        '--data-file', str(source), '--timeframe', '1m', '--date-policy', 'auto',
        '--base-source', 'config', '--base-params', str(output / 'best_params.json'),
        '--output-dir', str(output), '--performance', args.performance,
        '--auto-cycles', '4', '--auto-tests', '98',
        '--auto-validation-top', '32', '--auto-stress-top', '16',
        '--auto-walk-forward-top', '8', '--auto-walk-forward-folds', '3',
        '--auto-final-top', '8', '--auto-hall-size', '32',
        '--auto-advanced-min-candidates', '32', '--auto-halving-rungs', '0',
        '--snapshot-cycles', '4', '--snapshot-top', '8',
    ]
    if args.workers is not None:
        command += ['--workers', str(args.workers)]
    if args.dry_run:
        command.append('--dry-run')
    return command


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-file', default=str(pulse.DATA_FILE))
    parser.add_argument('--performance', choices=['power_saving', 'normal', 'boost'], default='normal')
    parser.add_argument('--workers', type=int)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    source = MarketDataSource(args.data_file, '1m')
    command = campaign_arguments(args, source.coverage(), source.data_file.stat())
    print('Pulse campaign: ' + command[command.index('--output-dir') + 1])
    import optimize
    optimize.main(command)


if __name__ == '__main__':
    main()
