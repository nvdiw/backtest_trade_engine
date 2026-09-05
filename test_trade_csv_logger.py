import tempfile
import unittest
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

from trade_csv_logger import TradeCSVLogger


class TradeCSVLoggerWorkbookTests(unittest.TestCase):
    def test_default_workbook_has_frozen_separate_strategy_sheets(self):
        logger = TradeCSVLogger()
        logger.rows = [
            {"trade_id": "ma_strategy_0001", "type": "LONG", "profit": 10},
            {"trade_id": "rsi_ma_strategy_0002", "type": "SHORT", "profit": -2},
            {"trade_id": "scale_ma_strategy_0003", "type": "LONG", "profit": 3},
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "data_orders.csv"
            logger.save_csv(
                first_balance=1000,
                final_balance=1011,
                total_profit=11,
                total_profit_percent=1.1,
                total_fee=0.5,
                start_time="2026-01-01 00:00:00",
                end_time="2026-01-02 00:00:00",
                days=1,
                hours=0,
                minutes=0,
                overview_metrics={
                    "Run": {"Start time": "2026-01-01 00:00:00"},
                    "Capital": {"Final balance": 1011, "Total profit %": 1.1},
                    "Performance": {"Maximum drawdown %": -2.5},
                    "Trades": {"Closed trades": 3},
                    "RSI": {"RSI trades": 1},
                    "Scale": {"Scale trades": 1},
                },
                file_name=str(csv_path),
            )
            workbook = load_workbook(csv_path.with_suffix(".xlsx"), read_only=False)
            sheet_names = workbook.sheetnames
            freeze_panes = [worksheet.freeze_panes for worksheet in workbook.worksheets]
            row_counts = {
                name: workbook[name].max_row
                for name in ("Main Strategy", "RSI Strategy", "Scale Strategy")
            }
            overview_headers = [cell.value for cell in workbook["Overview"][1]]
            overview_metrics = [
                workbook["Overview"].cell(row=row, column=2).value
                for row in range(2, workbook["Overview"].max_row + 1)
            ]
            dashboard = workbook["Dashboard"]
            dashboard_chart_count = len(workbook["Charts"]._charts)
            labeled_charts = [
                chart for chart in workbook["Charts"]._charts
                if chart.dLbls is not None and chart.dLbls.showVal
            ]
            net_profit_panel_title = dashboard["N2"].value
            win_rate_panel_title = dashboard["N18"].value
            workbook.close()
            summary_rows = pd.read_csv(
                csv_path.with_name("data_orders_summary.csv")
            )

        self.assertEqual(
            sheet_names,
            [
                "Dashboard", "Side Comparison", "Overview", "Monthly Analysis",
                "Exit Reasons", "All Trades", "Long Trades", "Short Trades",
                "Main Strategy", "RSI Strategy", "Scale Strategy", "Charts",
            ],
        )
        self.assertEqual(freeze_panes[:5], ["A2"] * 5)
        self.assertEqual(freeze_panes[5:8], ["E2"] * 3)
        self.assertEqual(freeze_panes[8:], ["A2"] * 4)
        self.assertEqual(row_counts["Main Strategy"], 2)
        self.assertEqual(row_counts["RSI Strategy"], 2)
        self.assertEqual(row_counts["Scale Strategy"], 2)
        self.assertEqual(overview_headers, ["Section", "Metric", "Value"])
        self.assertIn("Maximum drawdown %", overview_metrics)
        self.assertEqual(summary_rows["scope"].tolist(), ["TOTAL", "LONG", "SHORT"])
        self.assertEqual(summary_rows.loc[1, "net_profit"], 13)
        self.assertEqual(summary_rows.loc[2, "net_profit"], -2)
        self.assertGreaterEqual(dashboard_chart_count, 2)
        self.assertGreaterEqual(len(labeled_charts), 2)
        self.assertEqual(net_profit_panel_title, "Exact values - net profit")
        self.assertEqual(win_rate_panel_title, "Exact values - win rate")


if __name__ == "__main__":
    unittest.main()
