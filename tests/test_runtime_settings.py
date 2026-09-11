import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

import optimize
from ma_strategy import ma_strategy
from market_data import MarketDataSource, clear_market_data_cache
from runtime_settings import (add_runtime_arguments, configure_runtime,
                              performance_name, runtime_session, worker_count)
from tests.test_strategy_plugins import _write_minute_candles


class RuntimeTests(unittest.TestCase):
    def tearDown(self):
        clear_market_data_cache()

    def test_resource_modes_and_explicit_worker_override(self):
        self.assertEqual([worker_count(m, 16) for m in
                          ('power_saving', 'normal', 'boost')], [4, 8, 15])
        self.assertEqual(worker_count('boost', 1), 1)
        self.assertEqual(performance_name('power saving'), 'power_saving')
        parser = argparse.ArgumentParser()
        parser.add_argument('-w', '--workers', type=int, default=8)
        add_runtime_arguments(parser)

        @runtime_session
        def run(argv):
            args = parser.parse_args(argv)
            with patch('runtime_settings.os.cpu_count', return_value=16):
                configure_runtime(args, argv)
            return args.workers

        before = dict(os.environ)
        self.assertEqual(run(['--performance', 'power_saving']), 4)
        self.assertEqual(run(['--performance', 'boost', '-w', '2']), 2)
        self.assertEqual(dict(os.environ), before)

    def test_detect_timeframes_and_reject_bad_spacing(self):
        with tempfile.TemporaryDirectory() as directory:
            for minutes in (1, 5, 15, 60):
                path = Path(directory) / f'{minutes}.csv'
                times = pd.date_range('2026-01-01', periods=100, freq=f'{minutes}min')
                # An isolated missing candle does not change the base timeframe.
                pd.DataFrame({'Open time': times.delete(40)}).to_csv(path, index=False)
                source = MarketDataSource(path)
                self.assertEqual(source.interval(), pd.Timedelta(minutes=minutes))
                with self.assertRaises(ValueError):
                    MarketDataSource(path, '2m').interval()
            for name, times in (
                ('duplicate', ['2026-01-01', '2026-01-01']),
                ('mixed', pd.to_datetime(['2026-01-01 00:00', '2026-01-01 00:01',
                                         '2026-01-01 00:16', '2026-01-01 00:17'])),
            ):
                path = Path(directory) / f'{name}.csv'
                pd.DataFrame({'Open time': times}).to_csv(path, index=False)
                with self.assertRaises(ValueError):
                    MarketDataSource(path).interval()

    def test_ma_auto_matches_explicit_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'candles.csv'
            _write_minute_candles(path, 1000)
            for minutes in (1, 15):
                if minutes == 15:
                    frame = pd.read_csv(path)
                    frame['Open time'] = pd.date_range('2026-01-01', periods=1000, freq='15min')
                    frame['Close time'] = frame['Open time'] + pd.Timedelta(minutes=15) - pd.Timedelta(milliseconds=1)
                    frame.to_csv(path, index=False)
                    clear_market_data_cache()
                    from trade_engine import TradeEngine
                    TradeEngine.load_market_data.cache_clear()
                kwargs = dict(tune={'optimize': True}, start=300, end=900, data_file=path)
                self.assertEqual(ma_strategy(**kwargs, timeframe='auto'),
                                 ma_strategy(**kwargs, timeframe=f'{minutes}m'))

    def test_spawned_workers_use_selected_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'one_minute.csv'
            _write_minute_candles(path, 1000)
            grid = root / 'grid.json'
            grid.write_text(json.dumps({'long_ema_16_period': [14, 20]}))
            common = ['--strategy', 'ma', '--mode', 'grid', '--param-grid', str(grid),
                      '--data-file', str(path), '--date-policy', 'fixed',
                      '--start', '300', '--end', '900', '--excel-top', '0']
            results = []
            for workers, mode in ((1, 'normal'), (2, 'power_saving')):
                output = root / str(workers)
                with contextlib.redirect_stdout(io.StringIO()):
                    optimize.main(common + ['-w', str(workers), '--performance', mode,
                                            '--output-dir', str(output)])
                results.append(json.loads((output / 'best_params.json').read_text()))
                manifest = json.loads((output / 'research_manifest.json').read_text())
                self.assertEqual(Path(manifest['data_file']), path.resolve())
            self.assertEqual(results[0], results[1])

    def test_focused_grid_contains_only_independent_entry_keys(self):
        grid = optimize.FOCUSED_PARAM_GRID
        self.assertEqual(len(grid), 24)
        self.assertEqual(len(optimize.PARAMETER_PROFILES['long_focused']), 12)
        self.assertEqual(len(optimize.PARAMETER_PROFILES['short_focused']), 12)
        self.assertTrue(all(key.startswith(('long_', 'short_')) for key in grid))
        self.assertNotIn('entry_score_cross', grid)


if __name__ == '__main__':
    unittest.main()
