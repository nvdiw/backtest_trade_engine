"""Run a bounded Pulse quality campaign; signal-only Phase A remains explicit."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path

from market_data import MarketDataSource
import pulse_strategy as pulse


def campaign_arguments(args, coverage, stat):
    source = Path(args.data_file).resolve()
    profile = getattr(args, 'profile', 'quality')
    seed_source = getattr(args, 'seed_campaign', None)
    recipe = json.dumps({'grid': pulse.PARAMETER_PROFILES[profile],
                         'seed_campaign': str(Path(seed_source).resolve()) if seed_source else None}, sort_keys=True)
    revision = hashlib.sha256(
        f'{source}|{stat.st_size}|{stat.st_mtime_ns}|v3|{profile}|{recipe}'.encode()
    ).hexdigest()[:10]
    end = datetime.fromisoformat(coverage['end_exclusive']).strftime('%Y%m%dT%H%M%S')
    output = Path(__file__).resolve().parent / 'outputs/pulse/optimize' / f'{profile}_v3_{end}_{revision}'
    command = [
        '--strategy', 'pulse', '--profile', profile, '--auto',
        '--data-file', str(source), '--timeframe', '1m', '--date-policy', 'auto',
        '--base-params', str(output / 'best_params.json'),
        '--output-dir', str(output), '--performance', args.performance,
    ]
    for key, value in pulse.OPTIMIZER_DEFAULTS.items():
        if key != 'profile':
            command += ['--' + key.replace('_', '-'), str(value)]
    if args.workers is not None:
        command += ['--workers', str(args.workers)]
    if seed_source:
        command += ['--seed-campaign', seed_source]
    if args.dry_run:
        command.append('--dry-run')
    return command


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-file', default=str(pulse.DATA_FILE))
    parser.add_argument('--performance', choices=['power_saving', 'normal', 'boost'], default='normal')
    parser.add_argument('--workers', type=int)
    parser.add_argument('--seed-campaign', metavar='FOLDER')
    parser.add_argument('--profile', choices=sorted(pulse.PARAMETER_PROFILES), default='quality')
    parser.add_argument('--dry-run', action='store_true')
    args, extra = parser.parse_known_args(argv)
    source = MarketDataSource(args.data_file, '1m')
    command = campaign_arguments(args, source.coverage(), source.data_file.stat())
    command += extra
    import optimize
    selected = optimize.build_parser().parse_args(command)
    print('Pulse campaign: ' + selected.output_dir)
    optimize.main(command)


if __name__ == '__main__':
    main()
