import unittest

import numpy as np
import pandas as pd

from indicators import Indicator


def legacy_adx(high, low, close, period):
    frame = pd.DataFrame({"high": high, "low": low, "close": close})
    previous_close = frame["close"].shift(1)
    previous_high = frame["high"].shift(1)
    previous_low = frame["low"].shift(1)
    true_range = [None]
    plus_dm = [None]
    minus_dm = [None]
    for index in range(1, len(frame)):
        true_range.append(max(
            frame["high"].iloc[index] - frame["low"].iloc[index],
            abs(frame["high"].iloc[index] - previous_close.iloc[index]),
            abs(frame["low"].iloc[index] - previous_close.iloc[index]),
        ))
        up_move = frame["high"].iloc[index] - previous_high.iloc[index]
        down_move = previous_low.iloc[index] - frame["low"].iloc[index]
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0)

    tr_smooth = pd.Series(true_range).ewm(alpha=1 / period, adjust=False).mean()
    plus_smooth = pd.Series(plus_dm).ewm(alpha=1 / period, adjust=False).mean()
    minus_smooth = pd.Series(minus_dm).ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * plus_smooth / tr_smooth
    minus_di = 100 * minus_smooth / tr_smooth
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return dx.ewm(alpha=1 / period, adjust=False).mean().tolist()


class VectorizedIndicatorTests(unittest.TestCase):
    def test_moving_average_alignment_and_values(self):
        result = Indicator([1, 2, 3, 4, 5]).get_MA(3)
        self.assertEqual(result, [None, None, 2.0, 3.0, 4.0])

    def test_vectorized_adx_matches_previous_algorithm(self):
        high = np.array([10, 12, 11, 14, 15, 14, 17, 18], dtype=float)
        low = np.array([8, 9, 9.5, 10, 12, 11, 13, 15], dtype=float)
        close = np.array([9, 11, 10, 13, 13.5, 12, 16, 16.5], dtype=float)
        expected = legacy_adx(high, low, close, period=3)
        actual = Indicator(close).get_ADX(high, low, close, period=3)
        np.testing.assert_allclose(actual, expected, equal_nan=True)


if __name__ == "__main__":
    unittest.main()
