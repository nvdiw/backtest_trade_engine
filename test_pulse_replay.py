import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import pulse_strategy as pulse
from pulse_replay import load_campaign_replay
from test_pulse_strategy import candles


class PulseReplayTests(unittest.TestCase):
    def test_cli_replays_saved_window_parameters_and_market(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            data=root/'candles.csv'; candles().to_csv(data,index=False)
            params={'breakout_lookback_bars':20,'max_hold_bars':3}
            expected=pulse.pulse_strategy({**params,'optimize':True},0,100,data_file=data)
            campaign=root/'campaign'; campaign.mkdir()
            config=dict(strategy=pulse.IDENTIFIER,data_file=str(data),timeframe='1m',
                        indicator_warmup=True,base_tune={},ranges={'final':[0,100]})
            (campaign/'auto_state.json').write_text(json.dumps({'config':config}))
            (campaign/'hall_of_fame.json').write_text(json.dumps([
                dict(candidate_id='candidate',effective_params=params,stage_metrics={'final':expected})]))
            output=root/'replay'
            self.assertEqual(pulse.main(['--campaign',str(campaign),'--no-chart','--no-excel',
                                         '--quiet','--output-dir',str(output)]),0)
            comparison=json.loads((output/'replay_comparison.json').read_text())
            self.assertTrue(comparison['matches'])
            with self.assertRaises(ValueError): load_campaign_replay(campaign,2)
            changed=root/'changed'
            pulse.main(['--campaign',str(campaign),'--set','enable_long=false','--no-chart',
                        '--no-excel','--quiet','--output-dir',str(changed)])
            self.assertFalse(json.loads((changed/'replay_comparison.json').read_text())['matches'])
