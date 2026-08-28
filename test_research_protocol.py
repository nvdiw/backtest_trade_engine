import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from optimize import (
    _load_research_seed_candidates,
    _nested_walk_forward_ranges,
    _parameter_stability_report,
    _research_seed_provenance,
    _space_filling_candidates,
    _write_auto_cycle_snapshot,
    build_parser,
    run_nested_walk_forward,
    run_sealed_holdout,
)
from strategy_adapter import resolve_strategy


class ResearchProtocolTests(unittest.TestCase):
    def test_nested_ranges_are_strictly_chronological(self):
        folds = _nested_walk_forward_ranges(
            0, 50, train_months=0.002, validation_months=0.001,
            test_months=0.001, step_months=0.001, purge_candles=1,
        )
        self.assertGreater(len(folds), 1)
        for fold in folds:
            self.assertLess(fold["train"][1], fold["validation"][0])
            self.assertLess(fold["validation"][1], fold["test"][0])

    def test_space_filling_pool_is_unique_and_reproducible(self):
        grid = {"x": list(range(10)), "y": [False, True]}
        adapter = resolve_strategy("ma")
        first = _space_filling_candidates(grid, 12, {}, adapter, seed=7)
        second = _space_filling_candidates(grid, 12, {}, adapter, seed=7)
        self.assertEqual(first, second)
        self.assertEqual(len({(row["x"], row["y"]) for row in first}), len(first))

    def test_parameter_stability_includes_refined_off_grid_values(self):
        report = _parameter_stability_report(
            [
                {"selected_params": {"x": 2.5}},
                {"selected_params": {"x": 3}},
            ],
            {"x": [1, 3, 5]},
        )
        self.assertEqual(report["mutable_parameter_count"], 1)
        self.assertIsNotNone(report["mean_mutable_normalized_grid_spread"])

    def test_snapshot_winners_can_seed_research_pool(self):
        payload = [
            {"effective_params": {"x": 2.5}, "params": {"x": 1}},
            {"params": {"x": 2.5}},
            {"params": {"x": 3}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "top_100.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            seeds = _load_research_seed_candidates(
                path, {"x": [1, 3]}, {}, resolve_strategy("ma")
            )
        self.assertEqual(seeds, [{"x": 2.5}, {"x": 3}])

    def test_cycle_snapshot_exports_individual_top_params(self):
        hall = [{
            "candidate_id": "c1", "params": {"x": 1},
            "effective_params": {"x": 1}, "robust_score": 10,
            "cycle": 1, "stage_metrics": {"final": {"score": 10}},
        }]
        with tempfile.TemporaryDirectory() as directory:
            snapshot = _write_auto_cycle_snapshot(
                directory, hall, 50, 100, {"x": [1, 2]},
                {"config": {
                    "strategy": "demo:demo",
                    "ranges": {"final": [0, 8]},
                }},
            )
            manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
            self.assertTrue((snapshot / "top_100.csv").is_file())
            self.assertTrue((snapshot / "params" / "rank_001_params.json").is_file())
            self.assertEqual(manifest["cycle"], 50)
            provenance = _research_seed_provenance(
                snapshot / "top_100.json",
                [{"test": [10, 20]}],
                "demo:demo",
            )
            self.assertEqual(provenance["status"], "verified_pre_oos")
            self.assertTrue(provenance["safe_for_oos_claim"])
            manifest["development_range"] = [0, 11]
            manifest["development_end_exclusive"] = 11
            (snapshot / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            overlapping = _research_seed_provenance(
                snapshot / "top_100.json",
                [{"test": [10, 20]}],
                "demo:demo",
            )
            self.assertEqual(overlapping["status"], "overlaps_reporting_oos")
            self.assertFalse(overlapping["safe_for_oos_claim"])

    def test_nested_runner_writes_reporting_only_oos_report(self):
        args = build_parser().parse_args([
            "--research", "--wf-start", "0", "--wf-end", "40",
            "--wf-train-months", "0.002", "--wf-validation-months", "0.001",
            "--wf-test-months", "0.001", "--wf-step-months", "0.001",
            "--research-tests", "3", "--research-validation-top", "2",
            "--research-pbo-candidates", "2", "--bootstrap-samples", "20",
            "--min-oos-folds", "1", "--workers", "1", "--log-every", "0",
        ])

        def fake_evaluate(_args, candidates, start, end, base_tune, research=False):
            output = []
            for candidate in candidates:
                value = candidate["params"]["x"]
                output.append({
                    "candidate_id": candidate["candidate_id"],
                    "params": candidate["params"],
                    "result": {
                        "score": value, "closed_trades": 10,
                        "maximum_drawdown": -1, "total_profit_percent": value,
                        "monthly_returns": [value / 100.0, value / 200.0],
                    },
                    "objective_score": value, "range_candles": end - start,
                    "duration": 0, "error": None,
                })
            return output

        with tempfile.TemporaryDirectory() as directory:
            args.output_dir = directory
            seed_dir = Path(directory) / "seed_snapshot"
            seed_dir.mkdir()
            seed_path = seed_dir / "top_100.json"
            seed_path.write_text(
                json.dumps([{"effective_params": {"x": 3}}]),
                encoding="utf-8",
            )
            (seed_dir / "manifest.json").write_text(json.dumps({
                "strategy": "ma_strategy:ma_strategy",
                "development_range": [0, 8],
                "development_end_exclusive": 8,
            }), encoding="utf-8")
            args.research_seeds = str(seed_path)
            with patch("optimize._evaluate_research_candidates", side_effect=fake_evaluate):
                report = run_nested_walk_forward(args, grid={"x": [1, 2, 3]})
            args.resume = True
            with patch("optimize._evaluate_research_candidates") as evaluate_mock:
                resumed = run_nested_walk_forward(args, grid={"x": [1, 2, 3]})
                evaluate_mock.assert_not_called()
            saved = json.loads((
                Path(directory) / "walk_forward_research" / "walk_forward_report.json"
            ).read_text(encoding="utf-8"))
            decision = json.loads((
                Path(directory) / "walk_forward_research" / "research_decision.json"
            ).read_text(encoding="utf-8"))
        self.assertIn("reporting-only", saved["warning"])
        self.assertEqual(report["recommended_params"]["x"], 3)
        self.assertEqual(report["recommended_candidate_source"], "snapshot_seed")
        self.assertTrue(
            report["research_seed_provenance"]["safe_for_oos_claim"]
        )
        self.assertGreater(report["completed_oos_folds"], 0)
        self.assertEqual(resumed["recommended_params"], report["recommended_params"])
        self.assertEqual(report["trial_ledger"]["oos_evaluations_used_for_selection"], 0)
        self.assertIn("x", report["parameter_stability"]["parameters"])
        self.assertTrue(decision["holdout_ready"])

    def test_sealed_holdout_ledger_blocks_a_second_peek(self):
        fake_result = {
            "score": 5.0,
            "closed_trades": 10,
            "maximum_drawdown": -2.0,
            "total_profit_percent": 3.0,
            "liquidations": 0,
            "monthly_returns": [0.01, 0.02],
        }

        def fake_evaluate(index, params, base_tune, start, end, *args):
            return index, params, dict(fake_result), 0.01, None

        with tempfile.TemporaryDirectory() as directory:
            params_path = Path(directory) / "frozen.json"
            params_path.write_text(json.dumps({"leverage": 2}), encoding="utf-8")
            args = build_parser().parse_args([
                "--sealed-holdout",
                "--holdout-params", str(params_path),
                "--holdout-start", "0",
                "--holdout-end", "10",
                "--holdout-min-trades", "5",
                "--output-dir", directory,
            ])
            with patch("optimize._strategy_data_file", return_value=None):
                with patch(
                    "optimize._load_execution_scenarios", return_value={"base": {}}
                ):
                    with patch(
                        "optimize._evaluate_candidate", side_effect=fake_evaluate
                    ):
                        first = run_sealed_holdout(args)
                        with self.assertRaisesRegex(
                            PermissionError, "already been consumed"
                        ):
                            run_sealed_holdout(args)
                        args.allow_holdout_repeat = True
                        repeated = run_sealed_holdout(args)

            ledger = json.loads(
                (Path(directory) / "holdout_ledger.json").read_text(encoding="utf-8")
            )
        self.assertEqual(first["status"], "first_and_only_peek")
        self.assertTrue(first["accepted"])
        self.assertEqual(repeated["status"], "contaminated_repeat")
        self.assertFalse(repeated["accepted"])
        self.assertEqual(len(ledger), 2)


if __name__ == "__main__":
    unittest.main()
