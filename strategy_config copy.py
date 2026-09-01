from dataclasses import dataclass, fields


def _coerce_like(current_value, new_value):
    if isinstance(current_value, bool):
        if isinstance(new_value, str):
            return new_value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(new_value)
    if isinstance(current_value, int) and not isinstance(current_value, bool):
        return int(new_value)
    if isinstance(current_value, float):
        return float(new_value)
    return new_value


def _apply_common_tune(config, tune):
    if not tune:
        return config

    field_names = {field.name for field in fields(config)}
    for key, value in tune.items():
        if key in field_names:
            setattr(config, key, _coerce_like(getattr(config, key), value))

    # Backward-compatible tune aliases used by optimize.py and older runs.
    if 'trade_amount_percent_neworder' in tune:
        config.scale_entry_amount_percent = float(tune['trade_amount_percent_neworder'])

    if 'scale_entry_trigger_pct' in tune:
        trigger_pct = float(tune['scale_entry_trigger_pct'])
        config.scale_entry_profit_trigger_pct = trigger_pct
        config.scale_entry_loss_trigger_pct = trigger_pct
    elif 'scale_in_trigger_move_pct' in tune:
        trigger_pct = float(tune['scale_in_trigger_move_pct'])
        config.scale_entry_profit_trigger_pct = trigger_pct
        config.scale_entry_loss_trigger_pct = trigger_pct

    if 'loss_scale_entry_long_atr_ratio_min' in tune:
        config.profit_scale_entry_atr_ratio_min = float(tune['loss_scale_entry_long_atr_ratio_min'])
    if 'loss_scale_entry_short_atr_ratio_min' in tune:
        config.profit_scale_entry_atr_ratio_min = float(tune['loss_scale_entry_short_atr_ratio_min'])

    if 'ema_16' in tune:
        config.ema_16_period = int(tune['ema_16'])
    if 'ma_50' in tune:
        config.ma_50_period = int(tune['ma_50'])
    if 'ma_100' in tune:
        config.ma_100_period = int(tune['ma_100'])
    if 'ma_200' in tune:
        config.ma_200_period = int(tune['ma_200'])

    config.max_open_trades = max(1, int(config.max_open_trades))

    return config


@dataclass
class CapitalConfig:
    # Initial account balance used by the backtest.
    balance: float = 1000
    # Default leverage before safe-leverage tiers override it.
    leverage: float = 12
    # Main order size as a fraction of tactical balance.
    trade_amount_percent: float = 0.9
    # Reserved capital kept outside active balance.
    save_money: float = 0
    # fee rate.
    fee_rate = 0.0005


@dataclass
class SafeLeverageConfig:
    # Leverage used when account capital drops into the lowest safety tier.
    safe_leverage_low: float = 2
    # Leverage used for the middle safety tier.
    safe_leverage_med: float = 3
    # Leverage used for the high safety tier before default leverage resumes.
    safe_leverage_high: float = 5
    # Capital percent threshold for the low leverage tier.
    safe_leverage_balance_pct_low: float = 60
    # Capital percent threshold for the medium leverage tier.
    safe_leverage_balance_pct_med: float = 70
    # Capital percent threshold for the high leverage tier.
    safe_leverage_balance_pct_high: float = 85
    # Refill active balance from saved money below this tactical-balance percent.
    save_money_recover_trigger_pct: float = 80


@dataclass
class MonthlyControlConfig:
    # Monthly profit percent that pauses trading when profit filter is enabled.
    monthly_profit_percent_stop_trade: int = 20
    # Monthly loss percent that pauses trading when loss filter is enabled.
    monthly_loss_percent_stop_trade: int = 19
    # Tactical balance increase after a profitable stopped month.
    monthly_compound: float = 8
    # Enable monthly profit stop behavior.
    monthly_profit_close_filter: bool = True
    # Enable monthly loss stop behavior.
    monthly_loss_close_filter: bool = False


@dataclass
class ScaleEntryConfig:
    # Extra scale-in order size as a fraction of tactical balance.
    scale_entry_amount_percent: float = 0.05
    # Price move needed to add another entry in the winning direction.
    scale_entry_profit_trigger_pct: float = 0.039
    # Price move needed to add another entry in the losing direction.
    scale_entry_loss_trigger_pct: float = 0.02
    # Allow scale-in entries after price moves in favor of the first entry.
    scale_entry_on_profit_enabled: bool = True
    # Allow scale-in entries after price moves against the first entry.
    scale_entry_on_loss_enabled: bool = True
    # Master switch for all scale-in behavior.
    scale_in_enabled: bool = True
    # Require extra quality checks before loss-side scale-in entries.
    profit_scale_entry_filter_enabled: bool = False
    # Minimum entry score required for loss-side scale-in entries.
    profit_scale_entry_min_score: int = 6
    # Minimum ATR/ATR_MA ratio for loss-side scale-in entries.
    profit_scale_entry_atr_ratio_min: float = 0.9


