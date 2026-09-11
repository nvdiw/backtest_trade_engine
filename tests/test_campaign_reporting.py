import json
from pathlib import Path
import tempfile
import unittest

import pandas as pd
from openpyxl import load_workbook
from campaign_reporting import publish_campaign


class CampaignDirectionTests(unittest.TestCase):
    def test_long_evidence_survives_short_only_overall_winner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = root / 'cycles/cycle_000001'
            stage.mkdir(parents=True)
            state = {'config': {'strategy': 'pulse', 'minimum_trades': 1}}
            rows = [dict(candidate_id='short-winner', total_profit_percent=10,
                         objective_score=2, closed_trades=5, long_trades=0, long_profit=0,
                         short_trades=5, short_profit=100, enable_long=False, enable_short=True),
                    dict(candidate_id='long-trial', total_profit_percent=4,
                         objective_score=1, closed_trades=3, long_trades=3, long_profit=40,
                         short_trades=0, short_profit=0, enable_long=True, enable_short=False)]
            pd.DataFrame(rows).to_csv(stage / 'discovery_results.csv', index=False)
            publish_campaign(root, state=state, rebuild=True)
            evidence = json.loads((root / 'stage_evidence.json').read_text())['cycle_000001/discovery']
            self.assertEqual(evidence['best_observed']['candidate_id'], 'short-winner')
            self.assertEqual(evidence['direction_best']['long']['candidate_id'], 'long-trial')
            frame = pd.read_csv(root / 'direction_observations.csv')
            self.assertEqual(set(frame.candidate_id), {'short-winner', 'long-trial'})
            book = load_workbook(root / 'campaign_report.xlsx')
            sheet = book['Direction Leaders']
            self.assertEqual(sheet.freeze_panes, 'C2')
            headers = {cell.value: cell for cell in sheet[1]}
            self.assertNotEqual(headers['long_profit'].fill.fgColor.rgb,
                                headers['short_profit'].fill.fgColor.rgb)
            book.close()
