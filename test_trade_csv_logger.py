import tempfile
import unittest
from pathlib import Path

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
                file_name=str(csv_path),
            )
            workbook = load_workbook(csv_path.with_suffix(".xlsx"), read_only=False)
            sheet_names = workbook.sheetnames
            freeze_panes = [worksheet.freeze_panes for worksheet in workbook.worksheets]
            row_counts = {
                name: workbook[name].max_row
                for name in ("Main Strategy", "RSI Strategy", "Scale Strategy")
            }
            workbook.close()

        self.assertEqual(
            sheet_names,
            ["Overview", "All Trades", "Main Strategy", "RSI Strategy", "Scale Strategy"],
        )
        self.assertEqual(freeze_panes, ["A2"] * 5)
        self.assertEqual(row_counts["Main Strategy"], 2)
        self.assertEqual(row_counts["RSI Strategy"], 2)
        self.assertEqual(row_counts["Scale Strategy"], 2)


if __name__ == "__main__":
    unittest.main()
