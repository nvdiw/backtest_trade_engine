import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

import example_strategy
import optimize
from market_data import MarketDataSource, clear_market_data_cache
from strategy_adapter import resolve_strategy
from trade_engine import TradeEngine


def _write_minute_candles(path: Path, rows=360):
    open_times = pd.date_range("2026-01-01", periods=rows, freq="1min")
    close_times = open_times + pd.Timedelta(seconds=59, milliseconds=999)
    wave = 100.0 + np.sin(np.arange(rows) / 8.0) * 4.0
    frame = pd.DataFrame({
        "Open time": open_times,
        "Close time": close_times,
        "Open": wave,
        "High": wave + 0.5,
        "Low": wave - 0.5,
        "Close": wave + np.sin(np.arange(rows) / 3.0) * 0.1,
        "Volume": np.full(rows, 10.0),
    })
    frame.to_csv(path, index=False)


class StrategyOwnedMarketDataTests(unittest.TestCase):
    def tearDown(self):
        clear_market_data_cache()
        TradeEngine.load_market_data.cache_clear()
        optimize._ACTIVE_MARKET_DATA_SOURCE = None
        optimize._ACTIVE_CANDLES_PER_YEAR = optimize.CANDLES_PER_YEAR_15M

    def test_one_minute_source_controls_coverage_indices_and_warmup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candles.csv"
            _write_minute_candles(path, rows=120)
            source = MarketDataSource(path, "1m")

            coverage = source.coverage()
            self.assertEqual(coverage["interval_seconds"], 60.0)
            self.assertEqual(coverage["last_candle"], "2026-01-01 01:59:00")
            self.assertEqual(coverage["end_exclusive"], "2026-01-01 02:00:00")
            self.assertEqual(source.resolve_index("2026-01-01 01:00:00"), 60)

            market = TradeEngine.load_market_data(
                "2026-01-01 01:00:00",
                "2026-01-01 01:10:00",
                warmup_candles=20,
                data_file=path,
                timeframe="1m",
            )
            self.assertEqual(market["warmup_offset"], 20)
            self.assertEqual(len(market["close_prices"]), 10)
            self.assertEqual(len(market["history_close_prices"]), 30)

    def test_adapter_exposes_strategy_data_and_timeframe(self):
        adapter = resolve_strategy("example_strategy:example_strategy")
        self.assertEqual(adapter.timeframe, "1m")
        self.assertEqual(adapter.data_file.name, "btc_1m_data.csv")
        self.assertIn("fast_period", adapter.discovered_profiles()["focused"])

    def test_example_strategy_runs_and_returns_generic_optimizer_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candles.csv"
            _write_minute_candles(path)
            with patch.object(example_strategy, "DATA_FILE", path):
                result = example_strategy.example_strategy(
                    {
                        "fast_period": 3,
                        "slow_period": 10,
                        "leverage": 1,
                        "fee_rate": 0,
                        "slippage_rate": 0,
                        "funding_rate_per_8h": 0,
                        "optimize": True,
                    },
                    start="2026-01-01 00:30:00",
                    end="2026-01-01 05:30:00",
                    research=True,
                )

            self.assertGreater(result["closed_trades"], 0)
            self.assertIn("score", result)
            self.assertIn("maximum_drawdown", result)
            self.assertIn("profit_factor", result)
            self.assertEqual(result["timeframe"], "1m")
            self.assertTrue(result["monthly_returns"])

    def test_optimizer_uses_selected_strategy_source_for_date_math(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candles.csv"
            _write_minute_candles(path)
            adapter = resolve_strategy("example_strategy:example_strategy")
            args = optimize.build_parser().parse_args([
                "--auto", "--strategy", "example_strategy:example_strategy"
            ])
            with patch.object(example_strategy, "DATA_FILE", path):
                source = optimize._activate_strategy_market_data(args, adapter)
                coverage = optimize._market_data_coverage()

            self.assertEqual(source.data_file, path.resolve())
            self.assertEqual(coverage["interval_seconds"], 60.0)
            self.assertEqual(optimize._bound_index("2026-01-01 01:00:00"), 60)
            self.assertAlmostEqual(
                optimize._ACTIVE_CANDLES_PER_YEAR,
                365.25 * 24 * 60,
            )

    def test_optimizer_runs_example_strategy_end_to_end(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "candles.csv"
            output = root / "optimization"
            _write_minute_candles(path)
            with patch.object(example_strategy, "DATA_FILE", path):
                with contextlib.redirect_stdout(io.StringIO()):
                    optimize.main([
                        "--strategy", "example_strategy:example_strategy",
                        "--mode", "grid",
                        "--profile", "focused",
                        "--date-policy", "fixed",
                        "--start", "2026-01-01 00:30:00",
                        "--end", "2026-01-01 05:30:00",
                        "--workers", "1",
                        "--excel-top", "0",
                        "--output-dir", str(output),
                    ])

            results = pd.read_csv(output / "optimization_results.csv")
            self.assertEqual(len(results), 25)
            self.assertTrue((output / "best_params.json").is_file())
            manifest = pd.read_json(output / "research_manifest.json", typ="series")
            self.assertEqual(
                Path(manifest["data_file"]).resolve(),
                path.resolve(),
            )

    def test_auto_campaign_publishes_example_strategy_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "candles.csv"
            output = root / "auto"
            _write_minute_candles(path)
            with patch.object(example_strategy, "DATA_FILE", path):
                with contextlib.redirect_stdout(io.StringIO()):
                    optimize.main([
                        "--auto",
                        "--strategy", "example_strategy:example_strategy",
                        "--profile", "focused",
                        "--date-policy", "fixed",
                        "--auto-stress-start", "2026-01-01 00:30:00",
                        "--auto-validation-start", "2026-01-01 01:30:00",
                        "--auto-discovery-start", "2026-01-01 02:30:00",
                        "--auto-end", "2026-01-01 05:30:00",
                        "--auto-tests", "4",
                        "--auto-validation-top", "3",
                        "--auto-stress-top", "2",
                        "--auto-final-top", "1",
                        "--auto-hall-size", "5",
                        "--auto-cycles", "1",
                        "--snapshot-cycles", "1",
                        "--snapshot-top", "1",
                        "--workers", "1",
                        "--output-dir", str(output),
                    ])

            state = pd.read_json(output / "auto_state.json", typ="series")
            self.assertEqual(state["cycles_completed"], 1)
            self.assertEqual(
                state["config"]["strategy"],
                "example_strategy:example_strategy",
            )
            self.assertEqual(state["config"]["timeframe"], "1m")
            self.assertTrue((output / "candidate_catalog.csv").is_file())
            self.assertTrue(
                (output / "snapshots" / "cycles_000001" / "best_params.json").is_file()
            )

    def test_example_strategy_exposes_generic_staged_schedule(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candles.csv"
            _write_minute_candles(path)
            stdout = io.StringIO()
            with patch.object(example_strategy, "DATA_FILE", path):
                with contextlib.redirect_stdout(stdout):
                    optimize.main([
                        "--auto", "--staged", "--dry-run",
                        "--strategy", "example_strategy:example_strategy",
                        "--date-policy", "fixed",
                        "--auto-stress-start", "2026-01-01 00:30:00",
                        "--auto-validation-start", "2026-01-01 01:30:00",
                        "--auto-discovery-start", "2026-01-01 02:30:00",
                        "--auto-end", "2026-01-01 05:30:00",
                        "--stage-cycles", "1",
                        "--snapshot-cycles", "2",
                        "--snapshot-top", "1",
                        "--random-audit-tests", "1",
                        "--random-audit-top", "1",
                    ])

            output = stdout.getvalue()
            self.assertIn("signal (4 params, 1 cycles)", output)
            self.assertIn("risk (2 params, 1 cycles)", output)

    def test_generic_staged_runner_executes_strategy_owned_phase(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "candles.csv"
            output = root / "staged"
            _write_minute_candles(path)
            with patch.object(example_strategy, "DATA_FILE", path):
                with contextlib.redirect_stdout(io.StringIO()):
                    optimize.main([
                        "--auto", "--staged",
                        "--strategy", "example_strategy:example_strategy",
                        "--date-policy", "fixed",
                        "--auto-stress-start", "2026-01-01 00:30:00",
                        "--auto-validation-start", "2026-01-01 01:30:00",
                        "--auto-discovery-start", "2026-01-01 02:30:00",
                        "--auto-end", "2026-01-01 05:30:00",
                        "--auto-tests", "2",
                        "--auto-validation-top", "1",
                        "--auto-stress-top", "1",
                        "--auto-final-top", "1",
                        "--auto-cycles", "1",
                        "--stage-cycles", "1",
                        "--snapshot-cycles", "2",
                        "--snapshot-top", "1",
                        "--random-audit-tests", "1",
                        "--random-audit-top", "1",
                        "--workers", "1",
                        "--output-dir", str(output),
                    ])

            state = pd.read_json(output / "staged_state.json", typ="series")
            self.assertEqual(state["cycles_completed"], 1)
            self.assertEqual(state["phase_index"], 1)
            self.assertTrue(
                (
                    output / "blocks" / "block_0001" / "phases" /
                    "01_signal" / "auto_state.json"
                ).is_file()
            )


if __name__ == "__main__":
    unittest.main()