@dataclass
class TradeFilterConfig:
    # Enable ADX filters for entries/exits.
    adx_filter: bool = True
    # Enable volume spike filters.
    volume_filter: bool = True
    # Enable ATR quality filters.
    atr_filter: bool = False
    # Stop trading until next month after too many consecutive losses.
    consecutive_losses_month_stop_filter: bool = False
    # Skip trades after loss streak logic requests it.
    skip_logic: bool = False
    # Maximum simultaneously open positions.
    max_open_trades: int = 4
    # Candles to wait after a large unlevered PnL event.
    cooldown_after_big_pnl: int = 96


@dataclass
class EntryContextConfig:
    # Minimum EMA/MA distance used by score logic.
    ma_distance_threshold: float = 0.00159
    # Minimum candle body move used by score logic.
    candle_move_threshold: float = 0.015
    # Strong move threshold around cross events, in percent.
    impulse_move_threshold_pct: float = 0.75
    # Number of candles used to inspect impulse moves.
    impulse_lookback: int = 10
    # ATR multiplier used to penalize late entries.
    late_entry_atr_mult: float = 0.5
    # Candle body ratio used to detect late entries.
    late_entry_body_ratio: float = 0.4
    # EMA distance percent used to detect late entries.
    late_entry_ema_pct: float = 0.01


@dataclass
class ExitRuleConfig:
    # Minimum score needed to open a trade.
    entry_score_threshold: int = 9
    # Minimum score needed to close a trade.
    exit_score_threshold: int = 7
    # Window used for EMA slope calculations.
    slope_window: int = 8
    # Profit percent where trailing exit logic becomes active.
    trail_activate_pct: float = 0.02
    # Pullback percent from high/low that triggers trailing exit.
    trail_retrace_pct: float = 0.008
    # First loss guard threshold.
    loss_exit_pct_1: float = 0.03
    # Second loss guard threshold.
    loss_exit_pct_2: float = 0.03
    # First profit guard threshold.
    profit_exit_pct_1: float = 0.15
    # Second profit guard threshold.
    profit_exit_pct_2: float = 0.2
    # Legacy single loss guard key kept for old optimize grids.
    loss_exit_pct: float = 0.0
    # Legacy single profit guard key kept for old optimize grids.
    profit_exit_pct: float = 0.0
    # Step size for moving locked loss/profit targets.
    loss_lock_step_pct: float = 0.005
    # ADX value below/above which exit logic can score a close.
    adx_exit_threshold: float = 12
    # Lookback window for ADX exit checks.
    adx_exit_lookback: int = 3
    # ADX value required by entry scoring.
    entry_adx_threshold: float = 20.5
    # ATR/ATR_MA ratio required by entry scoring.
    entry_atr_threshold: float = 1.0
    # Opposite candle body size multiplier for exit scoring.
    opposite_atr_body_mult: float = 0.8
    # Sharp move threshold after a cross, in percent.
    sharp_move_threshold_pct: float = 15
    # Candles used to search for sharp post-cross moves.
    sharp_move_lookback_candles: int = 600
    # Candles where post-cross sharp-move penalty is active.
    post_cross_penalty_candles: int = 20
    # Loss streak count that can stop trading until next month.
    consecutive_losses_stop_until_month: int = 10


@dataclass
class IndicatorPeriodConfig:
    # ADX period.
    period_adx: int = 16
    # ATR period.
    period_atr: int = 12
    # Moving average period applied to ATR.
    period_atr_ma: int = 28
    # Volume moving average period.
    period_vol_avg: int = 15
    # RSI period.
    period_rsi: int = 7
    # Multiplier used to detect volume spikes.
    volume_spike_multiplier: float = 1.3
    # EMA period used as the fast MA line.
    ema_16_period: int = 14
    # Medium MA period.
    ma_50_period: int = 40
    # Long MA period.
    ma_100_period: int = 102
    # Slow MA period.
    ma_200_period: int = 260


@dataclass
class ChartConfig:
    # Candle limit for normal chart rendering.
    plot_max_candles: int = 1200
    # Skip latest N candles when inspecting older chart windows.
    plot_end_offset: int = 0
    # Candle step for chart navigation.
    plot_step_candles: int = 300
    # Minimum visible candles while zooming.
    plot_min_zoom_candles: int = 80
    # Max rendered candles after aggregation.
    plot_max_render_candles: int = 1600
    # Mouse-wheel zoom-in multiplier.
    plot_zoom_in_factor: float = 0.8
    # Mouse-wheel zoom-out multiplier.
    plot_zoom_out_factor: float = 1.6
    # Chart window width relative to screen.
    plot_window_width_scale: float = 0.94
    # Chart window height relative to screen.
    plot_window_height_scale: float = 0.90
    # Render less data during drag for smoother chart movement.
    plot_drag_preview_factor: float = 0.42
    # Drag redraw throttle in milliseconds.
    plot_drag_update_interval_ms: int = 16
    # Vertical zoom sensitivity for right-drag.
    plot_yscale_drag_sensitivity: float = 0.0030
    # Show penalty markers on the chart.
    plot_post_cross_penalty_markers: bool = True


