import tempfile
import unittest
from pathlib import Path

import pandas as pd

from market_data_audit import (
    AuditConfig,
    MarketDataAuditError,
    audit_market_data,
    fingerprint_config,
)


def candles():
    opens = pd.date_range("2025-01-01", periods=4, freq="15min", tz="UTC")
    return pd.DataFrame({
        "Open time": opens,
        "Close time": opens + pd.Timedelta(minutes=15) - pd.Timedelta(milliseconds=1),
        "Open": [100, 101, 102, 103],
        "High": [102, 103, 104, 105],
        "Low": [99, 100, 101, 102],
        "Close": [101, 102, 103, 104],
        "Volume": [1, 2, 3, 4],
    })


class MarketDataAuditTests(unittest.TestCase):
    def test_clean_frame_passes_and_is_fingerprinted(self):
        report = audit_market_data(candles())
        self.assertTrue(report.passed)
        self.assertEqual(report.row_count, 4)
        self.assertEqual(len(report.data_sha256), 64)

    def test_gap_warns_and_invalid_ohlc_fails(self):
        frame = candles()
        frame.loc[2:, "Open time"] += pd.Timedelta(minutes=15)
        frame.loc[2:, "Close time"] += pd.Timedelta(minutes=15)
        frame.loc[1, "High"] = 50
        report = audit_market_data(frame)
        self.assertIsNotNone(report.issue("time_gaps"))
        self.assertIsNotNone(report.issue("ohlc_inconsistent"))
        with self.assertRaises(MarketDataAuditError):
            report.raise_for_errors()

    def test_policy_can_promote_gap_to_failure(self):
        frame = candles().drop(index=1).reset_index(drop=True)
        report = audit_market_data(
            frame, AuditConfig(policies={"time_gaps": "fail"})
        )
        self.assertFalse(report.passed)

    def test_csv_malformed_or_missing_tail_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candles.csv"
            frame = candles()
            frame.to_csv(path, index=False)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(",1,2,3,4,5,\n")
            report = audit_market_data(path)
            self.assertFalse(report.passed)
            self.assertTrue(
                report.issue("missing_required_values")
                or report.issue("invalid_timestamps")
                or report.issue("malformed_rows")
            )

    def test_config_fingerprint_is_order_independent(self):
        self.assertEqual(
            fingerprint_config({"a": 1, "b": 2}),
            fingerprint_config({"b": 2, "a": 1}),
        )


if __name__ == "__main__":
    unittest.main()
