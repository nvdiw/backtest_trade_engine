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
    atr_period: int = 20
    enable_long: bool = True
    enable_short: bool = True
    balance: float = 1000.0
    risk_per_trade: float = .0025
    max_gross_exposure: float = 1.0
    quantity_step: float = 0.0
    min_quantity: float = 0.0
    min_notional: float = 0.0
    fee_rate: float = .0005
    slippage_rate: float = .0001
    funding_rate_per_8h: float = 0.0
    optimize: bool = False

SIGNAL_KEYS = ('breakout_lookback_bars', 'stop_atr_mult', 'max_hold_bars')
FULL_PARAM_GRID = {
    'breakout_lookback_bars': [15, 20, 30, 45, 60, 90, 120],
    'stop_atr_mult': [1.25, 1.5, 1.75, 2., 2.25, 2.5, 3.],
    'max_hold_bars': [10, 15, 20, 30, 45, 60, 90, 120],
}
# Same file-based focused workflow as MA; independent from the full grid.
FOCUSED_PARAM_GRID = json.loads(
    (Path(__file__).parent / 'param_grids' / 'pulse_focused.json').read_text(encoding='utf-8')
)
PHASE_A_GRID = FULL_PARAM_GRID  # Compatibility name for the Phase A research grid.
PARAMETER_PROFILES = {
    'focused': FOCUSED_PARAM_GRID,
    'full': FULL_PARAM_GRID,
    'signal': FULL_PARAM_GRID,
    'directional': {f'{side}_{key}': list(values)
                    for side in ('long', 'short')
                    for key, values in FULL_PARAM_GRID.items()},
}
param_grid = FULL_PARAM_GRID
STAGED_PHASES = (('signal','signal'),)
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
    if cfg.atr_period != 20:
        raise ValueError('Pulse fixes atr_period=20')
    for side in ('long','short'):
        if any(side_value(cfg,side,key) <= 0 for key in SIGNAL_KEYS):
            raise ValueError('Lookback, hold bars and ATR multiplier must be positive')
    if cfg.balance <= 0 or not 0 < cfg.risk_per_trade <= 1 or not 0 < cfg.max_gross_exposure <= 1:
        raise ValueError('Positive balance, risk in (0,1] and unlevered exposure in (0,1] required')
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
    allowed = set(SIGNAL_KEYS) | {f'{side}_{key}' for side in ('long','short') for key in SIGNAL_KEYS}
    if set(grid) - allowed:
        raise ValueError('Pulse grids tune signal parameters only; risk and execution costs are fixed policies')
    for key,values in grid.items():
        if not values or any(not is_valid_candidate({key:value}) for value in values):
            raise ValueError(f'Invalid Pulse grid: {key}')

