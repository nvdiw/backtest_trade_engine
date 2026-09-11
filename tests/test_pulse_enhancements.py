import unittest
import numpy as np
import pandas as pd
import pulse_strategy as pulse
from tests.test_pulse_strategy import PulseTests as _PulseRunner, candles


class PulseEnhancementTests(unittest.TestCase):
    setUp = _PulseRunner.setUp
    tearDown = _PulseRunner.tearDown
    run_frame = _PulseRunner.run_frame
    events = _PulseRunner.events
    def test_volatility_expansion_is_causal_and_resets_after_gap(self):
        atr = np.ones(180)
        atr[100] = 3.
        times = pd.date_range('2025-01-01', periods=180, freq='min').asi8
        ratios = pulse.atr_expansion(atr, times)
        self.assertAlmostEqual(ratios[100], 3.)
        changed = atr.copy(); changed[120:] = 10.
        np.testing.assert_allclose(ratios[:120], pulse.atr_expansion(changed, times)[:120], equal_nan=True)
        gapped = times.copy(); gapped[90:] += pulse.MINUTE_NS
        self.assertTrue(np.isnan(pulse.atr_expansion(atr, gapped)[90:170]).all())
        self.assertEqual(pulse.required_indicator_warmup({'max_atr_expansion': 2.}), 81)

    def test_volatility_filter_rejects_spike_but_leaves_other_side_alone(self):
        frame = candles(160)
        frame.loc[:, ['Open','High','Low','Close']] = [100.,101.,99.,100.]
        frame.loc[100, ['Open','High','Low','Close']] = [100.,150.,99.,149.]
        frame.loc[101:, ['Open','High','Low','Close']] = [149.,150.,148.,149.]
        for sample, side in ((frame, 'long'), (self.mirrored(frame), 'short')):
            baseline = self.run_frame(sample)
            blocked = self.run_frame(sample, tune={side+'_max_atr_expansion': 1.5})
            self.assertTrue(any(e['bar'] == 101 for e in self.events(baseline, 'entry')))
            self.assertTrue(any(e['bar'] == 100 and e['reason'] == 'volatility_expansion_filter'
                                for e in self.events(blocked, 'entry_rejected')))

    def test_stagnant_trade_exits_next_open_but_progressing_trade_stays(self):
        for sample in (candles(), self.mirrored(candles())):
            tune = {'max_hold_bars': 50, 'stagnation_exit_bars': 3, 'stagnation_min_progress_r': .5}
            result = self.run_frame(sample, tune=tune)
            self.assertEqual(self.events(result, 'stagnation_signal')[0]['bar'], 24)
            exit_ = self.events(result, 'exit')[0]
            self.assertEqual((exit_['bar'], exit_['reason']), (25, 'stagnation_exit'))
        frame = candles()
        frame.loc[23:, ['Open','High','Low','Close']] = [115.,116.,114.,115.]
        progressing = self.run_frame(frame, tune=tune)
        self.assertFalse(self.events(progressing, 'stagnation_signal'))
        self.assertEqual(self.events(progressing, 'exit')[0]['reason'], 'time_exit')

    def test_side_specific_sizing_controls_actual_entries(self):
        tune = {'long_leverage': 2., 'short_leverage': 5.,
                'long_trade_amount_percent': .01, 'short_trade_amount_percent': .02,
                'long_risk_per_trade': .1, 'short_risk_per_trade': .1}
        long_entry = self.events(self.run_frame(tune=tune), 'entry')[0]
        short_entry = self.events(self.run_frame(self.mirrored(candles()), tune=tune), 'entry')[0]
        self.assertEqual(long_entry['leverage'], 2.)
        self.assertEqual(short_entry['leverage'], 5.)
        self.assertAlmostEqual(long_entry['margin'], 10.)
        self.assertAlmostEqual(short_entry['margin'], 20.)
        for key, value in [('long_leverage', 0), ('short_risk_per_trade', 2),
                           ('long_trade_amount_percent', 1.1), ('short_max_gross_exposure', 0)]:
            self.assertFalse(pulse.is_valid_candidate({key: value}))

    def test_score_is_bounded_optional_and_side_specific(self):
        baseline = self.run_frame()
        blocked = self.run_frame(tune={'entry_score_min': 100.})
        self.assertTrue(self.events(baseline, 'entry'))
        self.assertTrue(any(e['reason'] == 'entry_score_filter'
                            for e in self.events(blocked, 'entry_rejected')))
        self.assertEqual(baseline['events'], self.run_frame(tune={'short_entry_score_min': 100.})['events'])
        for key in ('entry_score_min', 'long_entry_score_min'):
            with self.assertRaises(ValueError):
                pulse.build_strategy_config({key: 101})
        with self.assertRaises(ValueError):
            pulse.build_strategy_config({'failed_breakout_bars': 1.5})

    @staticmethod
    def mirrored(frame):
        result = frame.copy()
        result['Open'], result['Close'] = 200 - frame.Open, 200 - frame.Close
        result['High'], result['Low'] = 200 - frame.Low, 200 - frame.High
        return result

    def test_failed_breakout_uses_entry_boundary_and_exits_next_open(self):
        frame = candles()
        frame.loc[22:, ['Open', 'High', 'Low', 'Close']] = [102., 103., 100., 102.]
        frame.loc[23, ['Open', 'High', 'Low', 'Close']] = [102., 103., 100., 100.5]
        for sample in (frame, self.mirrored(frame)):
            result = self.run_frame(sample, tune={'failed_breakout_bars': 6, 'max_hold_bars': 50})
            signal = self.events(result, 'failed_breakout_signal')[0]
            exit_ = self.events(result, 'exit')[0]
            self.assertEqual(signal['bar'], 23)
            self.assertEqual((exit_['bar'], exit_['reason']), (24, 'failed_breakout_exit'))
            self.assertEqual(exit_['price'], sample.loc[24, 'Open'])
            # A pending close never overrides a stop gapped through at the next open.
            gapped = sample.copy()
            values = [90., 91., 89., 90.] if exit_['side'] == 'long' else [110., 111., 109., 110.]
            gapped.loc[24, ['Open', 'High', 'Low', 'Close']] = values
            stop_exit = self.events(self.run_frame(gapped, tune={'failed_breakout_bars': 6, 'max_hold_bars': 50}), 'exit')[0]
            self.assertEqual(stop_exit['reason'], 'stop_exit')

    def test_breakeven_is_next_bar_only_for_both_directions(self):
        frame = candles()
        frame.loc[23, ['Open','High','Low','Close']] = [110.,121.,109.,120.]
        frame.loc[24:, ['Open','High','Low','Close']] = [120.,121.,105.,118.]
        for sample in (frame, self.mirrored(frame)):
            result = self.run_frame(sample, tune={'breakeven_activation_r': 1., 'max_hold_bars': 50,
                                                  'fee_rate': .0005, 'slippage_rate': .0001})
            update = self.events(result, 'stop_update')[0]
            exit_ = self.events(result, 'exit')[0]
            self.assertEqual((update['bar'], update['effective_bar']), (23, 24))
            self.assertEqual((exit_['bar'], exit_['reason']), (24, 'breakeven_stop_exit'))
            self.assertAlmostEqual(result['total_profit'], 0., places=6)

    def test_new_features_do_not_use_future_prices(self):
        frame = candles()
        tune = {'entry_score_min': 40., 'failed_breakout_bars': 6, 'breakeven_activation_r': 1.}
        original = self.run_frame(frame, tune=tune)
        changed = frame.copy()
        changed.loc[60:, ['Open','High','Low','Close']] *= 1.5
        modified = self.run_frame(changed, tune=tune)
        self.assertEqual([e for e in original['events'] if e['bar'] < 60],
                         [e for e in modified['events'] if e['bar'] < 60])

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
