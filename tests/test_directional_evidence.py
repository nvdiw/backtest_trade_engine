import copy
import unittest

from optimizer_evidence import directional_evidence
from optimize import _auto_candidate_decision, _flatten_hall_record, _candidate_summary


class DirectionalEvidenceTests(unittest.TestCase):
    def record(self):
        metrics = dict(total_profit_percent=10, maximum_drawdown=-2, liquidations=0,
                       long_trades=2, short_trades=242, long_profit=80, short_profit=20)
        return dict(robust_score=90, recency_score=85, stage_consistency_score=90,
                    worst_stage_percentile=.8, cycle=1, candidate_id='test',
                    effective_params={'enable_long':True, 'enable_short':True},
                    stage_metrics={stage:copy.deepcopy(metrics) for stage in
                                   ('discovery', 'validation', 'stress', 'walk_forward', 'final')})

    def test_sparse_side_prevents_accept_without_pooling_overlapping_stages(self):
        record = self.record()
        record['stage_metrics']['discovery']['long_trades'] = 1000
        decision = _auto_candidate_decision(record)
        self.assertEqual(decision['decision'], 'WATCH')
        self.assertEqual(decision['directional_evidence']['long']['status'], 'INSUFFICIENT')
        self.assertEqual(decision['directional_evidence']['short']['status'], 'SUFFICIENT')
        self.assertTrue(any('LONG evidence insufficient' in r for r in decision['decision_reasons']))
        record.update(decision)
        self.assertEqual(_flatten_hall_record(record, (), 1)['long_evidence_status'], 'INSUFFICIENT')
        self.assertEqual(_candidate_summary(record,1,'params.json')['directional_evidence'], decision['directional_evidence'])
        record['stage_metrics']['final']['long_trades'] = 30
        self.assertEqual(_auto_candidate_decision(record)['decision'], 'ACCEPT')
        record['stage_metrics']['final']['long_profit'] = -1
        self.assertEqual(_auto_candidate_decision(record)['decision'], 'WATCH')

    def test_disabled_and_missing_sides(self):
        record = self.record()
        record['effective_params']['enable_long'] = False
        self.assertEqual(_auto_candidate_decision(record)['decision'], 'ACCEPT')
        self.assertEqual(directional_evidence({}, {'long_enabled':False})['long']['status'], 'DISABLED')
        self.assertEqual(directional_evidence({})['long']['status'], 'MISSING')
        self.assertEqual(directional_evidence({'long_trades':float('nan')})['long']['status'], 'MISSING')
        record['stage_metrics']['final']['total_profit_percent'] = -1
        self.assertEqual(_auto_candidate_decision(record)['decision'], 'REJECT')
