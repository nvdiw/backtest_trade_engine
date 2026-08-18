"""Readable trade-detail blocks used by chart marker tooltips."""


def _number(value, decimals=2, prefix="", suffix=""):
    try:
        return f"{prefix}{float(value):,.{decimals}f}{suffix}"
    except (TypeError, ValueError):
        return "N/A"


def _timestamp(value):
    if value is None:
        return "N/A"
    return str(value).split(".", 1)[0]


def _fraction_as_percent(value):
    try:
        return float(value) * 100
    except (TypeError, ValueError):
        return None


def generate_entry_reason_text(trade_id, updates):
    """Return complete execution details for an open marker."""
    return "\n".join([
        "EXECUTION DETAILS",
        f"Trade ID       : {trade_id}",
        f"Execution time : {_timestamp(updates.get('open_time_value'))}",
        f"Entry price    : {_number(updates.get('entry_price'), prefix='$')}",
        f"Position size  : {_number(updates.get('position_size'), decimals=8)}",
        f"Position value : {_number(updates.get('position_value'), prefix='$')}",
        f"Margin         : {_number(updates.get('margin'), prefix='$')}",
        f"Leverage       : {_number(updates.get('leverage'), suffix='x')}",
        f"Capital used   : {_number(_fraction_as_percent(updates.get('trade_amount_percent')), suffix='%')}",
        f"Balance before : {_number(updates.get('balance_before_trade'), prefix='$')}",
        f"Free balance   : {_number(updates.get('balance'), prefix='$')}",
    ])


def generate_close_reason_text(trade_id, updates):
    """Return complete PnL, fee, balance, and duration details for a close marker."""
    before = _number(updates.get("logged_balance_before"), prefix="$")
    after = _number(updates.get("logged_balance_after"), prefix="$")
    return "\n".join([
        "EXECUTION DETAILS",
        f"Trade ID       : {trade_id}",
        f"Execution time : {_timestamp(updates.get('close_time_value'))}",
        f"Close price    : {_number(updates.get('close_price'), prefix='$')}",
        f"Margin         : {_number(updates.get('margin'), prefix='$')}",
        f"Leverage       : {_number(updates.get('leverage'), suffix='x')}",
        f"Gross PnL      : {_number(updates.get('pnl'), prefix='$')}",
        f"PnL / margin   : {_number(updates.get('pnl_percent'), suffix='%')}",
        f"Total fees     : {_number(updates.get('total_fee'), decimals=4, prefix='$')}",
        f"Net profit     : {_number(updates.get('profit'), prefix='$')}",
        f"Profit/account : {_number(updates.get('profit_percent'), suffix='%')}",
        f"Balance        : {before} -> {after}",
        f"Saved money    : {_number(updates.get('save_money'), prefix='$')}",
        "Duration       : "
        f"{updates.get('days', 'N/A')}d "
        f"{updates.get('hours', 'N/A')}h "
        f"{updates.get('minutes', 'N/A')}m",
    ])
