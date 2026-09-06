"""Pulse / RollingRangeBreakout1m research hypothesis, not a profitability claim.

Prior-bar channels, SMA(TR,20), closed-bar signals and next-open execution.
Initial ATR stop and time exit only. See PULSE_GUIDE_FA.md for assumptions.
"""
from __future__ import annotations
import argparse
from collections import OrderedDict, Counter
from dataclasses import asdict
import json
import sys
from pathlib import Path
import numpy as np
import pandas as pd
from market_data import MarketDataSource
from runtime_settings import add_runtime_arguments, configure_runtime, market_selection, runtime_session, add_chart_arguments, chart_options
from strategy_workspace import claim_output, output_session
from trade_engine import AccountState, TradeEngine
from pulse_strategy_config import (
    PulseConfig, SIGNAL_KEYS, FULL_PARAM_GRID, FOCUSED_PARAM_GRID, PHASE_A_GRID,
    PARAMETER_PROFILES, param_grid, STAGED_PHASES, EXECUTION_SCENARIOS,
    AUTO_DATE_DEFAULTS, IDENTIFIER, side_value, build_strategy_config,
    load_strategy_tune, is_valid_candidate, validate_parameter_grid,
)

DISPLAY_NAME = 'Pulse / RollingRangeBreakout1m'
STRATEGY_READY = True
DATA_FILE = Path(__file__).resolve().parent / 'data_candle' / 'btc_1m_data_2025_to_2026.csv'
TIMEFRAME = '1m'
ALLOW_CONTINUOUS_REFINEMENT = False
MINUTE_NS = 60_000_000_000

def required_indicator_warmup(config):
    cfg = config if isinstance(config,PulseConfig) else build_strategy_config(config)
    return max(side_value(cfg,'long',SIGNAL_KEYS[0]),side_value(cfg,'short',SIGNAL_KEYS[0]),20) + 1

def maximum_optimizer_warmup(grid,base_tune=None):
    values = [required_indicator_warmup(base_tune)]
    for key,options in grid.items():
        values.extend(required_indicator_warmup({**dict(base_tune or {}),key:value}) for value in options)
    return max(values)

_FEATURE_CACHE = OrderedDict()

def rolling_features(high,low,close,times_ns,n_long,n_short):
    length = len(close)
    segment_start = np.maximum.accumulate(np.where(np.r_[True,np.diff(times_ns) != MINUTE_NS],np.arange(length),0))
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

def _prepare(market,cfg,now):
    ns = market['history_open_time_ns']
    length = int(np.searchsorted(ns + MINUTE_NS,now.value,side='right'))
    offset = market['warmup_offset']
    if length <= offset:
        raise ValueError('No completed candles after warmup')
    ns = ns[:length]
    prices = [market[f'history_{key}_prices'][:length] for key in ('open','high','low','close')]
    opens,high,low,close = prices
    stat = Path(market['data_file']).stat()
    key = (market['data_file'],stat.st_mtime_ns,stat.st_size,market['data_start'],market['end'],length,
           side_value(cfg,'long',SIGNAL_KEYS[0]),side_value(cfg,'short',SIGNAL_KEYS[0]))
    if key not in _FEATURE_CACHE:
        if (np.diff(ns) <= 0).any() or (np.diff(ns) % MINUTE_NS != 0).any():
            raise ValueError('Pulse requires increasing one-minute timestamps; missing minutes are allowed')
        if (not all(np.isfinite(x).all() and (x > 0).all() for x in prices) or
            (high < np.maximum(opens,close)).any() or (low > np.minimum(opens,close)).any() or (high < low).any()):
            raise ValueError('Invalid positive finite OHLC geometry')
        close_ns = pd.to_datetime(market['history_close_times'][:length],utc=True).asi8
        if not np.array_equal(close_ns,ns + MINUTE_NS):
            raise ValueError('Close time must use the inclusive one-minute millisecond convention')
        _FEATURE_CACHE[key] = rolling_features(high,low,close,ns,key[-2],key[-1])
        if len(_FEATURE_CACHE) > 8:
            _FEATURE_CACHE.popitem(last=False)
    _FEATURE_CACHE.move_to_end(key)
    return prices,ns,_FEATURE_CACHE[key],offset

