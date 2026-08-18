import ast
from pathlib import Path
import unittest

from trade_engine import AccountState, Position, TradeEngine


OPEN_TIME = "2026-01-01 00:00:00"
CLOSE_TIME = "2026-01-01 00:15:00"


class TradeEngineStateTests(unittest.TestCase):
    def setUp(self):
        self.engine = TradeEngine(optimize=True, verbose=False)

    def make_position(self, opened, side="long"):
        return Position.from_open_result(
            opened,
            trade_id="test_0001",
            side=side,
            entry_index=0,
            high_price=10.0,
            low_price=10.0,
            reason="test",
        )

    def test_open_defaults_use_all_balance_at_one_x(self):
        account = AccountState(balance=100.0)
        opened = self.engine.open_long(0, [10.0], [OPEN_TIME], account)

        self.assertEqual(opened["margin"], 100.0)
        self.assertEqual(opened["leverage"], 1.0)
        self.assertEqual(opened["position_value"], 100.0)
        self.assertEqual(account.balance, 0.0)

    def test_open_allows_custom_size_and_leverage(self):
        account = AccountState(balance=100.0)
        opened = self.engine.open_short(
            0,
            [10.0],
            [OPEN_TIME],
            account,
            trade_amount_percent=0.25,
            leverage=3,
        )

        self.assertEqual(opened["margin"], 25.0)
        self.assertEqual(opened["leverage"], 3)
        self.assertEqual(opened["position_value"], 75.0)
        self.assertEqual(account.balance, 75.0)

    def test_close_mutates_account_state(self):
        account = AccountState(balance=100.0)
        position = self.make_position(
            self.engine.open_long(0, [10.0], [OPEN_TIME], account)
        )

        closed = self.engine.close_long(
            0,
            [11.0],
            [CLOSE_TIME],
            position,
            account,
            fee_rate=0.0005,
            cooldown_after_big_pnl=12,
        )

        self.assertAlmostEqual(closed["profit"], 9.895)
        self.assertAlmostEqual(account.balance, 109.895)
        self.assertEqual(account.count_closed_orders, 1)
        self.assertEqual(account.total_wins, 1)
        self.assertEqual(account.total_wins_long, 1)

    def test_position_is_mutable_strategy_state(self):
        account = AccountState(balance=100.0)
        position = self.make_position(
            self.engine.open_long(0, [10.0], [OPEN_TIME], account)
        )

        position.target_close_price_loss = 10.5
        self.assertEqual(position["target_close_price_loss"], 10.5)
        self.assertEqual(position.side, "long")

    def test_short_close_and_liquidation_use_state_api(self):
        short_account = AccountState(balance=100.0)
        short_position = self.make_position(
            self.engine.open_short(0, [10.0], [OPEN_TIME], short_account),
            side="short",
        )
        closed = self.engine.close_short(
            0,
            [9.0],
            [CLOSE_TIME],
            short_position,
            short_account,
            fee_rate=0.0005,
            cooldown_after_big_pnl=12,
        )
        self.assertAlmostEqual(closed["profit"], 9.905)
        self.assertEqual(short_account.total_wins_short, 1)

        liquid_account = AccountState(balance=100.0)
        liquid_position = self.make_position(
            self.engine.open_long(
                0,
                [10.0],
                [OPEN_TIME],
                liquid_account,
                leverage=2,
            )
        )
        liquidation = self.engine.check_liquidation_long(
            0,
            [5.0],
            [CLOSE_TIME],
            liquid_position,
            liquid_account,
        )
        self.assertTrue(liquidation["liquidated"])
        self.assertEqual(liquid_account.total_liquids, 1)
        self.assertEqual(liquid_account.total_losses, 1)
        self.assertEqual(liquid_account.profits_lst, [-100.0])


class StrategyExecutionTimingTests(unittest.TestCase):
    def test_strategy_orders_execute_at_next_candle_open(self):
        source = Path("ma_strategy.py").read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        order_methods = {"open_long", "open_short", "close_long", "close_short"}
        order_calls = []

        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "trade_engine"
                and node.func.attr in order_methods
            ):
                order_calls.append(node)

        self.assertTrue(order_calls)
        for call in order_calls:
            self.assertEqual(ast.unparse(call.args[0]), "execution_i")
            self.assertEqual(ast.unparse(call.args[1]), "open_prices")
            self.assertEqual(ast.unparse(call.args[2]), "open_times")

    def test_new_positions_are_timestamped_at_execution_candle(self):
        source = Path("ma_strategy.py").read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        position_calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "from_open_result"
        ]

        self.assertTrue(position_calls)
        for call in position_calls:
            entry_index = next(
                keyword.value for keyword in call.keywords
                if keyword.arg == "entry_index"
            )
            self.assertEqual(ast.unparse(entry_index), "execution_i")


if __name__ == "__main__":
    unittest.main()
