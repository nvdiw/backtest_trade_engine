"""Risk, exposure and collateral limits for the shared risk-budgeted entry."""


def position_size_limits(*, equity, balance, fill, stop_distance, risk_per_trade,
                         max_gross_exposure, trade_amount_percent, leverage, fee_rate):
    """Quantity caps in their original order; leverage changes collateral capacity."""
    return {
        'risk': equity * risk_per_trade / stop_distance,
        'exposure': equity * max_gross_exposure / fill,
        'margin': equity * trade_amount_percent * leverage / fill,
        'cash': balance / (fill * (1 / leverage + 2 * fee_rate)),
    }