@output_session
def pulse_strategy(tune=None,start='2025-01-01',end='latest',*,use_indicator_warmup=True,
                   indicator_warmup_candles=None,research=False,output_dir='outputs/pulse/backtest',
                   data_file=None,timeframe=None,write_excel=True,trace=False,now=None,
                   show_chart=None,chart_file=None,write_chart=True,plot_max_candles=500,verbose=None):
    cfg = build_strategy_config(tune)
    verbose = (True if verbose is None else bool(verbose)) and not cfg.optimize
    if show_chart is None:
        show_chart = not cfg.optimize
    selected,_ = market_selection(data_file or DATA_FILE,timeframe or TIMEFRAME)
    source = MarketDataSource(selected,'1m')
    source.interval()
    if end == 'latest':
        end = source.coverage()['end_exclusive']
    market = TradeEngine.load_market_data(start,end,
        warmup_candles=max(required_indicator_warmup(cfg),int(indicator_warmup_candles or 0)) if use_indicator_warmup else 0,
        data_file=selected,timeframe='1m')
    now = pd.Timestamp.now(tz='UTC') if now is None else pd.Timestamp(now)
    now = now.tz_localize('UTC') if now.tzinfo is None else now.tz_convert('UTC')
    prices,ns,features,offset = _prepare(market,cfg,now)
    opens,high,low,close = [x[offset:] for x in prices]
    upper,lower,atr,ready = [x[offset:] for x in features]
    ns = ns[offset:]
    times = market['open_times'][:len(ns)]
    closes_at = market['close_times'][:len(ns)]
    if not cfg.optimize:
        claim_output(output_dir,IDENTIFIER,'backtest')
    engine = TradeEngine(first_balance=cfg.balance,tactical_balance=cfg.balance,
        optimize=cfg.optimize,verbose=verbose,write_trades=not cfg.optimize,write_excel=write_excel,
        output_dir=output_dir,fee_rate=cfg.fee_rate,slippage_rate=cfg.slippage_rate,
        funding_rate_per_8h=cfg.funding_rate_per_8h)
    account = AccountState(balance=cfg.balance)
    render_chart = not cfg.optimize and (write_chart or show_chart or chart_file is not None)
    chart_state = engine.create_chart_state(optimize=cfg.optimize, enabled=render_chart)
    stop_line = np.full(len(ns), np.nan) if render_chart else None
    position,stop,pending = None,None,None
    armed = {'long':True,'short':True}
    events,diagnostics = [],Counter()
    month,month_equity,monthly_returns = str(times[0])[:7],cfg.balance,[]
    trade_id = 0
    def emit(kind,i,**details):
        if trace:
            events.append(dict(kind=kind,bar=int(market['start']+i),**details))
        if render_chart and kind in ('entry', 'exit'):
            action = 'open' if kind == 'entry' else 'close'
            prefix = f'{details["side"]}_{action}'
            chart_state[prefix + '_points'].append((i, details['price']))
            chart_state[prefix + '_reasons'][i] = json.dumps(details, indent=2)
            stop_line[i] = details['stop']
    def exit_position(i,reference,reason,timestamp):
        nonlocal position
        result = engine.close_at_reference(position,account,reference,timestamp,reason=reason)
        emit('exit',i,side=position.side,price=result['close_price'],reason=reason,
             reference=float(reference),stop=float(stop),fees=result['total_fee'])
        diagnostics[reason] += 1
        position = None
    equity = cfg.balance
    for i in range(len(ns)):
        current_month = str(times[i])[:7]
        if current_month != month:
            monthly_returns.append(equity/month_equity-1 if month_equity > 0 else 0)
            month,month_equity = current_month,equity
        gap = i > 0 and ns[i]-ns[i-1] != MINUTE_NS
        exited = False
        if gap:
            diagnostics['data_gap'] += 1
            pending = None
            armed = {'long':True,'short':True}
            if position:
                reference = engine.protective_stop_reference(position.side,stop,opens[i],high[i],low[i],data_gap=True)
                exit_position(i,reference,'data_gap_exit',times[i])
                exited = True
        if position and not exited:
            breached = opens[i] <= stop if position.side == 'long' else opens[i] >= stop
            if breached:
                exit_position(i,opens[i],'stop_exit',times[i])
                exited = True
        if pending and pending['kind'] == 'exit' and position:
            exit_position(i,opens[i],'time_exit',times[i])
            exited = True
        elif pending and pending['kind'] == 'entry' and position is None:
            side = pending['side']
            trade_id += 1
            position,stop,rejected = engine.open_risk_position(i,opens,times,account,
                side=side,stop_distance=pending['distance'],risk_per_trade=cfg.risk_per_trade,
                max_gross_exposure=cfg.max_gross_exposure,quantity_step=cfg.quantity_step,
                min_quantity=cfg.min_quantity,min_notional=cfg.min_notional,trade_id=f'pulse_{trade_id:07d}')
            if rejected:
                diagnostics[rejected] += 1
            else:
                armed[side] = False
                emit('entry',i,side=side,price=position.entry_price,stop=stop,signal_atr=pending['atr'],
                     quantity=position.position_size,signal_bar=pending['signal_bar'])
        pending = None
        if position:
            reference = engine.protective_stop_reference(position.side,stop,opens[i],high[i],low[i])
            if reference is not None:
                exit_position(i,reference,'stop_exit',closes_at[i])
                exited = True
        equity = account.balance + (engine.position_equity(position,close[i]) - engine.accrued_position_costs(position,closes_at[i]) if position else 0)
        engine.update_account_drawdown(account,equity)
        if render_chart:
            chart_state['chart_data'].append([i, account.balance + (position.margin if position else 0), equity])
            if position:
                stop_line[i] = stop
        if ready[i]:
            if close[i] <= upper[i]: armed['long'] = True
            if close[i] >= lower[i]: armed['short'] = True
        next_exists = i+1 < len(ns) and ns[i+1]-ns[i] == MINUTE_NS
        if not next_exists: continue
        if position:
            if i-position.entry_index+1 >= side_value(cfg,position.side,'max_hold_bars'):
                pending = {'kind':'exit'}
                emit('time_signal',i,side=position.side)
            continue
        if not ready[i] or not np.isfinite(atr[i]) or atr[i] <= 0: continue
        long_raw,short_raw = raw_breakouts(close[i],upper[i],lower[i])
        if long_raw and short_raw:
            diagnostics['invalid_dual_signal'] += 1
            continue
        side = 'long' if long_raw else 'short' if short_raw else None
        if side and armed[side] and getattr(cfg,f'enable_{side}'):
            pending = dict(kind='entry',side=side,atr=float(atr[i]),
                distance=side_value(cfg,side,'stop_atr_mult')*float(atr[i]),signal_bar=int(market['start']+i))
            emit('signal',i,side=side,upper=float(upper[i]),lower=float(lower[i]),atr=float(atr[i]))
    monthly_returns.append(equity/month_equity-1 if month_equity > 0 else 0)
    result = engine.finalize_account(account,first_balance=cfg.balance,
        open_positions=[position] if position else [],ending_mark_price=close[-1],
        start_time=times[0],end_time=closes_at[-1],monthly_returns=monthly_returns,accrue_open_costs=True,
        report_metadata={'Name':DISPLAY_NAME, **{k:v for k,v in asdict(cfg).items() if v is not None}},
        extra_metrics={'strategy_name':DISPLAY_NAME,'timeframe':'1m',
            'terminal_accounting':'mark_to_market_no_synthetic_exit',
            'funding_model':'fixed_charge_proxy_not_historical' if cfg.funding_rate_per_8h else 'excluded_no_historical_funding',
            'diagnostics':dict(diagnostics)})
    if trace: result['events'] = events
    if not cfg.optimize:
        output = Path(output_dir)
        if render_chart:
            chart_file = Path(chart_file) if chart_file else output / 'chart.png'
            result['chart_file'] = str(chart_file)
        engine.save_run_metadata(result, asdict(cfg))
        if render_chart:
            engine.render_strategy_chart(
                market=dict(open_prices=opens, high_prices=high, low_prices=low, close_prices=close,
                            open_times=times, close_times=closes_at),
                chart_state=chart_state, account=account, result=result,
                price_overlays={'Upper channel': np.where(ready, upper, np.nan),
                                'Lower channel': np.where(ready, lower, np.nan), 'Initial stop': stop_line},
                oscillator_values=np.where(ready, atr, np.nan), oscillator_label='ATR20 SMA',
                title='Pulse | BTC 1m', show=show_chart, save_path=chart_file, max_candles=plot_max_candles)
    return result

