import subprocess
import sys
import unittest
from dataclasses import asdict
from pathlib import Path

import optimize
import ma_strategy_config as ma
import pulse_strategy_config as pulse
from strategy_adapter import resolve_strategy


class StrategyConfigIsolationTests(unittest.TestCase):
    def test_pulse_full_searches_decisions_not_fixed_policies(self):
        self.assertEqual(set(pulse.FULL_PARAM_GRID), set(pulse.SIGNAL_KEYS) | set(pulse.ENHANCEMENT_KEYS) | set(pulse.SIZING_KEYS))
        self.assertGreater(optimize.grid_size(pulse.FULL_PARAM_GRID), 42336)
        pulse.validate_parameter_grid(pulse.FULL_PARAM_GRID)
        for key in ('balance', 'fee_rate', 'optimize', 'quantity_step'):
            with self.assertRaisesRegex(ValueError, 'fixed policies'):
                pulse.validate_parameter_grid({key: [asdict(pulse.PulseConfig())[key]]})
        self.assertEqual(set(pulse.FOCUSED_PARAM_GRID), {
            f'{side}_{key}' for side in ('long', 'short') for key in pulse.SIGNAL_KEYS})

    def test_both_configs_use_shared_adapter_contract(self):
        for name, module in [('ma', ma), ('pulse', pulse)]:
            adapter = resolve_strategy(name)
            self.assertEqual(adapter.config_builder.__module__, module.__name__)
            for profile in ('focused', 'full'):
                self.assertEqual(adapter.discovered_profiles()[profile], module.PARAMETER_PROFILES[profile])

    def test_pulse_focus_does_not_mutate_full_or_ma(self):
        before = list(ma.FULL_PARAM_GRID['ma_50_period'])
        values = pulse.FOCUSED_PARAM_GRID['long_max_hold_bars']
        original = list(values)
        try:
            values[:] = [17]
            self.assertNotEqual(pulse.FULL_PARAM_GRID['max_hold_bars'], values)
            self.assertEqual(ma.FULL_PARAM_GRID['ma_50_period'], before)
        finally:
            values[:] = original

    def test_each_strategy_can_load_without_peer_config(self):
        for strategy, blocked in [('pulse', 'ma_strategy_config'), ('ma', 'pulse_strategy_config')]:
            code = f'''
import sys
class BlockPeer:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == {blocked!r}:
            raise AssertionError('Loaded unrelated config: ' + fullname)
sys.meta_path.insert(0, BlockPeer())
import optimize
optimize.main(['--strategy', {strategy!r}, '--list-profiles'])
'''
            result = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[1],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_pulse_staged_seeds_and_columns_use_pulse_config(self):
        adapter = resolve_strategy('pulse')
        seeds = optimize._staged_seed_data(
            [{'effective_params': {'stop_atr_mult': 2.5}}], pulse.FULL_PARAM_GRID,
            {'breakout_lookback_bars': 45}, adapter)
        self.assertEqual(seeds[0]['params']['breakout_lookback_bars'], 45)
        rows = optimize._staged_report_rows([{'effective_params': adapter.default_values({})}])
        self.assertNotIn('ma_50_period', rows[0])
        self.assertIn('breakout_lookback_bars', rows[0])


if __name__ == '__main__':
    unittest.main()
