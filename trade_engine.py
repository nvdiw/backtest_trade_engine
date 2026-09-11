"""Execution, accounting, logging, reporting, and chart lifecycle for backtests."""

import os
import math
import json
from pathlib import Path
from datetime import datetime, timezone
from functools import lru_cache
from dataclasses import dataclass, field, fields
from typing import Optional

import numpy as np
import pandas as pd

from chart_renderer import render_backtest_chart
from fetch_calculate_data import fetch_all_data
from get_candle_index import get_candle_index, get_month_start_indices
from market_data import MarketDataSource
from trade_csv_logger import TradeCSVLogger
from monthly_reporting import monthly_report
from run_reporting import publish_backtest, write_metadata, market_metadata


class _DataclassMapping:
    """Small compatibility layer for existing reason/strategy helpers."""

    __slots__ = ()

    def __getitem__(self, key):
        return getattr(self, key)

    def __setitem__(self, key, value):
        setattr(self, key, value)

    def get(self, key, default=None):
        return getattr(self, key, default)

    def __contains__(self, key):
        return any(item.name == key for item in fields(self))

    def to_dict(self):
        """Return a real mapping for serialization and dictionary merging."""
        return {item.name: getattr(self, item.name) for item in fields(self)}


@dataclass(slots=True)
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


@dataclass(slots=True)
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
    long_profits: list = field(default_factory=list)
    short_profits: list = field(default_factory=list)
    long_fees: list = field(default_factory=list)
    short_fees: list = field(default_factory=list)
    long_durations_minutes: list = field(default_factory=list)
    short_durations_minutes: list = field(default_factory=list)
    long_liquidations: int = 0
    short_liquidations: int = 0

    def __post_init__(self):
        if self.balance_without_fee is None:
            self.balance_without_fee = self.balance
        if self.tactical_balance is None:
            self.tactical_balance = self.balance

    def apply_result(self, result):
        """Apply calculator output fields in one controlled place."""
        if result is None:
            return
        for name in _ACCOUNT_STATE_FIELD_NAMES:
            if name in result:
                setattr(self, name, result[name])

    def record_closed_trade(self, side, result, *, liquidated=False):
        """Keep allocation-light directional history for reports and optimization."""
        if not result or side not in {"long", "short"}:
            return
        getattr(self, f"{side}_profits").append(float(result.get("profit", 0.0) or 0.0))
        getattr(self, f"{side}_fees").append(float(result.get("total_fee", 0.0) or 0.0))
        duration = (
            int(result.get("days", 0) or 0) * 24 * 60
            + int(result.get("hours", 0) or 0) * 60
            + int(result.get("minutes", 0) or 0)
        )
        getattr(self, f"{side}_durations_minutes").append(duration)
        if liquidated:
            setattr(self, f"{side}_liquidations", getattr(self, f"{side}_liquidations") + 1)


