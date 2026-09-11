import unittest

from generate_reason_text import generate_close_reason_text, generate_entry_reason_text


class GenerateReasonTextTests(unittest.TestCase):
    def test_entry_tooltip_contains_complete_execution_details(self):
        text = generate_entry_reason_text("ma_strategy_0007", {
            "open_time_value": "2026-01-02 03:04:05.123",
            "entry_price": 101.25,
            "position_size": 2.5,
            "position_value": 253.125,
            "margin": 50.625,
            "leverage": 5,
            "trade_amount_percent": 0.25,
            "balance_before_trade": 1000,
            "balance": 949.375,
        })

        self.assertIn("Trade ID       : ma_strategy_0007", text)
        self.assertIn("Execution time : 2026-01-02 03:04:05", text)
        self.assertIn("Capital used   : 25.00%", text)
        self.assertIn("Position size  : 2.50000000", text)

    def test_close_tooltip_contains_pnl_fees_balance_and_duration(self):
        text = generate_close_reason_text("ma_strategy_0007", {
            "close_time_value": "2026-01-03 05:34:05",
            "close_price": 110,
            "margin": 50,
            "leverage": 5,
            "pnl": 12.5,
            "pnl_percent": 25,
            "total_fee": 0.1234,
            "profit": 12.3766,
            "profit_percent": 1.2,
            "logged_balance_before": 1000,
            "logged_balance_after": 1012.3766,
            "save_money": 10,
            "days": 1,
            "hours": 2,
            "minutes": 30,
        })

        self.assertIn("Gross PnL      : $12.50", text)
        self.assertIn("Total fees     : $0.1234", text)
        self.assertIn("Balance        : $1,000.00 -> $1,012.38", text)
        self.assertIn("Duration       : 1d 2h 30m", text)


if __name__ == "__main__":
    unittest.main()
