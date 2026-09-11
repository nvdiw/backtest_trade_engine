"""Pulse / RollingRangeBreakout1m research hypothesis, not a profitability claim.

Prior-bar channels, SMA(TR,20), closed-bar signals and next-open execution.
Optional causal entry filters and close-updated ATR trailing. See docs/PULSE_GUIDE_FA.md.
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
from market_data import MarketDataSource, timeframe_label
from runtime_settings import add_runtime_arguments, configure_runtime, market_selection, runtime_session, add_chart_arguments, chart_options
from strategy_workspace import claim_output, output_session
from trade_engine import AccountState, TradeEngine
from pulse_diagnostics import sizing_metrics
from pulse_features import rolling_features, raw_breakouts, atr_expansion, quality_features, entry_quality_score, entry_filter_reason
from pulse_strategy_config import (
    PulseConfig, SIGNAL_KEYS, ENHANCEMENT_KEYS, FULL_PARAM_GRID, FOCUSED_PARAM_GRID, PHASE_A_GRID,
    PARAMETER_PROFILES, param_grid, STAGED_PHASES, EXECUTION_SCENARIOS,
    AUTO_DATE_DEFAULTS, OPTIMIZER_DEFAULTS, IDENTIFIER, side_value, build_strategy_config,
    load_strategy_tune, is_valid_candidate, validate_parameter_grid,
)

DISPLAY_NAME = 'Pulse / RollingRangeBreakout'
SYMBOL = None  # Optional explicit market label; otherwise derived from DATA_FILE.
STRATEGY_READY = True
DATA_FILE = Path(__file__).resolve().parent / 'data_candle' / 'btc_1m_data_2025_to_2026.csv'
TIMEFRAME = '1m'
ALLOW_CONTINUOUS_REFINEMENT = False
MINUTE_NS = 60_000_000_000

def required_indicator_warmup(config):
    cfg = config if isinstance(config,PulseConfig) else build_strategy_config(config)
    return max(side_value(cfg,'long',SIGNAL_KEYS[0]),side_value(cfg,'short',SIGNAL_KEYS[0]),20,
               80 if any(side_value(cfg, side, 'max_atr_expansion') for side in ('long', 'short')) else 0,
               side_value(cfg,'long','trend_ma_bars'),side_value(cfg,'short','trend_ma_bars')) + 1

def maximum_optimizer_warmup(grid,base_tune=None):
    values = [required_indicator_warmup(base_tune)]
    for key,options in grid.items():
        values.extend(required_indicator_warmup({**dict(base_tune or {}),key:value}) for value in options)
    return max(values)

_FEATURE_CACHE = OrderedDict()


def _prepare(market,cfg,now,interval_ns=MINUTE_NS):
    ns = market['history_open_time_ns']
    length = int(np.searchsorted(ns + interval_ns,now.value,side='right'))
    offset = market['warmup_offset']
    if length <= offset:
        raise ValueError('No completed candles after warmup')
    ns = ns[:length]
    prices = [market[f'history_{key}_prices'][:length] for key in ('open','high','low','close')]
    opens,high,low,close = prices
    stat = Path(market['data_file']).stat()
    key = (market['data_file'],stat.st_mtime_ns,stat.st_size,market['data_start'],market['end'],length,interval_ns,
           side_value(cfg,'long',SIGNAL_KEYS[0]),side_value(cfg,'short',SIGNAL_KEYS[0]),
           side_value(cfg,'long','trend_ma_bars'),side_value(cfg,'short','trend_ma_bars'))
    if key not in _FEATURE_CACHE:
        if (np.diff(ns) <= 0).any() or (np.diff(ns) % interval_ns != 0).any():
            raise ValueError('Pulse requires increasing timestamps aligned to the selected timeframe')
        if (not all(np.isfinite(x).all() and (x > 0).all() for x in prices) or
            (high < np.maximum(opens,close)).any() or (low > np.minimum(opens,close)).any() or (high < low).any()):
            raise ValueError('Invalid positive finite OHLC geometry')
        close_ns = pd.to_datetime(market['history_close_times'][:length],utc=True).asi8
        if not np.array_equal(close_ns,ns + interval_ns):
            raise ValueError('Close time must use the inclusive millisecond convention for the selected timeframe')
        volume = market['history_volume_prices'][:length]
        if not np.isfinite(volume).all() or (volume < 0).any():
            raise ValueError('Pulse requires finite nonnegative volume')
        _FEATURE_CACHE[key] = (*rolling_features(high,low,close,ns,key[-4],key[-3],interval_ns),
                              quality_features(close,volume,ns,key[-2],key[-1],interval_ns))
        _FEATURE_CACHE[key][4]['atr_expansion'] = atr_expansion(_FEATURE_CACHE[key][2], ns, interval_ns)
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
    selected,selected_timeframe = market_selection(data_file or DATA_FILE,timeframe or TIMEFRAME)
    source = MarketDataSource(selected,selected_timeframe)
    interval = source.interval()
    interval_ns = interval.value
    label = timeframe_label(interval)
    if end == 'latest':
        end = source.coverage()['end_exclusive']
    market = TradeEngine.load_market_data(start,end,
        warmup_candles=max(required_indicator_warmup(cfg),int(indicator_warmup_candles or 0)) if use_indicator_warmup else 0,
        data_file=selected,timeframe=label)
    now = pd.Timestamp.now(tz='UTC') if now is None else pd.Timestamp(now)
    now = now.tz_localize('UTC') if now.tzinfo is None else now.tz_convert('UTC')
    prices,ns,features,offset = _prepare(market,cfg,now,interval_ns)
    opens,high,low,close = [x[offset:] for x in prices]
    upper,lower,atr,ready = [x[offset:] for x in features[:4]]
    quality = {key: value[offset:] for key,value in features[4].items()}
    ns = ns[offset:]
    times = market['open_times'][:len(ns)]
    closes_at = market['close_times'][:len(ns)]
    if not cfg.optimize:
        claim_output(output_dir,IDENTIFIER,'backtest')
    engine = TradeEngine(first_balance=cfg.balance,tactical_balance=cfg.balance,
        market=market, strategy_name=DISPLAY_NAME, symbol=SYMBOL,
        optimize=cfg.optimize,verbose=verbose,write_trades=not cfg.optimize,write_excel=write_excel,
        output_dir=output_dir,fee_rate=cfg.fee_rate,slippage_rate=cfg.slippage_rate,
        monthly_profit_close_filter=False, monthly_loss_close_filter=False,
        funding_rate_per_8h=cfg.funding_rate_per_8h,
        maintenance_margin_rate=cfg.maintenance_margin_rate,
        liquidation_fee_rate=cfg.liquidation_fee_rate)
    account = AccountState(balance=cfg.balance)
    render_chart = not cfg.optimize and (write_chart or show_chart or chart_file is not None)
    chart_state = engine.create_chart_state(optimize=cfg.optimize, enabled=render_chart)
    stop_line = np.full(len(ns), np.nan) if render_chart else None
    position,stop,pending = None,None,None
    initial_risk, entry_boundary, stop_reason = 0.0, 0.0, 'stop_exit'
    best_close_gain = 0.0
    next_signal_index = {'long': 0, 'short': 0}
    armed = {'long':True,'short':True}
    events,diagnostics = [],Counter()
    month,month_equity,monthly_returns = str(times[0])[:7],cfg.balance,[]
    monthly_return_months = [month]
    trade_id = 0
    stop_context = {}
    def emit(kind,i,**details):
        reason_text = details.pop('reason_text', None)
        if trace:
            events.append(dict(kind=kind,bar=int(market['start']+i),**details))
        if render_chart and kind in ('entry', 'exit'):
            action = 'open' if kind == 'entry' else 'close'
            prefix = f'{details["side"]}_{action}'
            chart_state[prefix + '_points'].append((i, details['price']))
            chart_state[prefix + '_reasons'][i] = reason_text or json.dumps(details, indent=2)
            stop_line[i] = details['stop']
    def exit_position(i,reference,reason,timestamp,context=None):
        nonlocal position
        result = engine.close_at_reference(position,account,reference,timestamp,reason=reason)
        text = None
        if render_chart:
            from pulse_reason_text import exit_reason
            text = exit_reason(position,result,reason,stop=stop,initial_risk=initial_risk,
                               reference=reference,context=context or stop_context)
        emit('exit',i,side=position.side,price=result['close_price'],reason=reason,
             reference=float(reference),stop=float(stop),fees=result['total_fee'],reason_text=text)
        diagnostics[reason] += 1
        next_signal_index[position.side] = i + side_value(cfg,position.side,'cooldown_bars')
        position = None
    equity = cfg.balance
    for i in range(len(ns)):
        current_month = str(times[i])[:7]
        if current_month != month:
            # Reporting only: monthly returns never gate Pulse entries/exits.
            monthly_returns.append(equity/month_equity-1 if month_equity > 0 else 0)
            month,month_equity = current_month,equity
            monthly_return_months.append(month)
        gap = i > 0 and ns[i]-ns[i-1] != interval_ns
        exited = False
        if position and position.leverage > 1:
            check = engine.check_liquidation_long if position.side == 'long' else engine.check_liquidation_short
            liquidation = check(i, opens, times, position, account, reason_to_close='liquidation_exit')
            if liquidation.get('liquidated'):
                text = None
                if render_chart:
                    from pulse_reason_text import exit_reason
                    text = exit_reason(position,liquidation,'liquidation_exit',stop=stop,
                                       initial_risk=initial_risk)
                emit('exit', i, side=position.side, price=liquidation['close_price'],
                     reason='liquidation_exit', stop=float(stop), fees=liquidation['total_fee'],reason_text=text)
                diagnostics['liquidation_exit'] += 1
                next_signal_index[position.side] = i + side_value(cfg,position.side,'cooldown_bars')
                position, pending, exited = None, None, True
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
                exit_position(i,opens[i],stop_reason,times[i])
                exited = True
        if pending and pending['kind'] == 'exit' and position:
            exit_position(i,opens[i],pending.get('reason', 'time_exit'),times[i],pending.get('chart_context'))
            exited = True
        elif pending and pending['kind'] == 'entry' and position is None:
            side = pending['side']
            gap_limit = side_value(cfg, side, 'max_entry_gap_atr')
            adverse_gap = (opens[i] - pending['signal_close']) * (1 if side == 'long' else -1)
            if gap_limit and adverse_gap > gap_limit * pending['atr']:
                diagnostics['entry_gap_filter'] += 1
                emit('entry_rejected', i, side=side, reason='entry_gap_filter')
                pending = None
                # No fill is made; normal close-of-bar signal handling still runs.
            else:
                pending['accepted'] = True
        if pending and pending.get('accepted'):
            side = pending['side']
            trade_id += 1
            position,stop,rejected = engine.open_risk_position(i,opens,times,account,
                side=side,stop_distance=pending['distance'],risk_per_trade=side_value(cfg,side,'risk_per_trade'),
                max_gross_exposure=side_value(cfg,side,'max_gross_exposure'),quantity_step=cfg.quantity_step,
                leverage=side_value(cfg,side,'leverage'),trade_amount_percent=side_value(cfg,side,'trade_amount_percent'),
                min_quantity=cfg.min_quantity,min_notional=cfg.min_notional,trade_id=f'pulse_{trade_id:07d}')
            if rejected:
                diagnostics[rejected] += 1
                diagnostics[side + '_' + rejected] += 1
            else:
                sizing = engine.last_risk_sizing
                diagnostics[side + '_filled_entries'] += 1
                diagnostics[side + '_sizing_' + sizing['binding_limit']] += 1
                diagnostics[side + '_gross_exposure_sum'] += position.position_size * position.entry_price / sizing['account_equity']
                diagnostics[side + '_margin_fraction_sum'] += position.margin / sizing['account_equity']
                initial_risk, entry_boundary, stop_reason = pending['distance'], pending['boundary'], 'stop_exit'
                best_close_gain = 0.0
                stop_context = {}
                armed[side] = False
                text = None
                if render_chart:
                    from pulse_reason_text import entry_reason
                    text = entry_reason(cfg,position,pending['chart_signal'],balance=account.balance,fill_open=opens[i])
                emit('entry',i,side=side,price=position.entry_price,stop=stop,signal_atr=pending['atr'],
                     quantity=position.position_size,margin=position.margin,leverage=position.leverage,
                     signal_bar=pending['signal_bar'],reason_text=text)
        pending = None
        if position:
            reference = engine.protective_stop_reference(position.side,stop,opens[i],high[i],low[i])
            if reference is not None:
                exit_position(i,reference,stop_reason,closes_at[i])
                exited = True
        equity = account.balance + (engine.position_equity(position,close[i]) - engine.accrued_position_costs(position,closes_at[i]) if position else 0)
        engine.update_account_drawdown(account,equity)
        if render_chart:
            chart_state['chart_data'].append([i, account.balance + (position.margin if position else 0), equity])
            if position:
                stop_line[i] = stop
        if ready[i]:
            for direction, raw in (('long', close[i] > upper[i]), ('short', close[i] < lower[i])):
                if raw:
                    diagnostics[direction + '_raw_breakout_bars'] += 1
                    if position:
                        diagnostics[direction + '_blocked_by_open_position_bars'] += 1
            if close[i] <= upper[i]: armed['long'] = True
            if close[i] >= lower[i]: armed['short'] = True
        next_exists = i+1 < len(ns) and ns[i+1]-ns[i] == interval_ns
        if not next_exists: continue
        if position:
            # New stop is effective only from the NEXT candle. Never test it against
            # the current candle's already-observed extremes.
            multiplier = side_value(cfg,position.side,'trailing_stop_atr_mult')
            direction = 1 if position.side == 'long' else -1
            gain = direction * (close[i] - position.entry_price)
            best_close_gain = max(best_close_gain, gain)
            activation = side_value(cfg, position.side, 'breakeven_activation_r')
            if activation and gain >= initial_risk * activation:
                # Price covering modeled entry/exit fees and exit slippage.
                # Funding and adverse gaps can still produce a net loss.
                proposed = position.entry_price * (1 + direction * cfg.fee_rate) / (
                    (1 - direction * cfg.fee_rate) * (1 - direction * cfg.slippage_rate))
                if direction * (proposed - stop) > 0 and direction * (close[i] - proposed) > 0:
                    stop, stop_reason = proposed, 'breakeven_stop_exit'
                    if render_chart:
                        stop_context = dict(time=str(closes_at[i]),close=float(close[i]),
                                            activation_r=activation,gain_r=gain/initial_risk,
                                            effective_from=str(times[i+1]))
                    emit('stop_update', i, side=position.side, stop=float(stop),
                         effective_bar=int(market['start']+i+1), source='breakeven')
            if multiplier and np.isfinite(atr[i]) and gain >= initial_risk * side_value(cfg,position.side,'trailing_activation_r'):
                proposed = close[i] - direction * multiplier * atr[i]
                if proposed > 0 and direction * (proposed - stop) > 0:
                    stop, stop_reason = proposed, 'trailing_stop_exit'
                    if render_chart:
                        stop_context = dict(time=str(closes_at[i]),close=float(close[i]),atr=float(atr[i]),
                                            multiplier=multiplier,effective_from=str(times[i+1]))
                    emit('stop_update',i,side=position.side,stop=float(stop),effective_bar=int(market['start']+i+1))
            held_bars = i - position.entry_index + 1
            failure_window = side_value(cfg, position.side, 'failed_breakout_bars')
            if failure_window and held_bars <= failure_window and direction * (close[i] - entry_boundary) < 0:
                pending = {'kind': 'exit', 'reason': 'failed_breakout_exit'}
                emit('failed_breakout_signal', i, side=position.side, boundary=float(entry_boundary))
            elif (side_value(cfg, position.side, 'stagnation_exit_bars')
                  and held_bars >= side_value(cfg, position.side, 'stagnation_exit_bars')
                  and best_close_gain < initial_risk * side_value(cfg, position.side, 'stagnation_min_progress_r')):
                pending = {'kind': 'exit', 'reason': 'stagnation_exit'}
                emit('stagnation_signal', i, side=position.side,
                     best_progress_r=float(best_close_gain / initial_risk), held_bars=held_bars)
            elif held_bars >= side_value(cfg,position.side,'max_hold_bars'):
                pending = {'kind':'exit'}
                emit('time_signal',i,side=position.side)
            if render_chart and pending:
                pending['chart_context'] = dict(time=str(closes_at[i]),close=float(close[i]),held_bars=held_bars,
                    channel_boundary=entry_boundary,best_progress_r=best_close_gain/initial_risk,
                    maximum_hold_bars=side_value(cfg,position.side,'max_hold_bars'),
                    failure_window_bars=failure_window,
                    stagnation_bars=side_value(cfg,position.side,'stagnation_exit_bars'),
                    minimum_progress_r=side_value(cfg,position.side,'stagnation_min_progress_r'))
            continue
        if not ready[i] or not np.isfinite(atr[i]) or atr[i] <= 0: continue
        long_raw,short_raw = raw_breakouts(close[i],upper[i],lower[i])
        if long_raw and short_raw:
            diagnostics['invalid_dual_signal'] += 1
            continue
        side = 'long' if long_raw else 'short' if short_raw else None
        if side and armed[side] and getattr(cfg,f'enable_{side}'):
            rejection = ('cooldown' if i < next_signal_index[side] else
                         entry_filter_reason(cfg,side,i,close,high,low,upper,lower,atr,quality))
            if rejection:
                diagnostics[rejection] += 1
                diagnostics[side + '_' + rejection] += 1
                emit('entry_rejected',i,side=side,reason=rejection)
                continue
            pending = dict(kind='entry',side=side,atr=float(atr[i]),
                boundary=float(upper[i] if side == 'long' else lower[i]),
                signal_close=float(close[i]),
                distance=side_value(cfg,side,'stop_atr_mult')*float(atr[i]),signal_bar=int(market['start']+i))
            if render_chart:
                cost = close[i]*2*(cfg.fee_rate+cfg.slippage_rate)
                pending['chart_signal'] = {**pending, 'signal_time':str(times[i]), 'confirmed_at':str(closes_at[i]),
                    'efficiency':float(quality['efficiency'][i]),
                    'relative_volume':float(quality['relative_volume'][i]),
                    'trend':float(quality[side+'_trend'][i]),
                    'trend_previous':float(quality[side+'_trend_previous'][i]),
                    'atr_expansion':float(quality['atr_expansion'][i]),
                    'range_atr':float((high[i]-low[i])/atr[i]),
                    'atr_cost_ratio':float(atr[i]/cost) if cost else None,
                    'entry_score':entry_quality_score(side,i,close,high,low,upper,lower,atr,quality)}
            diagnostics[side + '_entry_signals'] += 1
            score_details = ({'entry_score': entry_quality_score(side, i, close, high, low, upper, lower, atr, quality)}
                             if side_value(cfg, side, 'entry_score_min') and trace else {})
            emit('signal',i,side=side,upper=float(upper[i]),lower=float(lower[i]),atr=float(atr[i]), **score_details)
    monthly_returns.append(equity/month_equity-1 if month_equity > 0 else 0)
    result = engine.finalize_account(account,first_balance=cfg.balance,
        open_positions=[position] if position else [],ending_mark_price=close[-1],
        start_time=times[0],end_time=closes_at[-1],monthly_returns=monthly_returns,accrue_open_costs=True,
        monthly_profit_target_percent=cfg.monthly_profit_target_percent,
        monthly_return_months=monthly_return_months,
        report_metadata={'Name':DISPLAY_NAME, **{k:v for k,v in asdict(cfg).items() if v is not None}},
        extra_metrics={'strategy_name':DISPLAY_NAME,'timeframe':label,
            'terminal_accounting':'mark_to_market_no_synthetic_exit',
            'funding_model':'fixed_charge_proxy_not_historical' if cfg.funding_rate_per_8h else 'excluded_no_historical_funding',
            'diagnostics':dict(diagnostics),
            **sizing_metrics(diagnostics)})
    if trace: result['events'] = events
    if not cfg.optimize:
        output = Path(output_dir)
        if render_chart:
            chart_file = Path(chart_file) if chart_file else output / 'chart.png'
            result['chart_file'] = str(chart_file)
        if render_chart:
            engine.render_strategy_chart(
                market=dict(open_prices=opens, high_prices=high, low_prices=low, close_prices=close,
                            open_times=times, close_times=closes_at),
                chart_state=chart_state, account=account, result=result,
                price_overlays={'Upper channel': np.where(ready, upper, np.nan),
                                'Lower channel': np.where(ready, lower, np.nan), 'Active stop': stop_line},
                oscillator_values=np.where(ready, atr, np.nan), oscillator_label='ATR20 SMA',
                title=DISPLAY_NAME, show=show_chart, save_path=chart_file, max_candles=plot_max_candles)
    return result

def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start')
    parser.add_argument('--end')
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--params-file')
    source.add_argument('--campaign', help='replay a ranked candidate using the stored market and stage dates')
    parser.add_argument('--params-source', choices=('config', 'best', 'file'), default=None,
                        help='optional: --params-file alone automatically selects file')
    parser.add_argument('--best-params', default='outputs/pulse/optimize/best_params.json')
    parser.add_argument('--rank', type=int, default=1)
    parser.add_argument('--stage', choices=('final', 'discovery', 'validation', 'stress'), default='final')
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
        if args.campaign and args.params_source:
            parser.error('--campaign already selects the parameter source; omit --params-source')
        if args.params_source == 'best':
            if args.params_file:
                parser.error('use --params-file FILE alone, or --params-source best')
            args.params_file = args.best_params
        elif args.params_source == 'file' and not args.params_file:
            parser.error('--params-source file requires --params-file FILE')
        elif args.params_source == 'config' and args.params_file:
            parser.error('use --params-file FILE alone, or --params-source config alone')
        replay = None
        if args.campaign:
            from pulse_replay import load_campaign_replay
            replay = load_campaign_replay(args.campaign, args.rank, args.stage)
            if not args.data_file:
                args.data_file = replay['data_file']
            if args.timeframe == 'auto':
                args.timeframe = replay['timeframe']
        args.start = args.start or (replay['start'] if replay else '2025-01-01')
        args.end = args.end or (replay['end'] if replay else '2025-01-03')
        configure_runtime(args,argv)
        tune = dict(replay['params']) if replay else load_strategy_tune(args.params_file) if args.params_file else {}
        for item in args.set:
            key,value = item.split('=',1)
            tune[key] = json.loads(value)
        tune['optimize'] = False
        if not args.quiet:
            print(f"Pulse parameters: {args.campaign or args.params_file or 'defaults (no parameter file selected)'} | {args.start} -> {args.end}")
        bound = lambda value: int(value) if value.isdigit() else value
        result = pulse_strategy(tune,bound(args.start),bound(args.end),output_dir=args.output_dir,
                                use_indicator_warmup=replay['use_indicator_warmup'] if replay else True,
                                write_excel=not args.no_excel,trace=args.trace,verbose=not args.quiet,
                                write_chart=not args.no_chart, **chart_options(args), plot_max_candles=args.plot_max_candles)
        if replay:
            keys = ('long_trades', 'short_trades', 'closed_trades', 'total_profit_percent', 'maximum_drawdown')
            comparison = dict(candidate_id=replay['candidate_id'], source=replay['source'], stage=replay['stage'],
                              actual={key: result.get(key) for key in keys},
                              expected={key: replay['expected'].get(key) for key in keys})
            comparison['matches'] = all(comparison['expected'][key] is not None and np.isclose(
                comparison['actual'][key], comparison['expected'][key], rtol=1e-8, atol=1e-6) for key in keys)
            (Path(args.output_dir) / 'replay_comparison.json').write_text(json.dumps(comparison, indent=2), encoding='utf-8')
            if not args.quiet:
                print('Campaign replay: ' + ('MATCH' if comparison['matches'] else 'DIFFERENT (check overrides, data or code changes)'))
    except (ValueError,TypeError,OSError) as exc:
        parser.error(str(exc))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
