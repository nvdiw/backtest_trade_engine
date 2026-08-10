import pandas as pd
import numpy as np

class Indicator:
    def __init__(self, close_prices, period=None):
        self.close_prices = close_prices
        self.period = period

    # Calculate Moving Average
    def get_MA(self, period):
        rolling = pd.Series(self.close_prices, dtype=float).rolling(
            window=period,
            min_periods=period,
        ).mean()
        return [None if pd.isna(value) else round(float(value), 2) for value in rolling]


    # Calculate Exponential Moving Average
    def get_EMA(self, period):
        ema_lst = []
        k = 2 / (period + 1)
        ema_prev = None

        for price in self.close_prices:

            if ema_prev is None:
                ema = None
            else:
                ema = (price * k) + (ema_prev * (1 - k))
                ema = round(ema, 2)

            ema_lst.append(ema)

            if ema is not None:
                ema_prev = ema

            # مقدار اولیه EMA بعد از پر شدن دوره
            if ema_prev is None and len(ema_lst) == period:
                sma = sum(self.close_prices[:period]) / period
                ema_prev = round(sma, 2)
                ema_lst[-1] = ema_prev

        return ema_lst


    # calculate: ADX --> Average Directional Index
    def get_ADX(self, high, low, close, period=14):
        high = np.asarray(high, dtype=float)
        low = np.asarray(low, dtype=float)
        close = np.asarray(close, dtype=float)
        length = len(close)
        if not (len(high) == len(low) == length):
            raise ValueError("high, low, and close must have the same length")
        if length == 0:
            return []

        tr = np.full(length, np.nan, dtype=float)
        plus_dm = np.full(length, np.nan, dtype=float)
        minus_dm = np.full(length, np.nan, dtype=float)
        if length > 1:
            tr[1:] = np.maximum.reduce((
                high[1:] - low[1:],
                np.abs(high[1:] - close[:-1]),
                np.abs(low[1:] - close[:-1]),
            ))
            up_move = high[1:] - high[:-1]
            down_move = low[:-1] - low[1:]
            plus_dm[1:] = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
            minus_dm[1:] = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

        alpha = 1 / period
        tr_smooth = pd.Series(tr).ewm(alpha=alpha, adjust=False).mean()
        plus_smooth = pd.Series(plus_dm).ewm(alpha=alpha, adjust=False).mean()
        minus_smooth = pd.Series(minus_dm).ewm(alpha=alpha, adjust=False).mean()
        plus_di = 100 * plus_smooth / tr_smooth
        minus_di = 100 * minus_smooth / tr_smooth
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
        return dx.ewm(alpha=alpha, adjust=False).mean().tolist()


    # Calculate ATR (Average True Range) using True Range and Wilder smoothing
    # Returns a list aligned with input candles. Values are None until ATR is fully formed.
    def get_ATR(self, high, low, close, period=14):
        tr_list = []

        # Build True Range list (first entry None to keep alignment)
        for i in range(len(high)):
            if i == 0:
                tr_list.append(None)
                continue

            tr = max(
                high[i] - low[i],
                abs(high[i] - close[i-1]),
                abs(low[i] - close[i-1])
            )
            tr_list.append(tr)

        # ATR calculation using Wilder's smoothing (seed with simple average)
        atr_list = [None] * len(tr_list)

        # need at least `period` TR values to seed the ATR
        if len(tr_list) <= period:
            return atr_list

        # find first index with full period of TRs (skip initial None at index 0)
        # the first usable TR index is 1, so the seed ATR will be at index `period`
        seed_start = 1
        seed_end = period + seed_start  # exclusive

        seed_trs = [t for t in tr_list[seed_start:seed_end] if t is not None]
        if len(seed_trs) < period:
            return atr_list

        # First ATR value is the simple average of the first `period` TRs
        first_atr = sum(seed_trs) / period
        first_atr_index = seed_end - 1
        atr_list[first_atr_index] = round(first_atr, 6)

        # Wilder smoothing for subsequent ATR values
        prev_atr = first_atr
        for i in range(first_atr_index + 1, len(tr_list)):
            tr = tr_list[i]
            if tr is None:
                atr_list[i] = None
                continue

            atr = (prev_atr * (period - 1) + tr) / period
            atr = round(atr, 6)
            atr_list[i] = atr
            prev_atr = atr

        return atr_list


    # Calculate ATR Moving Average (for entry filter)
    def get_ATR_MA(self, atr, period=20):
        atr_ma = []

        for i in range(len(atr)):
            if atr[i] is None:
                atr_ma.append(None)
                continue

            start = max(0, i - period + 1)
            values = []

            for j in range(start, i + 1):
                if atr[j] is not None:
                    values.append(atr[j])

            if len(values) == 0:
                atr_ma.append(None)
            else:
                atr_ma.append(round(sum(values) / len(values), 6))

        return atr_ma


    # Calculate rolling average volume
    def get_volume_avg(self, volumes, period=15):
        vol_avg = []
        for i in range(len(volumes)):
            start = max(0, i - period + 1)
            window = volumes[start:i + 1]
            vol_avg.append(sum(window) / len(window))

        return vol_avg


    # Calculate Relative Strength Index (RSI - Wilder's Method)
    def get_RSI(self, close_prices: list, period=14):
        """
        Calculate RSI using Wilder's Smoothing method (industry standard)
        
        Parameters:
            period: RSI calculation period (default = 14)
        
        Returns:
            List of RSI values (float or None) with same length as close_prices
            - First 'period' values are None (insufficient data)
            - Then RSI values between 0 and 100
        """
        rsi_lst = []

        # Check if we have enough data
        if len(close_prices) < period + 1:
            return [None] * len(close_prices)
        
        # Calculate daily price changes
        changes = []
        for i in range(1, len(close_prices)):
            changes.append(close_prices[i] - close_prices[i - 1])
        
        # Initial averages - simple average for first period
        initial_gains = [max(0, c) for c in changes[:period]]
        initial_losses = [max(0, -c) for c in changes[:period]]
        
        avg_gain = sum(initial_gains) / period
        avg_loss = sum(initial_losses) / period
        
        # Calculate initial RSI
        if avg_loss == 0:
            current_rsi = 100
        else:
            current_rsi = 100 - (100 / (1 + (avg_gain / avg_loss)))
        
        # Append None for the initial period (insufficient data)
        for i in range(period):
            rsi_lst.append(None)
        
        rsi_lst.append(round(current_rsi, 2))
        
        # Calculate RSI for remaining values using Wilder's smoothing method
        for i in range(period, len(changes)):
            change = changes[i]
            
            gain = max(0, change)
            loss = max(0, -change)
            
            # Wilder's smoothing formula
            avg_gain = ((avg_gain * (period - 1)) + gain) / period
            avg_loss = ((avg_loss * (period - 1)) + loss) / period
            
            if avg_loss == 0:
                rsi = 100
            else:
                rs = avg_gain / avg_loss
                rsi = 100 - (100 / (1 + rs))
            
            rsi_lst.append(round(rsi, 2))
        
        return rsi_lst
