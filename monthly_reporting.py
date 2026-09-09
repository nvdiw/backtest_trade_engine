"""Calendar-aware monthly target reporting, independent of trading controls."""
import math
import pandas as pd

MONTHLY_COLUMNS = [
    'backtest_days', 'backtest_months_30d', 'calendar_months',
    'monthly_observed_months', 'monthly_full_months', 'monthly_partial_months',
    'monthly_positive_months', 'monthly_loss_months', 'monthly_flat_months',
    'monthly_profit_target_percent', 'monthly_target_met_months',
    'monthly_target_missed_months', 'monthly_target_unknown_months',
    'monthly_target_success_percent',
    'monthly_profit_stop_months',
]


def monthly_report(start, end, returns, target=8.0, month_labels=None):
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    if start.tzinfo:
        start = start.tz_convert('UTC').tz_localize(None)
    if end.tzinfo:
        end = end.tz_convert('UTC').tz_localize(None)
    target = float(target)
    if not math.isfinite(target) or target < 0 or end < start:
        raise ValueError('Invalid monthly reporting target or date bounds')
    # End is exclusive, as returned by the market loader.
    months = pd.period_range(start, end - pd.Timedelta(nanoseconds=1), freq='M') if end > start else []
    values = list(returns or [])
    labels = list(month_labels) if month_labels is not None else [str(month) for month in months]
    mapped = dict(zip(labels, values))
    rows = []
    for month in months:
        left, right = month.start_time, (month + 1).start_time
        observed_start, observed_end = max(start, left), min(end, right)
        raw = mapped.get(str(month))
        value = float(raw) * 100 if raw is not None and math.isfinite(float(raw)) else None
        rows.append({'month': str(month), 'observed_start': str(observed_start),
                     'observed_end_exclusive': str(observed_end),
                     'observed_days': (observed_end - observed_start).total_seconds() / 86400,
                     'partial_month': observed_start > left or observed_end < right,
                     'net_return_percent': value, 'target_percent': target,
                     'target_status': 'UNKNOWN' if value is None else 'MET' if value >= target else 'MISSED'})
    known = [row for row in rows if row['net_return_percent'] is not None]
    met = sum(row['target_status'] == 'MET' for row in rows)
    days = (end - start).total_seconds() / 86400
    summary = dict(zip(MONTHLY_COLUMNS, [
        days, days / 30, len(rows), len(known),
        sum(not row['partial_month'] for row in rows), sum(row['partial_month'] for row in rows),
        sum(row['net_return_percent'] > 0 for row in known),
        sum(row['net_return_percent'] < 0 for row in known),
        sum(row['net_return_percent'] == 0 for row in known), target, met,
        len(known) - met, len(rows) - len(known), met * 100 / len(known) if known else None, None,
    ]))
    return summary, rows
