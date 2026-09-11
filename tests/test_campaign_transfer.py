import gzip
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from campaign_transfer import prepare_transfer, save_transfer, read_json


class CampaignTransferTests(unittest.TestCase):
    def test_history_requires_compatible_evidence_but_proposals_survive_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            grid = {'long_x': [1, 2, 3], 'short_x': [1, 2, 3], 'enable_long': [True], 'enable_short': [True]}
            config = dict(strategy='pulse_strategy:pulse_strategy', timeframe='1m',
                          data_file=str(root/'candles.csv'), base_tune={}, parameter_grid=grid)
            features = {'learning_target': 'profit-evidence'}
            fingerprints = {'code_sha256': 'code', 'data_sha256': 'data'}
            (root/'auto_state.json').write_text(json.dumps(dict(config=config, optimizer_features=features, cycles_completed=300)))
            (root/'research_manifest.json').write_text(json.dumps({'fingerprints': fingerprints}))
            params = [dict(long_x=i, short_x=4-i, enable_long=True, enable_short=True) for i in (1,2,3)]
            (root/'hall_of_fame.json').write_text(json.dumps([{'params':params[-1]}]))
            with gzip.open(root/'surrogate_history_cache.json.gz', 'wt') as handle:
                json.dump(dict(version=2, parameter_keys=list(grid), rows=[[f'c00000{i}-1', i/4, 'profit_evidence_v1', *p.values()]
                                                               for i,p in enumerate(params,1)]), handle)
            adapter = SimpleNamespace(identifier=config['strategy'], validate_candidate=lambda p: True)
            result = prepare_transfer(root, config, features, grid, adapter, fingerprints)
            self.assertEqual(len(result['history']),3)
            self.assertEqual(len(result['proposals']),3)
            self.assertEqual(result['summary']['source_cycles'],300)
            changed = prepare_transfer(root, config, features, grid, adapter, {**fingerprints,'code_sha256':'changed'})
            self.assertEqual(changed['history'], [])
            self.assertEqual(len(changed['proposals']),3)
            self.assertEqual(changed['summary']['mode'],'retest_proposals')
            save_transfer(root/'transfer_seed.json.gz', changed)
            self.assertEqual(read_json(root/'transfer_seed.json.gz'),changed)
            with self.assertRaisesRegex(ValueError,'timeframe'):
                prepare_transfer(root, {**config,'timeframe':'15m'}, features, grid, adapter, fingerprints)

    def test_old_shared_parameters_map_to_both_directions_and_fixed_enable(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            config=dict(strategy='pulse',timeframe='1m',data_file=str(root/'data.csv'),base_tune={})
            (root/'auto_state.json').write_text(json.dumps({'config':config}))
            (root/'hall_of_fame.json').write_text(json.dumps([{'params':{'x':2,'enable_long':False}}]))
            adapter=SimpleNamespace(identifier='pulse',validate_candidate=lambda p:True)
            result=prepare_transfer(root,config,{}, {'long_x':[1,2], 'short_x':[1,2], 'enable_long':[True]},adapter,{})
            self.assertEqual(result['proposals'],[{'long_x':2,'short_x':2,'enable_long':True}])
