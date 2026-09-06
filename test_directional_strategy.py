import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

import ma_strategy as ma
import optimize
from market_data import clear_market_data_cache
from ma_strategy_config import build_ma_strategy_config, directional_config, normalize_ma_strategy_tune
from strategy_adapter import resolve_strategy
from trade_engine import TradeEngine


class DirectionalStrategyTests(unittest.TestCase):
    def test_inheritance_and_explicit_overrides_roundtrip(self):
        tune = normalize_ma_strategy_tune({'ema_16_period': 14, 'long_ema_16_period': '20',
                                          'short_ma_50_period': '60'})
        cfg = build_ma_strategy_config(tune)
        self.assertEqual(directional_config(cfg, 'long').ema_16_period, 20)
        self.assertEqual(directional_config(cfg, 'short').ema_16_period, 14)
        self.assertEqual(directional_config(cfg, 'long').ma_50_period, 50)
        self.assertEqual(directional_config(cfg, 'short').ma_50_period, 60)
        self.assertEqual(asdict(cfg), asdict(build_ma_strategy_config(normalize_ma_strategy_tune(asdict(cfg)))))

    def test_both_directions_contribute_to_warmup(self):
        cfg = build_ma_strategy_config({'short_ma_200_period': 500})
        self.assertEqual(ma.required_indicator_warmup(cfg), 500)
        self.assertEqual(ma.maximum_optimizer_warmup({'long_period_atr': [300], 'long_period_atr_ma': [400]}), 700)

    def test_cross_states_are_independent(self):
        long, short = ma._new_cross_state(), ma._new_cross_state()
        ma._update_side_cross(long, {'ema_16': [1, 3], 'ma_50': [2, 2]}, 1, [100, 110], 10)
        ma._update_side_cross(short, {'ema_16': [3, 1], 'ma_50': [2, 2]}, 1, [100, 110], 10)
        self.assertEqual(long['last_cross_dir'], 'bull')
        self.assertEqual(short['last_cross_dir'], 'bear')
        long['last_trade_cross_index'] = 1
        self.assertIsNone(short['last_trade_cross_index'])

    def test_directional_grid_and_inactive_deduplication(self):
        grid = optimize.PARAMETER_PROFILES['directional']
        self.assertIn('long_ema_16_period', grid)
        self.assertIn('short_ema_16_period', grid)
        self.assertNotIn('ema_16_period', grid)
        generator = optimize.SmartCandidateGenerator({'short_ma_50_period': [40, 50, 60]},
                                                      baseline_params={'ma_50_period': 60},
                                                      strategy_adapter=resolve_strategy('ma'))
        self.assertEqual(generator.baseline['short_ma_50_period'], 60)
        self.assertFalse(ma.is_valid_candidate({'long_ema_16_period': 80, 'long_ma_50_period': 50,
                                               'long_ma_100_period': 100, 'long_ma_200_period': 200}))
        self.assertEqual(ma.canonicalize_candidate({'short_atr_filter': False, 'short_entry_atr_threshold': 9},
                                                  {'short_entry_atr_threshold': 1.2})['short_entry_atr_threshold'], 1.2)

    def test_opposite_disabled_side_changes_do_not_change_trades(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'candles.csv'
            times = pd.date_range('2025-01-01', periods=1800, freq='15min')
            price = 100 + 8 * np.sin(np.arange(len(times)) / 18)
            pd.DataFrame({'Open time': times, 'Close time': times + pd.Timedelta(minutes=15),
                          'Open': price, 'Close': price, 'High': price+1, 'Low': price-1,
                          'Volume': np.full(len(times), 100)}).to_csv(path, index=False)
            base = {'optimize': True, 'atr_filter': False, 'volume_filter': False,
                    'monthly_profit_close_filter': False, 'rsi_trade_monthly_filter_on': False,
                    'entry_score_threshold': 4, 'exit_score_threshold': 3, 'leverage': 1,
                    'safe_leverage_low': 1, 'safe_leverage_med': 1, 'safe_leverage_high': 1}
            try:
                with patch.object(ma, 'DATA_FILE', path):
                    for active, inactive in (('long', 'short'), ('short', 'long')):
                        tune = {**base, inactive + '_enabled': False}
                        first = ma.ma_strategy(tune, start=300, end=1800, research=True)
                        changed = ma.ma_strategy({**tune, inactive+'_ema_16_period': 20,
                                                  inactive+'_ma_50_period': 60,
                                                  inactive+'_ma_200_period': 600,
                                                  inactive+'_entry_score_threshold': 99},
                                                 start=300, end=1800, research=True)
                        self.assertGreater(first[active+'_trades'], 0)
                        self.assertEqual(first[inactive+'_trades'], 0)
                        self.assertEqual(first['trade_profits'], changed['trade_profits'])
            finally:
                clear_market_data_cache()
                TradeEngine.load_market_data.cache_clear()


if __name__ == '__main__':
    unittest.main()
