import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import matplotlib
matplotlib.use('Agg')
import pandas as pd
import ma_strategy
import pulse_strategy
from tests.test_strategy_plugins import _write_minute_candles

class OutputDefaultsTests(unittest.TestCase):
    def run_cli(self, module, minutes, root, extra=(), check_at_show=False):
        data=root/'candles.csv'
        _write_minute_candles(data,rows=600)
        if minutes != 1:
            frame=pd.read_csv(data)
            frame['Open time']=pd.date_range('2025-01-01',periods=len(frame),freq=f'{minutes}min')
            frame['Close time']=frame['Open time']+pd.Timedelta(minutes=minutes)-pd.Timedelta(milliseconds=1)
            frame.to_csv(data,index=False)
        output=root/'run'
        def at_show():
            if check_at_show:
                for name in ('params.json','result.json','chart.png','trades/data_orders.csv',
                             'trades/data_orders.xlsx','monthly/monthly_data_orders.csv'):
                    self.assertTrue((output/name).is_file(),name)
        argv=['--data-file',str(data),'--start','300','--end','600','--output-dir',str(output),*extra]
        if module is ma_strategy: argv.append('--quiet')
        with patch('chart_renderer.plt.show',side_effect=at_show) as show, contextlib.redirect_stdout(io.StringIO()):
            module.main(argv)
        return output,show.call_count
    def test_both_cli_defaults_save_all_files_before_showing_chart(self):
        for module,minutes in ((ma_strategy,15),(pulse_strategy,1)):
            with self.subTest(strategy=module.__name__), tempfile.TemporaryDirectory() as directory:
                output,calls=self.run_cli(module,minutes,Path(directory),check_at_show=True)
                self.assertEqual(calls,1)
                self.assertFalse(json.loads((output/'params.json').read_text()).get('optimize',False))
    def test_headless_keeps_chart_and_no_chart_keeps_data_reports(self):
        for module,minutes in ((ma_strategy,15),(pulse_strategy,1)):
            for flag,expected_png in (('--no-show-chart',True),('--no-chart',False)):
                with self.subTest(strategy=module.__name__,flag=flag), tempfile.TemporaryDirectory() as directory:
                    output,calls=self.run_cli(module,minutes,Path(directory),[flag])
                    self.assertEqual(calls,0)
                    self.assertEqual((output/'chart.png').exists(),expected_png)
                    self.assertTrue((output/'trades/data_orders.xlsx').is_file())
                    self.assertTrue((output/'monthly/monthly_data_orders.csv').is_file())
    def test_no_excel_preserves_csv_and_saved_worker_flag_cannot_disable_cli_reports(self):
        for module,minutes in ((ma_strategy,15),(pulse_strategy,1)):
            with self.subTest(strategy=module.__name__), tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                params=root/'params.json'; params.write_text(json.dumps({'optimize':True} if module is pulse_strategy else {}))
                flags=['--params-file',str(params),'--no-excel','--no-chart']
                if module is ma_strategy: flags.extend(['--params-source','file'])
                output,calls=self.run_cli(module,minutes,root,flags)
                self.assertFalse((output/'trades/data_orders.xlsx').exists())
                self.assertTrue((output/'trades/data_orders.csv').exists())
                self.assertTrue((output/'result.json').exists())
    def test_default_data_sources_and_chart_flags(self):
        self.assertEqual(Path(ma_strategy.DATA_FILE).name,'btc_15m_data_2018_to_2026.csv')
        self.assertEqual(pulse_strategy.DATA_FILE.name,'btc_1m_data_2025_to_2026.csv')
        for module in (ma_strategy,pulse_strategy):
            args=module.build_parser().parse_args([])
            self.assertTrue(args.show_chart)
            self.assertFalse(args.no_chart)

if __name__=='__main__': unittest.main()
