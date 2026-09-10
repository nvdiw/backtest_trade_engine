"""Bounded, chronological ablation of Pulse options; never promotes parameters.

These are development diagnostics, not sealed out-of-sample results: an input
parameter file may already have been selected using any of the windows below.
"""
import argparse
from dataclasses import asdict
import json
from pathlib import Path

import pandas as pd
import pulse_strategy as pulse


VARIANTS = {
    'baseline': {},
    'volatility_guard': {'max_atr_expansion': 2.},
    'stagnation_exit': {'stagnation_exit_bars': 30, 'stagnation_min_progress_r': .5},
    'combined_guards': {'max_atr_expansion': 2., 'stagnation_exit_bars': 30, 'stagnation_min_progress_r': .5},
    'entry_score_55': {'entry_score_min': 55.},
    'failed_breakout_6': {'failed_breakout_bars': 6},
    'breakeven_1r': {'breakeven_activation_r': 1.},
    'mixed': {'entry_score_min': 40., 'failed_breakout_bars': 6, 'breakeven_activation_r': 1.},
    'sizing_only': {'leverage': 3., 'trade_amount_percent': .25, 'risk_per_trade': .005, 'max_gross_exposure': 3.},
    'mixed_sizing': {'entry_score_min': 40., 'failed_breakout_bars': 6, 'breakeven_activation_r': 1.,
                     'leverage': 3., 'trade_amount_percent': .25, 'risk_per_trade': .005, 'max_gross_exposure': 3.},
    'mixed_both_sides': {'entry_score_min': 40., 'failed_breakout_bars': 6,
                         'breakeven_activation_r': 1., 'enable_long': True, 'enable_short': True},
}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--params-file', type=Path)
    parser.add_argument('--data-file', default=str(pulse.DATA_FILE))
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--variants', nargs='+', choices=list(VARIANTS), default=list(VARIANTS))
    parser.add_argument('--windows', nargs='+', default=['2025-01-01:2025-04-01',
                                                       '2025-07-01:2025-10-01',
                                                       '2026-01-01:2026-04-01'])
    args = parser.parse_args(argv)
    base = asdict(pulse.build_strategy_config(json.loads(args.params_file.read_text()) if args.params_file else {}))
    # Clear new side overrides as well: baseline and each ablation must be isolated.
    for key in ('entry_score_min', 'failed_breakout_bars', 'breakeven_activation_r',
                'max_atr_expansion', 'stagnation_exit_bars'):
        base[key] = 0
        for side in ('long', 'short'):
            base[side + '_' + key] = None
    args.output_dir.mkdir(parents=True, exist_ok=False)
    (args.output_dir / 'experiment.json').write_text(json.dumps(
        {'scope': __doc__, 'base': base, 'variants': {name: VARIANTS[name] for name in args.variants}, 'windows': args.windows,
         'data_file': str(Path(args.data_file).resolve())}, indent=2))
    rows = []
    for window in args.windows:
        start, end = window.split(':')
        for name in args.variants:
            changes = VARIANTS[name]
            overrides = dict(changes)
            for key, value in changes.items():
                for side in ('long', 'short'):
                    if side + '_' + key in base:
                        overrides[side + '_' + key] = value
            result = pulse.pulse_strategy({**base, **overrides, 'optimize': True}, start, end,
                                         data_file=args.data_file, verbose=False)
            row = dict(variant=name, start=start, end_exclusive=end, **{key: result.get(key) for key in
                ('total_profit_percent', 'maximum_drawdown', 'closed_trades', 'profit_factor',
                 'total_fees', 'long_trades', 'short_trades', 'long_profit', 'short_profit', 'liquidations')})
            row['diagnostics'] = json.dumps(result['diagnostics'], sort_keys=True)
            rows.append(row)
            pd.DataFrame(rows).to_csv(args.output_dir / 'comparison.csv', index=False)
            print(f"{start} {name}: return {row['total_profit_percent']:.2f}% | "
                  f"DD {row['maximum_drawdown']:.2f}% | trades {row['closed_trades']}", flush=True)


if __name__ == '__main__':
    main()
