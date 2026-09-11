"""Readable Pulse chart details built from frozen signal and execution data."""
from generate_reason_text import generate_entry_reason_text, generate_close_reason_text
from pulse_strategy_config import side_value


def number(value):
    try:
        import math
        return f'{float(value):,.4f}' if math.isfinite(float(value)) else 'N/A'
    except (TypeError, ValueError):
        return 'N/A'


def entry_reason(cfg, position, signal, *, balance, fill_open):
    side = position.side
    setting = lambda key: side_value(cfg, side, key)
    direction = 1 if side == 'long' else -1
    atr = signal['atr']
    boundary = signal['boundary']
    stop = position.entry_price - direction * signal['distance']
    def floor(label, value, minimum):
        return f'{label}: {number(value)} >= {number(minimum)} (passed)' if minimum else f'{label}: {number(value)} (filter disabled)'
    lines = [f'PULSE {side.upper()} ENTRY - channel breakout',
             f"Signal candle: {signal['signal_time']} | bar {signal['signal_bar']}",
             f"Confirmed at: {signal['confirmed_at']} (candle close boundary)",
             'Signal confirmed at candle close; filled at the next candle open.',
             f"Channel lookback: {setting('breakout_lookback_bars')} bars",
             f"Signal close: {number(signal['signal_close'])} | boundary: {number(boundary)}",
             f"Breakout distance / ATR: {number(direction * (signal['signal_close']-boundary)/atr)} "
             f"> {number(setting('breakout_buffer_atr'))}",
             f'ATR20 SMA: {number(atr)}', '', 'ENTRY FILTERS',
             floor('Efficiency', signal['efficiency'], setting('min_efficiency_ratio')),
             floor('Relative volume', signal['relative_volume'], setting('min_volume_ratio')),
             floor('Entry score / 100', signal['entry_score'], setting('entry_score_min')),
             floor('ATR / estimated round-trip cost', signal['atr_cost_ratio'], setting('min_atr_cost_ratio'))]
    if setting('trend_ma_bars'):
        lines += [f"Trend MA ({setting('trend_ma_bars')} bars): {number(signal['trend'])}; "
                  f"previous: {number(signal['trend_previous'])}; price and slope aligned with {side} (passed)"]
    else:
        lines += ['Trend MA: disabled']
    for label, value, limit in (
        ('Signal range / ATR', signal['range_atr'], setting('max_signal_range_atr')),
        ('ATR expansion', signal['atr_expansion'], setting('max_atr_expansion')),
        ('Adverse opening gap / ATR', direction*(fill_open-signal['signal_close'])/atr,
         setting('max_entry_gap_atr'))):
        lines += [f'{label}: {number(value)} <= {number(limit)} (passed)' if limit else f'{label}: {number(value)} (filter disabled)']
    lines += ['', 'RISK AND EXIT PLAN',
              f"Initial stop: {number(stop)} | stop distance: {number(signal['distance'])} "
              f"({number(setting('stop_atr_mult'))} ATR)",
              f"Risk budget: {number(setting('risk_per_trade')*100)}% | "
              f"filled stop risk: {number(position.position_size*signal['distance'])} (before costs/gaps)",
              f"Exposure cap: {number(setting('max_gross_exposure'))}x | margin allocation cap: "
              f"{number(setting('trade_amount_percent')*100)}%",
              f"Maximum hold: {setting('max_hold_bars')} bars",
              f"Trailing: {number(setting('trailing_stop_atr_mult'))} ATR; activate at {number(setting('trailing_activation_r'))} R (0 ATR disables)",
              f"Breakeven activation: {number(setting('breakeven_activation_r'))} R (0 disables)",
              f"Failed breakout window: {setting('failed_breakout_bars')} bars (0 disables)",
              f"Stagnation: {setting('stagnation_exit_bars')} bars; minimum progress {number(setting('stagnation_min_progress_r'))} R (0 bars disables)", '']
    execution = position.to_dict()
    execution.update(position_value=position.position_size*position.entry_price, balance=balance)
    return '\n'.join(lines) + generate_entry_reason_text(position.trade_id, execution)


EXIT_DESCRIPTIONS = {
    'stop_exit': 'Initial protective stop reached; gaps can fill beyond the stop.',
    'trailing_stop_exit': 'ATR trailing stop reached; the stop was set using an earlier closed candle.',
    'breakeven_stop_exit': 'Cost-adjusted breakeven stop reached; gaps and funding can still cause a loss.',
    'time_exit': 'Maximum holding period reached at the prior close; exit at next open.',
    'failed_breakout_exit': 'Price returned inside the entry channel within the failure window; exit at next open.',
    'stagnation_exit': 'Holding threshold reached without the required favorable close-price progress; exit at next open.',
    'data_gap_exit': 'Missing candles detected; close using the conservative gap reference.',
    'liquidation_exit': 'Liquidation threshold reached; liquidation takes precedence over other exits.',
}


def exit_reason(position, result, reason, *, stop, initial_risk, reference=None, context=None):
    context = context or {}
    lines = [f'PULSE {position.side.upper()} EXIT - {reason}', EXIT_DESCRIPTIONS.get(reason, reason),
             f'Entry price: {number(position.entry_price)} | entry time: {position.open_time_value}',
             f'Active stop: {number(stop)} | execution reference: {number(reference)}',
             f'Initial risk per unit (1R): {number(initial_risk)}']
    direction = 1 if position.side == 'long' else -1
    if initial_risk > 0:
        lines += [f"Realized price move: {number(direction*(result['close_price']-position.entry_price)/initial_risk)} R (before costs)"]
    if context:
        lines += ['', 'DECISION AT PRIOR CLOSE']
        lines += [f'{key.replace("_", " ").title()}: {value}' for key, value in context.items()]
    execution = {**position.to_dict(), **result}
    return '\n'.join(lines) + '\n\n' + generate_close_reason_text(position.trade_id, execution)
