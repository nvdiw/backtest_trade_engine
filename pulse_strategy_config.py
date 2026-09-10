"""Pulse defaults, validation and optimizer grids; independent of MA settings."""
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
from strategy_workspace import assert_strategy_path

IDENTIFIER = 'pulse_strategy:pulse_strategy'
AUTO_DATE_DEFAULTS = {
    'rolling_development_months': 12,
    'rolling_stress_months': 6,
    'rolling_validation_months': 3,
}

@dataclass(frozen=True)
class PulseConfig:
    breakout_lookback_bars: int = 30
    stop_atr_mult: float = 2.0
    max_hold_bars: int = 30
    long_breakout_lookback_bars: int | None = None
    short_breakout_lookback_bars: int | None = None
    long_stop_atr_mult: float | None = None
    short_stop_atr_mult: float | None = None
    long_max_hold_bars: int | None = None
    short_max_hold_bars: int | None = None
    breakout_buffer_atr: float = 0.0
    trend_ma_bars: int = 0
    min_efficiency_ratio: float = 0.0
    min_volume_ratio: float = 0.0
    min_atr_cost_ratio: float = 0.0
    max_signal_range_atr: float = 0.0
    trailing_stop_atr_mult: float = 0.0
    trailing_activation_r: float = 1.0
    cooldown_bars: int = 0
    max_entry_gap_atr: float = 0.0
    entry_score_min: float = 0.0
    failed_breakout_bars: int = 0
    breakeven_activation_r: float = 0.0
    max_atr_expansion: float = 0.0
    stagnation_exit_bars: int = 0
    stagnation_min_progress_r: float = 0.5
    long_max_atr_expansion: float | None = None
    short_max_atr_expansion: float | None = None
    long_stagnation_exit_bars: int | None = None
    short_stagnation_exit_bars: int | None = None
    long_stagnation_min_progress_r: float | None = None
    short_stagnation_min_progress_r: float | None = None
    long_entry_score_min: float | None = None
    short_entry_score_min: float | None = None
    long_failed_breakout_bars: int | None = None
    short_failed_breakout_bars: int | None = None
    long_breakeven_activation_r: float | None = None
    short_breakeven_activation_r: float | None = None
    long_breakout_buffer_atr: float | None = None
    long_trend_ma_bars: int | None = None
    long_min_efficiency_ratio: float | None = None
    long_min_volume_ratio: float | None = None
    long_min_atr_cost_ratio: float | None = None
    long_max_signal_range_atr: float | None = None
    long_trailing_stop_atr_mult: float | None = None
    long_trailing_activation_r: float | None = None
    long_cooldown_bars: int | None = None
    long_max_entry_gap_atr: float | None = None
    short_breakout_buffer_atr: float | None = None
    short_trend_ma_bars: int | None = None
    short_min_efficiency_ratio: float | None = None
    short_min_volume_ratio: float | None = None
    short_min_atr_cost_ratio: float | None = None
    short_max_signal_range_atr: float | None = None
    short_trailing_stop_atr_mult: float | None = None
    short_trailing_activation_r: float | None = None
    short_cooldown_bars: int | None = None
    short_max_entry_gap_atr: float | None = None
    atr_period: int = 20
    enable_long: bool = True
    enable_short: bool = True
    balance: float = 1000.0
    risk_per_trade: float = .0025
    max_gross_exposure: float = 1.0
    leverage: float = 1.0
    trade_amount_percent: float = 1.0
    long_risk_per_trade: float | None = None
    short_risk_per_trade: float | None = None
    long_max_gross_exposure: float | None = None
    short_max_gross_exposure: float | None = None
    long_leverage: float | None = None
    short_leverage: float | None = None
    long_trade_amount_percent: float | None = None
    short_trade_amount_percent: float | None = None
    maintenance_margin_rate: float = 0.005
    liquidation_fee_rate: float = 0.002
    quantity_step: float = 0.0
    min_quantity: float = 0.0
    min_notional: float = 0.0
    fee_rate: float = .0005
    slippage_rate: float = .0001
    funding_rate_per_8h: float = 0.0
    monthly_profit_target_percent: float = 8.0  # Reporting only; never pauses trading.
    optimize: bool = False

