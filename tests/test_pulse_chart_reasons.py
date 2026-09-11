import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pulse_strategy as pulse
from trade_engine import TradeEngine
from market_data import clear_market_data_cache
from tests.test_pulse_strategy import candles


class PulseChartReasonTests(unittest.TestCase):
    def tearDown(self):
        clear_market_data_cache()
        TradeEngine.load_market_data.cache_clear()
        pulse._FEATURE_CACHE.clear()

    def test_actual_long_and_short_markers_use_signal_and_ledger_details(self):
        for side in ('long', 'short'):
            with self.subTest(side=side), tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                frame=candles()
                if side == 'short':
                    high, low = frame['High'].copy(), frame['Low'].copy()
                    frame['High'], frame['Low'] = 200-low, 200-high
                    frame['Open'], frame['Close'] = 200-frame['Open'], 200-frame['Close']
                data=root/'data.csv'; frame.to_csv(data,index=False)
                tune=dict(breakout_lookback_bars=20,max_hold_bars=3,fee_rate=.0001,slippage_rate=.0001)
                with patch.object(TradeEngine,'render_strategy_chart') as render:
                    actual=pulse.pulse_strategy(tune,0,len(frame),data_file=data,output_dir=root/'run',
                        write_excel=False,show_chart=False,verbose=False)
                chart=render.call_args.kwargs['chart_state']
                entry=next(iter(chart[side+'_open_reasons'].values()))
                close=next(iter(chart[side+'_close_reasons'].values()))
                for text in ('Signal candle:', 'Channel lookback: 20', 'ATR20 SMA:', 'ENTRY FILTERS',
                             'RISK AND EXIT PLAN', 'Trade ID', 'Entry price', 'Position size', 'Leverage'):
                    self.assertIn(text,entry)
                self.assertIn('2025-01-01 00:21:00',entry)
                self.assertIn('2025-01-01 00:22:00',entry)
                for text in ('time_exit', 'DECISION AT PRIOR CLOSE', 'Held Bars: 3', 'Net profit', 'Total fees', 'Duration'):
                    self.assertIn(text,close)
                expected=pulse.pulse_strategy({**tune,'optimize':True},0,len(frame),data_file=data,verbose=False)
                for key in ('closed_trades','total_profit_percent','maximum_drawdown'):
                    self.assertEqual(actual[key],expected[key])
                self.assertEqual(actual['directional_evidence'][side]['status'],'INSUFFICIENT')

    def test_entry_tooltip_is_unchanged_when_future_candles_change(self):
        with tempfile.TemporaryDirectory() as directory:
            entries=[]
            for index in range(2):
                frame=candles()
                if index:
                    frame.loc[60:,['Open','High','Low','Close']] = [80,81,79,80]
                path=Path(directory)/f'data{index}.csv'; frame.to_csv(path,index=False)
                with patch.object(TradeEngine,'render_strategy_chart') as render:
                    pulse.pulse_strategy({'breakout_lookback_bars':20,'max_hold_bars':3},0,len(frame),
                        data_file=path,output_dir=Path(directory)/f'out{index}',write_excel=False,
                        show_chart=False,verbose=False)
                entries.append(next(iter(render.call_args.kwargs['chart_state']['long_open_reasons'].values())))
            self.assertEqual(*entries)
