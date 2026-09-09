import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import optimize
import pulse_strategy as pulse
from optimizer_evidence import profit_evidence, apply_profit_learning, chronological_probe_indices
from run_pulse_200 import build_parser, campaign_arguments, search_grid
from strategy_adapter import load_grid_source
from test_pulse_strategy import PulseTests as _PulseRunner, candles


class PulseTimeframeTests(unittest.TestCase):
    setUp = _PulseRunner.setUp
    tearDown = _PulseRunner.tearDown
    run_frame = _PulseRunner.run_frame
    events = _PulseRunner.events

    def fifteen_frame(self):
        frame = candles()
        frame['Open time'] = pd.date_range('2025-01-01', periods=len(frame), freq='15min')
        frame['Close time'] = frame['Open time'] + pd.Timedelta(minutes=15) - pd.Timedelta(milliseconds=1)
        return frame

    def test_fifteen_minute_same_bar_execution_without_false_gaps(self):
        minute = self.run_frame()
        fifteen = self.run_frame(self.fifteen_frame(), timeframe='15m')
        self.assertEqual(minute['events'], fifteen['events'])
        self.assertEqual(fifteen['timeframe'], '15m')
        self.assertEqual(fifteen['diagnostics'].get('data_gap', 0), 0)

    def test_fifteen_gap_and_unclosed_candle(self):
        frame = self.fifteen_frame()
        result = self.run_frame(frame, timeframe='15m', now=frame.loc[22, 'Open time'] + pd.Timedelta(minutes=5))
        self.assertFalse(self.events(result, 'entry'))
        frame.loc[23:, 'Open time'] += pd.Timedelta(minutes=15)
        frame.loc[23:, 'Close time'] += pd.Timedelta(minutes=15)
        result = self.run_frame(frame, timeframe='15m')
        self.assertEqual(result['diagnostics']['data_gap'], 1)
        self.assertEqual(self.events(result, 'exit')[0]['reason'], 'data_gap_exit')

    def test_adverse_entry_gap_is_rejected_for_both_sides(self):
        for mirrored in (False, True):
            frame = candles()
            if mirrored:
                original = frame.copy()
                for key in ('Open', 'Close'):
                    frame[key] = 200 - original[key]
                frame['High'], frame['Low'] = 200 - original.Low, 200 - original.High
            baseline = self.run_frame(frame)
            filtered = self.run_frame(frame, tune={'max_entry_gap_atr': .5})
            self.assertTrue(self.events(baseline, 'entry'))
            self.assertFalse(any(e['bar'] == 22 for e in self.events(filtered, 'entry')))
            self.assertTrue(any(e['bar'] == 22 and e['reason'] == 'entry_gap_filter'
                                for e in self.events(filtered, 'entry_rejected')))

    def test_timeframe_mismatch_fails(self):
        with self.assertRaisesRegex(ValueError, 'does not match'):
            self.run_frame(timeframe='15m')