SIGNAL_KEYS = ('breakout_lookback_bars', 'stop_atr_mult', 'max_hold_bars')
ENHANCEMENT_KEYS = ('breakout_buffer_atr', 'trend_ma_bars', 'min_efficiency_ratio', 'min_volume_ratio', 'min_atr_cost_ratio', 'max_signal_range_atr', 'trailing_stop_atr_mult', 'trailing_activation_r', 'cooldown_bars', 'max_entry_gap_atr', 'entry_score_min', 'failed_breakout_bars', 'breakeven_activation_r')
ENHANCEMENT_KEYS += ('max_atr_expansion', 'stagnation_exit_bars', 'stagnation_min_progress_r')
# Search only trading decisions. Capital, costs and exchange constraints live above.
FULL_PARAM_GRID = {
    'breakout_lookback_bars': [15, 20, 30, 45, 60, 90, 120],
    'stop_atr_mult': [1.25, 1.5, 1.75, 2., 2.25, 2.5, 3.],
    'max_hold_bars': [10, 15, 20, 30, 45, 60, 90, 120],
    'breakout_buffer_atr': [0.0, 0.1, 0.25, 0.5],
    'trend_ma_bars': [0, 50, 100, 200],
    'min_efficiency_ratio': [0.0, 0.2, 0.35, 0.5],
    'min_volume_ratio': [0.0, 1.0, 1.5, 2.0],
    'min_atr_cost_ratio': [0.0, 1.0, 2.0, 3.0],
    'max_signal_range_atr': [0.0, 2.0, 3.0, 4.0],
    'trailing_stop_atr_mult': [0.0, 1.5, 2.0, 3.0],
    'trailing_activation_r': [1.0, 2.0, 3.0],
    'cooldown_bars': [0, 5, 15, 30],
    'max_entry_gap_atr': [0.0, 0.25, 0.5, 1.0],
    'entry_score_min': [0.0, 40.0, 55.0, 70.0],
    'failed_breakout_bars': [0, 3, 6, 12],
    'breakeven_activation_r': [0.0, 1.0, 1.5, 2.0],
    'max_atr_expansion': [0.0, 1.5, 2.0, 3.0],
    'stagnation_exit_bars': [0, 10, 30, 60],
    'stagnation_min_progress_r': [0.25, 0.5, 1.0],
    'risk_per_trade': [0.001, 0.0025, 0.005],
    'max_gross_exposure': [1.0, 2.0, 3.0, 5.0],
    'leverage': [1.0, 2.0, 3.0, 5.0, 8.0, 10.0, 20.0, 30.0, 50.0, 75.0, 100.0],
    'trade_amount_percent': [0.05, 0.1, 0.25, 0.5, 1.0],
}
SIZING_KEYS = ('risk_per_trade', 'max_gross_exposure', 'leverage', 'trade_amount_percent')
# Same file-based focused workflow as MA; independent from the full grid.
FOCUSED_PARAM_GRID = json.loads(
    (Path(__file__).parent / 'param_grids' / 'pulse_focused.json').read_text(encoding='utf-8')
)
PHASE_A_GRID = {key: list(FULL_PARAM_GRID[key]) for key in SIGNAL_KEYS}
PARAMETER_PROFILES = {
    'quality': json.loads((Path(__file__).parent / 'param_grids/pulse_quality_1m.json').read_text(encoding='utf-8')),
    'focused': FOCUSED_PARAM_GRID,
    'full': FULL_PARAM_GRID,
    'signal': PHASE_A_GRID,
    'filters': {k: FULL_PARAM_GRID[k] for k in (*ENHANCEMENT_KEYS[:6], 'max_entry_gap_atr', 'entry_score_min', 'max_atr_expansion')},
    'exits': {k: FULL_PARAM_GRID[k] for k in ('max_hold_bars', *ENHANCEMENT_KEYS[6:9], 'failed_breakout_bars', 'breakeven_activation_r', 'stagnation_exit_bars', 'stagnation_min_progress_r')},
    'sizing': {k: FULL_PARAM_GRID[k] for k in SIZING_KEYS},
    'directional': {f'{side}_{key}': list(values)
                    for side in ('long', 'short')
                    for key, values in FULL_PARAM_GRID.items() if key in (*SIGNAL_KEYS, *ENHANCEMENT_KEYS, *SIZING_KEYS)}
                   | {'enable_long': [True], 'enable_short': [True]},
}
param_grid = FULL_PARAM_GRID
OPTIMIZER_DEFAULTS = {
    'profile': 'quality', 'auto_tests': 256, 'auto_validation_top': 64,
    'auto_stress_top': 32, 'auto_walk_forward_top': 16, 'auto_walk_forward_folds': 3,
    'auto_final_top': 16, 'auto_hall_size': 100, 'auto_advanced_min_candidates': 32,
    'auto_cycles': 4, 'snapshot_cycles': 4, 'snapshot_top': 8,
    'auto_halving_rungs': 0, 'auto_learning_target': 'profit-evidence',
    'auto_trade_count_policy': 'duration', 'min_trades': 30, 'max_drawdown': 25,
}
STAGED_PHASES = (('signal','signal'), ('filters','filters'), ('exits','exits'), ('sizing','sizing'))
EXECUTION_SCENARIOS = {'base': {}, 'adverse': {'fee_rate': .0007, 'slippage_rate': .0002},
                       'severe': {'fee_rate': .001, 'slippage_rate': .0005}}