@dataclass
class ScoreWeightConfig:
    # Entry score: EMA/MA cross condition.
    entry_score_cross: int = 1
    # Entry score: EMA position versus MA50.
    entry_score_ema_vs_ma50: int = 2
    # Entry score: MA trend alignment.
    entry_score_ma_trend: int = 1
    # Entry score: MA distance or candle strength.
    entry_score_ma_distance_or_candle: int = 1
    # Entry score: ADX strength.
    entry_score_adx: int = 3
    # Entry score: volume confirmation.
    entry_score_volume: int = 2
    # Entry score penalty for late entries.
    entry_late_penalty: int = 0
    # Exit score: first loss guard.
    exit_score_loss_guard_1: int = 1
    # Exit score: second loss guard.
    exit_score_loss_guard_2: int = 2
    # Exit score: first profit guard.
    exit_score_profit_guard_1: int = 2
    # Exit score: second profit guard.
    exit_score_profit_guard_2: int = 2
    # Legacy single loss guard score kept for old optimize grids.
    exit_score_loss_guard: int = 0
    # Legacy single profit guard score kept for old optimize grids.
    exit_score_profit_guard: int = 0
    # Exit score: EMA slope.
    exit_score_ema_slope: int = 1
    # Exit score: EMA/MA cross.
    exit_score_ema_cross: int = 1
    # Exit score: MA trend.
    exit_score_ma_trend: int = 1
    # Exit score: trailing exit condition.
    exit_score_trailing: int = 2
    # Exit score: ADX condition.
    exit_score_adx: int = 2
    # Exit score: strong opposite candle.
    exit_score_opposite_candle: int = 1
    # Exit score penalty after sharp post-cross moves.
    post_cross_penalty_score: int = 4

@dataclass
class PositionsMonthlyFilter:

    # --- master switch
    rsi_trade_monthly_filter_on: bool = True

    # --- RSI thresholds
    rsi_long_open_monthly_profit: int = 30
    rsi_long_close_monthly_profit: int = 79
    rsi_short_open_monthly_profit: int = 75
    rsi_short_close_monthly_profit: int = 20

    # --- risk / trade management
    rsi_long_tp_pct: float = 0.03
    rsi_long_sl_pct: float = 0.06
    rsi_short_tp_pct: float = 0.05
    rsi_short_sl_pct: float = 0.05

    rsi_max_open_trades: int = 4
    rsi_trade_amount_percent: float = 0.5
    rsi_leverage: int = 2

    # --- cooldown
    rsi_cooldown_filter: bool = True
    rsi_cooldown_bars: int = 12

    # --- RSI extreme detection
    lowest_rsi_last_n_value: int = 5
    highest_rsi_last_n_value: int = 2

    # --- NEW (IMPORTANT IMPROVEMENTS)
    rsi_entry_buffer: int = 8
    rsi_distance_threshold: int = 8

@dataclass
class BaseStrategyConfig(
    CapitalConfig,
    SafeLeverageConfig,
    MonthlyControlConfig,
    ScaleEntryConfig,
    TradeFilterConfig,
    EntryContextConfig,
    ExitRuleConfig,
    IndicatorPeriodConfig,
    ChartConfig,
    ScoreWeightConfig,
    PositionsMonthlyFilter,
):
    """Shared settings used by MA and RSI strategies."""


@dataclass
class MAStrategyConfig(BaseStrategyConfig):
    pass


@dataclass
class RSIStrategyConfig(BaseStrategyConfig):
    leverage: float = 1
    trade_amount_percent: float = 0.10
    monthly_profit_close_filter: bool = False
    monthly_loss_close_filter: bool = False
    adx_filter: bool = False
    volume_filter: bool = False
    atr_filter: bool = False
    max_open_trades: int = 5
    safe_leverage_low: float = 1
    safe_leverage_med: float = 1
    safe_leverage_high: float = 1
    period_rsi: int = 10

    # RSI strategy controls
    symbol_change_pct: float = 0.0
    more_symbol_change_pct: float = 0.05
    rsi_symbol_change_pct: float = 0.02
    max_trade_change_pct: float = 0.0
    static_dynamic_money_pct: float = 0.90
    rsi_open_value: int = 35
    rsi_close_value: float = 90
    add_remove_maxtrades_power: bool = True


def build_ma_strategy_config(tune=None):
    return _apply_common_tune(MAStrategyConfig(), tune)


def build_rsi_strategy_config(tune=None):
    return _apply_common_tune(RSIStrategyConfig(), tune)
