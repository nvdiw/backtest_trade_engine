import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch
import numpy as np
import pandas as pd
import optimize
import pulse_strategy as pulse
from test_pulse_strategy import candles

class PulseIntegrationTests(unittest.TestCase):
    def test_grid_multiprocessing_reports_and_auto_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            data=root/'candles.csv'
            frame=candles(1100)
            wave=100+np.sin(np.arange(1100)/30)*8
            frame['Open']=wave; frame['Close']=wave+.1
            frame['High']=wave+.2; frame['Low']=wave-.2
            frame.to_csv(data,index=False)
            grid=root/'grid.json'
            grid.write_text(json.dumps({**{k: [getattr(pulse.PulseConfig(), k)] for k in pulse.FULL_PARAM_GRID}, 'breakout_lookback_bars':[15,20],
                                        'stop_atr_mult':[1.5,2.], 'max_hold_bars':[10,15]}))
            common=['--strategy','pulse','--param-grid',str(grid),'--data-file',str(data),
                    '--date-policy','fixed','--excel-top','2','--seed','17']
            output=root/'grid_run'
            with contextlib.redirect_stdout(io.StringIO()):
                optimize.main(common+['--mode','grid','--start','200','--end','900','-w','2',
                                      '--output-dir',str(output)])
            best=pulse.load_strategy_tune(output/'best_params.json')
            self.assertIn(best['breakout_lookback_bars'],[15,20])
            self.assertEqual(best['risk_per_trade'],.0025)
            self.assertNotIn('ema_16_period',best)
            manifest=json.loads((output/'research_manifest.json').read_text())
            self.assertEqual(manifest['strategy'],pulse.IDENTIFIER)
            with zipfile.ZipFile(output/'optimization_results.xlsx') as archive:
                text=''.join(archive.read(n).decode('utf-8') for n in archive.namelist() if n.endswith('.xml'))
                self.assertIn('breakout_lookback_bars',text)
            auto=root/'auto'
            args=common+['--auto','--auto-stress-start','200','--auto-validation-start','400',
                '--auto-discovery-start','600','--auto-end','1000','--auto-tests','4',
                '--auto-validation-top','3','--auto-stress-top','2','--auto-final-top','1',
                '--auto-hall-size','4','--snapshot-cycles','1','--snapshot-top','1',
                '--output-dir',str(auto)]
            with contextlib.redirect_stdout(io.StringIO()):
                optimize.main(args+['--auto-cycles','1','-w','1'])
            state=json.loads((auto/'auto_state.json').read_text())
            self.assertEqual(state['cycles_completed'],1)
            with contextlib.redirect_stdout(io.StringIO()):
                optimize.main(args+['--auto-cycles','2','--resume','--performance','power_saving','-w','2'])
            state=json.loads((auto/'auto_state.json').read_text())
            self.assertEqual(state['cycles_completed'],2)
            self.assertEqual(state['config']['strategy'],pulse.IDENTIFIER)
            self.assertEqual(len(state['config']['parameter_grid']),len(pulse.FULL_PARAM_GRID))
            self.assertTrue((auto/'snapshots/cycles_000002/best_params.json').exists())
    def test_backtest_xlsx_contains_strategy_and_parameters(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); data=root/'candles.csv'; candles().to_csv(data,index=False)
            output=root/'backtest'
            pulse.pulse_strategy({'breakout_lookback_bars':20,'max_hold_bars':3},0,100,
                                 data_file=data,output_dir=output,show_chart=False)
            self.assertTrue((output/'result.json').exists())
            self.assertGreater((output/'chart.png').stat().st_size, 1000)
            self.assertTrue((output/'monthly/monthly_data_orders.csv').exists())
            self.assertEqual(json.loads((output/'params.json').read_text())['max_hold_bars'],3)
            with zipfile.ZipFile(output/'trades/data_orders.xlsx') as archive:
                text=''.join(archive.read(n).decode('utf-8') for n in archive.namelist() if n.endswith('.xml'))
                self.assertIn(pulse.DISPLAY_NAME,text)
                self.assertIn('max_hold_bars',text)
    def test_optimizer_rejects_cost_search(self):
        with tempfile.TemporaryDirectory() as directory:
            grid=Path(directory)/'grid.json'
            grid.write_text(json.dumps({'fee_rate':[0.,.001]}))
            with self.assertRaisesRegex(SystemExit,'fixed policies'):
                optimize.main(['--strategy','pulse','--param-grid',str(grid),'--list-profiles'])

if __name__=='__main__': unittest.main()
