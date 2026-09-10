"""Start 300 new Pulse cycles using the previous campaign's search history."""
import argparse
from pathlib import Path


def main(argv=None):
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seed-campaign', default=str(root / 'outputs/pulse/optimize/2026_09_10'))
    parser.add_argument('--output-dir', default=str(root / 'outputs/pulse/optimize/2026-09-11'))
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    print(f'Seed campaign: {args.seed_campaign}')
    print(f'New output: {args.output_dir}')
    command = ['pulse', '--timeframe', '1m', '--seed-campaign', args.seed_campaign,
               '--output-dir', args.output_dir, '--cycles', '300', '--workers', str(args.workers)]
    if args.dry_run:
        command.append('--dry-run')
    import optimize
    optimize.main(command)


if __name__ == '__main__':
    main()
