"""Causal Pulse features and entry filters; signal formulas preserved."""
import numpy as np
import pandas as pd
from pulse_strategy_config import side_value

MINUTE_NS = 60_000_000_000

def rolling_features(high,low,close,times_ns,n_long,n_short,interval_ns=MINUTE_NS):
    length = len(close)
    segment_start = np.maximum.accumulate(np.where(np.r_[True,np.diff(times_ns) != interval_ns],np.arange(length),0))
    count = np.arange(length) - segment_start + 1
    previous = np.r_[np.nan,close[:-1]]
    tr = np.maximum.reduce([high-low,np.abs(high-previous),np.abs(low-previous)])
    atr = pd.Series(tr).rolling(20,min_periods=20).mean().to_numpy()
    upper = pd.Series(high).shift(1).rolling(n_long,min_periods=n_long).max().to_numpy()
    lower = pd.Series(low).shift(1).rolling(n_short,min_periods=n_short).min().to_numpy()
    ready = count >= max(n_long,n_short,20) + 1
    return upper,lower,atr,ready

def raw_breakouts(close,upper,lower):
    return bool(close > upper),bool(close < lower)

def atr_expansion(atr, times_ns, interval_ns=MINUTE_NS):
    """Current ATR / previous 60 ATRs; require entirely contiguous history."""
    average = pd.Series(atr).shift(1).rolling(60, min_periods=60).mean().to_numpy()
    ratio = np.divide(atr, average, out=np.full(len(atr), np.nan), where=average > 0)
    count = np.arange(len(atr)) - np.maximum.accumulate(
        np.where(np.r_[True, np.diff(times_ns) != interval_ns], np.arange(len(atr)), 0)) + 1
    ratio[count < 81] = np.nan
    return ratio

def quality_features(close, volume, times_ns, trend_long, trend_short, interval_ns=MINUTE_NS):
    """Closed-bar features with finite windows and no pre-gap history leakage."""
    count = np.arange(len(close)) - np.maximum.accumulate(
        np.where(np.r_[True, np.diff(times_ns) != interval_ns], np.arange(len(close)), 0)) + 1
    series = pd.Series(close)
    path = series.diff().abs().rolling(20, min_periods=20).sum().to_numpy()
    displacement = series.diff(20).abs().to_numpy()
    efficiency = np.divide(displacement, path, out=np.zeros(len(close)), where=path > 0)
    efficiency[count < 21] = np.nan
    average_volume = pd.Series(volume).shift(1).rolling(20, min_periods=20).mean().to_numpy()
    relative_volume = np.divide(volume, average_volume, out=np.zeros(len(close)), where=average_volume > 0)
    relative_volume[count < 21] = np.nan
    result = {'efficiency': efficiency, 'relative_volume': relative_volume}
    for side, period in (('long', trend_long), ('short', trend_short)):
        average = series.rolling(period, min_periods=period).mean().to_numpy() if period else np.full(len(close), np.nan)
        previous = np.r_[np.nan, average[:-1]]
        average[count < period + 1] = np.nan
        previous[count < period + 1] = np.nan
        result[side + '_trend'] = average
        result[side + '_trend_previous'] = previous
    return result

def entry_quality_score(side, i, close, high, low, upper, lower, atr, quality):
    """0..100, four equal closed-bar confirmations; a heuristic, not a probability."""
    direction = 1 if side == 'long' else -1
    boundary = upper[i] if side == 'long' else lower[i]
    span = high[i] - low[i]
    location = ((close[i] - low[i]) if side == 'long' else (high[i] - close[i])) / span if span > 0 else 0.
    strength = direction * (close[i] - boundary) / atr[i] if atr[i] > 0 else 0.
    components = np.asarray([location, strength, quality['efficiency'][i],
                             quality['relative_volume'][i] / 2.], dtype=float)
    if not np.isfinite(components).all():
        return 0.
    return float(np.clip(components, 0., 1.).sum() * 25.)

def entry_filter_reason(cfg, side, i, close, high, low, upper, lower, atr, quality):
    """Return one explainable rejection, using information available at signal close."""
    setting = lambda key: side_value(cfg, side, key)
    direction = 1 if side == 'long' else -1
    boundary = upper[i] if side == 'long' else lower[i]
    if direction * (close[i] - boundary) <= setting('breakout_buffer_atr') * atr[i]:
        return 'breakout_buffer'
    if setting('trend_ma_bars'):
        trend, previous = quality[side + '_trend'][i], quality[side + '_trend_previous'][i]
        if not np.isfinite(trend + previous) or direction * (close[i] - trend) <= 0 or direction * (trend - previous) < 0:
            return 'trend_filter'
    for key, feature in (('min_efficiency_ratio', 'efficiency'), ('min_volume_ratio', 'relative_volume')):
        if setting(key) and (not np.isfinite(quality[feature][i]) or quality[feature][i] < setting(key)):
            return key
    estimated_cost = close[i] * 2 * (cfg.fee_rate + cfg.slippage_rate)
    if atr[i] < setting('min_atr_cost_ratio') * estimated_cost:
        return 'cost_filter'
    if setting('max_signal_range_atr') and high[i] - low[i] > setting('max_signal_range_atr') * atr[i]:
        return 'signal_spike_filter'
    if setting('entry_score_min') and entry_quality_score(side, i, close, high, low, upper, lower, atr, quality) < setting('entry_score_min'):
        return 'entry_score_filter'
    expansion_limit = setting('max_atr_expansion')
    if expansion_limit and (not np.isfinite(quality['atr_expansion'][i]) or quality['atr_expansion'][i] > expansion_limit):
        return 'volatility_expansion_filter'
    return None
