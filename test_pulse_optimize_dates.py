import unittest
from argparse import Namespace
from types import SimpleNamespace

import optimize
import pulse_strategy as pulse
from run_pulse_optimize import campaign_arguments


class PulseOptimizeDateTests(unittest.TestCase):
    def protocol(self, extra=(), first='2025-01-01', end='2026-08-01'):
        argv = ['--strategy', 'pulse', '--auto', *extra]
        args = optimize.build_parser().parse_args(argv)
        optimize._apply_strategy_date_defaults(args, pulse, argv)
        coverage = dict(first_candle=first, last_candle=end, end_exclusive=end)
        return args, optimize._apply_date_policy(args, coverage)

    def test_current_data_and_appended_data(self):
        args, protocol = self.protocol()
        self.assertEqual(optimize._auto_ranges(args, args.auto_end), {
            'stress': ['2025-02-01', '2025-08-01'],
            'validation': ['2025-08-01', '2025-11-01'],
            'discovery': ['2025-11-01', '2026-08-01'],
            'final': ['2025-08-01', '2026-08-01'],
        })
        args, newer = self.protocol(end='2026-10-01')
        self.assertEqual(newer['development_start'], '2025-10-01')
        self.assertEqual(newer['stability_start'], '2025-04-01')
        optimize._restore_frozen_date_protocol(args, protocol)
        self.assertEqual(args.auto_end, '2026-08-01')

    def test_explicit_override_and_fixed_dates(self):
        args, protocol = self.protocol(['--rolling-development-months=10', '--rolling-validation-months', '2'])
        self.assertEqual(protocol['development_start'], '2025-10-01')
        self.assertEqual(protocol['discovery_start'], '2025-12-01')
        args, _ = self.protocol(['--date-policy', 'fixed', '--auto-end', '200'])
        self.assertEqual(args.auto_end, '200')
        self.assertEqual(args.rolling_development_months, 24)

    def test_clip_stress_and_reject_insufficient_data(self):
        _, protocol = self.protocol(first='2025-05-01')
        self.assertEqual(protocol['stability_start'], '2025-05-01')
        self.assertTrue(protocol['stress_clipped_to_data'])
        with self.assertRaisesRegex(ValueError, 'too short'):
            self.protocol(first='2025-09-01')

    def test_launcher_is_bounded_and_isolates_revisions(self):
        args = Namespace(data_file=str(pulse.DATA_FILE), performance='normal', workers=2, dry_run=True)
        coverage = {'end_exclusive': '2026-08-01 00:00:00'}
        stat = SimpleNamespace(st_size=100, st_mtime_ns=1)
        command = campaign_arguments(args, coverage, stat)
        parsed = optimize.build_parser().parse_args(command)
        self.assertEqual(parsed.auto_cycles, 4)
        self.assertEqual(parsed.auto_tests, 98)
        self.assertEqual(parsed.auto_halving_rungs, 0)
        self.assertEqual(command, campaign_arguments(args, coverage, stat))
        changed = campaign_arguments(args, coverage, SimpleNamespace(st_size=101, st_mtime_ns=2))
        self.assertNotEqual(parsed.output_dir, optimize.build_parser().parse_args(changed).output_dir)

    def test_phase_a_four_cycles_cover_grid(self):
        generator = optimize.SmartCandidateGenerator(
            pulse.PHASE_A_GRID, strategy_adapter=optimize._adapter_from_spec('pulse'))
        candidates = []
        for _ in range(4):
            candidates += generator.generate_auto(98, elites=[{'params': p, 'score': 1.} for p in candidates[:8]])
        self.assertEqual(len(candidates), 392)
        self.assertEqual(len(generator.seen), 392)
        self.assertEqual(generator.generate_auto(1, elites=[{'params': p, 'score': 1.} for p in candidates[:8]]), [])


if __name__ == '__main__':
    unittest.main()
