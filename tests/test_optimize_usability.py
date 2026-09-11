import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from optimize import build_parser
from optimize_resume import restore_campaign
from optimize_overview import comparison_rows, write_overview


class CampaignUsabilityTests(unittest.TestCase):
    def restore(self, argv):
        parser = build_parser()
        args = parser.parse_args(argv)
        restore_campaign(parser, args, argv)
        return args

    def test_folder_restores_strategy_data_and_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / 'campaign_config.json').write_text(json.dumps({
                'strategy': 'pulse', 'profile': 'signal', 'auto': True,
                'data_file': 'candles.csv', 'base_source': 'file',
                'base_params': 'seed.json', 'auto_tests': 123,
                'auto_cycles': 4, 'workers': 99, 'snapshot_cycles': 50,
                'seed_campaign': 'old-source-folder',
            }))
            args = self.restore(['--resume', temp, '--workers', '2'])
            self.assertEqual(args.strategy, 'pulse')
            self.assertEqual(args.data_file, 'candles.csv')
            self.assertEqual(args.base_params, 'seed.json')
            self.assertEqual(args.auto_tests, 123)
            self.assertEqual(args.workers, 2)
            self.assertNotEqual(args.auto_cycles, 4)
            self.assertIsNone(args.seed_campaign)
            self.assertTrue(args.auto)
            self.assertTrue(args.resume)

    def test_legacy_checkpoint_needs_no_baseline_file(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / 'auto_state.json').write_text(json.dumps({'config': {
                'strategy': 'pulse_strategy:pulse_strategy', 'profile': 'signal',
                'data_file': 'original.csv', 'timeframe': '1m',
                'parameter_grid': {'x': [1, 2]}, 'base_tune': {'x': 1},
            }}))
            args = self.restore(['--resume', temp])
            self.assertEqual(args.strategy, 'pulse_strategy:pulse_strategy')
            self.assertEqual(args._resume_grid, {'x': [1, 2]})
            self.assertTrue(args.auto)

    def test_missing_and_ambiguous_folders_fail(self):
        with patch('optimize_resume.Path.rglob', return_value=[]):
            with self.assertRaisesRegex(ValueError, 'not found or ambiguous'):
                self.restore(['--resume', 'nonexistent-campaign'])
        paths = [Path('outputs/ma/same/auto_state.json'), Path('outputs/pulse/same/auto_state.json')]
        with patch('optimize_resume.Path.rglob', return_value=paths):
            with self.assertRaisesRegex(ValueError, 'ambiguous'):
                self.restore(['--resume', 'same'])

    def test_unique_folder_name(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / 'campaign'
            folder.mkdir()
            marker = folder / 'campaign_config.json'
            marker.write_text('{"strategy": "pulse", "auto": true}')
            with patch('optimize_resume.Path.rglob', return_value=[marker]):
                args = self.restore(['--resume', 'campaign'])
            self.assertEqual(Path(args.output_dir), folder)

    def test_snapshot_default_and_legacy_flag(self):
        args = build_parser().parse_args(['--auto', '--resume'])
        self.assertIs(args.resume, True)
        self.assertEqual(args.snapshot_cycles, 50)

    def test_saved_baseline_survives_missing_source(self):
        from optimize import _load_base_tune
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / 'campaign_config.json').write_text(json.dumps({
                'base_source': 'file', 'base_params': 'deleted.json',
                'frozen_base_tune': {'x': 7},
            }))
            args = self.restore(['--resume', temp])
            self.assertEqual(_load_base_tune(args)[0], {'x': 7})

    def test_comparison_prioritizes_differences_and_preserves_zero(self):
        rows = [{'rank': 1, 'decision': 'WATCH', 'a': 0, 'b': 2},
                {'rank': 2, 'decision': 'REJECT', 'a': 0, 'b': 3}]
        comparison = comparison_rows(rows, ['a', 'b'])
        self.assertEqual(comparison[0]['parameter'], 'b')
        self.assertEqual(comparison[1]['Rank 1'], 0)
        with tempfile.TemporaryDirectory() as temp:
            write_overview(temp, rows, ['a', 'b'], {})
            report = (Path(temp) / 'OVERVIEW.md').read_text(encoding='utf-8')
            self.assertIn('WATCH', report)
            self.assertIn('| a | 0 | 0 |', report)
            write_overview(temp, [], [], {})
            self.assertIn('No fully evaluated finalist',
                          (Path(temp) / 'OVERVIEW.md').read_text(encoding='utf-8'))
