import unittest
from unittest.mock import patch

import numpy as np

from rsi_strategy import (
    _RSI_CACHE,
    _cached_rsi,
    _funding_cost,
    build_strategy_config,
    rsi_strategy,
)
from strategy_adapter import resolve_strategy


class RSIStrategyPluginTests(unittest.TestCase):
    def test_rsi_indicator_is_reused_for_same_range_and_period(self):
        closes = np.asarray([100.0, 101.0, 102.0, 101.0])
        _RSI_CACHE.clear()
        with patch(
            "rsi_strategy.Indicator.get_RSI",
            return_value=[None, None, 75.0, 50.0],
        ) as calculator:
            first = _cached_rsi((0, 4, 0), 2, closes)
            second = _cached_rsi((0, 4, 0), 2, closes)

        self.assertIs(first, second)
        calculator.assert_called_once()

    def test_rsi_alias_exposes_its_own_profiles(self):
        adapter = resolve_strategy("rsi")
        profiles = adapter.discovered_profiles()
        self.assertEqual(adapter.identifier, "rsi_strategy:rsi_strategy")
        self.assertIn("focused", profiles)
        self.assertIn("full", profiles)
        self.assertIn("period_rsi", profiles["focused"])

    def test_config_rejects_invalid_threshold_relationships(self):
        with self.assertRaisesRegex(ValueError, "long levels"):
            build_strategy_config({"long_entry_rsi": 60, "long_exit_rsi": 50})

    def test_funding_uses_elapsed_time_including_market_data_gaps(self):
        config = build_strategy_config({"funding_rate_per_8h": 0.001})
        position = {
            "entry_time": "2024-01-01 00:00:00+00:00",
            "notional": 1000.0,
        }
        self.assertEqual(
            _funding_cost(position, "2024-01-01 09:00:00+00:00", config),
            2.0,
        )

    @patch(
        "rsi_strategy.Indicator.get_RSI",
        return_value=[None, 25.0, 35.0, 40.0, 60.0, 55.0],
    )
    @patch("rsi_strategy.TradeEngine.load_market_data")
    def test_signal_is_filled_at_next_open_and_research_series_are_compact(
        self, market_mock, _rsi_mock
    ):
        opens = np.asarray([100.0, 100.0, 90.0, 110.0, 112.0, 120.0])
        market_mock.return_value = {
            "open_prices": opens,
            "close_prices": opens,
            "low_prices": opens * 0.99,
            "high_prices": opens * 1.01,
            "open_times": [f"2024-01-01 0{i}:00:00" for i in range(6)],
            "history_close_prices": opens,
            "warmup_offset": 0,
        }
        result = rsi_strategy(
            start=0,
            end=6,
            research=True,
            tune={
                "fee_rate": 0.0,
                "slippage_rate": 0.0,
                "funding_rate_per_8h": 0.0,
                "trade_amount_percent": 1.0,
                "leverage": 1.0,
                "take_profit_pct": 0.50,
                "stop_loss_pct": 0.50,
                "cooldown_candles": 0,
            },
        )

        # Cross at candle 2 enters at candle 3 open (110), then exits at
        # candle 5 open (120): 1000 * (120/110 - 1).
        self.assertEqual(result["closed_trades"], 1)
        self.assertAlmostEqual(result["total_profit"], 90.909091, places=5)
        self.assertEqual(len(result["trade_profits"]), 1)
        self.assertEqual(len(result["monthly_returns"]), 1)
        self.assertEqual(market_mock.call_args.kwargs["warmup_candles"], 15)


if __name__ == "__main__":
    unittest.main()
