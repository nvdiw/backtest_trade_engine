"""Causal RSI strategy that demonstrates the optimizer plug-in contract.

Signals are calculated at candle close and filled at the next candle open.
Intrabar ambiguity is handled pessimistically: liquidation, then stop-loss,
then take-profit.  The module deliberately exposes its own ``param_grid`` so it
can be optimized with ``python optimize.py --strategy rsi ...``.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
import json
import math
import numpy as np
from pathlib import Path
from typing import Any, Mapping

from fetch_calculate_data import DATA_FILE
from indicators import Indicator
from trade_engine import TradeEngine, trade_duration


TIMEFRAME = "15m"
_RSI_CACHE_LIMIT = 64
_RSI_CACHE = OrderedDict()


@dataclass(frozen=True)
class RSIStrategyConfig:
    balance: float = 1000.0
    period_rsi: int = 14
    long_entry_rsi: float = 30.0
    long_exit_rsi: float = 55.0
    short_entry_rsi: float = 70.0
    short_exit_rsi: float = 45.0
    enable_long: bool = True
    enable_short: bool = True
    trade_amount_percent: float = 0.50
    leverage: float = 2.0
    stop_loss_pct: float = 0.04
    take_profit_pct: float = 0.08
    cooldown_candles: int = 4
    fee_rate: float = 0.0005
    slippage_rate: float = 0.0001
    funding_rate_per_8h: float = 0.00005
    maintenance_margin_rate: float = 0.005
    liquidation_fee_rate: float = 0.002
    optimize: bool = False


FULL_PARAM_GRID = {
    "period_rsi": [7, 9, 14, 21],
    "long_entry_rsi": [20.0, 25.0, 30.0, 35.0],
    "long_exit_rsi": [50.0, 55.0, 60.0, 65.0],
    "short_entry_rsi": [65.0, 70.0, 75.0, 80.0],
    "short_exit_rsi": [35.0, 40.0, 45.0, 50.0],
    "trade_amount_percent": [0.25, 0.50, 0.75],
    "leverage": [1.0, 2.0, 3.0, 4.0],
    "stop_loss_pct": [0.02, 0.04, 0.06, 0.08],
    "take_profit_pct": [0.04, 0.08, 0.12, 0.16],
    "cooldown_candles": [0, 4, 12, 24],
    # Execution assumptions stay fixed during signal discovery and are stressed
    # independently by --sealed-holdout.
    "fee_rate": [0.0005],
    "slippage_rate": [0.0001],
    "funding_rate_per_8h": [0.00005],
    "maintenance_margin_rate": [0.005],
    "liquidation_fee_rate": [0.002],
}

FOCUSED_PARAM_GRID = {
    key: FULL_PARAM_GRID[key]
    for key in (
        "period_rsi",
        "long_entry_rsi",
        "long_exit_rsi",
        "short_entry_rsi",
        "short_exit_rsi",
    )
}

PARAMETER_PROFILES = {
    "focused": FOCUSED_PARAM_GRID,
    "full": FULL_PARAM_GRID,
}

# Conventional name discovered automatically by strategy_adapter.py.
param_grid = FULL_PARAM_GRID

EXECUTION_SCENARIOS = {
    "base": {},
    "adverse": {
        "fee_rate": 0.0007,
        "slippage_rate": 0.0002,
        "funding_rate_per_8h": 0.0001,
        "maintenance_margin_rate": 0.005,
        "liquidation_fee_rate": 0.002,
    },
    "severe": {
        "fee_rate": 0.0010,
        "slippage_rate": 0.0005,
        "funding_rate_per_8h": 0.0003,
        "maintenance_margin_rate": 0.010,
        "liquidation_fee_rate": 0.005,
    },
}


def _coerce_config_values(tune: Mapping[str, Any] | None) -> dict[str, Any]:
    values = dict(tune or {})
    definitions = {field.name: field for field in fields(RSIStrategyConfig)}
    unknown = sorted(set(values) - set(definitions))
    if unknown:
        raise ValueError("unknown RSI strategy setting(s): " + ", ".join(unknown))
    defaults = RSIStrategyConfig()
    for name, value in list(values.items()):
        default = getattr(defaults, name)
        if isinstance(default, bool):
            if isinstance(value, str):
                lowered = value.strip().lower()
                if lowered not in {"true", "false"}:
                    raise ValueError(f"{name} must be true or false")
                value = lowered == "true"
            else:
                value = bool(value)
        elif isinstance(default, int) and not isinstance(default, bool):
            value = int(value)
        elif isinstance(default, float):
            value = float(value)
        values[name] = value
    return values


def build_strategy_config(
    tune: Mapping[str, Any] | None = None,
) -> RSIStrategyConfig:
    config = RSIStrategyConfig(**_coerce_config_values(tune))
    if config.balance <= 0:
        raise ValueError("balance must be greater than zero")
    if config.period_rsi < 2:
        raise ValueError("period_rsi must be at least 2")
    if not 0 <= config.long_entry_rsi < config.long_exit_rsi <= 100:
        raise ValueError("RSI long levels must satisfy 0 <= entry < exit <= 100")
    if not 0 <= config.short_exit_rsi < config.short_entry_rsi <= 100:
        raise ValueError("RSI short levels must satisfy 0 <= exit < entry <= 100")
    if not (config.enable_long or config.enable_short):
        raise ValueError("at least one side must be enabled")
    if not 0 < config.trade_amount_percent <= 1:
        raise ValueError("trade_amount_percent must be in (0, 1]")
    if config.leverage < 1:
        raise ValueError("leverage must be at least 1")
    if config.stop_loss_pct <= 0 or config.take_profit_pct <= 0:
        raise ValueError("stop_loss_pct and take_profit_pct must be positive")
    if config.cooldown_candles < 0:
        raise ValueError("cooldown_candles cannot be negative")
    for name in (
        "fee_rate",
        "slippage_rate",
        "funding_rate_per_8h",
        "maintenance_margin_rate",
        "liquidation_fee_rate",
    ):
        if getattr(config, name) < 0:
            raise ValueError(f"{name} cannot be negative")
    if config.maintenance_margin_rate >= 1.0 / config.leverage:
        raise ValueError("maintenance margin must be below initial margin")
    return config


# The function-name-specific alias is also recognized by StrategyAdapter.
build_rsi_strategy_config = build_strategy_config


def load_strategy_tune(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("RSI parameter JSON must contain an object")
    return asdict(build_strategy_config(payload))


def required_indicator_warmup(config: RSIStrategyConfig | Mapping[str, Any]) -> int:
    if not isinstance(config, RSIStrategyConfig):
        config = build_strategy_config(config)
    return int(config.period_rsi) + 1


def maximum_optimizer_warmup(parameter_grid, base_tune=None) -> int:
    config = build_strategy_config(base_tune)
    periods = parameter_grid.get("period_rsi", ())
    return max([config.period_rsi, *periods]) + 1 if periods else config.period_rsi + 1


def _cached_rsi(range_key, period, history_closes):
    key = (range_key, int(period))
    if key in _RSI_CACHE:
        _RSI_CACHE.move_to_end(key)
        return _RSI_CACHE[key]
    values = Indicator(history_closes).get_RSI(history_closes, period=int(period))
    _RSI_CACHE[key] = values
    _RSI_CACHE.move_to_end(key)
    if len(_RSI_CACHE) > _RSI_CACHE_LIMIT:
        _RSI_CACHE.popitem(last=False)
    return values


def is_valid_candidate(params: Mapping[str, Any]) -> bool:
    try:
        build_strategy_config(params)
    except (TypeError, ValueError):
        return False
    return True


def canonicalize_candidate(
    params: Mapping[str, Any], baseline: Mapping[str, Any]
) -> dict[str, Any]:
    candidate = dict(params)
    if candidate.get("enable_long") is False:
        for key in ("long_entry_rsi", "long_exit_rsi"):
            if key in candidate and key in baseline:
                candidate[key] = baseline[key]
    if candidate.get("enable_short") is False:
        for key in ("short_entry_rsi", "short_exit_rsi"):
            if key in candidate and key in baseline:
                candidate[key] = baseline[key]
    return candidate


def preload_optimizer_data(
    start,
    end,
    tune,
    use_indicator_warmup=True,
    indicator_warmup_candles=None,
):
    config = build_strategy_config(tune)
    required_warmup = required_indicator_warmup(config)
    TradeEngine.load_market_data(
        start=start,
        end=end,
        warmup_candles=(
            max(required_warmup, int(indicator_warmup_candles or 0))
            if use_indicator_warmup else 0
        ),
        data_file=DATA_FILE,
        timeframe=TIMEFRAME,
    )


def _adverse_fill(raw_price: float, *, side: str, entering: bool, slippage: float) -> float:
    buying = (side == "long" and entering) or (side == "short" and not entering)
    return float(raw_price) * (1.0 + slippage if buying else 1.0 - slippage)


def _timestamp_ns(value: Any) -> int:
    text = str(value).strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.astimezone(timezone.utc).timestamp() * 1_000_000_000)


def _funding_cost(position: Mapping[str, Any], current_time: Any, config: RSIStrategyConfig) -> float:
    if config.funding_rate_per_8h <= 0:
        return 0.0
    entry_time_ns = position.get("entry_time_ns")
    if entry_time_ns is not None and isinstance(current_time, (int, np.integer)):
        elapsed_hours = max(
            0.0, (int(current_time) - int(entry_time_ns)) / 3_600_000_000_000.0
        )
    else:
        days, hours, minutes = trade_duration(position["entry_time"], current_time)
        elapsed_hours = days * 24.0 + hours + minutes / 60.0
    intervals = int(math.ceil(elapsed_hours / 8.0 - 1e-12))
    return float(position["notional"]) * config.funding_rate_per_8h * intervals


def _position_mark(
    cash: float,
    position: Mapping[str, Any] | None,
    price: float,
    current_time: Any,
    config: RSIStrategyConfig,
) -> float:
    if position is None:
        return cash
    direction = 1.0 if position["side"] == "long" else -1.0
    gross = position["quantity"] * (float(price) - position["entry_price"]) * direction
    projected_exit_fee = position["quantity"] * float(price) * config.fee_rate
    return (
        cash
        + position["margin"]
        + gross
        - position["entry_fee"]
        - projected_exit_fee
        - _funding_cost(position, current_time, config)
    )


def rsi_strategy(
    tune: Mapping[str, Any] | None = None,
    start="2022-01-01",
    end="2025-06-01",
    *,
    use_indicator_warmup=True,
    indicator_warmup_candles=None,
    research=False,
    **_ignored,
):
    """Run a one-position long/short RSI strategy and return optimizer metrics."""
    config = build_strategy_config(tune)
    required_warmup = required_indicator_warmup(config)
    warmup = (
        max(required_warmup, int(indicator_warmup_candles or 0))
        if use_indicator_warmup else 0
    )
    market = TradeEngine.load_market_data(
        start=start,
        end=end,
        warmup_candles=warmup,
        data_file=DATA_FILE,
        timeframe=TIMEFRAME,
    )
    open_prices = market["open_prices"]
    close_prices = market["close_prices"]
    low_prices = market["low_prices"]
    high_prices = market["high_prices"]
    open_times = market["open_times"]
    open_time_ns = market.get("open_time_ns")
    if open_time_ns is None:
        open_time_ns = np.asarray([_timestamp_ns(value) for value in open_times])
    if len(close_prices) < 2:
        raise ValueError("RSI strategy requires at least two trading candles")

    history_closes = market["history_close_prices"]
    range_key = (
        market.get("data_start", start),
        market.get("end", end),
        market["warmup_offset"],
    )
    history_rsi = _cached_rsi(
        range_key, config.period_rsi, history_closes
    )
    offset = int(market["warmup_offset"])
    rsi_values = history_rsi[offset : offset + len(close_prices)]

    first_balance = float(config.balance)
    cash = first_balance
    position = None
    pending_order = None
    cooldown_until = 0
    profits = []
    gross_profits = []
    total_fees = 0.0
    liquidations = 0
    long_trades = 0
    short_trades = 0
    long_wins = 0
    long_losses = 0
    short_wins = 0
    short_losses = 0
    long_profits = []
    short_profits = []
    long_fees = []
    short_fees = []
    long_durations_minutes = []
    short_durations_minutes = []
    long_liquidations = 0
    short_liquidations = 0
    equity_peak = first_balance
    maximum_drawdown = 0.0
    month_ends = {}

    def close_position(raw_price, candle_index, *, liquidation=False):
        nonlocal cash, position, total_fees, liquidations, cooldown_until
        nonlocal long_trades, short_trades, long_wins, long_losses
        nonlocal short_wins, short_losses
        nonlocal long_liquidations, short_liquidations
        side = position["side"]
        exit_price = (
            float(raw_price)
            if liquidation
            else _adverse_fill(
                raw_price,
                side=side,
                entering=False,
                slippage=config.slippage_rate,
            )
        )
        direction = 1.0 if side == "long" else -1.0
        gross = position["quantity"] * (exit_price - position["entry_price"]) * direction
        funding = _funding_cost(position, open_time_ns[candle_index], config)
        exit_rate = config.liquidation_fee_rate if liquidation else config.fee_rate
        exit_fee = position["quantity"] * exit_price * exit_rate
        costs = position["entry_fee"] + exit_fee + funding
        net = gross - costs
        cash += position["margin"] + gross - costs
        cash = max(0.0, cash)
        profits.append(net)
        gross_profits.append(gross)
        total_fees += costs
        if liquidation:
            liquidations += 1
        if side == "long":
            long_profits.append(net)
            long_fees.append(costs)
            long_durations_minutes.append(
                max(0.0, (open_time_ns[candle_index] - position["entry_time_ns"]) / 60_000_000_000)
            )
            if liquidation:
                long_liquidations += 1
            long_trades += 1
            if net > 0:
                long_wins += 1
            else:
                long_losses += 1
        else:
            short_profits.append(net)
            short_fees.append(costs)
            short_durations_minutes.append(
                max(0.0, (open_time_ns[candle_index] - position["entry_time_ns"]) / 60_000_000_000)
            )
            if liquidation:
                short_liquidations += 1
            short_trades += 1
            if net > 0:
                short_wins += 1
            else:
                short_losses += 1
        position = None
        cooldown_until = candle_index + config.cooldown_candles

    for index in range(len(close_prices)):
        # Orders created from candle i-1 information execute at candle i open.
        if pending_order is not None:
            action = pending_order
            pending_order = None
            if action == "exit" and position is not None:
                close_position(open_prices[index], index)
            elif action in {"long", "short"} and position is None and index >= cooldown_until:
                side = action
                entry_price = _adverse_fill(
                    open_prices[index],
                    side=side,
                    entering=True,
                    slippage=config.slippage_rate,
                )
                margin = min(cash, cash * config.trade_amount_percent)
                if margin > 0 and entry_price > 0:
                    notional = margin * config.leverage
                    quantity = notional / entry_price
                    position = {
                        "side": side,
                        "entry_price": entry_price,
                        "entry_index": index,
                        "entry_time": open_times[index],
                        "entry_time_ns": open_time_ns[index],
                        "margin": margin,
                        "notional": notional,
                        "quantity": quantity,
                        "entry_fee": notional * config.fee_rate,
                    }
                    cash -= margin

        # OHLC cannot reveal the order in which levels were touched.  The fixed
        # pessimistic order prevents a favourable-path bias.
        if position is not None:
            side = position["side"]
            entry = position["entry_price"]
            leverage = config.leverage
            if side == "long":
                liquidation_price = entry * (
                    1.0 - 1.0 / leverage + config.maintenance_margin_rate
                )
                stop_price = entry * (1.0 - config.stop_loss_pct)
                target_price = entry * (1.0 + config.take_profit_pct)
                if low_prices[index] <= liquidation_price:
                    close_position(liquidation_price, index, liquidation=True)
                elif low_prices[index] <= stop_price:
                    close_position(min(open_prices[index], stop_price), index)
                elif high_prices[index] >= target_price:
                    close_position(target_price, index)
            else:
                liquidation_price = entry * (
                    1.0 + 1.0 / leverage - config.maintenance_margin_rate
                )
                stop_price = entry * (1.0 + config.stop_loss_pct)
                target_price = entry * (1.0 - config.take_profit_pct)
                if high_prices[index] >= liquidation_price:
                    close_position(liquidation_price, index, liquidation=True)
                elif high_prices[index] >= stop_price:
                    close_position(max(open_prices[index], stop_price), index)
                elif low_prices[index] <= target_price:
                    close_position(target_price, index)

        equity = _position_mark(
            cash, position, close_prices[index], open_time_ns[index], config
        )
        equity_peak = max(equity_peak, equity)
        if equity_peak > 0:
            maximum_drawdown = min(
                maximum_drawdown, (equity / equity_peak - 1.0) * 100.0
            )
        month_ends[str(open_times[index])[:7]] = equity

        if index + 1 >= len(close_prices):
            continue
        current_rsi = rsi_values[index]
        previous_rsi = rsi_values[index - 1] if index else None
        if current_rsi is None or previous_rsi is None:
            continue
        if position is not None:
            if (
                position["side"] == "long"
                and current_rsi >= config.long_exit_rsi
            ) or (
                position["side"] == "short"
                and current_rsi <= config.short_exit_rsi
            ):
                pending_order = "exit"
        elif index >= cooldown_until:
            if (
                config.enable_long
                and previous_rsi <= config.long_entry_rsi < current_rsi
            ):
                pending_order = "long"
            elif (
                config.enable_short
                and previous_rsi >= config.short_entry_rsi > current_rsi
            ):
                pending_order = "short"

    final_equity = _position_mark(
        cash, position, close_prices[-1], open_time_ns[-1], config
    )
    open_gross = 0.0
    open_projected_costs = 0.0
    if position is not None:
        direction = 1.0 if position["side"] == "long" else -1.0
        open_gross = position["quantity"] * (
            close_prices[-1] - position["entry_price"]
        ) * direction
        open_projected_costs = (
            position["entry_fee"]
            + position["quantity"] * close_prices[-1] * config.fee_rate
            + _funding_cost(position, open_time_ns[-1], config)
        )
    final_without_fee = first_balance + sum(gross_profits) + open_gross
    total_profit = final_equity - first_balance
    total_profit_percent = total_profit * 100.0 / first_balance
    wins = sum(value > 0 for value in profits)
    losses = sum(value <= 0 for value in profits)
    monthly_returns = []
    previous_equity = first_balance
    for equity in month_ends.values():
        monthly_returns.append(equity / previous_equity - 1.0)
        previous_equity = equity
    profit_months = sum(value > 0 for value in monthly_returns)
    loss_months = sum(value < 0 for value in monthly_returns)
    closed_trades = len(profits)
    win_rate = wins * 100.0 / closed_trades if closed_trades else 0.0
    score_metrics = TradeEngine.calculate_performance_score(
        return_percent=total_profit_percent,
        max_drawdown=maximum_drawdown,
        win_rate=win_rate,
        profits=profits,
        first_balance=first_balance,
        profit_months=profit_months,
        loss_months=loss_months,
        liquidations=liquidations,
        closed_trades=closed_trades,
    )
    directional_metrics = TradeEngine.calculate_directional_metrics(
        first_balance=first_balance,
        long_profits=long_profits,
        short_profits=short_profits,
        long_fees=long_fees,
        short_fees=short_fees,
        long_durations_minutes=long_durations_minutes,
        short_durations_minutes=short_durations_minutes,
        long_liquidations=long_liquidations,
        short_liquidations=short_liquidations,
        long_open_positions=int(position is not None and position["side"] == "long"),
        short_open_positions=int(position is not None and position["side"] == "short"),
    )
    result = {
        "final_balance_static": round(final_equity, 6),
        "final_balance_dynamic": round(final_equity, 6),
        "final_balance": round(final_equity, 6),
        "final_balance_without_fee": round(final_without_fee, 6),
        "total_profit": round(total_profit, 6),
        "realized_profit": round(sum(profits), 6),
        "unrealized_profit": round(total_profit - sum(profits), 6),
        "open_positions": int(position is not None),
        "total_fees": round(total_fees + open_projected_costs, 6),
        "saved_money": 0.0,
        "liquidations": liquidations,
        "total_profit_percent": round(total_profit_percent, 6),
        "closed_trades": closed_trades,
        "wins": wins,
        "losses": losses,
        "long_trades": long_trades,
        "long_wins": long_wins,
        "long_losses": long_losses,
        "short_trades": short_trades,
        "short_wins": short_wins,
        "short_losses": short_losses,
        "maximum_drawdown": round(maximum_drawdown, 6),
        "win_rate": round(win_rate, 6),
        "profit_months": profit_months,
        "loss_months": loss_months,
        "score": score_metrics["score"],
        "profit_factor": score_metrics["profit_factor"],
        "expectancy_percent": score_metrics["expectancy_percent"],
        "calmar_ratio": score_metrics["calmar_ratio"],
        **directional_metrics,
    }
    if research:
        result["trade_profits"] = [float(value) for value in profits]
        result["monthly_returns"] = [float(value) for value in monthly_returns]
    return result


__all__ = [
    "DATA_FILE",
    "EXECUTION_SCENARIOS",
    "PARAMETER_PROFILES",
    "RSIStrategyConfig",
    "build_strategy_config",
    "param_grid",
    "rsi_strategy",
]
