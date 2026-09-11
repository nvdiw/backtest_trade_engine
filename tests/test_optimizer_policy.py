import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import optimize
from campaign_reporting import publish_campaign
from optimizer_policy import apply_directional_gate, required_directional_trades
from trade_engine import TradeEngine, AccountState


class DirectionalPolicyTests(unittest.TestCase):
    def test_scaled_gate_and_disabled_side(self):
        self.assertEqual(required_directional_trades('pulse', 100, 1000), 3)
        self.assertEqual(required_directional_trades('ma', 100, 1000), 0)
        record = dict(params={}, result={'long_trades': 2, 'short_trades': 242},
                      objective_score=100, time_normalized_score=200)
        rejected = apply_directional_gate(copy.deepcopy(record), 30)
        self.assertIsNone(rejected['objective_score'])
        self.assertIsNone(rejected['time_normalized_score'])
        self.assertEqual(rejected['learning_score'], 0)
        accepted = apply_directional_gate(record, 30, {'enable_long': False})
        self.assertEqual(accepted['objective_score'], 100)

    def test_live_stage_and_resume_cannot_promote_sparse_long(self):
        args = optimize.build_parser().parse_args(['--workers', '1', '--log-every', '0'])
        state = dict(config={'strategy': 'pulse_strategy:pulse_strategy',
                             'parameter_grid': {}, 'ranges': {'final': [0, 1000]}},
                     cycle=1, total_evaluations=0, optimizer_features={})
        candidates = [{'candidate_id': 'sparse', 'params': {}}]
        metrics = dict(score=100, closed_trades=244, long_trades=2, short_trades=242)
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temp)
            with patch('optimize._init_worker'), patch('optimize._maximum_candidate_warmup', return_value=0), patch(
                'optimize._evaluate_candidate', return_value=('sparse', {}, metrics, .01, None)
            ):
                records, _ = optimize._run_auto_stage(args, 1, 'final', candidates, 0, 1000,
                                                      {}, root, state, root / 'state.json')
            self.assertEqual(optimize._promote_candidates(records, 1), [])
            with patch('optimize._evaluate_candidate', side_effect=AssertionError('must reuse checkpoint')):
                resumed, _ = optimize._run_auto_stage(args, 1, 'final', candidates, 0, 1000,
                                                      {}, root, state, root / 'state.json')
            self.assertEqual(optimize._promote_candidates(resumed, 1), [])
            self.assertEqual(resumed[0]['result']['long_trades'], 2)

    def test_watch_is_not_a_qualified_finalist(self):
        record = dict(robust_score=90, recency_score=85, stage_consistency_score=90,
                      worst_stage_percentile=.8, stage_metrics={stage: dict(
                          total_profit_percent=10, long_trades=2, short_trades=242,
                          long_profit=80, short_profit=20) for stage in optimize.AUTO_STAGE_ORDER})
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / 'hall_of_fame.json').write_text(json.dumps([record]))
            result = publish_campaign(temp, {'config': {}}, excel=False)
        self.assertEqual(result['qualified_finalists'], 0)
        self.assertEqual(result['observed_finalists'], 1)
        self.assertIsNone(result['recommended_params'])

    def test_walk_forward_counts_sum_disjoint_folds(self):
        folds = {str(i): [dict(candidate_id='a', result=dict(long_trades=i, short_trades=10),
                              objective_score=1, time_normalized_score=1)] for i in (1, 2, 3)}
        result = optimize._aggregate_walk_forward_records(folds, [{'candidate_id': 'a', 'params': {}}], .15)
        self.assertEqual(result[0]['result']['long_trades'], 6)
        self.assertEqual(result[0]['result']['short_trades'], 30)


class LeverageMeaningTests(unittest.TestCase):
    def test_risk_bound_quantity_stays_fixed_while_margin_changes(self):
        positions = []
        for leverage in (1., 5.):
            engine = TradeEngine(first_balance=1000, tactical_balance=1000, optimize=True,
                                 verbose=False, fee_rate=0, slippage_rate=0)
            position, _, rejection = engine.open_risk_position(
                0, [100.], ['2025-01-01'], AccountState(balance=1000), side='long',
                stop_distance=2, risk_per_trade=.001, max_gross_exposure=3,
                trade_id='test', leverage=leverage)
            self.assertIsNone(rejection)
            self.assertEqual(engine.last_risk_sizing['binding_limit'], 'risk')
            positions.append(position)
        self.assertAlmostEqual(positions[0].position_size, positions[1].position_size)
        self.assertAlmostEqual(positions[0].margin, 5 * positions[1].margin)
