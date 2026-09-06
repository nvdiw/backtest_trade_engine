"""Minimal strategy plug-in template using a strategy-owned 1-minute dataset.

Copy this file, rename ``example_strategy`` and its config class, then replace
only the signal section.  The optimizer discovers DATA_FILE, TIMEFRAME,
PARAMETER_PROFILES, configuration, warm-up, and candidate validation hooks.

Signals use closed candle ``i`` and orders fill at candle ``i + 1`` open.  This
prevents look-ahead bias.  Liquidation is checked against each candle's low/high
before signal exits, which is deliberately conservative.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import asdict, dataclass, fields
import json
from pathlib import Path
from typing import Any, Mapping

from indicators import Indicator
from trade_engine import AccountState, Position, TradeEngine
from strategy_workspace import claim_output, output_session


DATA_FILE = Path(__file__).resolve().parent / "data_candle" / "btc_1m_data.csv"
TIMEFRAME = "1m"


@dataclass(frozen=True)
class ExampleStrategyConfig:
    balance: float = 1000.0
    fast_period: int = 12
    slow_period: int = 48
    enable_long: bool = True
    enable_short: bool = True
    trade_amount_percent: float = 0.50
    leverage: float = 2.0
    fee_rate: float = 0.0005
    slippage_rate: float = 0.0001
    funding_rate_per_8h: float = 0.00005
    maintenance_margin_rate: float = 0.005
    liquidation_fee_rate: float = 0.002
    optimize: bool = False


FOCUSED_PARAM_GRID = {
    "fast_period": [5, 8, 12, 16, 20],
    "slow_period": [30, 40, 48, 60, 90],
}

SIGNAL_PARAM_GRID = {
    **FOCUSED_PARAM_GRID,
    "enable_long": [True, False],
    "enable_short": [True, False],
}

RISK_PARAM_GRID = {
    "trade_amount_percent": [0.25, 0.50, 0.75],
    "leverage": [1.0, 2.0, 3.0],
}

FULL_PARAM_GRID = {
    **FOCUSED_PARAM_GRID,
    "enable_long": [True, False],
    "enable_short": [True, False],
    "trade_amount_percent": [0.25, 0.50, 0.75],
    "leverage": [1.0, 2.0, 3.0],
    # Execution assumptions stay fixed during discovery and are stressed later.
    "fee_rate": [0.0005],
    "slippage_rate": [0.0001],
    "funding_rate_per_8h": [0.00005],
    "maintenance_margin_rate": [0.005],
    "liquidation_fee_rate": [0.002],
}

PARAMETER_PROFILES = {
    "focused": FOCUSED_PARAM_GRID,
    "signal": SIGNAL_PARAM_GRID,
    "risk": RISK_PARAM_GRID,
    "full": FULL_PARAM_GRID,
}
param_grid = FULL_PARAM_GRID

# Optional: enables generic ``--staged`` optimization for this strategy.
STAGED_PHASES = (
    ("signal", "signal"),
    ("risk", "risk"),
)

EXECUTION_SCENARIOS = {
    "base": {},
    "adverse": {"fee_rate": 0.0007, "slippage_rate": 0.0002},
    "severe": {"fee_rate": 0.0010, "slippage_rate": 0.0005},
}


def _coerce_config_values(tune: Mapping[str, Any] | None) -> dict[str, Any]:
    values = dict(tune or {})
    definitions = {field.name: field for field in fields(ExampleStrategyConfig)}
    unknown = sorted(set(values) - set(definitions))
    if unknown:
        raise ValueError("unknown example strategy setting(s): " + ", ".join(unknown))
    defaults = ExampleStrategyConfig()
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
        elif isinstance(default, int):
            value = int(value)
        elif isinstance(default, float):
            value = float(value)
        values[name] = value
    return values


def build_strategy_config(
    tune: Mapping[str, Any] | None = None,
) -> ExampleStrategyConfig:
    config = ExampleStrategyConfig(**_coerce_config_values(tune))
    if config.balance <= 0:
        raise ValueError("balance must be greater than zero")
    if config.fast_period < 2 or config.slow_period <= config.fast_period:
        raise ValueError("periods must satisfy 2 <= fast_period < slow_period")
    if not (config.enable_long or config.enable_short):
        raise ValueError("at least one side must be enabled")
    if not 0 < config.trade_amount_percent <= 1:
        raise ValueError("trade_amount_percent must be in (0, 1]")
    if config.leverage < 1:
        raise ValueError("leverage must be at least 1")
    if config.maintenance_margin_rate >= 1.0 / config.leverage:
        raise ValueError("maintenance margin must be below initial margin")
    return config


build_example_strategy_config = build_strategy_config


def load_strategy_tune(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("strategy parameter JSON must contain an object")
    return asdict(build_strategy_config(payload))


def required_indicator_warmup(config: ExampleStrategyConfig | Mapping[str, Any]) -> int:
    if not isinstance(config, ExampleStrategyConfig):
        config = build_strategy_config(config)
    return int(config.slow_period)


def maximum_optimizer_warmup(parameter_grid, base_tune=None) -> int:
    config = build_strategy_config(base_tune)
    values = parameter_grid.get("slow_period", ())
    return max([config.slow_period, *values]) if values else config.slow_period


def is_valid_candidate(params: Mapping[str, Any]) -> bool:
    try:
        build_strategy_config(params)
    except (TypeError, ValueError):
        return False
    return True


_INDICATOR_CACHE = OrderedDict()
_INDICATOR_CACHE_LIMIT = 64


def _moving_averages(market, fast_period, slow_period):
    key = (
        market["data_start"], market["end"], market["warmup_offset"],
        int(fast_period), int(slow_period),
    )
    if key in _INDICATOR_CACHE:
        _INDICATOR_CACHE.move_to_end(key)
        return _INDICATOR_CACHE[key]
    indicator = Indicator(market["history_close_prices"])
    offset = market["warmup_offset"]
    values = (
        indicator.get_MA(int(fast_period))[offset:],
        indicator.get_MA(int(slow_period))[offset:],
    )
    _INDICATOR_CACHE[key] = values
    _INDICATOR_CACHE.move_to_end(key)
    if len(_INDICATOR_CACHE) > _INDICATOR_CACHE_LIMIT:
        _INDICATOR_CACHE.popitem(last=False)
    return values


def _portfolio_equity(engine, account, position, mark_price):
    position_equity = engine.position_equity(position, mark_price) if position else 0.0
    return account.balance + account.save_money + position_equity


@output_session
def example_strategy(
    tune: Mapping[str, Any] | None = None,
    start="2025-01-01",
    end="latest",
    *,
    use_indicator_warmup=True,
    indicator_warmup_candles=None,
    research=False,
    output_dir="outputs/example/backtest",
    **_ignored,
):
    """Run a causal one-position MA-crossover example on BTC 1m candles."""
    config = build_strategy_config(tune)
    optimize = bool(config.optimize)
    if not optimize:
        claim_output(output_dir, 'example_strategy:example_strategy', 'backtest')
    required_warmup = required_indicator_warmup(config)
    warmup = (
        max(required_warmup, int(indicator_warmup_candles or 0))
        if use_indicator_warmup else 0
    )
    if end == "latest":
        from market_data import MarketDataSource

        end = MarketDataSource(DATA_FILE, TIMEFRAME).coverage()["end_exclusive"]
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
    close_times = market["close_times"]
    fast_ma, slow_ma = _moving_averages(
        market, config.fast_period, config.slow_period
    )

    engine = TradeEngine(
        first_balance=config.balance,
        tactical_balance=config.balance,
        leverage=config.leverage,
        optimize=optimize,
        verbose=not optimize,
        write_trades=not optimize,
        write_excel=not optimize,
        output_dir=output_dir,
        track_equity_curve=not optimize,
        fee_rate=config.fee_rate,
        slippage_rate=config.slippage_rate,
        funding_rate_per_8h=config.funding_rate_per_8h,
        maintenance_margin_rate=config.maintenance_margin_rate,
        liquidation_fee_rate=config.liquidation_fee_rate,
    )
    account = AccountState(balance=config.balance)
    position = None
    next_trade_id = 1
    current_month = str(open_times[0])[:7]
    month_start_equity = config.balance
    monthly_returns = []

    for i in range(len(close_prices)):
        if position is not None:
            if position.side == "long":
                liquidation = engine.check_liquidation_long(
                    i, low_prices, close_times, position, account,
                    reason_to_close="liquidation",
                )
            else:
                liquidation = engine.check_liquidation_short(
                    i, high_prices, close_times, position, account,
                    reason_to_close="liquidation",
                )
            if liquidation["liquidated"]:
                position = None

        equity = _portfolio_equity(engine, account, position, close_prices[i])
        engine.update_account_drawdown(account, equity)
        candle_month = str(open_times[i])[:7]
        if candle_month != current_month:
            monthly_returns.append(equity / month_start_equity - 1.0)
            current_month = candle_month
            month_start_equity = equity

        execution_i = i + 1
        if execution_i >= len(open_prices) or i == 0:
            continue
        if None in (fast_ma[i - 1], slow_ma[i - 1], fast_ma[i], slow_ma[i]):
            continue
        # ---- CUSTOM SIGNAL SECTION: replace these rules in your strategy. ----
        crossed_up = fast_ma[i - 1] <= slow_ma[i - 1] and fast_ma[i] > slow_ma[i]
        crossed_down = fast_ma[i - 1] >= slow_ma[i - 1] and fast_ma[i] < slow_ma[i]

        if position is not None and (
            (position.side == "long" and crossed_down)
            or (position.side == "short" and crossed_up)
        ):
            if position.side == "long":
                engine.close_long(
                    execution_i, open_prices, open_times, position, account,
                    fee_rate=config.fee_rate,
                    cooldown_after_big_pnl=0,
                    reason_to_close="opposite_crossover",
                )
            else:
                engine.close_short(
                    execution_i, open_prices, open_times, position, account,
                    fee_rate=config.fee_rate,
                    cooldown_after_big_pnl=0,
                    reason_to_close="opposite_crossover",
                )
            position = None

        side = "long" if crossed_up else "short" if crossed_down else None
        side_enabled = (
            side == "long" and config.enable_long
        ) or (
            side == "short" and config.enable_short
        )
        if position is None and side_enabled and account.balance > 0:
            if side == "long":
                opened = engine.open_long(
                    execution_i, open_prices, open_times, account,
                    trade_amount_percent=config.trade_amount_percent,
                    leverage=config.leverage,
                )
            else:
                opened = engine.open_short(
                    execution_i, open_prices, open_times, account,
                    trade_amount_percent=config.trade_amount_percent,
                    leverage=config.leverage,
                )
            position = Position.from_open_result(
                opened,
                trade_id=f"example_{next_trade_id:06d}",
                side=side,
                entry_index=execution_i,
                high_price=high_prices[execution_i],
                low_price=low_prices[execution_i],
                reason=f"{side}_crossover",
            )
            next_trade_id += 1

    final_equity = _portfolio_equity(engine, account, position, close_prices[-1])
    monthly_returns.append(final_equity / month_start_equity - 1.0)
    return engine.finalize_account(
        account,
        first_balance=config.balance,
        open_positions=[position] if position else [],
        ending_mark_price=close_prices[-1],
        start_time=open_times[0],
        end_time=close_times[-1],
        monthly_returns=monthly_returns,
        extra_metrics={
            "strategy_name": "example_strategy",
            "timeframe": TIMEFRAME,
        },
    )


if __name__ == "__main__":
    print(json.dumps(example_strategy(), indent=2, default=str))
