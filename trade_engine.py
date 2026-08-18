"""Execution, accounting, logging, reporting, and chart lifecycle for backtests."""

import os
import math
from datetime import datetime, timezone
from functools import lru_cache
from dataclasses import dataclass, field, fields
from typing import Optional

import numpy as np
import pandas as pd

from chart_renderer import render_backtest_chart
from check_monthly_data import write_monthly_summary
from fetch_calculate_data import fetch_all_data
from get_candle_index import get_candle_index, get_month_start_indices
from trade_csv_logger import TradeCSVLogger


class _DataclassMapping:
    """Small compatibility layer for existing reason/strategy helpers."""

    def __getitem__(self, key):
        return getattr(self, key)

    def __setitem__(self, key, value):
        setattr(self, key, value)

    def get(self, key, default=None):
        return getattr(self, key, default)

    def __contains__(self, key):
        return any(item.name == key for item in fields(self))


@dataclass
class Position(_DataclassMapping):
    trade_id: str
    side: str
    entry_price: float
    entry_index: Optional[int]
    highest_since_entry: Optional[float]
    lowest_since_entry: Optional[float]
    position_size: float
    position_size_no_fee: float
    balance_before_trade: float
    balance_before_trade_no_fee: float
    margin: float
    margin_no_fee: float
    trade_amount_percent: float
    leverage: float
    open_time_value: str
    target_close_price_loss: Optional[float]
    reason: str

    @classmethod
    def from_open_result(
        cls,
        result,
        *,
        trade_id,
        side,
        entry_index,
        high_price,
        low_price,
        reason,
    ):
        entry_price = result["entry_price"]
        return cls(
            trade_id=trade_id,
            side=side,
            entry_price=entry_price,
            entry_index=entry_index,
            highest_since_entry=max(entry_price, high_price),
            lowest_since_entry=min(entry_price, low_price),
            position_size=result["position_size"],
            position_size_no_fee=result["position_size_no_fee"],
            balance_before_trade=result["balance_before_trade"],
            balance_before_trade_no_fee=result["balance_before_trade_no_fee"],
            margin=result["margin"],
            margin_no_fee=result["margin_no_fee"],
            trade_amount_percent=result["trade_amount_percent"],
            leverage=result["leverage"],
            open_time_value=result["open_time_value"],
            target_close_price_loss=entry_price,
            reason=reason,
        )


@dataclass
class AccountState:
    balance: float
    balance_without_fee: Optional[float] = None
    tactical_balance: Optional[float] = None
    deducting_fee_total: float = 0.0
    profits_lst: list = field(default_factory=list)
    total_profit_percent: float = 0.0
    count_closed_orders: int = 0
    equity_curve: list = field(default_factory=list)
    max_drawdown: float = 0.0
    total_wins: int = 0
    total_wins_long: int = 0
    total_wins_short: int = 0
    total_losses: int = 0
    total_long: int = 0
    total_short: int = 0
    cooldown_until_index: int = -1
    profit_percent_per_month: float = 0.0
    save_money: float = 0.0
    trade_power: bool = True
    total_liquids: int = 0

    def __post_init__(self):
        if self.balance_without_fee is None:
            self.balance_without_fee = self.balance
        if self.tactical_balance is None:
            self.tactical_balance = self.balance

    def apply_result(self, result):
        """Apply calculator output fields in one controlled place."""
        if result is None:
            return
        for item in fields(self):
            if item.name in result:
                setattr(self, item.name, result[item.name])


# Calculate Trade Duration
def trade_duration(open_time: str, close_time: str):
    """Return elapsed whole days/hours/minutes for ISO-like timestamps."""

    def parse(value):
        text = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    seconds = max(0, int((parse(close_time) - parse(open_time)).total_seconds()))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    return days, hours, minutes


