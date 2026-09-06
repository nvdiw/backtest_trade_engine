import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import optimize
from strategy_adapter import resolve_strategy
from strategy_workspace import (assert_strategy_path, claim_output, output_path,
                                output_session)
from test_strategy_plugins import _write_minute_candles


class StrategyWorkspaceTests(unittest.TestCase):
    def test_foreign_strategy_and_workflow_are_rejected(self):
        @output_session
        def claim(path, strategy, workflow='optimize'):
            claim_output(path, strategy, workflow)
        with tempfile.TemporaryDirectory() as directory:
            claim(directory, 'ma_strategy:ma_strategy')
            with self.assertRaises(ValueError):
                claim(directory, 'pulse_strategy:pulse_strategy')
            with self.assertRaises(ValueError):
                claim(directory, 'ma_strategy:ma_strategy', 'evaluate')
            with self.assertRaises(ValueError):
                assert_strategy_path(Path(directory) / 'snapshots/best_params.json',
                                     'pulse_strategy:pulse_strategy')
            claim(directory, 'ma_strategy:ma_strategy')  # Sequential resume is allowed.

    def test_concurrent_output_lock_and_exception_cleanup(self):
        @output_session
        def claim(path):
            claim_output(path, 'ma_strategy:ma_strategy', 'optimize')
        @output_session
        def first(path):
            claim_output(path, 'ma_strategy:ma_strategy', 'optimize')
            with self.assertRaisesRegex(ValueError, 'Another run'):
                claim(path)
            raise RuntimeError('simulated interruption')
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):
                first(directory)
            claim(directory)

    def test_legacy_manifest_prevents_foreign_adoption(self):
        @output_session
        def claim(path):
            claim_output(path, 'pulse_strategy:pulse_strategy', 'optimize')
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'research_manifest.json').write_text(
                json.dumps({'strategy': 'ma_strategy:ma_strategy'}))
            with self.assertRaises(ValueError):
                claim(directory)

    def test_default_cli_outputs_are_independent(self):
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / 'candles.csv'
            _write_minute_candles(data, 1000)
            try:
                os.chdir(root)
                for strategy, key, values in (
                    ('ma', 'long_ema_16_period', [14]),
                    ('example_strategy:example_strategy', 'fast_period', [12]),
                ):
                    grid = root / 'grid.json'
                    grid.write_text(json.dumps({key: values}))
                    argv = ['optimize.py', '--strategy', strategy, '--mode', 'grid',
                            '--param-grid', str(grid), '--data-file', str(data),
                            '--date-policy', 'fixed', '--start', '300', '--end', '900',
                            '-w', '1', '--excel-top', '0']
                    with patch.object(sys, 'argv', argv), contextlib.redirect_stdout(io.StringIO()):
                        optimize.main()
                ma = root / output_path('ma_strategy:ma_strategy', 'optimize')
                example = root / output_path('example_strategy:example_strategy', 'optimize')
                self.assertTrue((ma / 'best_params.json').exists())
                self.assertTrue((example / 'best_params.json').exists())
                with self.assertRaises(ValueError):
                    resolve_strategy('example_strategy:example_strategy').load_tune(ma / 'best_params.json')
            finally:
                os.chdir(previous)

    def test_ma_module_is_not_required_to_import_optimizer(self):
        code = '''
import sys
sys.modules['ma_strategy'] = None
import optimize
assert optimize._adapter_from_spec('example_strategy:example_strategy').identifier.startswith('example_strategy:')
'''
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_pulse_has_its_own_data_and_parameter_schema(self):
        self.assertEqual(resolve_strategy('pulse').identifier, 'pulse_strategy:pulse_strategy')
        import pulse_strategy
        self.assertEqual(pulse_strategy.DATA_FILE.name, 'btc_1m_data_2025_to_2026.csv')


if __name__ == '__main__':
    unittest.main()
