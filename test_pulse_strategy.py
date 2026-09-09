import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
import pandas as pd
import optimize
import pulse_strategy as pulse
from market_data import clear_market_data_cache
from trade_engine import TradeEngine, AccountState
from strategy_adapter import resolve_strategy


def candles(rows=100):
    times = pd.date_range('2025-01-01',periods=rows,freq='1min')
    frame = pd.DataFrame({'Open time':times,'Close time':times+pd.Timedelta(minutes=1)-pd.Timedelta(milliseconds=1),
        'Open':np.full(rows,100.),'High':np.full(rows,101.),'Low':np.full(rows,99.),'Close':np.full(rows,100.),'Volume':1.})
    frame.loc[21,['High','Close']] = [103.,102.]
    frame.loc[22:,['Open','High','Low','Close']] = [110.,111.,109.,110.]
    return frame


class PulseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root/'candles.csv'
    def tearDown(self):
        clear_market_data_cache()
        TradeEngine.load_market_data.cache_clear()
        pulse._FEATURE_CACHE.clear()
        self.temp.cleanup()
    def run_frame(self,frame=None,**kwargs):
        frame = candles() if frame is None else frame
        frame.to_csv(self.path,index=False)
        clear_market_data_cache()
        TradeEngine.load_market_data.cache_clear()
        pulse._FEATURE_CACHE.clear()
        tune = dict(optimize=True,breakout_lookback_bars=20,max_hold_bars=3,fee_rate=0.,slippage_rate=0.)
        tune.update(kwargs.pop('tune',{}))
        return pulse.pulse_strategy(tune,start=0,end=len(frame),data_file=self.path,trace=True,**kwargs)
    def events(self,result,kind):
        return [e for e in result['events'] if e['kind']==kind]
    def test_prior_channel_warmup_strict_equality_and_next_open(self):
        result = self.run_frame()
        signal = self.events(result,'signal')[0]
        entry = self.events(result,'entry')[0]
        self.assertEqual((signal['bar'],signal['upper']),(21,101.))
        self.assertEqual((entry['bar'],entry['price']),(22,110.))
        self.assertNotEqual(entry['price'],102.)
        self.assertEqual(pulse.required_indicator_warmup({'breakout_lookback_bars':20}),21)
        self.assertEqual(pulse.raw_breakouts(101,101,99),(False,False))
        frame = candles(); frame.loc[5,['High','Close']] = [105.,104.]
        self.assertFalse(any(e['bar'] < 20 for e in self.events(self.run_frame(frame),'signal')))
    def test_current_high_excluded_and_atr_is_sma(self):
        frame = candles(); frame.loc[21,'High'] = 110.
        ns = pd.to_datetime(frame['Open time']).array.asi8
        upper,lower,atr,ready = pulse.rolling_features(frame.High.to_numpy(),frame.Low.to_numpy(),frame.Close.to_numpy(),ns,20,20)
        self.assertEqual(upper[21],101.)
        self.assertAlmostEqual(atr[21],(19*2+11)/20)
        self.assertTrue(ready[21])
        self.assertEqual(self.events(self.run_frame(frame),'signal')[0]['bar'],21)
    def test_future_mutation_preserves_finalized_events(self):
        original = self.run_frame()
        frame = candles(); frame.loc[30:,['Open','High','Low','Close']] = [200.,210.,190.,205.]
        changed = self.run_frame(frame)
        self.assertEqual([e for e in original['events'] if e['bar']<=25],
                         [e for e in changed['events'] if e['bar']<=25])
    def test_time_exit_exact_completed_bars_and_stop_priority(self):
        result = self.run_frame()
        self.assertEqual(self.events(result,'time_signal')[0]['bar'],24)
        exit = self.events(result,'exit')[0]
        self.assertEqual((exit['bar'],exit['reason']),(25,'time_exit'))
        frame = candles(); frame.loc[24,'Low'] = 104.
        result = self.run_frame(frame)
        self.assertEqual(self.events(result,'exit')[0]['reason'],'stop_exit')
        self.assertFalse(self.events(result,'time_signal'))
    def test_stop_based_on_fill_and_signal_atr_entry_bar_stop(self):
        frame = candles(); frame.loc[22,'Low'] = 104.
        result = self.run_frame(frame)
        entry = self.events(result,'entry')[0]; exit = self.events(result,'exit')[0]
        self.assertAlmostEqual(entry['signal_atr'],2.1)
        self.assertAlmostEqual(entry['stop'],110.-4.2)
        self.assertEqual(exit['bar'],22)
        self.assertAlmostEqual(exit['price'],entry['stop'])
    def test_gap_through_stop_long_and_short(self):
        for mirrored in (False,True):
            frame = candles(); frame.loc[23,['Open','High','Low','Close']] = [100.,101.,99.,100.]
            if mirrored:
                old = frame.copy()
                frame['Open']=200-old.Open; frame['Close']=200-old.Close
                frame['High']=200-old.Low; frame['Low']=200-old.High
            exit = self.events(self.run_frame(frame),'exit')[0]
            self.assertEqual(exit['bar'],23)
            self.assertEqual(exit['price'],100.)
    def test_gap_exit_and_fresh_warmup(self):
        frame = candles()
        frame.loc[24:,'Open time'] += pd.Timedelta(minutes=1)
        frame.loc[24:,'Close time'] += pd.Timedelta(minutes=1)
        result = self.run_frame(frame,tune={'max_hold_bars':50})
        exit = self.events(result,'exit')[0]
        self.assertEqual((exit['bar'],exit['reason']),(24,'data_gap_exit'))
        self.assertEqual(exit['price'],exit['stop'])
        features = pulse.rolling_features(frame.High.to_numpy(),frame.Low.to_numpy(),frame.Close.to_numpy(),
                                          pd.to_datetime(frame['Open time']).array.asi8,20,20)
        self.assertFalse(features[3][24:44].any())
        self.assertTrue(features[3][44])
    def test_no_entry_over_missing_next_minute(self):
        frame = candles(); frame.loc[22:,'Open time'] += pd.Timedelta(minutes=1)
        frame.loc[22:,'Close time'] += pd.Timedelta(minutes=1)
        self.assertFalse(self.events(self.run_frame(frame),'entry'))
    def test_incomplete_candle_and_last_bar_never_fill(self):
        frame = candles()
        now = '2025-01-01 00:22:30Z'
        result = self.run_frame(frame,now=now)
        frame.loc[22,['Open','High','Low','Close']] = [500.,600.,400.,550.]
        changed = self.run_frame(frame,now=now)
        self.assertEqual(result['events'],changed['events'])
        self.assertFalse(self.events(result,'entry'))
        self.assertFalse(self.events(self.run_frame(candles().iloc[:22]),'entry'))
    def test_mirrored_prices_have_symmetric_decisions(self):
        frame = candles(); mirror = frame.copy()
        mirror['Open']=200-frame.Open; mirror['Close']=200-frame.Close
        mirror['High']=200-frame.Low; mirror['Low']=200-frame.High
        long = self.run_frame(frame); short = self.run_frame(mirror)
        for kind in ('signal','entry','time_signal','exit'):
            self.assertEqual([e['bar'] for e in self.events(long,kind)],[e['bar'] for e in self.events(short,kind)])
        self.assertEqual(self.events(short,'entry')[0]['side'],'short')
    def test_rearm_and_one_position_no_reverse(self):
        frame = candles()
        for i in range(22,len(frame)):
            price = 104.+i-22
            frame.loc[i,['Open','High','Low','Close']] = [price-.2,price+.1,price-.3,price]
        result = self.run_frame(frame,tune={'max_hold_bars':2})
        self.assertEqual(len(self.events(result,'entry')),1)
        frame.loc[30,['Open','High','Low','Close']] = [110.,111.,109.,110.]
        result = self.run_frame(frame,tune={'max_hold_bars':2})
        self.assertGreater(len(self.events(result,'entry')),1)
        active=0
        for event in result['events']:
            if event['kind']=='entry': active+=1
            if event['kind']=='exit': active-=1
            self.assertIn(active,(0,1))
    def test_invalid_ohlc_and_dual_signal_diagnostic(self):
        frame = candles(); frame.loc[10,'High']=50.
        with self.assertRaisesRegex(ValueError,'OHLC'): self.run_frame(frame)
        with patch.object(pulse,'raw_breakouts',return_value=(True,True)):
            result = self.run_frame()
        self.assertFalse(self.events(result,'entry'))
        self.assertGreater(result['diagnostics']['invalid_dual_signal'],0)
    def test_costs_adverse_and_open_position_accrual(self):
        result = self.run_frame(tune={'fee_rate':.001,'slippage_rate':.001})
        entry = self.events(result,'entry')[0]; exit = self.events(result,'exit')[0]
        self.assertAlmostEqual(entry['price'],110*1.001)
        self.assertAlmostEqual(exit['price'],110*.999)
        self.assertAlmostEqual(exit['fees'],(entry['price']+exit['price'])*entry['quantity']*.001)
        terminal = self.run_frame(candles().iloc[:24],tune={'fee_rate':.001,'max_hold_bars':30})
        self.assertEqual(terminal['open_positions'],1)
        self.assertEqual(terminal['closed_trades'],0)
        self.assertGreater(terminal['accrued_open_costs'],0)
        self.assertAlmostEqual(terminal['total_profit'],-terminal['accrued_open_costs'])
    def test_risk_rounding_and_minimum_rejection(self):
        result = self.run_frame(tune={'quantity_step':.1})
        entry = self.events(result,'entry')[0]
        self.assertLessEqual(entry['quantity']*abs(entry['price']-entry['stop']),1000*.0025+1e-10)
        self.assertAlmostEqual(entry['quantity']/.1,round(entry['quantity']/.1))
        rejected = self.run_frame(tune={'min_notional':100000.})
        self.assertFalse(self.events(rejected,'entry'))
        self.assertGreater(rejected['diagnostics']['below_minimum_order'],0)

    def test_leverage_and_margin_cap_change_actual_position(self):
        for side in ('long', 'short'):
            quantities = []
            for leverage in (1., 3.):
                engine = TradeEngine(first_balance=1000, tactical_balance=1000, optimize=True)
                account = AccountState(balance=1000)
                position, stop, rejected = engine.open_risk_position(
                    0, [100.], ['2025-01-01'], account, side=side, stop_distance=1.,
                    risk_per_trade=1., max_gross_exposure=10., trade_id='sizing',
                    leverage=leverage, trade_amount_percent=.1)
                self.assertIsNone(rejected)
                self.assertAlmostEqual(position.margin, 100.)
                self.assertEqual(position.leverage, leverage)
                quantities.append(position.position_size)
            self.assertAlmostEqual(quantities[1], 3 * quantities[0])

    def test_leveraged_gap_uses_shared_liquidation_for_both_sides(self):
        for side in ('long', 'short'):
            frame = candles()
            if side == 'short':
                for col in ('Open', 'High', 'Low', 'Close'):
                    frame[col] = 200 - frame[col]
                frame[['High', 'Low']] = frame[['Low', 'High']].to_numpy()
            frame.loc[23:, ['Open', 'High', 'Low', 'Close']] = 50. if side == 'long' else 200.
            result = self.run_frame(frame, tune={'leverage':3., 'max_gross_exposure':3.})
            self.assertEqual(result['liquidations'], 1)
            self.assertEqual(self.events(result, 'exit')[0]['reason'], 'liquidation_exit')

    def test_reject_stop_beyond_leveraged_liquidation(self):
        engine = TradeEngine(first_balance=1000, tactical_balance=1000, optimize=True)
        _, _, rejected = engine.open_risk_position(
            0, [100.], ['2025-01-01'], AccountState(balance=1000), side='long',
            stop_distance=40., risk_per_trade=.01, max_gross_exposure=3.,
            trade_id='invalid', leverage=3.)
        self.assertEqual(rejected, 'stop_beyond_liquidation')
    def test_shared_and_directional_schema_and_fixed_policies(self):
        adapter=resolve_strategy('pulse')
        self.assertEqual(len(adapter.discovered_profiles()['focused']),6)
        self.assertEqual(optimize.grid_size(pulse.PHASE_A_GRID),392)
        cfg=pulse.build_strategy_config({'breakout_lookback_bars':45,'short_breakout_lookback_bars':60})
        self.assertEqual(pulse.side_value(cfg,'long','breakout_lookback_bars'),45)
        self.assertEqual(pulse.side_value(cfg,'short','breakout_lookback_bars'),60)
        pulse.validate_parameter_grid({'risk_per_trade':[.01,.02]})
        with self.assertRaises(ValueError): pulse.build_strategy_config({'atr_period':14})
    def test_opposite_breakout_does_not_close_or_reverse(self):
        frame=candles()
        frame.loc[23:28,['Open','High','Low','Close']]=[90.,91.,89.,90.]
        result=self.run_frame(frame,tune={'stop_atr_mult':20.,'max_hold_bars':50})
        self.assertFalse(any(e['bar'] in range(23,29) for e in self.events(result,'entry')+self.events(result,'exit')))
        self.assertEqual(self.events(result,'exit')[0]['reason'],'time_exit')
    def test_short_market_costs_are_adverse(self):
        frame=candles(); old=frame.copy()
        frame['Open']=200-old.Open; frame['Close']=200-old.Close
        frame['High']=200-old.Low; frame['Low']=200-old.High
        result=self.run_frame(frame,tune={'fee_rate':.001,'slippage_rate':.001})
        entry=self.events(result,'entry')[0]; exit=self.events(result,'exit')[0]
        self.assertAlmostEqual(entry['price'],90*.999)
        self.assertAlmostEqual(exit['price'],90*1.001)
        self.assertAlmostEqual(exit['fees'],(entry['price']+exit['price'])*entry['quantity']*.001)
    def test_wrong_timeframe_is_rejected(self):
        frame=candles()
        frame['Open time']=pd.date_range('2025-01-01',periods=len(frame),freq='15min')
        frame['Close time']=frame['Open time']+pd.Timedelta(minutes=15)-pd.Timedelta(milliseconds=1)
        with self.assertRaises(ValueError): self.run_frame(frame)

    def test_console_trade_logs_and_summary_use_shared_engine(self):
        for side in ('long', 'short'):
            frame = candles()
            if side == 'short':
                old = frame.copy()
                frame['Open'] = 200-old.Open; frame['Close'] = 200-old.Close
                frame['High'] = 200-old.Low; frame['Low'] = 200-old.High
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = self.run_frame(frame, tune={'optimize':False}, show_chart=False,
                                        write_chart=False, write_excel=False, output_dir=self.root/'logged')
            for text in (f'Open {side.upper()} at price:', f'Close {side.upper()} at price:',
                         'Balance:', 'Balance (no fee):', 'pnl:', 'fee:', 'Profit:',
                         'Trade Duration:', 'BACKTEST FINISHED', 'Total Fees Paid:',
                         'Final Balance:', 'Maximum Drawdown:', 'Total score:'):
                self.assertIn(text, stdout.getvalue())
            self.assertNotIn('Cooldown Activated', stdout.getvalue())
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                quiet = self.run_frame(frame, tune={'optimize':False}, verbose=False,
                                       show_chart=False, write_chart=False, write_excel=False,
                                       output_dir=self.root/'quiet')
            self.assertEqual(stdout.getvalue(), '')
            self.assertEqual(result, quiet)
            self.assertTrue((self.root/'quiet/result.json').exists())

    def test_optimizer_stays_silent_even_if_verbose_is_requested(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.run_frame(verbose=True)
        self.assertEqual(stdout.getvalue(), '')

if __name__=='__main__': unittest.main()
