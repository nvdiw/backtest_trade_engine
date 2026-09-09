import unittest
import numpy as np
import pandas as pd
import pulse_strategy as pulse
from test_pulse_strategy import PulseTests as _PulseRunner, candles


class PulseEnhancementTests(unittest.TestCase):
    setUp = _PulseRunner.setUp
    tearDown = _PulseRunner.tearDown
    run_frame = _PulseRunner.run_frame
    events = _PulseRunner.events
    def test_filters_are_optional_and_side_specific(self):
        for tune, reason in [
            ({'breakout_buffer_atr': 2.}, 'breakout_buffer'),
            ({'min_volume_ratio': 2.}, 'min_volume_ratio'),
            ({'min_efficiency_ratio': 1., 'min_volume_ratio': 2.}, 'min_volume_ratio'),
            ({'trend_ma_bars': 50}, 'trend_filter'),
            ({'min_atr_cost_ratio': 1000., 'fee_rate': .0005}, 'cost_filter'),
            ({'max_signal_range_atr': .5}, 'signal_spike_filter'),
        ]:
            result = self.run_frame(tune=tune)
            self.assertTrue(any(e['reason'] == reason for e in self.events(result, 'entry_rejected')), tune)
        unaffected = self.run_frame(tune={'short_min_volume_ratio': 2.})
        self.assertTrue(self.events(unaffected, 'entry'))

    def test_trailing_applies_next_bar_and_never_loosens(self):
        frame = candles()
        frame.loc[23, ['Open','High','Low','Close']] = [110.,121.,109.,120.]
        frame.loc[24:, ['Open','High','Low','Close']] = [120.,121.,115.,118.]
        result = self.run_frame(frame, tune={'trailing_stop_atr_mult':1., 'trailing_activation_r':1., 'max_hold_bars':50})
        updates = self.events(result,'stop_update')
        self.assertTrue(updates)
        self.assertEqual(updates[0]['bar'],23)
        self.assertEqual(updates[0]['effective_bar'],24)
        self.assertEqual(self.events(result,'exit')[0]['bar'],24)
        self.assertEqual(self.events(result,'exit')[0]['reason'],'trailing_stop_exit')
        self.assertGreater(updates[0]['stop'],109.)  # This bar's low must not retroactively hit the new stop.
        self.assertTrue(all(a['stop'] <= b['stop'] for a,b in zip(updates,updates[1:])))

    def test_quality_features_ignore_future_and_reset_after_gap(self):
        close = 100 + np.arange(90,dtype=float)
        volume = np.ones(90)
        times = pd.date_range('2025-01-01', periods=90, freq='min').asi8
        original = pulse.quality_features(close,volume,times,30,30)
        modified = close.copy(); modified[60:] *= 5
        changed = pulse.quality_features(modified,volume,times,30,30)
        for key in original:
            np.testing.assert_allclose(original[key][:60],changed[key][:60],equal_nan=True)
        times = times.copy(); times[45:] += pulse.MINUTE_NS
        gapped = pulse.quality_features(close,volume,times,30,30)
        self.assertTrue(np.isnan(gapped['long_trend'][45:75]).all())
        self.assertTrue(np.isnan(gapped['relative_volume'][45:65]).all())

    def test_new_grid_values_validate_and_high_leverage_is_real(self):
        pulse.validate_parameter_grid(pulse.FULL_PARAM_GRID)
        self.assertIn(10.,pulse.FULL_PARAM_GRID['leverage'])
        result = self.run_frame(tune={'leverage':10.,'max_gross_exposure':5.})
        self.assertEqual(self.events(result,'entry')[0]['leverage'],10.)

    def test_cooldown_blocks_a_fresh_breakout_after_exit(self):
        frame = candles()
        frame.loc[26, ['Open','High','Low','Close']] = [100.,101.,99.,100.]
        frame.loc[27, ['Open','High','Low','Close']] = [110.,113.,109.,112.]
        baseline = self.run_frame(frame)
        delayed = self.run_frame(frame,tune={'cooldown_bars':20})
        self.assertTrue(any(e['bar'] == 27 for e in self.events(baseline,'signal')))
        self.assertTrue(any(e['bar'] == 27 and e['reason'] == 'cooldown'
                            for e in self.events(delayed,'entry_rejected')))


if __name__ == '__main__': unittest.main()