# Trade engine for execution and backtest lifecycle concerns.
class TradeEngine:
    """Own the non-strategy side of a backtest.

    Public order methods default to 100% capital and 1x leverage. Strategies that
    want the configured safe-leverage selection can explicitly pass
    ``leverage=None``.
    """

    def __init__(
        self,
        first_balance=1000.0,
        monthly_profit_percent_stop_trade=9,
        monthly_loss_percent_stop_trade=19,
        tactical_balance=None,
        monthly_profit_close_filter=True,
        monthly_loss_close_filter=False,
        monthly_compound=3.0,
        leverage=1.0,
        safe_leverage_low=1.0,
        safe_leverage_med=1.0,
        safe_leverage_high=1.0,
        safe_leverage_balance_pct_low=80.0,
        safe_leverage_balance_pct_med=80.0,
        safe_leverage_balance_pct_high=90.0,
        save_money_recover_trigger_pct=75.0,
        verbose=True,
        optimize=False,
        write_trades=True,
        write_excel=False,
        output_dir="outputs",
        track_equity_curve=None,
        csv_logger=None,
    ):
        self.write_trades = bool(write_trades) and not bool(optimize)
        self.output_dir = os.fspath(output_dir)
        self.csv_logger = csv_logger or TradeCSVLogger(
            optimize=not self.write_trades,
            write_excel=write_excel,
        )
        self.first_balance = first_balance
        self.monthly_profit_percent_stop_trade = monthly_profit_percent_stop_trade
        self.monthly_loss_percent_stop_trade = monthly_loss_percent_stop_trade
        self.tactical_balance = first_balance if tactical_balance is None else tactical_balance
        self.monthly_profit_close_filter = monthly_profit_close_filter
        self.monthly_loss_close_filter = monthly_loss_close_filter
        self.monthly_compound = monthly_compound
        self.leverage = leverage
        self.safe_leverage_low = safe_leverage_low
        self.safe_leverage_med = safe_leverage_med
        self.safe_leverage_high = safe_leverage_high
        self.safe_leverage_balance_pct_low = safe_leverage_balance_pct_low
        self.safe_leverage_balance_pct_med = safe_leverage_balance_pct_med
        self.safe_leverage_balance_pct_high = safe_leverage_balance_pct_high
        self.save_money_recover_trigger_pct = save_money_recover_trigger_pct
        self.verbose = bool(verbose)
        if track_equity_curve is None:
            track_equity_curve = not bool(optimize)
        self.track_equity_curve = bool(track_equity_curve)
        self.equity_peak = None
        # self.just_one_time = True

    @staticmethod
    @lru_cache(maxsize=4)
    def load_market_data(start="2025-01-01", end="2026-02-23"):
        """Resolve and cache an inclusive start/exclusive end candle range.

        Optimization workers call the strategy many times for the same range.  The
        cached immutable market arrays avoid re-reading and parsing the CSV for
        every candidate in a worker process.
        """
        start_index = get_candle_index(start) if isinstance(start, str) else int(start)
        end_index = get_candle_index(end) if isinstance(end, str) else int(end)
        if end_index <= start_index:
            raise ValueError("end must resolve to a candle after start")

        all_data = fetch_all_data(start_index, end_index)
        if not all_data or len(all_data["Close"]) == 0:
            raise ValueError("the selected start/end range contains no candles")

        close_times = (
            pd.to_datetime(all_data["Close time"], utc=True)
            + pd.Timedelta(milliseconds=1)
        ).strftime("%Y-%m-%d %H:%M:%S.%f").tolist()

        return {
            "start": start_index,
            "end": end_index,
            "month_starts": get_month_start_indices(start_index, end_index, just_index=True),
            "open_prices": np.asarray(all_data["Open"], dtype=float),
            "close_prices": np.asarray(all_data["Close"], dtype=float),
            "open_times": all_data["Open time"],
            "close_times": close_times,
            "low_prices": np.asarray(all_data["Low"], dtype=float),
            "high_prices": np.asarray(all_data["High"], dtype=float),
            "volume_prices": np.asarray(all_data["Volume"], dtype=float),
        }

    @staticmethod
    def create_chart_state(optimize=False, plot_penalties=True, enabled=None):
        """Create chart bookkeeping without leaking renderer setup into a strategy."""
        if enabled is None:
            enabled = not optimize
        enabled = bool(enabled) and not bool(optimize)
        penalty_enabled = enabled and plot_penalties
        return {
            "chart_data": [] if enabled else None,
            "long_open_points": [] if enabled else None,
            "long_close_points": [] if enabled else None,
            "short_open_points": [] if enabled else None,
            "short_close_points": [] if enabled else None,
            "long_open_reasons": {} if enabled else None,
            "long_close_reasons": {} if enabled else None,
            "short_open_reasons": {} if enabled else None,
            "short_close_reasons": {} if enabled else None,
            "penalty_long_points": [] if penalty_enabled else None,
            "penalty_short_points": [] if penalty_enabled else None,
            "penalty_long_reasons": {} if penalty_enabled else None,
            "penalty_short_reasons": {} if penalty_enabled else None,
        }

    @staticmethod
    def display(enabled, *parts):
        if enabled:
            print(*parts)

    @staticmethod
    def _safe_percent(value, base):
        return (value * 100 / base) if base != 0 else 0

    @staticmethod
    def _resolve_csv_balances(
        balance_before_free,
        margin,
        profit,
        balance_before_override=None,
        remaining_open_margin=0,
    ):
        # CSV balance_before/after are portfolio-level values for readability:
        # before = free capital + current margin + other locked margins, excluding save_money.
        balance_before = balance_before_free + margin + remaining_open_margin
        if balance_before_override is not None:
            balance_before = balance_before_override
        balance_after = balance_before + profit
        return balance_before, balance_after

    @staticmethod
    def _resolve_total_assets(balance, save_money, remaining_open_margin, remaining_open_equity=None):
        open_position_value = remaining_open_margin if remaining_open_equity is None else remaining_open_equity
        return balance + open_position_value + save_money

    def _update_drawdown(self, equity_curve, max_drawdown, total_assets):
        if self.track_equity_curve:
            equity_curve.append(total_assets)
        if self.equity_peak is None:
            self.equity_peak = total_assets
        elif total_assets > self.equity_peak:
            self.equity_peak = total_assets

        peak = self.equity_peak
        if peak <= 0:
            return equity_curve, max_drawdown
        drawdown = (total_assets - peak) / peak * 100
        return equity_curve, min(max_drawdown, drawdown)

    @staticmethod
    def position_equity(position, price):
        if position["side"] == "long":
            pnl_percent = (price - position["entry_price"]) / position["entry_price"] * 100
        else:
            pnl_percent = (position["entry_price"] - price) / position["entry_price"] * 100
        return position["margin"] + position["margin"] * pnl_percent * position["leverage"] / 100

    @staticmethod
    def position_equity_no_fee(position, price):
        """Mark one position using its fee-free size and margin fields."""
        direction = 1.0 if position["side"] == "long" else -1.0
        pnl = (
            position["position_size_no_fee"]
            * (price - position["entry_price"])
            * direction
        )
        return position["margin_no_fee"] + pnl

    @classmethod
    def open_positions_equity(cls, positions, price, exclude_position=None):
        return sum(
            cls.position_equity(position, price)
            for position in positions
            if position is not exclude_position
        )

    def open_long(
        self,
        i,
        open_prices,
        open_times,
        account: AccountState,
        *,
        trade_amount_percent=1.0,
        margin_balance=None,
        margin_balance_no_fee=None,
        leverage=1.0,
    ):
        """Open a long using an AccountState; defaults are 100% capital at 1x."""
        result = self._calculate_open_long(
            i,
            open_prices,
            open_times,
            account.balance,
            account.balance_without_fee,
            trade_amount_percent,
            margin_balance,
            margin_balance_no_fee,
            account.tactical_balance,
            leverage,
        )
        account.apply_result(result)
        return result

    def open_short(
        self,
        i,
        open_prices,
        open_times,
        account: AccountState,
        *,
        trade_amount_percent=1.0,
        margin_balance=None,
        margin_balance_no_fee=None,
        leverage=1.0,
    ):
        """Open a short using an AccountState; defaults are 100% capital at 1x."""
        result = self._calculate_open_short(
            i,
            open_prices,
            open_times,
            account.balance,
            account.balance_without_fee,
            trade_amount_percent,
            margin_balance,
            margin_balance_no_fee,
            account.tactical_balance,
            leverage,
        )
        account.apply_result(result)
        return result

    def close_long(
        self,
        i,
        prices,
        times,
        position: Position,
        account: AccountState,
        *,
        fee_rate,
        cooldown_after_big_pnl,
        remaining_open_margin=0.0,
        remaining_open_margin_no_fee=0.0,
        reason_to_close=None,
        balance_before_close_snapshot=None,
        balance_before_close_no_fee_snapshot=None,
        balance_before_log_override=None,
        balance_before_log_override_no_fee=None,
        remaining_open_equity=None,
    ):
        """Close the full supplied long and mutate AccountState atomically."""
        result = self._calculate_close_long(
            i, prices, times,
            position["entry_price"], position["position_size"], position["position_size_no_fee"],
            fee_rate, position["margin"], position["margin_no_fee"],
            account.balance, account.balance_without_fee,
            account.deducting_fee_total, account.profits_lst, account.total_profit_percent,
            account.count_closed_orders, account.equity_curve,
            account.max_drawdown, account.total_wins, account.total_wins_long, account.total_losses,
            account.total_long, cooldown_after_big_pnl, position["leverage"],
            account.cooldown_until_index, position["open_time_value"], position["trade_amount_percent"],
            account.profit_percent_per_month, account.save_money, account.trade_power,
            position["trade_id"], remaining_open_margin, remaining_open_margin_no_fee,
            account.tactical_balance, position["reason"] if reason_to_close is None else reason_to_close,
            balance_before_close_snapshot, balance_before_close_no_fee_snapshot,
            balance_before_log_override, balance_before_log_override_no_fee,
            remaining_open_equity,
        )
        account.apply_result(result)
        return result

    def close_short(
        self,
        i,
        prices,
        times,
        position: Position,
        account: AccountState,
        *,
        fee_rate,
        cooldown_after_big_pnl,
        remaining_open_margin=0.0,
        remaining_open_margin_no_fee=0.0,
        reason_to_close=None,
        balance_before_close_snapshot=None,
        balance_before_close_no_fee_snapshot=None,
        balance_before_log_override=None,
        balance_before_log_override_no_fee=None,
        remaining_open_equity=None,
    ):
        """Close the full supplied short and mutate AccountState atomically."""
        result = self._calculate_close_short(
            i, prices, times,
            position["entry_price"], position["position_size"], position["position_size_no_fee"],
            fee_rate, position["margin"], position["margin_no_fee"],
            account.balance, account.balance_without_fee,
            account.deducting_fee_total, account.profits_lst, account.total_profit_percent,
            account.count_closed_orders, account.equity_curve,
            account.max_drawdown, account.total_wins, account.total_wins_short, account.total_losses,
            account.total_short, cooldown_after_big_pnl, position["leverage"],
            account.cooldown_until_index, position["open_time_value"], position["trade_amount_percent"],
            account.profit_percent_per_month, account.save_money, account.trade_power,
            position["trade_id"], remaining_open_margin, remaining_open_margin_no_fee,
            account.tactical_balance, position["reason"] if reason_to_close is None else reason_to_close,
            balance_before_close_snapshot, balance_before_close_no_fee_snapshot,
            balance_before_log_override, balance_before_log_override_no_fee,
            remaining_open_equity,
        )
        account.apply_result(result)
        return result

    def check_liquidation_long(
        self,
        i,
        low_prices,
        close_times,
        position: Position,
        account: AccountState,
        *,
        remaining_open_margin=0.0,
        remaining_open_margin_no_fee=0.0,
        reason_to_close=None,
        balance_before_close_snapshot=None,
        balance_before_close_no_fee_snapshot=None,
        balance_before_log_override=None,
        balance_before_log_override_no_fee=None,
        remaining_open_equity=None,
    ):
        result = self._calculate_liquidation_long(
            i, low_prices, close_times,
            position["entry_price"], position["leverage"], position["margin"],
            account.balance, account.balance_without_fee,
            account.deducting_fee_total, account.profits_lst, account.count_closed_orders,
            account.total_losses, account.total_long, account.equity_curve,
            account.save_money, account.max_drawdown, position["open_time_value"],
            position["trade_amount_percent"], account.total_liquids, position["trade_id"],
            remaining_open_margin, remaining_open_margin_no_fee,
            account.tactical_balance, position["reason"] if reason_to_close is None else reason_to_close,
            balance_before_close_snapshot, balance_before_close_no_fee_snapshot,
            balance_before_log_override, balance_before_log_override_no_fee,
            remaining_open_equity,
        )
        account.apply_result(result)
        return result

    def check_liquidation_short(
        self,
        i,
        high_prices,
        close_times,
        position: Position,
        account: AccountState,
        *,
        remaining_open_margin=0.0,
        remaining_open_margin_no_fee=0.0,
        reason_to_close=None,
        balance_before_close_snapshot=None,
        balance_before_close_no_fee_snapshot=None,
        balance_before_log_override=None,
        balance_before_log_override_no_fee=None,
        remaining_open_equity=None,
    ):
        result = self._calculate_liquidation_short(
            i, high_prices, close_times,
            position["entry_price"], position["leverage"], position["margin"],
            account.balance, account.balance_without_fee,
            account.deducting_fee_total, account.profits_lst, account.count_closed_orders,
            account.total_losses, account.total_short, account.equity_curve,
            account.save_money, account.max_drawdown, position["open_time_value"],
            position["trade_amount_percent"], account.total_liquids, position["trade_id"],
            remaining_open_margin, remaining_open_margin_no_fee,
            account.tactical_balance, position["reason"] if reason_to_close is None else reason_to_close,
            balance_before_close_snapshot, balance_before_close_no_fee_snapshot,
            balance_before_log_override, balance_before_log_override_no_fee,
            remaining_open_equity,
        )
        account.apply_result(result)
        return result


    # open long processes
    def _calculate_open_long(self, i, open_prices, open_times,
                    balance, balance_without_fee=None,
                    trade_amount_percent=1.0, margin_balance=None, margin_balance_no_fee=None,
                    tactical_balance=None, leverage=1.0):
        """Open a long; defaults allocate all available balance at 1x leverage."""

        if balance_without_fee is None:
            balance_without_fee = balance
        if tactical_balance is None:
            tactical_balance = balance

        entry_price = open_prices[i]

        portfolio_balance_before_open = margin_balance if margin_balance is not None else balance
        portfolio_balance_before_open_no_fee = margin_balance_no_fee if margin_balance_no_fee is not None else balance_without_fee

        # ---------- Margin ----------
        if balance >= trade_amount_percent * tactical_balance:
            margin = trade_amount_percent * tactical_balance
        else:
            margin = balance
        margin = max(0.0, min(margin, balance))
        if margin <= 0:
            return None

        # ---------- Margin No Fee ----------
        if balance_without_fee >= trade_amount_percent * tactical_balance:
            margin_no_fee = trade_amount_percent * tactical_balance
        else:
            margin_no_fee = balance_without_fee
        margin_no_fee = max(0.0, min(margin_no_fee, balance_without_fee))
        
        # ---------- Leverage ----------
        # In multi-position mode, free balance drops after each open.
        # Use total active capital (free + locked margin) for leverage safety tiers.
        leverage_ref_balance = margin_balance if margin_balance is not None else balance
        leverage_ref_balance = max(0.0, leverage_ref_balance)
        if leverage == None:
            if leverage_ref_balance <= tactical_balance * self.safe_leverage_balance_pct_low / 100:
                leverage = self.safe_leverage_low
            elif leverage_ref_balance <= tactical_balance * self.safe_leverage_balance_pct_med / 100:
                leverage = self.safe_leverage_med
            elif leverage_ref_balance <= tactical_balance * self.safe_leverage_balance_pct_high / 100:
                leverage =  self.safe_leverage_high
            else:
                leverage = self.leverage    # = 10
        else:
            leverage = leverage

        position_value = margin * leverage
        position_size = position_value / entry_price

        position_value_no_fee = margin_no_fee * leverage
        position_size_no_fee = position_value_no_fee / entry_price

        # update balance after allocating margin
        balance -= margin
        balance_without_fee -= margin_no_fee

        # update open time and current position
        open_time_value = open_times[i]
        current_position = "long"

        if self.verbose:
            print("Open LONG at price:", entry_price, "$", "| Open Time:", open_time_value, "| leverage:", leverage)

        return {
            'entry_price': entry_price,
            'balance': balance,
            'balance_without_fee': balance_without_fee,
            'balance_before_trade': portfolio_balance_before_open,
            'balance_before_trade_no_fee': portfolio_balance_before_open_no_fee,
            'margin': margin,
            'trade_amount_percent': trade_amount_percent,
            'leverage': leverage,
            'position_value': position_value,
            'position_size': position_size,
            'margin_no_fee': margin_no_fee,
            'position_value_no_fee': position_value_no_fee,
            'position_size_no_fee': position_size_no_fee,
            'open_time_value': open_time_value,
            'current_position': current_position
        }


    # close long processes
    def _calculate_close_long(self, i, open_prices, open_times,
                entry_price, position_size, position_size_no_fee,
                fee_rate, margin, margin_no_fee,
                balance, balance_without_fee,
                deducting_fee_total, profits_lst, total_profit_percent,
                count_closed_orders, equity_curve,
                max_drawdown, total_wins, total_wins_long, total_losses,
                total_long, cooldown_after_big_pnl, leverage,
                cooldown_until_index, open_time_value, trade_amount_percent,
                profit_percent_per_month, save_money, trade_power, trade_id, remaining_open_margin,
                remaining_open_margin_no_fee, tactical_balance, reason_to_close,
                balance_before_close_snapshot=None, balance_before_close_no_fee_snapshot=None,
                balance_before_log_override=None, balance_before_log_override_no_fee=None, remaining_open_equity=None):
        """Close 100% of the supplied long position using its original leverage."""

        close_price = open_prices[i]
        if balance_before_close_snapshot is None:
            free_balance_before_close = balance
        else:
            free_balance_before_close = balance_before_close_snapshot
        if balance_before_close_no_fee_snapshot is None:
            free_balance_before_close_no_fee = balance_without_fee
        else:
            free_balance_before_close_no_fee = balance_before_close_no_fee_snapshot

        # PnL
        pnl = position_size * (close_price - entry_price)
        pnl_no_fee = position_size_no_fee * (close_price - entry_price)

        # Fee like Toobit
        entry_fee = entry_price * position_size * fee_rate
        exit_fee = close_price * position_size * fee_rate
        total_fee = entry_fee + exit_fee

        # Update balance
        balance += margin + pnl - total_fee
        balance_without_fee += margin_no_fee + pnl_no_fee

        # per-position net result (stable under multi-position mode)
        profit = pnl - total_fee

        logged_balance_before, logged_balance_after = self._resolve_csv_balances(
            free_balance_before_close,
            margin,
            profit,
            balance_before_override=balance_before_log_override,
            remaining_open_margin=remaining_open_margin
        )

        logged_balance_before_no_fee, logged_balance_after_no_fee = self._resolve_csv_balances(
            free_balance_before_close_no_fee,
            margin_no_fee,
            pnl_no_fee,
            balance_before_override=balance_before_log_override_no_fee,
            remaining_open_margin=remaining_open_margin_no_fee
        )

        profit_percent = self._safe_percent(profit, logged_balance_before)
        total_assets = self._resolve_total_assets(balance, save_money, remaining_open_margin, remaining_open_equity)
        profit_percent_per_month = (((total_assets - save_money) * 100) / tactical_balance) - 100
        pnl_percent = self._safe_percent(pnl, margin)

        deducting_fee_total += total_fee
        profits_lst.append(profit)
        total_profit_percent += profit_percent
        count_closed_orders += 1

        # ---- calculate max drawdown ----
        equity_curve, max_drawdown = self._update_drawdown(equity_curve, max_drawdown, total_assets)

        # ---- count wins and losses ----
        if profit_percent > 0:
            total_wins += 1
            total_wins_long += 1
        else:
            total_losses += 1

        # ---- count LONG trades ----
        total_long += 1

        # ---- COOLDOWN AFTER BIG PROFIT ----
        pnl_percent_without_leverage = (((pnl / margin) * 100 ) / leverage) if (margin != 0 and leverage != 0) else 0
        if pnl_percent_without_leverage >= 4:
            cooldown_until_index = i + cooldown_after_big_pnl
            if self.verbose:
                print(f"🟡 Cooldown Activated (LONG) until candle index {cooldown_until_index}")

        close_time_value = open_times[i]
        days, hours, minutes = trade_duration(open_time_value, close_time_value)


        if self.verbose:
            print("Close LONG at price:", close_price, "$", "| Close Time:", close_time_value, "| leverage:", leverage)
            print("Balance:", round(logged_balance_before, 2), "$", "→", round(logged_balance_after, 2), "$", "| Save Money:", round(save_money, 2), "$")
            print("Balance (no fee):",
                round(logged_balance_before_no_fee, 2), "$", "→", round(logged_balance_after_no_fee, 2), "$")
            print("pnl:", round(pnl, 2), "$ |", round(pnl_percent, 2), "% |" , "Amount:", round(margin), "$")
            print("fee:", round(total_fee, 2), "$")
            print("Profit:", round(profit, 2), "$ |", round(profit_percent, 2), "%")
            print(f"Trade Duration: {days} days, {hours} hours, {minutes} minutes")
            print("-" * 90)


        logged_balance_before, logged_balance_after = self._resolve_csv_balances(
            free_balance_before_close,
            margin,
            profit,
            balance_before_override=balance_before_log_override,
            remaining_open_margin=remaining_open_margin
        )
        has_other_open_positions_at_close = remaining_open_margin > 0
        log_total_assets = total_assets
        log_profit_percent_per_month = (((log_total_assets - save_money) * 100) / tactical_balance) - 100
        if reason_to_close == "rsi_ma_strategy" and log_profit_percent_per_month > 0:
            log_profit_percent_per_month = 0
        self.csv_logger.log_trade(
            trade_id,
            "LONG",
            open_time_value,
            close_time_value,
            entry_price,
            close_price,
            round(tactical_balance, 2),
            round(log_total_assets, 2),
            round(logged_balance_before, 2),
            round(logged_balance_after, 2),
            round(margin , 2),
            leverage,
            trade_amount_percent,
            round(profit, 2),
            round(profit_percent, 2),
            round(pnl_percent, 2),
            round(total_fee, 4),
            days,
            hours,
            minutes,
            round(save_money, 6),
            log_profit_percent_per_month,
            has_other_open_positions_at_close,
            reason_to_close
        )
        profit_percent_per_month = log_profit_percent_per_month

        current_position = None

        return {
            'balance': balance,
            'balance_without_fee': balance_without_fee,
            'deducting_fee_total': deducting_fee_total,
            'close_price': close_price,
            'close_time_value': close_time_value,
            'leverage': leverage,
            'margin': margin,
            'total_fee': total_fee,
            'profit': profit,
            'profit_percent': profit_percent,
            'profits_lst': profits_lst,
            'total_profit_percent': total_profit_percent,
            'pnl': pnl,
            'pnl_percent': pnl_percent,
            'count_closed_orders': count_closed_orders,
            'equity_curve': equity_curve,
            'max_drawdown': max_drawdown,
            'total_wins': total_wins,
            'total_wins_long': total_wins_long,
            'total_losses': total_losses,
            'total_long': total_long,
            'cooldown_until_index': cooldown_until_index,
            'current_position': current_position,
            'trade_power': trade_power,
            'profit_percent_per_month': profit_percent_per_month,
            'save_money' : save_money,
            'logged_balance_before': logged_balance_before,
            'logged_balance_after': logged_balance_after,
            'days': days,
            'hours': hours,
            'minutes': minutes,
        }
    

    # open short processes
    def _calculate_open_short(self, i, open_prices, open_times,
                    balance, balance_without_fee=None,
                    trade_amount_percent=1.0, margin_balance=None, margin_balance_no_fee=None,
                    tactical_balance=None, leverage=1.0):
        """Open a short; defaults allocate all available balance at 1x leverage."""

        if balance_without_fee is None:
            balance_without_fee = balance
        if tactical_balance is None:
            tactical_balance = balance

        entry_price = open_prices[i]

        portfolio_balance_before_open = margin_balance if margin_balance is not None else balance
        portfolio_balance_before_open_no_fee = margin_balance_no_fee if margin_balance_no_fee is not None else balance_without_fee

        # ---------- Margin ----------
        if balance >= trade_amount_percent * tactical_balance:
            margin = trade_amount_percent * tactical_balance
        else:
            margin = balance
        margin = max(0.0, min(margin, balance))
        if margin <= 0:
            return None

        # ---------- Margin No Fee ----------
        if balance_without_fee >= trade_amount_percent * tactical_balance:
            margin_no_fee = trade_amount_percent * tactical_balance
        else:
            margin_no_fee = balance_without_fee
        margin_no_fee = max(0.0, min(margin_no_fee, balance_without_fee))

        # ---------- Leverage ----------
        # In multi-position mode, free balance drops after each open.
        # Use total active capital (free + locked margin) for leverage safety tiers.
        leverage_ref_balance = margin_balance if margin_balance is not None else balance
        leverage_ref_balance = max(0.0, leverage_ref_balance)
        if leverage == None:
            if leverage_ref_balance <= tactical_balance * self.safe_leverage_balance_pct_low / 100:
                leverage = self.safe_leverage_low  # 2 low
            elif leverage_ref_balance <= tactical_balance * self.safe_leverage_balance_pct_med / 100:
                leverage = self.safe_leverage_med  # 3 med
            elif leverage_ref_balance <= tactical_balance * self.safe_leverage_balance_pct_high / 100:
                leverage = self.safe_leverage_high # 4 high
            else:
                leverage = self.leverage    # = 10
        else:
            leverage = leverage

        position_value = margin * leverage
        position_size = position_value / entry_price

        position_value_no_fee = margin_no_fee * leverage
        position_size_no_fee = position_value_no_fee / entry_price

        # update balance after allocating margin
        balance -= margin
        balance_without_fee -= margin_no_fee

        # update open time and current position
        open_time_value = open_times[i]
        current_position = "short"

        if self.verbose:
            print("Open SHORT at price:", entry_price, "$", "| Open Time:", open_time_value, "| leverage:", leverage)

        return {
            'entry_price': entry_price,
            'balance': balance,
            'balance_without_fee': balance_without_fee,
            'balance_before_trade': portfolio_balance_before_open,
            'balance_before_trade_no_fee': portfolio_balance_before_open_no_fee,
            'margin': margin,
            'trade_amount_percent': trade_amount_percent,
            'leverage': leverage,
            'position_value': position_value,
            'position_size': position_size,
            'margin_no_fee': margin_no_fee,
            'position_value_no_fee': position_value_no_fee,
            'position_size_no_fee': position_size_no_fee,
            'open_time_value': open_time_value,
            'current_position': current_position
        }


    # close short processes
    def _calculate_close_short(self, i, open_prices, open_times,
            entry_price, position_size, position_size_no_fee,
            fee_rate, margin, margin_no_fee,
            balance, balance_without_fee,
            deducting_fee_total, profits_lst, total_profit_percent,
            count_closed_orders, equity_curve,
            max_drawdown, total_wins, total_wins_short, total_losses,
            total_short, cooldown_after_big_pnl, leverage,
            cooldown_until_index, open_time_value, trade_amount_percent,
            profit_percent_per_month, save_money, trade_power, trade_id, remaining_open_margin,
            remaining_open_margin_no_fee, tactical_balance, reason_to_close,
            balance_before_close_snapshot=None, balance_before_close_no_fee_snapshot=None,
            balance_before_log_override=None, balance_before_log_override_no_fee=None, remaining_open_equity=None):
        """Close 100% of the supplied short position using its original leverage."""

        close_price = open_prices[i]
        if balance_before_close_snapshot is None:
            free_balance_before_close = balance
        else:
            free_balance_before_close = balance_before_close_snapshot
        if balance_before_close_no_fee_snapshot is None:
            free_balance_before_close_no_fee = balance_without_fee
        else:
            free_balance_before_close_no_fee = balance_before_close_no_fee_snapshot

        # PnL
        pnl = position_size * (entry_price - close_price)
        pnl_no_fee = position_size_no_fee * (entry_price - close_price)

        # Fee like Toobit
        entry_fee = entry_price * position_size * fee_rate
        exit_fee = close_price * position_size * fee_rate
        total_fee = entry_fee + exit_fee

        # Update balance
        balance += margin + pnl - total_fee
        balance_without_fee += margin_no_fee + pnl_no_fee

        # per-position net result (stable under multi-position mode)
        profit = pnl - total_fee

        logged_balance_before, logged_balance_after = self._resolve_csv_balances(
            free_balance_before_close,
            margin,
            profit,
            balance_before_override=balance_before_log_override,
            remaining_open_margin=remaining_open_margin
        )

        logged_balance_before_no_fee, logged_balance_after_no_fee = self._resolve_csv_balances(
            free_balance_before_close_no_fee,
            margin_no_fee,
            pnl_no_fee,
            balance_before_override=balance_before_log_override_no_fee,
            remaining_open_margin=remaining_open_margin_no_fee
        )

        profit_percent = self._safe_percent(profit, logged_balance_before)
        total_assets = self._resolve_total_assets(balance, save_money, remaining_open_margin, remaining_open_equity)
        profit_percent_per_month = (((total_assets - save_money) * 100) / tactical_balance) - 100
        pnl_percent = self._safe_percent(pnl, margin)

        deducting_fee_total += total_fee
        profits_lst.append(profit)
        total_profit_percent += profit_percent
        count_closed_orders += 1

        # ---- calculate max drawdown ----
        equity_curve, max_drawdown = self._update_drawdown(equity_curve, max_drawdown, total_assets)

        # ---- count wins and losses ----
        if profit_percent > 0:
            total_wins += 1
            total_wins_short += 1
        else:
            total_losses += 1

        # ---- count shorts ----
        total_short += 1

        # ---- COOLDOWN AFTER BIG PROFIT ----
        pnl_percent_without_leverage = (((pnl / margin) * 100) / leverage) if (margin != 0 and leverage != 0) else 0
        if pnl_percent_without_leverage >= 4:
            cooldown_until_index = i + cooldown_after_big_pnl
            if self.verbose:
                print(f"🟡 Cooldown Activated (SHORT) until candle index {cooldown_until_index}")

        close_time_value = open_times[i]
        days, hours, minutes = trade_duration(open_time_value, close_time_value)


        if self.verbose:
            print("Close SHORT at price:", close_price, "$", "| Close Time:", close_time_value, "| leverage:", leverage)
            print("Balance:", round(logged_balance_before, 2), "$", "→", round(logged_balance_after, 2), "$", "| Save Money:", round(save_money, 2), "$")
            print("Balance (no fee):",
                round(logged_balance_before_no_fee, 2), "$", "→", round(logged_balance_after_no_fee, 2), "$")
            print("pnl:", round(pnl, 2), "$ |", round(pnl_percent, 2), "% |", "Amount:", round(margin), "$")
            print("fee:", round(total_fee, 2), "$")
            print("Profit:", round(profit, 2), "$ |", round(profit_percent, 2), "%")
            print(f"Trade Duration: {days} days, {hours} hours, {minutes} minutes")
            print("-" * 90)


        logged_balance_before, logged_balance_after = self._resolve_csv_balances(
            free_balance_before_close,
            margin,
            profit,
            balance_before_override=balance_before_log_override,
            remaining_open_margin=remaining_open_margin
        )
        has_other_open_positions_at_close = remaining_open_margin > 0
        log_total_assets = total_assets
        log_profit_percent_per_month = (((log_total_assets - save_money) * 100) / tactical_balance) - 100
        if reason_to_close == "rsi_ma_strategy" and log_profit_percent_per_month > 0:
            log_profit_percent_per_month = 0
        self.csv_logger.log_trade(
            trade_id,
            "SHORT",
            open_time_value,
            close_time_value,
            entry_price,
            close_price,
            round(tactical_balance, 2),
            round(log_total_assets, 2),
            round(logged_balance_before, 2),
            round(logged_balance_after, 2),
            round(margin , 2),
            leverage,
            trade_amount_percent,
            round(profit, 2),
            round(profit_percent, 2),
            round(pnl_percent, 2),
            round(total_fee, 4),
            days,
            hours,
            minutes,
            round(save_money, 6),
            log_profit_percent_per_month,
            has_other_open_positions_at_close,
            reason_to_close
        )
        profit_percent_per_month = log_profit_percent_per_month

        current_position = None

        return {
            'balance': balance,
            'balance_without_fee': balance_without_fee,
            'deducting_fee_total': deducting_fee_total,
            'close_price': close_price,
            'close_time_value': close_time_value,
            'leverage': leverage,
            'margin': margin,
            'total_fee': total_fee,
            'profit': profit,
            'profit_percent': profit_percent,
            'pnl': pnl,
            'pnl_percent': pnl_percent,
            'profits_lst': profits_lst,
            'total_profit_percent': total_profit_percent,
            'pnl_percent': pnl_percent,
            'count_closed_orders': count_closed_orders,
            'equity_curve': equity_curve,
            'max_drawdown': max_drawdown,
            'total_wins': total_wins,
            'total_wins_short': total_wins_short,
            'total_losses': total_losses,
            'total_short': total_short,
            'cooldown_until_index': cooldown_until_index,
            'current_position': current_position,
            'trade_power': trade_power,
            'profit_percent_per_month': profit_percent_per_month,
            'save_money' : save_money,
            'logged_balance_before': logged_balance_before,
            'logged_balance_after': logged_balance_after,
            'days': days,
            'hours': hours,
            'minutes': minutes,
        }


    # check liquidation long
    def _calculate_liquidation_long(
        self, i, low_prices, close_times,
        entry_price, leverage, margin,
        balance, balance_without_fee,
        deducting_fee_total, profits_lst, count_closed_orders,
        total_losses, total_long, equity_curve,
        save_money, max_drawdown, open_time_value,
        trade_amount_percent,
        total_liquids, trade_id, remaining_open_margin,
        remaining_open_margin_no_fee, tactical_balance, reason_to_close,
        balance_before_close_snapshot=None,
        balance_before_close_no_fee_snapshot=None,
        balance_before_log_override=None,
        balance_before_log_override_no_fee=None,
        remaining_open_equity=None
    ):

        liquid_price_long = entry_price * (1 - 1 / leverage)

        # --------------------------
        # NOT LIQUIDATED
        # --------------------------
        if low_prices[i] > liquid_price_long:
            return {
                'liquidated': False,
                'balance': balance,
                'balance_without_fee': balance_without_fee,
                'deducting_fee_total': deducting_fee_total,
                'profits_lst': profits_lst,
                'count_closed_orders': count_closed_orders,
                'total_losses': total_losses,
                'total_long': total_long,
                'equity_curve': equity_curve,
                'max_drawdown': max_drawdown,
                'close_price': None,
                'close_time_value': None
            }

        # --------------------------
        # LIQUIDATION HAPPENS
        # --------------------------
        close_price = liquid_price_long
        close_time_value = close_times[i]

        if balance_before_close_snapshot is None:
            free_balance_before_close = balance
        else:
            free_balance_before_close = balance_before_close_snapshot

        if balance_before_close_no_fee_snapshot is None:
            free_balance_before_close_no_fee = balance_without_fee
        else:
            free_balance_before_close_no_fee = balance_before_close_no_fee_snapshot

        # --------------------------
        # PnL (FULL LOSS)
        # --------------------------
        pnl = -margin
        pnl_no_fee = -margin

        entry_fee = 0
        exit_fee = 0
        total_fee = 0

        # --------------------------
        # BALANCE UPDATE (same logic style as close_long)
        # --------------------------
        balance += margin + pnl - total_fee
        balance_without_fee += margin + pnl_no_fee

        profit = pnl - total_fee
        profits_lst.append(profit)

        # --------------------------
        # CSV BALANCE (same as close_long)
        # --------------------------
        logged_balance_before, logged_balance_after = self._resolve_csv_balances(
            free_balance_before_close,
            margin,
            profit,
            balance_before_override=balance_before_log_override,
            remaining_open_margin=remaining_open_margin
        )

        logged_balance_before_no_fee, logged_balance_after_no_fee = self._resolve_csv_balances(
            free_balance_before_close_no_fee,
            margin,
            pnl_no_fee,
            balance_before_override=balance_before_log_override_no_fee,
            remaining_open_margin=remaining_open_margin_no_fee
        )

        # --------------------------
        # METRICS (aligned with close_long)
        # --------------------------
        profit_percent = self._safe_percent(profit, logged_balance_before)
        pnl_percent = self._safe_percent(pnl, margin)

        deducting_fee_total += total_fee
        count_closed_orders += 1
        total_losses += 1
        total_long += 1
        total_liquids += 1

        total_assets = self._resolve_total_assets(
            balance,
            save_money,
            remaining_open_margin,
            remaining_open_equity
        )

        equity_curve, max_drawdown = self._update_drawdown(
            equity_curve,
            max_drawdown,
            total_assets
        )

        profit_percent_per_month = (
            ((total_assets - save_money) * 100) / tactical_balance
        ) - 100

        # --------------------------
        # LOG TIME
        # --------------------------
        days, hours, minutes = trade_duration(open_time_value, close_time_value)

        if self.verbose:
            print("🔴 LONG LIQUIDATED at price:", round(close_price, 2),
                "| Time:", close_time_value)

        # --------------------------
        # CSV LOG (same structure as close_long)
        # --------------------------
        has_other_open_positions_at_close = remaining_open_margin > 0

        self.csv_logger.log_trade(
            trade_id,
            "LONG_LIQUIDATED",
            open_time_value,
            close_time_value,
            entry_price,
            close_price,
            round(tactical_balance, 2),
            round(total_assets, 2),
            round(logged_balance_before, 2),
            round(logged_balance_after, 2),
            round(margin, 2),
            leverage,
            trade_amount_percent,
            round(profit, 2),
            round(profit_percent, 2),
            round(pnl_percent, 2),
            round(total_fee, 4),
            days,
            hours,
            minutes,
            round(save_money, 6),
            profit_percent_per_month,
            has_other_open_positions_at_close,
            reason_to_close
        )

        # --------------------------
        # RETURN
        # --------------------------
        return {
            'liquidated': True,
            'balance': balance,
            'balance_without_fee': balance_without_fee,
            'deducting_fee_total': deducting_fee_total,
            'profits_lst': profits_lst,
            'count_closed_orders': count_closed_orders,
            'total_losses': total_losses,
            'total_long': total_long,
            'equity_curve': equity_curve,
            'max_drawdown': max_drawdown,
            'close_price': close_price,
            'close_time_value': close_time_value,
            'pnl': pnl,
            'pnl_percent': pnl_percent,
            'total_fee': total_fee,
            'profit': profit,
            'profit_percent': profit_percent,
            'logged_balance_before': logged_balance_before,
            'logged_balance_after': logged_balance_after,
            'save_money': save_money,
            'days': days,
            'hours': hours,
            'minutes': minutes,
            'total_liquids': total_liquids
        }

    @staticmethod
    def calculate_performance_score(
        *, return_percent, max_drawdown, win_rate, profits, first_balance,
        profit_months, loss_months, liquidations=0, closed_trades=None,
    ):
        """Return a robust optimizer score and its supporting quality metrics."""
        if closed_trades is None:
            closed_trades = len(profits)
        gross_profit = sum(value for value in profits if value > 0)
        gross_loss = abs(sum(value for value in profits if value < 0))
        if gross_loss > 0:
            profit_factor = gross_profit / gross_loss
        elif gross_profit > 0:
            profit_factor = 5.0
        else:
            profit_factor = 0.0

        drawdown_pct = abs(max_drawdown)
        expectancy_pct = (
            (sum(profits) / closed_trades) * 100 / first_balance
            if closed_trades and first_balance
            else 0.0
        )
        calmar_ratio = return_percent / max(drawdown_pct, 1.0)
        consistency = profit_months / max(1, profit_months + loss_months)

        if closed_trades == 0:
            score = -1_000_000.0
        else:
            sample_confidence = min(1.0, math.sqrt(closed_trades / 30.0))
            bounded_profit_factor = min(5.0, max(0.05, profit_factor))
            risk_adjusted = max(-10.0, min(10.0, calmar_ratio)) * 10.0
            quality = math.log(bounded_profit_factor) * 10.0
            win_quality = max(-10.0, min(10.0, (win_rate - 50.0) * 0.2))
            consistency_score = (consistency - 0.5) * 20.0
            expectancy_score = max(-10.0, min(10.0, expectancy_pct)) * 2.0
            liquidation_rate = liquidations / closed_trades
            score = sample_confidence * (
                return_percent
                + risk_adjusted
                + quality
                + win_quality
                + consistency_score
                + expectancy_score
            ) - drawdown_pct * 0.75 - liquidation_rate * 50.0

        return {
            "score": score,
            "profit_factor": profit_factor,
            "expectancy_percent": expectancy_pct,
            "calmar_ratio": calmar_ratio,
        }

    def finalize_backtest(self, **state):
        """Calculate final metrics, emit reports/files, render the chart, and return results."""
        optimize = bool(state["optimize"])
        balance = state["balance"]
        balance_without_fee = state["balance_without_fee"]
        save_money = state["save_money"]
        open_positions = state["open_positions"]
        unrealized_profit = 0.0
        if open_positions:
            ending_mark_price = float(state["ending_mark_price"])
            marked_equity = self.open_positions_equity(
                open_positions, ending_mark_price
            )
            open_margin = sum(position["margin"] for position in open_positions)
            unrealized_profit = marked_equity - open_margin
            balance += marked_equity
            balance_without_fee += sum(
                self.position_equity_no_fee(position, ending_mark_price)
                for position in open_positions
            )
        balance += save_money

        first_balance = state["first_balance"]
        profits = state["profits_lst"]
        total_wins = state["total_wins"]
        total_losses = state["total_losses"]
        max_drawdown = state["max_drawdown"]
        t_profit_percent = balance * 100 / first_balance - 100
        days, hours, minutes = trade_duration(state["first_open_time"], state["last_close_time"])
        win_rate = total_wins / (total_wins + total_losses) * 100 if total_wins + total_losses else 0

        scale_long_total = state["scale_ma_long_wins"] + state["scale_ma_long_losses"]
        scale_short_total = state["scale_ma_short_wins"] + state["scale_ma_short_losses"]
        scale_long_winrate = round(state["scale_ma_long_wins"] / scale_long_total * 100, 2) if scale_long_total else 0
        scale_short_winrate = round(state["scale_ma_short_wins"] / scale_short_total * 100, 2) if scale_short_total else 0
        scale_wins = state["scale_ma_long_wins"] + state["scale_ma_short_wins"]
        scale_losses = state["scale_ma_long_losses"] + state["scale_ma_short_losses"]
        scale_total = scale_wins + scale_losses
        scale_winrate = round(scale_wins / scale_total * 100, 2) if scale_total else 0
        scale_profit = state["scale_ma_long_total_profit"] + state["scale_ma_short_total_profit"]

        rsi_total = state["rsi_long_total"] + state["rsi_short_total"]
        rsi_wins = state["rsi_long_wins"] + state["rsi_short_wins"]
        rsi_losses = state["rsi_long_losses"] + state["rsi_short_losses"]
        rsi_profit = state["rsi_long_total_profit"] + state["rsi_short_total_profit"]
        rsi_winrate = round(rsi_wins / rsi_total * 100, 2) if rsi_total else 0

        monthly_stop_reasons = state["monthly_stop_reasons"]
        monthly_profits = state["lst_profit_percent_per_month"]
        if monthly_stop_reasons:
            profit_months_count = sum(reason == "profit" for reason in monthly_stop_reasons)
            loss_months_count = sum(reason == "loss" for reason in monthly_stop_reasons)
        else:
            profit_months_count = sum(value > 0 for value in monthly_profits)
            loss_months_count = sum(value < 0 for value in monthly_profits)

        score_metrics = self.calculate_performance_score(
            return_percent=t_profit_percent,
            max_drawdown=max_drawdown,
            win_rate=win_rate,
            profits=profits,
            first_balance=first_balance,
            profit_months=profit_months_count,
            loss_months=loss_months_count,
            liquidations=state["total_liquids"],
            closed_trades=state["count_closed_orders"],
        )
        score = score_metrics["score"]
        profit_factor = score_metrics["profit_factor"]
        expectancy_pct = score_metrics["expectancy_percent"]
        calmar_ratio = score_metrics["calmar_ratio"]

        if self.verbose:
            self._print_backtest_report(
                state=state,
                balance=balance,
                balance_without_fee=balance_without_fee,
                t_profit_percent=t_profit_percent,
                days=days,
                hours=hours,
                minutes=minutes,
                win_rate=win_rate,
                profit_months_count=profit_months_count,
                loss_months_count=loss_months_count,
                score=score,
                scale_long_total=scale_long_total,
                scale_short_total=scale_short_total,
                scale_long_winrate=scale_long_winrate,
                scale_short_winrate=scale_short_winrate,
                scale_total=scale_total,
                scale_wins=scale_wins,
                scale_losses=scale_losses,
                scale_winrate=scale_winrate,
                scale_profit=scale_profit,
                rsi_total=rsi_total,
                rsi_wins=rsi_wins,
                rsi_losses=rsi_losses,
                rsi_winrate=rsi_winrate,
                rsi_profit=rsi_profit,
            )

        total_profit = balance - first_balance
        realized_profit = sum(profits)
        output_file = os.path.join(self.output_dir, "trades", "data_orders.csv")
        self.csv_logger.save_csv(
            first_balance=first_balance,
            final_balance=balance,
            total_profit=total_profit,
            total_profit_percent=t_profit_percent,
            total_fee=state["deducting_fee_total"],
            start_time=state["first_open_time"],
            end_time=state["last_close_time"],
            days=days,
            hours=hours,
            minutes=minutes,
            file_name=output_file,
        )

        if state.get("show_chart", not optimize):
            chart_payload = dict(state["chart_payload"])
            chart_payload.update(
                balance=balance,
                profits_lst=profits,
                t_profit_percent=t_profit_percent,
                count_closed_orders=state["count_closed_orders"],
                total_wins=total_wins,
                total_losses=total_losses,
                max_drawdown=max_drawdown,
                lst_profit_percent_per_month=monthly_profits,
            )
            chart_result = render_backtest_chart(**chart_payload)
            if chart_result is not None:
                return chart_result

        if self.write_trades:
            try:
                write_monthly_summary(
                    in_file=output_file,
                    out_file=os.path.join(
                        self.output_dir, "monthly", "monthly_data_orders.csv"
                    ),
                    quiet=True,
                )
            except Exception:
                pass

        return {
            "final_balance_static": state["total_money_static"],
            "final_balance_dynamic": state["total_money_dynamic"],
            "final_balance": round(balance, 6),
            "total_profit": round(total_profit, 6),
            "realized_profit": round(realized_profit, 6),
            "unrealized_profit": round(unrealized_profit, 6),
            "open_positions": len(open_positions),
            "total_profit_percent": round(t_profit_percent, 6),
            "closed_trades": state["count_closed_orders"],
            "wins": total_wins,
            "losses": total_losses,
            "maximum_drawdown": round(max_drawdown, 2),
            "win_rate": round(win_rate, 2),
            "profit_more_than_8%": profit_months_count,
            "profit_months": profit_months_count,
            "loss_months": loss_months_count,
            "score": score,
            "profit_factor": round(profit_factor, 4),
            "expectancy_percent": round(expectancy_pct, 6),
            "calmar_ratio": round(calmar_ratio, 4),
            "rsi_total_trades": rsi_total,
            "rsi_wins": rsi_wins,
            "rsi_losses": rsi_losses,
            "rsi_winrate": rsi_winrate,
            "rsi_total_profit": rsi_profit,
            "rsi_long_trades": state["rsi_long_total"],
            "rsi_long_wins": state["rsi_long_wins"],
            "rsi_long_losses": state["rsi_long_losses"],
            "rsi_long_winrate": round(state["rsi_long_wins"] / state["rsi_long_total"] * 100, 2) if state["rsi_long_total"] else 0,
            "rsi_long_profit": state["rsi_long_total_profit"],
            "rsi_short_trades": state["rsi_short_total"],
            "rsi_short_wins": state["rsi_short_wins"],
            "rsi_short_losses": state["rsi_short_losses"],
            "rsi_short_winrate": round(state["rsi_short_wins"] / state["rsi_short_total"] * 100, 2) if state["rsi_short_total"] else 0,
            "rsi_short_profit": state["rsi_short_total_profit"],
            "scale_total_trades": scale_total,
            "scale_wins": scale_wins,
            "scale_losses": scale_losses,
            "scale_winrate": scale_winrate,
            "scale_total_profit": scale_profit,
            "scale_long_trades": scale_long_total,
            "scale_long_wins": state["scale_ma_long_wins"],
            "scale_long_losses": state["scale_ma_long_losses"],
            "scale_long_winrate": scale_long_winrate,
            "scale_long_profit": state["scale_ma_long_total_profit"],
            "scale_short_trades": scale_short_total,
            "scale_short_wins": state["scale_ma_short_wins"],
            "scale_short_losses": state["scale_ma_short_losses"],
            "scale_short_winrate": scale_short_winrate,
            "scale_short_profit": state["scale_ma_short_total_profit"],
        }

    @staticmethod
    def _print_backtest_report(**report):
        state = report["state"]
        print("✅ BACKTEST FINISHED")
        print("Closed Trades:", state["count_closed_orders"], "( Longs:", state["total_long"], "| Shorts:", state["total_short"], ")")
        print("Count open Trades:", len(state["open_positions"]))
        print("Total Wins:", state["total_wins"], "| Total Wins Long:", state["total_wins_long"], "| Total Wins Short:", state["total_wins_short"])
        print("Total Losses:", state["total_losses"])
        print("Final Balance:", round(report["balance"], 2), "$")
        print("Final Balance (No Fee):", round(report["balance_without_fee"], 2), "$")
        print("Final balance if close, open orders:", round(state["total_money_dynamic"], 2), "$")
        print("Total Fees Paid:", round(state["deducting_fee_total"], 2), "$")
        print("Maximum Drawdown:", round(state["max_drawdown"], 2), "%")
        print(f'Total Duration : {report["days"]} days, {report["hours"]} hours, {report["minutes"]} minutes')
        print("Win Rate:", round(report["win_rate"], 2), "%")
        print(
            "Total Profit:",
            round(report["balance"] - state["first_balance"], 2),
            "$",
        )
        print("Total Profit Percent:", round(report["t_profit_percent"], 2), "%")
        print("saved Money:", round(state["save_money"], 2), "$")
        print("Count Liquids:", state["total_liquids"])
        print("count_profit_months:", report["profit_months_count"])
        print("count_loss_months:", report["loss_months_count"])
        print("Total score:", report["score"])

        print("\n================ SCALE ENTRY REPORT ================\n")
        print("===== LONG SCALE ENTRY =====")
        print("Profit Triggered :", state["long_profit_scale_entry_attempts"])
        print("Profit Executed  :", state["long_profit_scale_entries"])
        print("Profit Filtered  :", state["long_filtered_profit_scale_entries"])
        print("Loss Triggered   :", state["long_loss_scale_entry_attempts"])
        print("Loss Executed    :", state["long_loss_scale_entries"])
        print("===== SHORT SCALE ENTRY =====")
        print("Profit Triggered :", state["short_profit_scale_entry_attempts"])
        print("Profit Executed  :", state["short_profit_scale_entries"])
        print("Profit Filtered  :", state["short_filtered_profit_scale_entries"])
        print("Loss Triggered   :", state["short_loss_scale_entry_attempts"])
        print("Loss Executed    :", state["short_loss_scale_entries"])

        print("\n================ SCALE PERFORMANCE =================\n")
        print("LONG:", report["scale_long_total"], "trades |", report["scale_long_winrate"], "% winrate |", round(state["scale_ma_long_total_profit"], 2), "profit")
        print("SHORT:", report["scale_short_total"], "trades |", report["scale_short_winrate"], "% winrate |", round(state["scale_ma_short_total_profit"], 2), "profit")
        print("TOTAL:", report["scale_total"], "trades |", report["scale_winrate"], "% winrate |", round(report["scale_profit"], 2), "profit")

        print("\n================ RSI STRATEGY REPORT ================\n")
        print("LONG:", state["rsi_long_total"], "trades |", state["rsi_long_wins"], "wins |", round(state["rsi_long_total_profit"], 2), "profit")
        print("SHORT:", state["rsi_short_total"], "trades |", state["rsi_short_wins"], "wins |", round(state["rsi_short_total_profit"], 2), "profit")
        print("TOTAL:", report["rsi_total"], "trades |", report["rsi_winrate"], "% winrate |", round(report["rsi_profit"], 2), "profit")


    # check liquidation short
    def _calculate_liquidation_short(
        self, i, high_prices, close_times,
        entry_price, leverage, margin,
        balance, balance_without_fee,
        deducting_fee_total, profits_lst, count_closed_orders,
        total_losses, total_short, equity_curve,
        save_money, max_drawdown, open_time_value,
        trade_amount_percent,
        total_liquids, trade_id, remaining_open_margin,
        remaining_open_margin_no_fee, tactical_balance, reason_to_close,
        balance_before_close_snapshot=None,
        balance_before_close_no_fee_snapshot=None,
        balance_before_log_override=None,
        balance_before_log_override_no_fee=None,
        remaining_open_equity=None
    ):

        liquid_price_short = entry_price * (1 + 1 / leverage)

        # --------------------------
        # NOT LIQUIDATED
        # --------------------------
        if high_prices[i] < liquid_price_short:
            return {
                'liquidated': False,
                'balance': balance,
                'balance_without_fee': balance_without_fee,
                'deducting_fee_total': deducting_fee_total,
                'profits_lst': profits_lst,
                'count_closed_orders': count_closed_orders,
                'total_losses': total_losses,
                'total_short': total_short,
                'equity_curve': equity_curve,
                'max_drawdown': max_drawdown,
                'close_price': None,
                'close_time_value': None
            }

        # --------------------------
        # LIQUIDATION HAPPENS
        # --------------------------
        close_price = liquid_price_short
        close_time_value = close_times[i]

        if balance_before_close_snapshot is None:
            free_balance_before_close = balance
        else:
            free_balance_before_close = balance_before_close_snapshot

        if balance_before_close_no_fee_snapshot is None:
            free_balance_before_close_no_fee = balance_without_fee
        else:
            free_balance_before_close_no_fee = balance_before_close_no_fee_snapshot

        # --------------------------
        # PnL (FULL LOSS)
        # --------------------------
        pnl = -margin
        pnl_no_fee = -margin

        total_fee = 0

        # --------------------------
        # BALANCE UPDATE (same logic as close_long style)
        # --------------------------
        balance += margin + pnl - total_fee
        balance_without_fee += margin + pnl_no_fee

        profit = pnl - total_fee
        profits_lst.append(profit)

        # --------------------------
        # CSV BALANCE (aligned with close_long)
        # --------------------------
        logged_balance_before, logged_balance_after = self._resolve_csv_balances(
            free_balance_before_close,
            margin,
            profit,
            balance_before_override=balance_before_log_override,
            remaining_open_margin=remaining_open_margin
        )

        logged_balance_before_no_fee, logged_balance_after_no_fee = self._resolve_csv_balances(
            free_balance_before_close_no_fee,
            margin,
            pnl_no_fee,
            balance_before_override=balance_before_log_override_no_fee,
            remaining_open_margin=remaining_open_margin_no_fee
        )

        # --------------------------
        # METRICS (aligned with close_long)
        # --------------------------
        profit_percent = self._safe_percent(profit, logged_balance_before)
        pnl_percent = self._safe_percent(pnl, margin)

        deducting_fee_total += total_fee
        count_closed_orders += 1
        total_losses += 1
        total_short += 1
        total_liquids += 1

        total_assets = self._resolve_total_assets(
            balance,
            save_money,
            remaining_open_margin,
            remaining_open_equity
        )

        equity_curve, max_drawdown = self._update_drawdown(
            equity_curve,
            max_drawdown,
            total_assets
        )

        profit_percent_per_month = (
            ((total_assets - save_money) * 100) / tactical_balance
        ) - 100

        # --------------------------
        # TIME
        # --------------------------
        days, hours, minutes = trade_duration(open_time_value, close_time_value)

        if self.verbose:
            print("🔴 SHORT LIQUIDATED at price:", round(close_price, 2),
                "| Time:", close_time_value)

        # --------------------------
        # CSV LOG
        # --------------------------
        has_other_open_positions_at_close = remaining_open_margin > 0

        self.csv_logger.log_trade(
            trade_id,
            "SHORT_LIQUIDATED",
            open_time_value,
            close_time_value,
            entry_price,
            close_price,
            round(tactical_balance, 2),
            round(total_assets, 2),
            round(logged_balance_before, 2),
            round(logged_balance_after, 2),
            round(margin, 2),
            leverage,
            trade_amount_percent,
            round(profit, 2),
            round(profit_percent, 2),
            round(pnl_percent, 2),
            round(total_fee, 4),
            days,
            hours,
            minutes,
            round(save_money, 6),
            profit_percent_per_month,
            has_other_open_positions_at_close,
            reason_to_close
        )

        # --------------------------
        # RETURN
        # --------------------------
        return {
            'liquidated': True,
            'balance': balance,
            'balance_without_fee': balance_without_fee,
            'deducting_fee_total': deducting_fee_total,
            'profits_lst': profits_lst,
            'count_closed_orders': count_closed_orders,
            'total_losses': total_losses,
            'total_short': total_short,
            'equity_curve': equity_curve,
            'max_drawdown': max_drawdown,
            'close_price': close_price,
            'close_time_value': close_time_value,
            'pnl': pnl,
            'pnl_percent': pnl_percent,
            'total_fee': total_fee,
            'profit': profit,
            'profit_percent': profit_percent,
            'logged_balance_before': logged_balance_before,
            'logged_balance_after': logged_balance_after,
            'save_money': save_money,
            'days': days,
            'hours': hours,
            'minutes': minutes,
            'total_liquids': total_liquids
        }