class ProfitLearningTests(unittest.TestCase):
    def result(self, **overrides):
        return dict(closed_trades=200, total_profit_percent=20, maximum_drawdown=-5,
                    monthly_returns=[.02] * 12, liquidations=0) | overrides

    def test_rewards_stability_and_economic_profit(self):
        good = profit_evidence(self.result())
        self.assertGreater(good, .5)
        self.assertLess(profit_evidence(self.result(total_profit_percent=-20)), .5)
        self.assertLess(profit_evidence(self.result(monthly_returns=[-.2, .5])), good)
        self.assertLess(profit_evidence(self.result(closed_trades=2)), good)
        self.assertEqual(profit_evidence(self.result(liquidations=1)), 0)
        self.assertEqual(profit_evidence(self.result(closed_trades=0)), 0)

    def test_funnel_uses_failures_but_ignores_execution_errors(self):
        history = [{'candidate_id': 'win', 'learning_score': .8},
                   {'candidate_id': 'loss', 'learning_score': .8},
                   {'candidate_id': 'empty'}, {'candidate_id': 'error'}]
        stages = {'discovery': [
            {'candidate_id': 'win', 'result': self.result()},
            {'candidate_id': 'loss', 'result': self.result(total_profit_percent=-30)},
            {'candidate_id': 'empty', 'result': self.result(closed_trades=0)},
            {'candidate_id': 'error', 'result': {}, 'error': 'worker failed'},
        ]}
        apply_profit_learning(history, stages, {'discovery': 1})
        self.assertGreater(history[0]['learning_score'], history[1]['learning_score'])
        self.assertEqual(history[2]['learning_score'], 0)
        self.assertNotIn('learning_score', history[3])

    def test_later_market_regime_changes_learning_without_holdout(self):
        history = [{'candidate_id': 'a', 'learning_score': .8}]
        stages = {'discovery': [{'candidate_id': 'a', 'result': self.result()}]}
        strong = apply_profit_learning(copy.deepcopy(history), stages, {'discovery': .5})[0]['learning_score']
        stages['stress'] = [{'candidate_id': 'a', 'result': self.result(total_profit_percent=-50)}]
        weak = apply_profit_learning(history, stages, {'discovery': .5, 'stress': .5})[0]['learning_score']
        self.assertLess(weak, strong)

    def test_disqualified_profit_is_not_a_positive_training_example(self):
        history = [{'candidate_id': 'a', 'learning_score': 0}]
        stages = {'discovery': [{'candidate_id': 'a', 'result': self.result(),
                                 'objective_score': None}]}
        apply_profit_learning(history, stages, {'discovery': 1})
        self.assertEqual(history[0]['learning_score'], 0)

    def test_evidence_and_zero_labels_survive_cache_round_trip(self):
        history = [{'candidate_id': 'c000001-000001', 'params': {'x': 1},
                    'learning_score': 0., 'learning_source': 'profit_evidence_v1'},
                   {'candidate_id': 'c000001-000002', 'params': {'x': 2},
                    'learning_score': .7, 'learning_source': 'profit_evidence_v1'}]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'history.json.gz'
            optimize._write_surrogate_history_cache(path, history, ('x',), 1)
            loaded, cycle = optimize._read_surrogate_history_cache(path, ('x',))
        self.assertEqual(cycle, 1)
        self.assertEqual(loaded, history)

    def test_probe_separates_whole_search_cycles(self):
        history = [{'candidate_id': f'c{cycle:06d}-{i:06d}'}
                   for cycle in range(1, 6) for i in range(10)]
        validation, fit = chronological_probe_indices(history)
        self.assertEqual(validation, list(range(40, 50)))
        self.assertTrue(set(fit).isdisjoint(validation))
        self.assertIsNone(chronological_probe_indices(history[:10]))

    def test_learning_mode_resumes_and_legacy_stays_legacy(self):
        args = optimize.build_parser().parse_args(['--auto-learning-target', 'profit-evidence'])
        state = {'config': {}, 'optimizer_features': {'learning_target': 'profit-evidence'}}
        optimize._restore_auto_resume_args(args, state)
        self.assertEqual(args.auto_learning_target, 'profit-evidence')
        optimize._restore_auto_resume_args(args, {'config': {}})
        self.assertEqual(args.auto_learning_target, 'rank')


class CampaignLauncherTests(unittest.TestCase):
    def test_both_grids_are_loadable_and_exclude_risk_escalation(self):
        for timeframe in ('1m', '15m'):
            grid = load_grid_source(f'run_pulse_200:GRID_{timeframe.upper()}')['full']
            pulse.validate_parameter_grid(grid)
            self.assertIn('min_atr_cost_ratio', grid)
            self.assertIn('max_entry_gap_atr', grid)
            self.assertNotIn('leverage', grid)
            self.assertNotIn('fee_rate', grid)
        self.assertNotEqual(search_grid('1m'), search_grid('15m'))

    def test_campaign_defaults_and_resume(self):
        args = build_parser().parse_args(['--dry-run'])
        self.assertEqual(args.cycles, 200)
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'data.csv'
            source.write_text('test fixture')
            args.data_file = str(source)
            with patch('run_pulse_200.MarketDataSource.interval'):
                command = campaign_arguments(args)
        parsed = optimize.build_parser().parse_args(command)
        self.assertEqual(parsed.auto_learning_target, 'profit-evidence')
        self.assertEqual(parsed.snapshot_cycles, 50)
        self.assertEqual(parsed.auto_cycles, 200)
        self.assertEqual(parsed.timeframe, '1m')
        self.assertEqual(parsed.max_drawdown, 25)
        resumed = campaign_arguments(build_parser().parse_args(['--resume', 'folder', '--cycles', '10']))
        self.assertIn('folder', resumed)
        self.assertNotIn('--data-file', resumed)


if __name__ == '__main__':
    unittest.main()