def side_value(cfg, side, key):
    value = getattr(cfg, f'{side}_{key}')
    return getattr(cfg, key) if value is None else value

def build_strategy_config(tune=None):
    values = dict(tune or {})
    defaults = asdict(PulseConfig())
    unknown = set(values) - set(defaults)
    if unknown:
        raise ValueError('Unknown Pulse parameters: ' + ', '.join(sorted(unknown)))
    for key,value in values.items():
        base = key.split('_',1)[1] if key.startswith(('long_','short_')) else key
        default = defaults[base]
        if value is None and key != base:
            continue
        if isinstance(default,bool):
            if value not in (True,False,'true','false'):
                raise ValueError(f'{key} must be boolean')
            values[key] = value in (True,'true')
        else:
            number = float(value)
            if not math.isfinite(number) or (isinstance(default,int) and number != int(number)):
                raise ValueError(f'{key} must be finite and use the declared type')
            values[key] = int(number) if isinstance(default,int) else number
    cfg = PulseConfig(**values)
    if cfg.monthly_profit_target_percent < 0:
        raise ValueError('monthly_profit_target_percent must be nonnegative')
    if cfg.atr_period != 20:
        raise ValueError('Pulse fixes atr_period=20')
    for side in ('long','short'):
        if any(side_value(cfg,side,key) <= 0 for key in SIGNAL_KEYS):
            raise ValueError('Lookback, hold bars and ATR multiplier must be positive')
    for side in ('long', 'short'):
        if any(side_value(cfg, side, key) < 0 for key in ENHANCEMENT_KEYS):
            raise ValueError('Pulse filters, trailing settings and cooldown must be nonnegative')
        if side_value(cfg, side, 'min_efficiency_ratio') > 1:
            raise ValueError('min_efficiency_ratio must be in [0,1]')
        if side_value(cfg, side, 'entry_score_min') > 100:
            raise ValueError('entry_score_min must be in [0,100]')
        if side_value(cfg, side, 'trend_ma_bars') == 1:
            raise ValueError('trend_ma_bars must be zero (disabled) or at least 2')
    if cfg.balance <= 0 or not 0 < cfg.risk_per_trade <= 1 or cfg.max_gross_exposure <= 0:
        raise ValueError('Positive balance/exposure and risk in (0,1] required')
    if cfg.leverage < 1 or not 0 < cfg.trade_amount_percent <= 1:
        raise ValueError('Leverage >= 1 and trade_amount_percent in (0,1] required')
    if not 0 <= cfg.maintenance_margin_rate < 1 / cfg.leverage or not 0 <= cfg.liquidation_fee_rate < 1:
        raise ValueError('Invalid maintenance margin or liquidation fee')
    for side in ('long', 'short'):
        leverage = side_value(cfg, side, 'leverage')
        if (not 0 < side_value(cfg, side, 'risk_per_trade') <= 1
                or side_value(cfg, side, 'max_gross_exposure') <= 0
                or not 0 < side_value(cfg, side, 'trade_amount_percent') <= 1
                or leverage < 1 or cfg.maintenance_margin_rate >= 1 / leverage):
            raise ValueError(f'Invalid {side} risk, margin allocation or leverage')
    if not (cfg.enable_long or cfg.enable_short):
        raise ValueError('Enable at least one side')
    if min(cfg.quantity_step,cfg.min_quantity,cfg.min_notional,cfg.fee_rate,cfg.slippage_rate,cfg.funding_rate_per_8h) < 0 or max(cfg.fee_rate,cfg.slippage_rate) >= 1:
        raise ValueError('Invalid order constraints or costs')
    return cfg

def load_strategy_tune(path):
    assert_strategy_path(path, IDENTIFIER)
    return asdict(build_strategy_config(json.loads(Path(path).read_text(encoding='utf-8'))))

def is_valid_candidate(params):
    try:
        build_strategy_config(params)
        return True
    except (ValueError,TypeError):
        return False

def validate_parameter_grid(grid):
    allowed = {'enable_long', 'enable_short'} | set(SIGNAL_KEYS) | set(ENHANCEMENT_KEYS) | set(SIZING_KEYS) | {f'{side}_{key}' for side in ('long','short') for key in (*SIGNAL_KEYS, *ENHANCEMENT_KEYS, *SIZING_KEYS)}
    defaults = asdict(PulseConfig())
    if set(grid) - set(defaults):
        raise ValueError('Unknown Pulse grid parameters: ' + ', '.join(sorted(set(grid) - set(defaults))))
    for key,values in grid.items():
        if key not in allowed:
            raise ValueError('Pulse grids tune signal and sizing parameters only; capital and execution costs are fixed policies')
        if not values or any(not is_valid_candidate({key:value}) for value in values):
            raise ValueError(f'Invalid Pulse grid: {key}')