_ACCOUNT_STATE_FIELD_NAMES = tuple(item.name for item in fields(AccountState))


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
        write_excel=True,
        output_dir="outputs",
        track_equity_curve=None,
        csv_logger=None,
        fee_rate=0.0,
        slippage_rate=0.0,
        funding_rate_per_8h=0.0,
        maintenance_margin_rate=0.0,
        liquidation_fee_rate=0.0,
        market=None,
        strategy_name=None,
        symbol=None,
    ):
        self.market_metadata = market_metadata(market, strategy_name or 'Strategy', os.environ.get('BTE_SYMBOL') or symbol) if market else {}
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
        self.fee_rate = max(0.0, float(fee_rate))
        self.slippage_rate = max(0.0, float(slippage_rate))
        self.funding_rate_per_8h = abs(float(funding_rate_per_8h))
        self.maintenance_margin_rate = max(0.0, float(maintenance_margin_rate))
        self.liquidation_fee_rate = max(0.0, float(liquidation_fee_rate))
        self.verbose = bool(verbose)
        if self.verbose:
            import sys
            # Library callers may still use a legacy Windows code page.
            # Keep logging from aborting a completed trade on an arrow/emoji.
            if hasattr(sys.stdout, 'reconfigure'):
                sys.stdout.reconfigure(errors='replace')
        if track_equity_curve is None:
            track_equity_curve = not bool(optimize)
        self.track_equity_curve = bool(track_equity_curve)
        self.equity_peak = None
        # self.just_one_time = True

    @staticmethod
    @lru_cache(maxsize=4)
    def load_market_data(
        start="2025-01-01",
        end="2026-02-23",
        warmup_candles=0,
        data_file=None,
        timeframe=None,
    ):
        """Resolve and cache an inclusive start/exclusive end candle range.

        Optimization workers call the strategy many times for the same range.  The
        cached immutable market arrays avoid re-reading and parsing the CSV for
        every candidate in a worker process.
        """
        from runtime_settings import market_selection, apply_process_policy
        apply_process_policy()
        data_file, timeframe = market_selection(data_file, timeframe)
        # The no-argument branch preserves the legacy MA loader and its public
        # test/mocking surface.  Plug-in strategies pass their own DATA_FILE and
        # TIMEFRAME and are completely independent of the 15-minute dataset.
        if data_file is not None:
            return MarketDataSource(data_file, timeframe).load(
                start=start,
                end=end,
                warmup_candles=warmup_candles,
            )

        start_index = get_candle_index(start) if isinstance(start, str) else int(start)
        end_index = get_candle_index(end) if isinstance(end, str) else int(end)
        if end_index <= start_index:
            raise ValueError("end must resolve to a candle after start")

        warmup_candles = max(0, int(warmup_candles))
        data_start_index = max(0, start_index - warmup_candles)
        all_data = fetch_all_data(data_start_index, end_index)
        if not all_data or len(all_data["Close"]) == 0:
            raise ValueError("the selected start/end range contains no candles")

        warmup_offset = start_index - data_start_index

        close_times = (
            pd.to_datetime(all_data["Close time"], utc=True)
            + pd.Timedelta(milliseconds=1)
        ).strftime("%Y-%m-%d %H:%M:%S.%f").tolist()
        try:
            open_time_ns = np.asarray(
                pd.to_datetime(
                    all_data["Open time"], utc=True, format="mixed", errors="raise"
                ).asi8,
                dtype=np.int64,
            )
        except (TypeError, ValueError):
            # Some unit/integration clients deliberately use opaque timestamp
            # labels.  They retain the legacy payload; audited market CSVs get
            # the fast elapsed-time clock used for accurate funding across gaps.
            open_time_ns = None

        history = {
            "open_prices": np.asarray(all_data["Open"], dtype=float),
            "close_prices": np.asarray(all_data["Close"], dtype=float),
            "open_times": all_data["Open time"],
            "close_times": close_times,
            "low_prices": np.asarray(all_data["Low"], dtype=float),
            "high_prices": np.asarray(all_data["High"], dtype=float),
            "volume_prices": np.asarray(all_data["Volume"], dtype=float),
        }
        if open_time_ns is not None:
            history["open_time_ns"] = open_time_ns

        return {
            "start": start_index,
            "end": end_index,
            "data_start": data_start_index,
            "warmup_offset": warmup_offset,
            "month_starts": get_month_start_indices(start_index, end_index, just_index=True),
            **{name: values[warmup_offset:] for name, values in history.items()},
            **{f"history_{name}": values for name, values in history.items()},
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

    def render_strategy_chart(self, *, market, chart_state, account, result,
                              price_overlays, oscillator_values, oscillator_label,
                              title, show=False, save_path=None, max_candles=500):
        """Strategy-neutral bridge to the same interactive renderer used by MA."""
        empty = np.full(len(market['close_prices']), np.nan)
        payload = dict(
            **chart_state,
            **{key: market[key] for key in ('close_prices', 'close_times', 'open_times',
                                           'open_prices', 'high_prices', 'low_prices')},
            ema_16=empty, ma_50=empty, ma_100=empty, ma_200=empty,
            rsi_values=oscillator_values, oscillator_label=oscillator_label,
            price_overlays=price_overlays, chart_title=title,
            plot_end_offset=0, plot_max_candles=max_candles, plot_step_candles=100,
            plot_min_zoom_candles=20, plot_max_render_candles=900,
            plot_zoom_in_factor=.8, plot_zoom_out_factor=1.25,
            plot_window_width_scale=.9, plot_window_height_scale=.9,
            plot_drag_preview_factor=.3, plot_drag_update_interval_ms=75,
            plot_yscale_drag_sensitivity=.003, balance=result['final_balance'],
            profits_lst=account.profits_lst, t_profit_percent=result['total_profit_percent'],
            count_closed_orders=account.count_closed_orders, total_wins=account.total_wins,
            total_losses=account.total_losses, max_drawdown=account.max_drawdown,
            lst_profit_percent_per_month=[value * 100 for value in result['monthly_returns']],
            chart_show=show, chart_save_path=save_path)
        return self.render_result_chart(result, None, payload)

    def render_result_chart(self, result, parameters, payload):
        """One chart publication path for the generic and legacy state adapters."""
        payload = dict(payload)
        payload['chart_title'] = ' | '.join(str(value) for value in (
            result.get('strategy_name', 'Strategy'), result.get('symbol'), result.get('timeframe')) if value)
        chart_path = payload.get('chart_save_path') or str(Path(self.output_dir) / 'chart.png')
        payload['chart_save_path'] = chart_path
        chart_result = render_backtest_chart(**payload)
        result['chart_file'] = str(chart_path)
        if self.write_trades:
            if parameters is None:
                path = Path(self.output_dir) / 'params.json'
                parameters = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
            self.save_run_metadata(result, parameters)
        return chart_result

    def save_run_metadata(self, result, parameters):
        """Shared JSON sidecars; callers hold their strategy workspace lock."""
        write_metadata(self.output_dir, result, parameters)

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

    def _funding_cost(self, notional, open_time, close_time):
        """Conservative funding charge for every crossed eight-hour interval."""
        if self.funding_rate_per_8h <= 0 or notional <= 0:
            return 0.0
        try:
            opened = pd.to_datetime(open_time, utc=True)
            closed = pd.to_datetime(close_time, utc=True)
            elapsed_hours = max(0.0, (closed - opened).total_seconds() / 3600.0)
        except (TypeError, ValueError):
            return 0.0
        intervals = int(math.ceil(elapsed_hours / 8.0 - 1e-12))
        return float(notional) * self.funding_rate_per_8h * max(0, intervals)

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

    def update_account_drawdown(self, account: AccountState, total_assets: float):
        """Record mark-to-market drawdown through the public strategy API."""
        account.equity_curve, account.max_drawdown = self._update_drawdown(
            account.equity_curve,
            account.max_drawdown,
            float(total_assets),
        )
        return account.max_drawdown

    @staticmethod
    def position_equity(position, price):
        if position.side == "long":
            pnl_percent = (price - position.entry_price) / position.entry_price * 100
        else:
            pnl_percent = (position.entry_price - price) / position.entry_price * 100
        return position.margin + position.margin * pnl_percent * position.leverage / 100

    @staticmethod
    def position_equity_no_fee(position, price):
        """Mark one position using its fee-free size and margin fields."""
        direction = 1.0 if position.side == "long" else -1.0
        pnl = (
            position.position_size_no_fee
            * (price - position.entry_price)
            * direction
        )
        return position.margin_no_fee + pnl

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
            position.entry_price, position.position_size, position.position_size_no_fee,
            fee_rate, position.margin, position.margin_no_fee,
            account.balance, account.balance_without_fee,
            account.deducting_fee_total, account.profits_lst, account.total_profit_percent,
            account.count_closed_orders, account.equity_curve,
            account.max_drawdown, account.total_wins, account.total_wins_long, account.total_losses,
            account.total_long, cooldown_after_big_pnl, position.leverage,
            account.cooldown_until_index, position.open_time_value, position.trade_amount_percent,
            account.profit_percent_per_month, account.save_money, account.trade_power,
            position.trade_id, remaining_open_margin, remaining_open_margin_no_fee,
            account.tactical_balance, position.reason if reason_to_close is None else reason_to_close,
            balance_before_close_snapshot, balance_before_close_no_fee_snapshot,
            balance_before_log_override, balance_before_log_override_no_fee,
            remaining_open_equity,
        )
        account.apply_result(result)
        account.record_closed_trade("long", result)
        return result

    def open_risk_position(self, i, prices, times, account, *, side, stop_distance,
                           risk_per_trade, max_gross_exposure, trade_id,
                           quantity_step=0.0, min_quantity=0.0, min_notional=0.0,
                           leverage=1.0, trade_amount_percent=1.0):
        """Risk-budgeted entry with exposure and margin caps in the shared ledger.

        Risk excludes costs/gaps. Reserve both estimated fees in the cash cap;
        the existing ledger charges entry and exit fees together when closed.
        """
        if side not in ('long', 'short'):
            raise ValueError('side must be long or short')
        if not math.isfinite(leverage) or leverage < 1 or not 0 < trade_amount_percent <= 1:
            raise ValueError('Invalid leverage or margin allocation')
        if not math.isfinite(stop_distance) or stop_distance <= 0:
            return None, None, 'invalid_stop_distance'
        fill = float(prices[i]) * (1 + self.slippage_rate if side == 'long' else 1 - self.slippage_rate)
        equity = account.balance + account.save_money
        if min(fill, equity, account.balance, account.tactical_balance) <= 0:
            return None, None, 'insufficient_balance'
        from risk_sizing import position_size_limits
        limits = position_size_limits(
            equity=equity, balance=account.balance, fill=fill, stop_distance=stop_distance,
            risk_per_trade=risk_per_trade, max_gross_exposure=max_gross_exposure,
            trade_amount_percent=trade_amount_percent, leverage=leverage, fee_rate=self.fee_rate)
        qty = min(limits.values())
        self.last_risk_sizing = dict(limits=limits, binding_limit=min(limits, key=limits.get),
                                     account_equity=equity)
        if quantity_step:
            from decimal import Decimal, ROUND_FLOOR
            step = Decimal(str(quantity_step))
            qty = float((Decimal(str(qty)) / step).to_integral_value(rounding=ROUND_FLOOR) * step)
        if qty <= 0 or qty < min_quantity or qty * fill < min_notional:
            return None, None, 'below_minimum_order'
        stop = fill - stop_distance if side == 'long' else fill + stop_distance
        if stop <= 0:
            return None, None, 'invalid_stop_price'
        # Stop must precede liquidation intrabar; gaps are handled at the next open.
        if leverage > 1 and stop_distance / fill >= 1 / leverage - self.maintenance_margin_rate:
            return None, None, 'stop_beyond_liquidation'
        fraction = qty * fill / (leverage * account.tactical_balance)
        method = self.open_long if side == 'long' else self.open_short
        opened = method(i, prices, times, account, trade_amount_percent=fraction, leverage=leverage)
        position = Position.from_open_result(opened, trade_id=trade_id, side=side,
                                            entry_index=i, high_price=opened['entry_price'],
                                            low_price=opened['entry_price'], reason='range_breakout')
        return position, stop, None

    @staticmethod
    def protective_stop_reference(side, stop, open_price, high, low, *, data_gap=False):
        """Conservative market-stop reference, before adverse execution costs."""
        if side not in ('long', 'short'):
            raise ValueError('side must be long or short')
        if data_gap or (low <= stop if side == 'long' else high >= stop):
            return min(stop, open_price) if side == 'long' else max(stop, open_price)
        return None

    def close_at_reference(self, position, account, price, time, *, reason):
        """Close through the normal fee/slippage ledger at an explicit reference."""
        method = self.close_long if position.side == 'long' else self.close_short
        return method(0, [price], [time], position, account, fee_rate=self.fee_rate,
                      cooldown_after_big_pnl=0, reason_to_close=reason)

    def accrued_position_costs(self, position, time):
        """Entry fee and accrued funding; no unexecuted exit is fabricated."""
        notional = position.entry_price * position.position_size
        return notional * self.fee_rate + self._funding_cost(notional, position.open_time_value, time)

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
            position.entry_price, position.position_size, position.position_size_no_fee,
            fee_rate, position.margin, position.margin_no_fee,
            account.balance, account.balance_without_fee,
            account.deducting_fee_total, account.profits_lst, account.total_profit_percent,
            account.count_closed_orders, account.equity_curve,
            account.max_drawdown, account.total_wins, account.total_wins_short, account.total_losses,
            account.total_short, cooldown_after_big_pnl, position.leverage,
            account.cooldown_until_index, position.open_time_value, position.trade_amount_percent,
            account.profit_percent_per_month, account.save_money, account.trade_power,
            position.trade_id, remaining_open_margin, remaining_open_margin_no_fee,
            account.tactical_balance, position.reason if reason_to_close is None else reason_to_close,
            balance_before_close_snapshot, balance_before_close_no_fee_snapshot,
            balance_before_log_override, balance_before_log_override_no_fee,
            remaining_open_equity,
        )
        account.apply_result(result)
        account.record_closed_trade("short", result)
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
            position.entry_price, position.leverage, position.margin,
            account.balance, account.balance_without_fee,
            account.deducting_fee_total, account.profits_lst, account.count_closed_orders,
            account.total_losses, account.total_long, account.equity_curve,
            account.save_money, account.max_drawdown, position.open_time_value,
            position.trade_amount_percent, account.total_liquids, position.trade_id,
            remaining_open_margin, remaining_open_margin_no_fee,
            account.tactical_balance, position.reason if reason_to_close is None else reason_to_close,
            balance_before_close_snapshot, balance_before_close_no_fee_snapshot,
            balance_before_log_override, balance_before_log_override_no_fee,
            remaining_open_equity,
        )
        account.apply_result(result)
        if result and result.get("liquidated"):
            account.record_closed_trade("long", result, liquidated=True)
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
            position.entry_price, position.leverage, position.margin,
            account.balance, account.balance_without_fee,
            account.deducting_fee_total, account.profits_lst, account.count_closed_orders,
            account.total_losses, account.total_short, account.equity_curve,
            account.save_money, account.max_drawdown, position.open_time_value,
            position.trade_amount_percent, account.total_liquids, position.trade_id,
            remaining_open_margin, remaining_open_margin_no_fee,
            account.tactical_balance, position.reason if reason_to_close is None else reason_to_close,
            balance_before_close_snapshot, balance_before_close_no_fee_snapshot,
            balance_before_log_override, balance_before_log_override_no_fee,
            remaining_open_equity,
        )
        account.apply_result(result)
        if result and result.get("liquidated"):
            account.record_closed_trade("short", result, liquidated=True)
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

        entry_price = open_prices[i] * (1.0 + self.slippage_rate)

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

        close_price = open_prices[i] * (1.0 - self.slippage_rate)
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
        funding_fee = self._funding_cost(
            entry_price * position_size, open_time_value, open_times[i]
        )
        total_fee = entry_fee + exit_fee + funding_fee

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
            if self.verbose and cooldown_after_big_pnl > 0:
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
            profit,
            profit_percent,
            pnl_percent,
            total_fee,
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

        entry_price = open_prices[i] * (1.0 - self.slippage_rate)

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

        close_price = open_prices[i] * (1.0 + self.slippage_rate)
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
        funding_fee = self._funding_cost(
            entry_price * position_size, open_time_value, open_times[i]
        )
        total_fee = entry_fee + exit_fee + funding_fee

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
            if self.verbose and cooldown_after_big_pnl > 0:
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
            profit,
            profit_percent,
            pnl_percent,
            total_fee,
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

        liquid_price_long = entry_price * (
            1 - 1 / leverage + self.maintenance_margin_rate
        )

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

        notional = margin * leverage
        entry_fee = notional * self.fee_rate
        exit_fee = notional * self.liquidation_fee_rate
        funding_fee = self._funding_cost(
            notional, open_time_value, close_time_value
        )
        total_fee = entry_fee + exit_fee + funding_fee

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
            profit,
            profit_percent,
            pnl_percent,
            total_fee,
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
    def calculate_side_metrics(
        profits,
        *,
        first_balance,
        fees=(),
        durations_minutes=(),
        liquidations=0,
        open_positions=0,
    ):
        """Summarize one direction using net closed-trade results.

        Directional drawdown is the drawdown of an isolated curve that starts at
        ``first_balance`` and applies only that side's net profits in close order.
        This makes Long and Short risk directly comparable without pretending
        that either side had a separate live account during the backtest.
        """
        values = [float(value or 0.0) for value in (profits or ())]
        fee_values = [float(value or 0.0) for value in (fees or ())]
        duration_values = [float(value or 0.0) for value in (durations_minutes or ())]
        wins = [value for value in values if value > 0]
        losses = [value for value in values if value <= 0]
        negative_losses = [value for value in values if value < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(negative_losses))
        if gross_loss > 0:
            profit_factor = gross_profit / gross_loss
        elif gross_profit > 0:
            profit_factor = 5.0
        else:
            profit_factor = 0.0

        equity = peak = float(first_balance or 0.0)
        maximum_drawdown = 0.0
        consecutive_losses = max_consecutive_losses = 0
        for value in values:
            equity += value
            peak = max(peak, equity)
            if peak > 0:
                maximum_drawdown = min(
                    maximum_drawdown, (equity / peak - 1.0) * 100.0
                )
            if value <= 0:
                consecutive_losses += 1
                max_consecutive_losses = max(max_consecutive_losses, consecutive_losses)
            else:
                consecutive_losses = 0

        trades = len(values)
        avg_win = gross_profit / len(wins) if wins else 0.0
        avg_loss = sum(negative_losses) / len(negative_losses) if negative_losses else 0.0
        payoff_ratio = avg_win / abs(avg_loss) if avg_loss else (5.0 if avg_win else 0.0)
        net_profit = sum(values)
        return {
            "trades": trades,
            "wins": len(wins),
            "losses": len(losses),
            "breakeven_trades": sum(value == 0 for value in values),
            "win_rate": len(wins) * 100.0 / trades if trades else 0.0,
            "net_profit": net_profit,
            "return_contribution_percent": (
                net_profit * 100.0 / float(first_balance) if first_balance else 0.0
            ),
            "gross_profit": gross_profit,
            "gross_loss": gross_loss,
            "profit_factor": profit_factor,
            "expectancy": net_profit / trades if trades else 0.0,
            "expectancy_percent": (
                (net_profit / trades) * 100.0 / float(first_balance)
                if trades and first_balance else 0.0
            ),
            "average_win": avg_win,
            "average_loss": avg_loss,
            "payoff_ratio": payoff_ratio,
            "best_trade": max(values) if values else 0.0,
            "worst_trade": min(values) if values else 0.0,
            "total_fees": sum(fee_values),
            "average_duration_minutes": (
                sum(duration_values) / len(duration_values) if duration_values else 0.0
            ),
            "maximum_drawdown": maximum_drawdown,
            "max_consecutive_losses": max_consecutive_losses,
            "liquidations": int(liquidations or 0),
            "open_positions": int(open_positions or 0),
        }

    @classmethod
    def calculate_directional_metrics(
        cls,
        *,
        first_balance,
        long_profits=(),
        short_profits=(),
        long_fees=(),
        short_fees=(),
        long_durations_minutes=(),
        short_durations_minutes=(),
        long_liquidations=0,
        short_liquidations=0,
        long_open_positions=0,
        short_open_positions=0,
    ):
        """Return a flat optimizer/JSON-friendly Long-versus-Short comparison."""
        sides = {}
        for side, profits, fees, durations, liquidations, open_positions in (
            (
                "long", long_profits, long_fees, long_durations_minutes,
                long_liquidations, long_open_positions,
            ),
            (
                "short", short_profits, short_fees, short_durations_minutes,
                short_liquidations, short_open_positions,
            ),
        ):
            sides[side] = cls.calculate_side_metrics(
                profits,
                first_balance=first_balance,
                fees=fees,
                durations_minutes=durations,
                liquidations=liquidations,
                open_positions=open_positions,
            )
        flat = {
            f"{side}_{metric}": value
            for side, metrics in sides.items()
            for metric, value in metrics.items()
        }
        long_profit = sides["long"]["net_profit"]
        short_profit = sides["short"]["net_profit"]
        contribution_base = abs(long_profit) + abs(short_profit)
        flat.update({
            "long_profit": long_profit,
            "short_profit": short_profit,
            "stronger_side": (
                "LONG" if long_profit > short_profit
                else "SHORT" if short_profit > long_profit
                else "BALANCED"
            ),
            "directional_profit_gap": abs(long_profit - short_profit),
            "long_profit_contribution_share_percent": (
                long_profit * 100.0 / contribution_base if contribution_base else 0.0
            ),
            "short_profit_contribution_share_percent": (
                short_profit * 100.0 / contribution_base if contribution_base else 0.0
            ),
        })
        return flat

    @staticmethod
    def _overview_side_metrics(label, metrics):
        """Map flat directional metrics to human-readable workbook labels."""
        prefix = label.lower()
        return {
            f"{label} net profit": metrics.get(f"{prefix}_net_profit", 0.0),
            f"{label} return contribution %": metrics.get(
                f"{prefix}_return_contribution_percent", 0.0
            ),
            f"{label} trades": metrics.get(f"{prefix}_trades", 0),
            f"{label} wins": metrics.get(f"{prefix}_wins", 0),
            f"{label} losses": metrics.get(f"{prefix}_losses", 0),
            f"{label} win rate %": metrics.get(f"{prefix}_win_rate", 0.0),
            f"{label} profit factor": metrics.get(f"{prefix}_profit_factor", 0.0),
            f"{label} expectancy": metrics.get(f"{prefix}_expectancy", 0.0),
            f"{label} payoff ratio": metrics.get(f"{prefix}_payoff_ratio", 0.0),
            f"{label} average win": metrics.get(f"{prefix}_average_win", 0.0),
            f"{label} average loss": metrics.get(f"{prefix}_average_loss", 0.0),
            f"{label} best trade": metrics.get(f"{prefix}_best_trade", 0.0),
            f"{label} worst trade": metrics.get(f"{prefix}_worst_trade", 0.0),
            f"{label} maximum drawdown %": metrics.get(
                f"{prefix}_maximum_drawdown", 0.0
            ),
            f"{label} fees": metrics.get(f"{prefix}_total_fees", 0.0),
            f"{label} liquidations": metrics.get(f"{prefix}_liquidations", 0),
            f"{label} average duration minutes": metrics.get(
                f"{prefix}_average_duration_minutes", 0.0
            ),
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

    def finalize_account(
        self,
        account: AccountState,
        *,
        first_balance: float,
        open_positions=(),
        ending_mark_price: float,
        start_time,
        end_time,
        monthly_returns=(),
        output_file=None,
        extra_metrics=None,
        accrue_open_costs=False,
        report_metadata=None,
        monthly_profit_target_percent=8.0,
        monthly_return_months=None,
    ):
        """Build the generic optimizer/report result for any strategy.

        Unlike :meth:`finalize_backtest`, this API has no MA, RSI, scale-in, or
        chart-specific state.  New strategy plug-ins can use the execution and
        accounting methods on ``TradeEngine`` and finish with this compact
        contract.  Strategy-specific numeric metrics may be supplied through
        ``extra_metrics``.
        """
        positions = list(open_positions or ())
        ending_mark_price = float(ending_mark_price)
        open_margin = sum(position.margin for position in positions)
        marked_equity = self.open_positions_equity(positions, ending_mark_price)
        accrued_costs = sum(self.accrued_position_costs(p, end_time) for p in positions) if accrue_open_costs else 0.0
        marked_equity -= accrued_costs
        marked_equity_no_fee = sum(
            self.position_equity_no_fee(position, ending_mark_price)
            for position in positions
        )
        static_balance = account.balance + open_margin + account.save_money
        final_balance = account.balance + marked_equity + account.save_money
        final_balance_without_fee = (
            account.balance_without_fee + marked_equity_no_fee + account.save_money
        )
        total_profit = final_balance - float(first_balance)
        realized_profit = sum(account.profits_lst)
        unrealized_profit = marked_equity - open_margin
        closed_trades = int(account.count_closed_orders)
        wins = int(account.total_wins)
        losses = int(account.total_losses)
        win_rate = wins * 100.0 / closed_trades if closed_trades else 0.0
        monthly_returns = [float(value) for value in (monthly_returns or ())]
        profit_months = sum(value > 0 for value in monthly_returns)
        loss_months = sum(value < 0 for value in monthly_returns)
        profit_more_than_8 = sum(value >= 0.08 for value in monthly_returns)
        return_percent = (
            final_balance * 100.0 / float(first_balance) - 100.0
            if first_balance else 0.0
        )
        score_metrics = self.calculate_performance_score(
            return_percent=return_percent,
            max_drawdown=account.max_drawdown,
            win_rate=win_rate,
            profits=account.profits_lst,
            first_balance=float(first_balance),
            profit_months=profit_months,
            loss_months=loss_months,
            liquidations=account.total_liquids,
            closed_trades=closed_trades,
        )
        directional_metrics = self.calculate_directional_metrics(
            first_balance=first_balance,
            long_profits=account.long_profits,
            short_profits=account.short_profits,
            long_fees=account.long_fees,
            short_fees=account.short_fees,
            long_durations_minutes=account.long_durations_minutes,
            short_durations_minutes=account.short_durations_minutes,
            long_liquidations=account.long_liquidations,
            short_liquidations=account.short_liquidations,
            long_open_positions=sum(position.side == "long" for position in positions),
            short_open_positions=sum(position.side == "short" for position in positions),
        )
        result = {
            "final_balance_static": round(static_balance, 6),
            "final_balance_dynamic": round(final_balance, 6),
            "final_balance": round(final_balance, 6),
            "final_balance_without_fee": round(final_balance_without_fee, 6),
            "total_profit": round(total_profit, 6),
            "realized_profit": round(realized_profit, 6),
            "unrealized_profit": round(unrealized_profit, 6),
            "open_positions": len(positions),
            "total_fees": round(account.deducting_fee_total + accrued_costs, 6),
            "saved_money": round(account.save_money, 6),
            "liquidations": int(account.total_liquids),
            "total_profit_percent": round(return_percent, 6),
            "sum_trade_profit_percent": round(account.total_profit_percent, 6),
            "closed_trades": closed_trades,
            "wins": wins,
            "losses": losses,
            "long_trades": int(account.total_long),
            "long_wins": int(account.total_wins_long),
            "long_losses": int(account.total_long - account.total_wins_long),
            "short_trades": int(account.total_short),
            "short_wins": int(account.total_wins_short),
            "short_losses": int(account.total_short - account.total_wins_short),
            "maximum_drawdown": round(float(account.max_drawdown), 6),
            "win_rate": round(win_rate, 6),
            "profit_months": int(profit_months),
            "loss_months": int(loss_months),
            "profit_more_than_8%": int(profit_more_than_8),
            "trade_profits": list(account.profits_lst),
            "monthly_returns": monthly_returns,
            **score_metrics,
            **directional_metrics,
        }
        if extra_metrics:
            result.update(dict(extra_metrics))
        monthly_summary, monthly_rows = monthly_report(
            start_time, end_time, monthly_returns, monthly_profit_target_percent, monthly_return_months)
        result.update(monthly_summary)

        if accrue_open_costs:
            result['accrued_open_costs'] = round(accrued_costs, 6)

        publish_backtest(self, result, {key: value for key, value in (report_metadata or {}).items() if key != 'Name'}, start_time, end_time,
                         first_balance, (monthly_summary, monthly_rows), output_file)
        return result

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
            open_margin = sum(position.margin for position in open_positions)
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
        reporting_returns = state.get('research_monthly_returns')
        if reporting_returns is None:
            reporting_returns = [float(value) / 100 for value in monthly_profits]
        monthly_summary, monthly_rows = monthly_report(
            state['first_open_time'], state['last_close_time'], reporting_returns,
            self.monthly_profit_percent_stop_trade, state.get('reporting_month_labels'))
        monthly_summary['monthly_profit_stop_months'] = state.get('monthly_profit_stop_months')
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
        directional_metrics = self.calculate_directional_metrics(
            first_balance=first_balance,
            long_profits=state.get("long_profits", ()),
            short_profits=state.get("short_profits", ()),
            long_fees=state.get("long_fees", ()),
            short_fees=state.get("short_fees", ()),
            long_durations_minutes=state.get("long_durations_minutes", ()),
            short_durations_minutes=state.get("short_durations_minutes", ()),
            long_liquidations=state.get("long_liquidations", 0),
            short_liquidations=state.get("short_liquidations", 0),
            long_open_positions=sum(
                position.side == "long" for position in open_positions
            ),
            short_open_positions=sum(
                position.side == "short" for position in open_positions
            ),
        )
        # Compatibility for callers that still provide aggregate counters only.
        for side in ("long", "short"):
            if not state.get(f"{side}_profits") and state.get(f"total_{side}", 0):
                total = int(state[f"total_{side}"])
                wins_for_side = int(state[f"total_wins_{side}"])
                directional_metrics.update({
                    f"{side}_trades": total,
                    f"{side}_wins": wins_for_side,
                    f"{side}_losses": total - wins_for_side,
                    f"{side}_win_rate": wins_for_side * 100.0 / total,
                })

        total_profit = balance - first_balance
        realized_profit = sum(profits)
        result = {
            "final_balance_static": state["total_money_static"],
            "final_balance_dynamic": state["total_money_dynamic"],
            "final_balance": round(balance, 6),
            "final_balance_without_fee": round(balance_without_fee, 6),
            "total_profit": round(total_profit, 6),
            "realized_profit": round(realized_profit, 6),
            "unrealized_profit": round(unrealized_profit, 6),
            "open_positions": len(open_positions),
            "total_fees": round(state["deducting_fee_total"], 6),
            "saved_money": round(save_money, 6),
            "liquidations": state["total_liquids"],
            "total_profit_percent": round(t_profit_percent, 6),
            "sum_trade_profit_percent": round(state.get('total_profit_percent', 0.0), 6),
            "closed_trades": state["count_closed_orders"],
            "wins": total_wins,
            "losses": total_losses,
            "long_trades": state["total_long"],
            "long_wins": state["total_wins_long"],
            "long_losses": state["total_long"] - state["total_wins_long"],
            "short_trades": state["total_short"],
            "short_wins": state["total_wins_short"],
            "short_losses": state["total_short"] - state["total_wins_short"],
            "maximum_drawdown": round(max_drawdown, 2),
            "win_rate": round(win_rate, 2),
            "profit_more_than_8%": sum(
                value >= 8.0 for value in monthly_profits
                if value is not None and math.isfinite(float(value))
            ),
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
            **directional_metrics,
        }
        result.update(monthly_summary)
        result.update({key: value for key, value in state.items()
                       if '_scale_entry_attempts' in key or key.endswith('_scale_entries')})
        if state.get("research"):
            # These compact series are emitted only for finalists/audits.  They
            # are intentionally omitted from mass discovery results and CSVs.
            result["trade_profits"] = [float(value) for value in profits]
            dedicated_returns = state.get("research_monthly_returns")
            if dedicated_returns is not None:
                result["monthly_returns"] = [
                    float(value) for value in dedicated_returns
                    if value is not None and math.isfinite(float(value))
                ]
            else:
                result["monthly_returns"] = [
                    float(value) / 100.0 for value in monthly_profits
                    if value is not None and math.isfinite(float(value))
                ]
        result['monthly_returns'] = list(reporting_returns)
        result['wins'], result['losses'] = total_wins, total_losses
        parameters = state.get('strategy_parameters', {})
        publish_backtest(self, result, parameters, state['first_open_time'], state['last_close_time'],
                         first_balance, (monthly_summary, monthly_rows))
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
            self.render_result_chart(result, parameters, chart_payload)

        return result

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

        liquid_price_short = entry_price * (
            1 + 1 / leverage - self.maintenance_margin_rate
        )

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

        notional = margin * leverage
        total_fee = (
            notional * self.fee_rate
            + notional * self.liquidation_fee_rate
            + self._funding_cost(notional, open_time_value, close_time_value)
        )

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
            profit,
            profit_percent,
            pnl_percent,
            total_fee,
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