def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start',default='2025-01-01')
    parser.add_argument('--end',default='2025-01-03')
    parser.add_argument('--params-file')
    parser.add_argument('--set',action='append',default=[],metavar='KEY=VALUE')
    parser.add_argument('--output-dir',default='outputs/pulse/backtest')
    parser.add_argument('--no-excel',action='store_true')
    parser.add_argument('--quiet', action='store_true', help='suppress trade and final console logs; keep output files')
    add_chart_arguments(parser)
    parser.add_argument('--plot-max-candles', type=int, default=500)
    parser.add_argument('--trace',action='store_true')
    add_runtime_arguments(parser)
    return parser

@runtime_session
def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        configure_runtime(args,argv)
        tune = load_strategy_tune(args.params_file) if args.params_file else {}
        for item in args.set:
            key,value = item.split('=',1)
            tune[key] = json.loads(value)
        tune['optimize'] = False
        bound = lambda value: int(value) if value.isdigit() else value
        result = pulse_strategy(tune,bound(args.start),bound(args.end),output_dir=args.output_dir,
                                write_excel=not args.no_excel,trace=args.trace,verbose=not args.quiet,
                                write_chart=not args.no_chart, **chart_options(args), plot_max_candles=args.plot_max_candles)
    except (ValueError,TypeError,OSError) as exc:
        parser.error(str(exc))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
