import csv
import gzip
import json
import re
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from openpyxl import load_workbook

from optimize import (
    ExtraTreesSurrogate, SmartCandidateGenerator, STAGED_AUTO_PHASES,
    _aggregate_random_audit, _aggregate_walk_forward_records, _auto_bootstrap,
    _apply_date_policy, _auto_candidate_decision,
    _compact_auto_candidate_plans, _generate_random_audit_windows,
    _annotate_discovery_learning_scores, _apply_funnel_learning_scores,
    _latest_market_end, _learn_mutation_guidance, _learn_parameter_importance,
    _load_frozen_date_protocol,
    _market_data_coverage, _open_csv_text,
    _monthly_performance_summary,
    _resolve_csv_path,
    _read_candidate_plan, _read_surrogate_history_cache,
    _representative_surrogate_history, _run_random_window_audit,
    _restore_auto_resume_args, _select_surrogate_candidates,
    _staged_seed_data, _write_staged_ranking, _write_surrogate_history_cache,
    _robust_validation_score, _time_normalized_score,
    _auto_ranges, build_parser, grid_size,
    iter_grid_candidates,
    param_grid, run_auto_optimization, run_optimization, run_staged_optimization,
)
from ma_strategy import resolve_parameter_source
from ma_strategy_config import build_ma_strategy_config, load_ma_strategy_tune
from trade_engine import TradeEngine


class OptimizerSearchTests(unittest.TestCase):
    def test_latest_market_end_includes_the_final_candle(self):
        open_times = pd.Series(pd.to_datetime([
            "2026-05-31 23:15:00",
            "2026-05-31 23:30:00",
            "2026-05-31 23:45:00",
        ]))

        with patch("get_candle_index._open_times", return_value=open_times):
            coverage = _market_data_coverage()
            latest_end = _latest_market_end()

        self.assertEqual(coverage["last_candle"], "2026-05-31 23:45:00")
        self.assertEqual(coverage["end_exclusive"], "2026-06-01 00:00:00")
        self.assertEqual(latest_end, "2026-06-01 00:00:00")
        self.assertEqual(coverage["interval_seconds"], 900.0)

    def test_auto_date_policy_rolls_back_from_latest_candle(self):
        args = build_parser().parse_args(["--auto"])
        protocol = _apply_date_policy(args, coverage={
            "first_candle": "2018-01-01 00:00:00",
            "last_candle": "2026-07-31 23:45:00",
            "end_exclusive": "2026-08-01 00:00:00",
            "interval_seconds": 900.0,
        })

        self.assertEqual(protocol["development_start"], "2024-08-01")
        self.assertEqual(protocol["stability_start"], "2024-05-01")
        self.assertEqual(protocol["stability_end"], "2024-08-01")
        self.assertEqual(protocol["validation_start"], "2024-08-01")
        self.assertEqual(protocol["discovery_start"], "2025-02-01")
        self.assertEqual(protocol["development_end"], "2026-08-01")
        self.assertEqual(protocol["research_end"], "2026-02-01")
        self.assertEqual(protocol["holdout_start"], "2026-03-01")
        self.assertEqual(protocol["holdout_end"], "2026-08-01")
        self.assertEqual(args.auto_stress_end, "2024-08-01")
        self.assertEqual(args.auto_end, "2026-08-01")
        self.assertEqual(args.wf_end, "2026-02-01")
        self.assertEqual(args.holdout_start, "2026-03-01")

        ranges = _auto_ranges(args, args.auto_end)
        self.assertEqual(ranges["stress"], ["2024-05-01", "2024-08-01"])
        self.assertEqual(ranges["validation"], ["2024-08-01", "2025-02-01"])
        self.assertEqual(ranges["discovery"], ["2025-02-01", "2026-08-01"])
        self.assertEqual(ranges["final"], ["2024-08-01", "2026-08-01"])

    def test_fixed_date_policy_preserves_manual_boundaries(self):
        args = build_parser().parse_args([
            "--auto", "--date-policy", "fixed",
            "--auto-stress-start", "10", "--auto-validation-start", "20",
            "--auto-discovery-start", "30", "--auto-end", "40",
        ])

        protocol = _apply_date_policy(args)

        self.assertEqual(protocol["policy"], "fixed")
        self.assertEqual(args.auto_stress_start, "10")
        self.assertEqual(args.auto_validation_start, "20")
        self.assertEqual(args.auto_discovery_start, "30")
        self.assertEqual(args.auto_end, "40")

    def test_campaign_protocol_is_reused_across_research_and_holdout(self):
        protocol = {
            "policy": "auto", "development_start": "2023-03-01",
            "development_end": "2025-06-01", "research_end": "2026-02-01",
            "holdout_start": "2026-03-01", "holdout_end": "2026-08-01",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            report_dir = Path(temp_dir) / "walk_forward_research"
            report_dir.mkdir()
            (report_dir / "walk_forward_report.json").write_text(
                json.dumps({"date_protocol": protocol}), encoding="utf-8"
            )

            loaded = _load_frozen_date_protocol(temp_dir)

        self.assertEqual(loaded, protocol)

    def test_auto_decision_explains_accept_watch_and_reject(self):
        stable = {
            "robust_score": 90, "recency_score": 85,
            "stage_consistency_score": 90, "worst_stage_percentile": 0.7,
            "stage_metrics": {
                stage: {"total_profit_percent": 10, "maximum_drawdown": 10, "liquidations": 0}
                for stage in ("discovery", "validation", "stress", "walk_forward", "final")
            },
        }
        rejected = {
            **stable,
            "stage_metrics": {**stable["stage_metrics"], "final": {
                "total_profit_percent": -5, "maximum_drawdown": 15, "liquidations": 0,
            }},
        }

        self.assertEqual(_auto_candidate_decision(stable)["decision"], "ACCEPT")
        self.assertEqual(_auto_candidate_decision(rejected)["decision"], "REJECT")
        self.assertIn("non-positive final return", _auto_candidate_decision(rejected)["decision_reasons"])

    def test_legacy_auto_checkpoint_restores_saved_defaults_before_resume(self):
        args = build_parser().parse_args(["--auto"])
        args.profile = "full"
        state = {
            "config": {
                "profile": "full", "tests_per_cycle": 2000,
                "validation_top": 30, "stress_top": 10, "final_top": 3,
                "hall_size": 20, "seed": 42, "minimum_trades": 0,
                "maximum_allowed_drawdown": None,
                "ranges": {
                    "discovery": ["2025-01-01", "2026-06-01"],
                    "validation": ["2023-01-01", "2025-01-01"],
                    "stress": ["2019-01-01", "2023-01-01"],
                    "final": ["2019-01-01", "2026-06-01"],
                },
            },
            "optimizer_features": {
                "surrogate_trees": 32, "walk_forward_top": 10,
            },
        }

        _restore_auto_resume_args(args, state)

        self.assertEqual(args.auto_validation_top, 30)
        self.assertEqual(args.auto_stress_top, 10)
        self.assertEqual(args.auto_final_top, 3)
        self.assertEqual(args.auto_hall_size, 20)
        self.assertEqual(args.auto_surrogate_trees, 32)
        self.assertEqual(args.auto_surrogate_max_samples, 1024)
        self.assertEqual(args.auto_end, "2026-06-01")
        self.assertFalse(args._indicator_warmup_enabled)
        self.assertFalse(state["config"]["indicator_warmup"])
        self.assertIn("strategy_semantics_migration", state)

    def test_legacy_surrogate_cache_migrates_raw_scores_to_comparable_ranks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "surrogate_history_cache.json.gz"
            payload = {
                "version": 1,
                "latest_cycle": 12,
                "parameter_keys": ["x"],
                "rows": [["low", 100, 1], ["high", 2000, 2]],
            }
            with gzip.open(cache_path, "wt", encoding="utf-8") as cache_file:
                json.dump(payload, cache_file)

            loaded, latest_cycle = _read_surrogate_history_cache(cache_path, ("x",))

        targets = {row["candidate_id"]: row["learning_score"] for row in loaded}
        self.assertEqual(latest_cycle, 12)
        self.assertEqual(targets, {"low": 0.0, "high": 1.0})

    def test_funnel_learning_replaces_raw_scale_with_later_stage_evidence(self):
        discovery = [
            {
                "candidate_id": "lucky", "params": {"x": 1},
                "objective_score": 1000, "time_normalized_score": 1000,
            },
            {
                "candidate_id": "stable", "params": {"x": 2},
                "objective_score": 900, "time_normalized_score": 900,
            },
        ]
        _annotate_discovery_learning_scores(discovery)
        stages = {
            "discovery": discovery,
            "validation": [
                {"candidate_id": "lucky", "time_normalized_score": -50},
                {"candidate_id": "stable", "time_normalized_score": 80},
            ],
            "stress": [
                {"candidate_id": "stable", "time_normalized_score": 70},
            ],
            "walk_forward": [
                {"candidate_id": "stable", "time_normalized_score": 60},
            ],
            "final": [
                {"candidate_id": "stable", "time_normalized_score": 65},
            ],
        }

        _apply_funnel_learning_scores(discovery, stages)
        targets = {row["candidate_id"]: row["learning_score"] for row in discovery}

        self.assertGreater(targets["stable"], targets["lucky"])
        self.assertTrue(all(row["learning_source"] == "robust_funnel_rank" for row in discovery))

    def test_mutation_guidance_learns_direction_and_step_size(self):
        history = [
            {
                "candidate_id": str(value), "params": {"x": value},
                "learning_score": value / 20,
            }
            for value in range(21)
        ]

        guidance = _learn_mutation_guidance(history, ("x",), {"x": [0, 10, 20]})
        generator = SmartCandidateGenerator(
            {"x": [0.0, 10.0, 20.0]}, seed=2,
            continuous_refinement=True, mutation_guidance=guidance,
        )

        self.assertEqual(guidance["x"]["direction"], 1)
        self.assertEqual(guidance["x"]["step_multiplier"], 3)
        self.assertEqual(generator._refined_neighbors("x", 10.0)[0], 20.0)

    def test_surrogate_selection_reports_quality_uncertainty_and_diversity(self):
        grid = {"x": list(range(16)), "y": list(range(16))}
        history = [
            {
                "candidate_id": f"h-{x}-{y}", "params": {"x": x, "y": y},
                "learning_score": (x + y) / 30,
            }
            for x in range(8) for y in range(8)
        ]
        candidates = [
            {"x": x, "y": y} for x in range(16) for y in range(16)
            if x >= 8 or y >= 8
        ]
        features = {"surrogate_min_samples": 4, "surrogate_trees": 8}

        selected, metadata = _select_surrogate_candidates(
            candidates, history, 64, ("x", "y"), grid, features, seed=7,
            parameter_importance={"x": {"weight": 0.6}, "y": {"weight": 0.4}},
        )

        self.assertEqual(len(selected), 64)
        self.assertEqual(metadata["training_target"], "normalized_and_robust_funnel_rank")
        self.assertEqual(metadata["selection_mix"]["diversity_novelty"], 0.20)
        self.assertGreater(metadata["diversity_buckets"], 10)

    def test_compact_surrogate_cache_round_trips_legacy_history(self):
        history = [
            {
                "candidate_id": f"c1-{score}",
                "objective_score": score,
                "params": {"x": score, "y": score % 3},
            }
            for score in range(10_000)
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "surrogate_history_cache.json.gz"
            selected = _write_surrogate_history_cache(
                cache_path, history, ("x", "y"), 159
            )
            loaded, latest_cycle = _read_surrogate_history_cache(
                cache_path, ("x", "y")
            )

        self.assertEqual(len(selected), 10_000)
        self.assertEqual(loaded, selected)
        self.assertEqual(latest_cycle, 159)

    def test_large_surrogate_history_is_representative_and_bounded(self):
        history = [
            {"objective_score": score, "params": {"x": score}}
            for score in range(10_000)
        ]

        selected = _representative_surrogate_history(history, 1024)

        self.assertEqual(len(selected), 1024)
        self.assertEqual(selected[0]["objective_score"], 0)
        self.assertEqual(selected[-1]["objective_score"], 9_999)
        self.assertEqual(selected, _representative_surrogate_history(history, 1024))

    def test_legacy_candidate_plans_are_compacted_without_losing_candidates(self):
        candidates = [
            {"candidate_id": f"c1-{index}", "params": {"x": index, "y": index % 2}}
            for index in range(100)
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            cycle_dir = Path(temp_dir) / "cycles" / "cycle_000001"
            cycle_dir.mkdir(parents=True)
            payload = {
                "cycle": 1,
                "stage": "discovery_pool",
                "candidate_count": len(candidates),
                "candidates": candidates,
            }
            source = cycle_dir / "discovery_pool_candidates.json"
            rung = cycle_dir / "discovery_rung_01_candidates.json"
            source.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            payload["stage"] = "discovery_rung_01"
            rung.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            rewritten, reclaimed = _compact_auto_candidate_plans(temp_dir)
            rung_payload = json.loads(rung.read_text(encoding="utf-8"))

            self.assertEqual(rewritten, 2)
            self.assertGreater(reclaimed, 0)
            self.assertEqual(_read_candidate_plan(source), candidates)
            self.assertEqual(_read_candidate_plan(rung), candidates)
            self.assertEqual(rung_payload["source"], "discovery_pool_candidates.json")

    def test_help_explains_modes_and_includes_runnable_examples(self):
        help_text = build_parser().format_help()

        self.assertIn("Choose one search path:", help_text)
        self.assertIn("search mode and parameter scope:", help_text)
        self.assertIn("standard search ranges and robustness:", help_text)
        self.assertIn("auto campaign (used only with --auto):", help_text)
        self.assertIn("recommended examples:", help_text)
        self.assertIn("--validation-start 2024-01-01", help_text)
        self.assertIn("--auto --auto-cycles 2", help_text)
        self.assertIn("--resume", help_text)

    def test_planning_and_output_options_are_available(self):
        args = build_parser().parse_args([
            "--profile", "risk", "--dry-run", "--top-n", "7",
            "--output-dir", "custom-output",
        ])
        self.assertEqual(args.profile, "risk")
        self.assertTrue(args.dry_run)
        self.assertEqual(args.top_n, 7)
        self.assertEqual(args.output_dir, "custom-output")

        auto_args = build_parser().parse_args(["--auto"])
        self.assertTrue(auto_args.auto)
        self.assertEqual(auto_args.auto_tests, 2000)
        self.assertEqual(auto_args.auto_validation_top, 500)
        self.assertEqual(auto_args.auto_stress_top, 250)
        self.assertEqual(auto_args.auto_walk_forward_top, 150)
        self.assertEqual(auto_args.auto_final_top, 100)
        self.assertEqual(auto_args.auto_hall_size, 100)
        self.assertEqual(auto_args.auto_surrogate_trees, 64)
        self.assertEqual(auto_args.auto_surrogate_max_samples, 10_000)
        self.assertEqual(auto_args.auto_stress_start, "2023-01-01")
        self.assertEqual(auto_args.auto_validation_start, "2023-07-01")
        self.assertEqual(auto_args.auto_discovery_start, "2024-01-01")
        self.assertEqual(auto_args.auto_end, "2025-04-01")

        research_args = build_parser().parse_args(["--research"])
        self.assertEqual(research_args.wf_start, "2023-01-01")
        self.assertEqual(research_args.wf_end, "2026-01-01")
        self.assertEqual(research_args.holdout_start, "2026-01-01")

        staged = build_parser().parse_args(["--auto", "--staged"])
        self.assertTrue(staged.staged)
        self.assertEqual(staged.stage_cycles, 10)
        self.assertEqual(staged.snapshot_cycles, 50)
        self.assertEqual(staged.snapshot_top, 100)
        self.assertEqual(staged.random_audit_tests, 500)
        self.assertEqual(staged.random_audit_top, 10)
        self.assertEqual(staged.random_audit_recent_ratio, 0.70)
        self.assertEqual([name for name, _ in STAGED_AUTO_PHASES], [
            "signal", "exit", "risk", "rsi", "scale",
        ])

    def test_staged_top_records_seed_the_next_parameter_phase(self):
        records = [
            {
                "candidate_id": "winner",
                "robust_score": 90,
                "effective_params": {"entry_score_threshold": 11},
                "stage_metrics": {"final": {"score": 50}},
            },
            {
                "candidate_id": "runner-up",
                "robust_score": 80, "random_audit_score": 95,
                "effective_params": {"entry_score_threshold": 7},
                "stage_metrics": {"final": {"score": 40}},
            },
        ]
        grid = {"entry_score_threshold": [6, 7, 8, 9, 10, 11, 12]}

        seeds = _staged_seed_data(records, grid, {})

        self.assertEqual([seed["params"]["entry_score_threshold"] for seed in seeds], [7, 11])
        self.assertEqual(seeds[0]["learning_score"], 1.0)
        self.assertEqual(seeds[-1]["learning_score"], 0.0)

    def test_random_audit_windows_enforce_recent_quota_and_are_reproducible(self):
        first = _generate_random_audit_windows(
            0, 50_000, 100_000, 50, 0.70, seed=123,
        )
        second = _generate_random_audit_windows(
            0, 50_000, 100_000, 50, 0.70, seed=123,
        )

        self.assertEqual(first, second)
        self.assertEqual(len(first), 50)
        self.assertEqual(sum(row["tier"] == "recent" for row in first), 35)
        self.assertTrue(all(row["start"] >= 0 for row in first))
        self.assertTrue(all(row["end"] <= 100_000 for row in first))

    def test_random_audit_prefers_consistent_cross_window_candidate(self):
        candidates = [
            {"candidate_id": "stable", "effective_params": {"leverage": 2}},
            {"candidate_id": "unstable", "effective_params": {"leverage": 10}},
        ]
        records = []
        for window_id in range(1, 11):
            records.extend([
                {
                    "candidate_id": "stable", "window_id": window_id,
                    "time_normalized_score": 80,
                    "result": {"total_profit": 10, "total_profit_percent": 5,
                               "maximum_drawdown": -10},
                },
                {
                    "candidate_id": "unstable", "window_id": window_id,
                    "time_normalized_score": 100 if window_id <= 4 else 20,
                    "result": {"total_profit": 10 if window_id <= 4 else -10,
                               "total_profit_percent": 8 if window_id <= 4 else -5,
                               "maximum_drawdown": -40},
                },
            ])

        summaries = _aggregate_random_audit(records, candidates, 10)

        self.assertEqual(summaries[0]["candidate_id"], "stable")
        self.assertGreater(
            summaries[0]["random_audit_score"], summaries[1]["random_audit_score"]
        )

    def test_random_audit_runs_requested_shared_window_matrix_and_writes_reports(self):
        candidates = [
            {"candidate_id": "safe", "effective_params": {"leverage": 2}},
            {"candidate_id": "risky", "effective_params": {"leverage": 10}},
        ]
        args = build_parser().parse_args([
            "--auto", "--staged", "--workers", "1",
            "--random-audit-tests", "4", "--random-audit-top", "2",
            "--random-audit-earliest", "0",
            "--random-audit-recent-start", "20000",
            "--random-audit-min-months", "1",
            "--random-audit-max-months", "1",
            "--log-every", "0",
        ])

        def fake_strategy(tune, start, end):
            safe = tune["leverage"] == 2
            return {
                "score": 100 if safe else 50,
                "total_profit": 10 if safe else -5,
                "total_profit_percent": 5 if safe else -2,
                "closed_trades": 20,
                "maximum_drawdown": -8 if safe else -30,
            }

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("optimize.ma_strategy", side_effect=fake_strategy):
                winner = _run_random_window_audit(
                    args, candidates, temp_dir, block=1, final_end=50_000
                )
            output = Path(temp_dir)
            with (output / "random_window_results.csv").open(encoding="utf-8") as file:
                result_rows = list(csv.DictReader(file))
            best = json.loads((output / "best_params.json").read_text(encoding="utf-8"))

        self.assertEqual(len(result_rows), 4)
        self.assertEqual(winner["candidate_id"], "safe")
        self.assertEqual(best["leverage"], 2)

    def test_staged_snapshot_writes_ranked_csv_and_best_params(self):
        records = [{
            "candidate_id": "best", "robust_score": 99, "cycle": 10,
            "block": 1, "phase": "signal",
            "effective_params": {"entry_score_threshold": 11},
            "stage_metrics": {"final": {"score": 88, "total_profit": 123}},
        }]
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir)
            snapshot = output / "snapshots" / "cycles_000050"
            _write_staged_ranking(output, records, 100, snapshot_dir=snapshot)
            best = json.loads((output / "best_params.json").read_text(encoding="utf-8"))
            with (snapshot / "top_100.csv").open(encoding="utf-8") as csv_file:
                rows = list(csv.DictReader(csv_file))
            ranked_json = json.loads(
                (snapshot / "top_100.json").read_text(encoding="utf-8")
            )
            individual_params_exists = (
                snapshot / "params" / "rank_001_params.json"
            ).is_file()
            individual_summary_exists = (
                snapshot / "summaries" / "rank_001_summary.json"
            ).is_file()

        self.assertEqual(best["entry_score_threshold"], 11)
        self.assertEqual(rows[0]["phase"], "signal")
        self.assertEqual(rows[0]["robust_score"], "99")
        self.assertEqual(ranked_json[0]["candidate_id"], "best")
        self.assertTrue(individual_params_exists)
        self.assertTrue(individual_summary_exists)
        self.assertEqual(list(rows[0])[0:4], [
            "rank", "decision", "decision_reasons", "decision_scope",
        ])

    def test_staged_campaign_locks_each_phase_winner_and_writes_block_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            args = build_parser().parse_args([
                "--auto", "--staged", "--auto-cycles", "5",
                "--stage-cycles", "1", "--snapshot-cycles", "5",
                "--snapshot-top", "100", "--output-dir", temp_dir,
            ])
            observed_baselines = []

            def fake_phase(phase_args, grid=None):
                phase_output = Path(phase_args.output_dir)
                baseline = json.loads(
                    Path(phase_args.base_params).read_text(encoding="utf-8")
                )
                observed_baselines.append(dict(baseline))
                key = next(iter(grid))
                effective = {**baseline, key: list(grid[key])[-1]}
                record = {
                    "candidate_id": f"phase-{len(observed_baselines)}",
                    "params": {key: effective[key]},
                    "effective_params": effective,
                    "robust_score": float(len(observed_baselines)),
                    "cycle": 1,
                    "stage_metrics": {"final": {"score": len(observed_baselines)}},
                }
                phase_output.mkdir(parents=True, exist_ok=True)
                (phase_output / "auto_state.json").write_text(
                    json.dumps({"cycles_completed": 1}), encoding="utf-8"
                )
                (phase_output / "hall_of_fame.json").write_text(
                    json.dumps([record]), encoding="utf-8"
                )
                return record

            with patch("optimize.run_auto_optimization", side_effect=fake_phase):
                with patch("optimize._run_random_window_audit", return_value={
                    "candidate_id": "audited", "random_audit_score": 99,
                    "effective_params": {"leverage": 3},
                }):
                    run_staged_optimization(args)

            output = Path(temp_dir)
            state = json.loads((output / "staged_state.json").read_text(encoding="utf-8"))
            snapshot = output / "snapshots" / "cycles_000005"
            snapshot_csv_exists = (snapshot / "top_100.csv").is_file()
            snapshot_best_exists = (snapshot / "best_params.json").is_file()
            snapshot_manifest = json.loads(
                (snapshot / "manifest.json").read_text(encoding="utf-8")
            )

        self.assertEqual(len(observed_baselines), 5)
        self.assertTrue(observed_baselines[1])
        self.assertEqual(state["cycles_completed"], 5)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["baseline_params"]["leverage"], 3)
        self.assertTrue(snapshot_csv_exists)
        self.assertTrue(snapshot_best_exists)
        self.assertEqual(snapshot_manifest["development_end_exclusive"], "2025-04-01")

    def test_grid_is_complete_and_deterministic(self):
        grid = {"a": [1, 2], "b": ["x", "y", "z"]}
        candidates = list(iter_grid_candidates(grid))

        self.assertEqual(grid_size(grid), 6)
        self.assertEqual(len(candidates), 6)
        self.assertEqual(candidates[0], {"a": 1, "b": "x"})
        self.assertEqual(candidates[-1], {"a": 2, "b": "z"})

    def test_search_grid_keeps_every_current_default_as_a_candidate(self):
        defaults = build_ma_strategy_config()
        for key, values in param_grid.items():
            self.assertIn(getattr(defaults, key), values, key)

    def test_every_config_value_used_by_ma_strategy_is_in_grid(self):
        strategy_source = Path("ma_strategy.py").read_text(encoding="utf-8-sig")
        used_config_keys = set(re.findall(r"cfg\.([A-Za-z_]\w*)", strategy_source))
        self.assertEqual(used_config_keys - set(param_grid), set())

    def test_smart_search_is_unique_and_respects_period_order(self):
        grid = {
            "ema_16_period": [10, 20],
            "ma_50_period": [15, 30],
            "ma_100_period": [25, 40],
            "ma_200_period": [35, 50],
            "entry_score_threshold": [7, 8, 9],
        }
        generator = SmartCandidateGenerator(grid, seed=7)
        candidates = generator.generate(20)
        signatures = {tuple(candidate.items()) for candidate in candidates}

        self.assertEqual(len(signatures), len(candidates))
        self.assertTrue(candidates)
        for candidate in candidates:
            periods = [
                candidate["ema_16_period"], candidate["ma_50_period"],
                candidate["ma_100_period"], candidate["ma_200_period"],
            ]
            self.assertEqual(periods, sorted(periods))

    def test_smart_search_queues_exact_neighbors_around_elites(self):
        generator = SmartCandidateGenerator(
            {"entry_score_threshold": [6, 7, 8, 9, 10]}, seed=11
        )
        generator.seen.add((8,))
        generator._queue_elite_neighbors([
            {"params": {"entry_score_threshold": 8}}
        ])

        self.assertEqual(
            [candidate["entry_score_threshold"] for candidate in generator.local_queue],
            [7, 9],
        )

    def test_auto_refinement_creates_values_between_grid_points(self):
        generator = SmartCandidateGenerator(
            {"ma_50_period": [10, 20, 30]},
            seed=11,
            continuous_refinement=True,
        )
        generator.seen.add((20,))
        generator._queue_elite_neighbors([
            {"params": {"ma_50_period": 20}}
        ])

        self.assertEqual(
            [candidate["ma_50_period"] for candidate in generator.local_queue],
            [19, 21],
        )

    def test_parameter_importance_favors_variables_with_large_score_effect(self):
        records = []
        for index in range(40):
            important = index % 2
            noise = (index // 2) % 2
            records.append({
                "params": {"important": important, "noise": noise},
                "objective_score": important * 100 + noise,
                "result": {},
            })

        learned = _learn_parameter_importance(
            records, ("important", "noise"), target="objective_score"
        )

        self.assertGreater(learned["important"]["weight"], learned["noise"]["weight"])

    def test_extra_trees_surrogate_learns_interactions_and_uncertainty(self):
        features = []
        targets = []
        for left in range(8):
            for right in range(8):
                features.append([left, right])
                targets.append(100 if left >= 5 and right <= 2 else left - right)
        model = ExtraTreesSurrogate(n_trees=24, min_leaf=2, seed=7).fit(
            features, targets
        )

        predictions = model.predict_mean_std([[6, 1], [1, 6]])

        self.assertGreater(predictions[0][0], predictions[1][0] + 40)
        self.assertGreaterEqual(predictions[0][1], 0)

    def test_walk_forward_score_penalizes_an_unstable_candidate(self):
        candidates = [
            {"candidate_id": "stable", "params": {"x": 1}},
            {"candidate_id": "unstable", "params": {"x": 2}},
        ]
        fold_records = {}
        for fold, stable_score, unstable_score in (
            ("f1", 70, 140), ("f2", 72, 140), ("f3", 71, -100),
        ):
            fold_records[fold] = [
                {
                    "candidate_id": "stable", "params": {"x": 1},
                    "objective_score": stable_score,
                    "result": {"score": stable_score, "closed_trades": 10},
                },
                {
                    "candidate_id": "unstable", "params": {"x": 2},
                    "objective_score": unstable_score,
                    "result": {"score": unstable_score, "closed_trades": 10},
                },
            ]

        aggregated = _aggregate_walk_forward_records(
            fold_records, candidates, stability_penalty=0.15
        )
        scores = {record["candidate_id"]: record["objective_score"] for record in aggregated}

        self.assertGreater(scores["stable"], scores["unstable"])

    def test_time_normalization_compares_scores_relative_to_range_length(self):
        one_month = 30 * 24 * 4
        one_year = round(365.25 * 24 * 4)

        monthly_rate = _time_normalized_score(100, one_month)
        yearly_rate = _time_normalized_score(2000, one_year)

        self.assertAlmostEqual(monthly_rate, 1217.5, places=1)
        self.assertAlmostEqual(yearly_rate, 2000, places=1)
        self.assertGreater(yearly_rate, monthly_rate)

    def test_new_auto_campaign_can_warm_start_from_existing_best_params(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            best_path = Path(temp_dir) / "best_params.json"
            best_path.write_text(
                json.dumps({"entry_score_threshold": 11}), encoding="utf-8"
            )
            args = build_parser().parse_args([
                "--auto", "--base-params", str(best_path)
            ])

            bootstrap = _auto_bootstrap(
                args, {"entry_score_threshold": [6, 7, 8, 9, 10, 11, 12]}
            )

        self.assertEqual(bootstrap["params"], {"entry_score_threshold": 11})
        self.assertEqual(bootstrap["compatible_parameter_count"], 1)

    def test_advanced_auto_reuses_history_for_surrogate_halving_and_walk_forward(self):
        args = build_parser().parse_args([
            "--auto", "--auto-tests", "64", "--auto-validation-top", "8",
            "--auto-stress-top", "4", "--auto-walk-forward-top", "3",
            "--auto-final-top", "2", "--auto-cycles", "2",
            "--auto-stress-start", "0", "--auto-validation-start", "60",
            "--auto-discovery-start", "90", "--auto-end", "120",
            "--auto-surrogate-min-samples", "4", "--workers", "1",
            "--log-every", "0", "--excel-top", "0",
        ])

        def fake_strategy(tune, start, end):
            score = 100 - abs(tune["x"] - 10) * 4 - abs(tune["y"] - 3)
            return {
                "score": score, "total_profit": score, "closed_trades": 10,
                "maximum_drawdown": -2,
            }

        with tempfile.TemporaryDirectory() as temp_dir:
            args.output_dir = temp_dir
            with patch("optimize._init_worker"), patch(
                "optimize.ma_strategy", side_effect=fake_strategy
            ):
                run_auto_optimization(
                    args,
                    grid={"x": list(range(16)), "y": list(range(16))},
                )
            output = Path(temp_dir)
            second_surrogate = json.loads((
                output / "cycles" / "cycle_000002" / "surrogate_search.json"
            ).read_text(encoding="utf-8"))
            state = json.loads((output / "auto_state.json").read_text(encoding="utf-8"))
            compressed_results = (
                output / "cycles" / "cycle_000001"
                / "discovery_rung_01_results.csv.gz"
            )
            with _open_csv_text(compressed_results) as results_file:
                auto_columns = next(csv.reader(results_file))

            self.assertTrue(compressed_results.is_file())
            self.assertLess(auto_columns.index("total_profit"), auto_columns.index("x"))
            self.assertTrue((
                output / "cycles" / "cycle_000001" / "walk_forward_summary.json"
            ).is_file())
            self.assertTrue(second_surrogate["enabled"])
            self.assertGreaterEqual(second_surrogate["history_samples"], 8)
            self.assertEqual(state["version"], 2)
            self.assertEqual(state["cycles_completed"], 2)

    def test_auto_mode_runs_full_funnel_and_writes_resumable_state(self):
        args = build_parser().parse_args([
            "--auto", "--auto-tests", "4", "--auto-validation-top", "3",
            "--auto-stress-top", "2", "--auto-final-top", "1",
            "--auto-cycles", "1", "--auto-stress-start", "0",
            "--auto-validation-start", "20", "--auto-discovery-start", "30",
            "--auto-end", "40", "--workers", "1", "--log-every", "0",
            "--excel-top", "1",
        ])

        def fake_strategy(tune, start, end):
            value = tune["x"]
            return {
                "score": value,
                "total_profit": value * 10,
                "total_profit_percent": value,
                "closed_trades": 10,
                "maximum_drawdown": -1,
            }

        with tempfile.TemporaryDirectory() as temp_dir:
            args.output_dir = temp_dir
            with patch("optimize._init_worker"), patch(
                "optimize.ma_strategy", side_effect=fake_strategy
            ) as strategy:
                best = run_auto_optimization(args, grid={"x": [1, 2, 3, 4]})
            state = json.loads(
                (Path(temp_dir) / "auto_state.json").read_text(encoding="utf-8")
            )
            saved = json.loads(
                (Path(temp_dir) / "best_params.json").read_text(encoding="utf-8")
            )
            workbook = load_workbook(Path(temp_dir) / "auto_report.xlsx")
            auto_sheets = workbook.sheetnames
            dashboard_status = workbook["Dashboard"]["C2"].value
            dashboard_header_color = workbook["Dashboard"]["A1"].fill.fgColor.rgb
            dashboard_chart_count = len(workbook["Charts"]._charts)
            dashboard_top_10_title = workbook["Dashboard"]["N2"].value
            dashboard_direction_title = workbook["Dashboard"]["N18"].value
            dashboard_chart_labels = [
                chart.dLbls.showVal for chart in workbook["Charts"]._charts
            ]
            workbook.close()
            completed_checkpoints_removed = not (
                Path(temp_dir) / "cycles" / "cycle_000001" / "checkpoints"
            ).exists()

        self.assertEqual(strategy.call_count, 10)
        self.assertEqual(state["cycles_completed"], 1)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["config"]["profile"], "full")
        self.assertEqual(saved["x"], 4)
        self.assertEqual(saved["slippage_rate"], 0.0001)
        self.assertEqual(best["params"], {"x": 4})
        self.assertEqual(auto_sheets, [
            "Quick Compare", "Parameter Compare",
            "Dashboard", "Hall of Fame", "Directional Metrics", "Monthly Analysis",
            "Best Monthly Returns", "Parameter Importance", "Charts",
        ])
        self.assertEqual(dashboard_status, "completed")
        self.assertEqual(dashboard_header_color, "0017365D")
        self.assertEqual(dashboard_chart_count, 2)
        self.assertEqual(
            dashboard_top_10_title,
            "Top 10 exact results - snapshot / Hall of Fame",
        )
        self.assertEqual(
            dashboard_direction_title,
            "Top 10 directional breakdown - exact net profit",
        )
        self.assertEqual(dashboard_chart_labels, [True, False])
        self.assertTrue(completed_checkpoints_removed)

    def test_auto_resume_recovers_a_missing_cycle_boundary_snapshot(self):
        args = build_parser().parse_args([
            "--auto", "--auto-tests", "2", "--auto-validation-top", "1",
            "--auto-stress-top", "1", "--auto-final-top", "1",
            "--auto-cycles", "1", "--snapshot-cycles", "1",
            "--snapshot-top", "2", "--auto-stress-start", "0",
            "--auto-validation-start", "20", "--auto-discovery-start", "30",
            "--auto-end", "40", "--workers", "1", "--log-every", "0",
            "--excel-top", "0",
        ])

        def fake_strategy(tune, start, end):
            value = tune["x"]
            return {
                "score": value, "total_profit": value, "closed_trades": 5,
                "maximum_drawdown": -1,
            }

        with tempfile.TemporaryDirectory() as temp_dir:
            args.output_dir = temp_dir
            with patch("optimize._init_worker"), patch(
                "optimize.ma_strategy", side_effect=fake_strategy
            ):
                run_auto_optimization(args, grid={"x": [1, 2]})
            manifest = (
                Path(temp_dir) / "snapshots" / "cycles_000001" / "manifest.json"
            )
            manifest.unlink()
            args.resume = True
            with patch("optimize._init_worker"), patch(
                "optimize.ma_strategy", side_effect=fake_strategy
            ):
                run_auto_optimization(args, grid={"x": [1, 2]})
            recovered = manifest.is_file()
            snapshot_workbook = (
                Path(temp_dir) / "snapshots" / "cycles_000001" /
                "snapshot_report.xlsx"
            ).is_file()

        self.assertTrue(recovered)
        self.assertTrue(snapshot_workbook)

    def test_next_auto_cycle_continues_from_hall_of_fame_winner(self):
        args = build_parser().parse_args([
            "--auto", "--auto-tests", "4", "--auto-validation-top", "1",
            "--auto-stress-top", "1", "--auto-final-top", "1",
            "--auto-cycles", "2", "--auto-stress-start", "0",
            "--auto-validation-start", "20", "--auto-discovery-start", "30",
            "--auto-end", "40", "--workers", "1", "--log-every", "0",
            "--excel-top", "0",
        ])

        def fake_strategy(tune, start, end):
            value = tune["x"]
            score = 100 - abs(value - 20)
            return {
                "score": score,
                "total_profit": score,
                "closed_trades": 2,
                "maximum_drawdown": -1,
            }

        with tempfile.TemporaryDirectory() as temp_dir:
            args.output_dir = temp_dir
            with patch("optimize._init_worker"), patch(
                "optimize.ma_strategy", side_effect=fake_strategy
            ):
                run_auto_optimization(args, grid={"x": [0, 10, 20, 30]})
            parent = json.loads((
                Path(temp_dir) / "cycles" / "cycle_000002" / "training_parent.json"
            ).read_text(encoding="utf-8"))
            second_plan = _read_candidate_plan(
                Path(temp_dir) / "cycles" / "cycle_000002"
                / "discovery_candidates.json"
            )

        refined_values = {
            candidate["params"]["x"] for candidate in second_plan
        }
        self.assertEqual(parent["source"], "hall_of_fame")
        self.assertEqual(parent["params"]["x"], 20)
        self.assertTrue(refined_values & {19, 21})

    def test_auto_resume_continues_the_interrupted_stage(self):
        args = build_parser().parse_args([
            "--auto", "--auto-tests", "2", "--auto-validation-top", "2",
            "--auto-stress-top", "1", "--auto-final-top", "1",
            "--auto-cycles", "1", "--auto-stress-start", "0",
            "--auto-validation-start", "20", "--auto-discovery-start", "30",
            "--auto-end", "40", "--workers", "1", "--log-every", "0",
            "--excel-top", "0",
        ])

        def fake_strategy(tune, start, end):
            value = tune["x"]
            return {
                "score": value,
                "total_profit": value,
                "closed_trades": 2,
                "maximum_drawdown": -1,
            }

        interrupted_calls = 0

        def interrupt_after_one_result(tune, start, end):
            nonlocal interrupted_calls
            interrupted_calls += 1
            if interrupted_calls == 2:
                raise KeyboardInterrupt
            return fake_strategy(tune, start, end)

        with tempfile.TemporaryDirectory() as temp_dir:
            args.output_dir = temp_dir
            with patch("optimize._init_worker"), patch(
                "optimize.ma_strategy", side_effect=interrupt_after_one_result
            ):
                first = run_auto_optimization(args, grid={"x": [1, 2]})
            interrupted_state = json.loads(
                (Path(temp_dir) / "auto_state.json").read_text(encoding="utf-8")
            )
            interrupted_best_exists = (
                Path(temp_dir) / "best_params.json"
            ).is_file() and (
                Path(temp_dir) / "cycles" / "cycle_000001" / "checkpoints"
                / "discovery" / "best_params.json"
            ).is_file()

            # Simulate an interrupted version-1 campaign. Plain --auto must
            # migrate and resume it without requiring an explicit --resume.
            state_path = Path(temp_dir) / "auto_state.json"
            legacy_state = json.loads(state_path.read_text(encoding="utf-8"))
            legacy_state["version"] = 1
            legacy_state.pop("optimizer_features", None)
            legacy_state.pop("advanced_from_cycle", None)
            state_path.write_text(json.dumps(legacy_state), encoding="utf-8")
            discovery_path = (
                Path(temp_dir) / "cycles" / "cycle_000001"
                / "discovery_results.csv"
            )
            with discovery_path.open(newline="", encoding="utf-8") as csv_file:
                legacy_rows = list(csv.DictReader(csv_file))
            legacy_fields = [
                key for key in legacy_rows[0]
                if key not in ("time_normalized_score", "range_candles")
            ]
            with discovery_path.open("w", newline="", encoding="utf-8") as csv_file:
                writer = csv.DictWriter(csv_file, fieldnames=legacy_fields)
                writer.writeheader()
                writer.writerows({key: row[key] for key in legacy_fields} for row in legacy_rows)
            with patch("optimize._init_worker"), patch(
                "optimize.ma_strategy", side_effect=fake_strategy
            ):
                resumed = run_auto_optimization(args, grid={"x": [1, 2]})
            completed_state = json.loads(
                (Path(temp_dir) / "auto_state.json").read_text(encoding="utf-8")
            )
            discovery_results = _resolve_csv_path(
                Path(temp_dir) / "cycles" / "cycle_000001" / "discovery_results.csv"
            )
            with _open_csv_text(discovery_results) as results_file:
                discovery_reader = csv.DictReader(results_file)
                discovery_rows = list(discovery_reader)
                discovery_fields = discovery_reader.fieldnames

        self.assertIsNone(first)
        self.assertEqual(interrupted_state["status"], "interrupted")
        self.assertEqual(interrupted_state["stage_completed"], 1)
        self.assertTrue(interrupted_best_exists)
        self.assertEqual(completed_state["status"], "completed")
        self.assertEqual(completed_state["version"], 2)
        self.assertEqual(completed_state["migrated_from_version"], 1)
        self.assertEqual(completed_state["cycles_completed"], 1)
        self.assertEqual(completed_state["total_evaluations"], 6)
        self.assertEqual(discovery_results.suffixes[-2:], [".csv", ".gz"])
        self.assertEqual(len(discovery_rows), 2)
        self.assertIn("time_normalized_score", discovery_fields)
        self.assertIn("range_candles", discovery_fields)
        self.assertLess(discovery_fields.index("total_profit"), discovery_fields.index("x"))
        self.assertEqual(len({row["candidate_id"] for row in discovery_rows}), 2)
        self.assertEqual(resumed["params"], {"x": 2})

    def test_inactive_filter_parameters_collapse_to_one_effective_signature(self):
        generator = SmartCandidateGenerator({
            "volume_filter": [False, True],
            "volume_spike_multiplier": [1.0, 1.5],
            "entry_score_volume": [1, 2],
        })
        first = generator._canonicalize({
            "volume_filter": False,
            "volume_spike_multiplier": 1.0,
            "entry_score_volume": 1,
        })
        second = generator._canonicalize({
            "volume_filter": False,
            "volume_spike_multiplier": 1.5,
            "entry_score_volume": 2,
        })

        self.assertEqual(generator._signature(first), generator._signature(second))

    def test_grid_run_writes_best_params_as_json_by_score(self):
        args = Namespace(
            output_dir=None, mode="grid", tests=99, workers=1, batch_size=2,
            chunksize=1, start="0", end="10", seed=3, log_every=0,
            elite_size=2,
        )

        def fake_strategy(tune, start, end):
            value = tune["x"]
            return {"score": value, "total_profit": 100 - value}

        with tempfile.TemporaryDirectory() as temp_dir:
            args.output_dir = temp_dir
            with patch("optimize._init_worker"), patch("optimize.ma_strategy", fake_strategy):
                best = run_optimization(args, grid={"x": [1, 2, 3]})
            with (Path(temp_dir) / "best_params.json").open(encoding="utf-8") as file:
                saved = json.load(file)
            manifest = json.loads(
                (Path(temp_dir) / "best_params_manifest.json").read_text(encoding="utf-8")
            )
            workbook = load_workbook(Path(temp_dir) / "optimization_results.xlsx")
            with (Path(temp_dir) / "optimization_results.csv").open(
                encoding="utf-8"
            ) as results_file:
                columns = next(csv.reader(results_file))
            sheet_names = workbook.sheetnames
            dashboard_top_10_title = workbook["Dashboard"]["N2"].value
            dashboard_direction_title = workbook["Dashboard"]["N18"].value
            dashboard_chart_labels = [
                chart.dLbls.showVal for chart in workbook["Charts"]._charts
            ]
            workbook.close()

        self.assertEqual(best["params"]["x"], 3)
        self.assertEqual(saved["x"], 3)
        self.assertEqual(saved["funding_rate_per_8h"], 0.00005)
        self.assertEqual(manifest["recommended_file"], "best_params.json")
        self.assertEqual(manifest["status"], "final")
        self.assertIn("Dashboard", sheet_names)
        self.assertIn("Best Parameters", sheet_names)
        self.assertIn("Directional Metrics", sheet_names)
        self.assertIn("Core Metrics", sheet_names)
        self.assertIn("RSI Metrics", sheet_names)
        self.assertIn("Scale Metrics", sheet_names)
        self.assertEqual(
            dashboard_top_10_title,
            "Top 10 exact results - score, profit and risk",
        )
        self.assertEqual(
            dashboard_direction_title,
            "Top 10 directional breakdown - exact net profit",
        )
        self.assertEqual(dashboard_chart_labels, [True, False])
        self.assertIn("objective_score", columns)
        self.assertIn("profit_per_trade", columns)
        self.assertLess(columns.index("total_profit"), columns.index("x"))

    def test_keyboard_interrupt_stops_cleanly_and_keeps_checkpoint(self):
        args = Namespace(
            output_dir=None, mode="smart", tests=3, workers=1, batch_size=2,
            chunksize=1, start="0", end="10", seed=3, log_every=0,
            elite_size=2,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            args.output_dir = temp_dir
            with patch("optimize._init_worker"), patch(
                "optimize.ma_strategy", side_effect=KeyboardInterrupt
            ):
                best = run_optimization(args, grid={"entry_score_threshold": [7, 8]})
            results_path = Path(temp_dir) / "optimization_results.csv"
            rows = results_path.read_text(encoding="utf-8").splitlines()

        self.assertIsNone(best)
        self.assertEqual(len(rows), 1)


class PerformanceScoreTests(unittest.TestCase):
    def test_monthly_summary_counts_true_eight_percent_months_and_recent_year(self):
        returns = [0.01, 0.08, -0.03, 0.12] + [0.02] * 9
        summary = _monthly_performance_summary(returns)

        self.assertEqual(summary["months_observed"], 13)
        self.assertEqual(summary["months_ge_8pct"], 2)
        self.assertEqual(summary["last_12_months_observed"], 12)
        self.assertEqual(summary["last_12_months_ge_8pct"], 2)
        self.assertEqual(summary["last_12_losing_months"], 1)

    def score(self, **overrides):
        values = {
            "return_percent": 20,
            "max_drawdown": -10,
            "win_rate": 60,
            "profits": [10, -5] * 20,
            "first_balance": 1000,
            "profit_months": 8,
            "loss_months": 4,
            "liquidations": 0,
        }
        values.update(overrides)
        return TradeEngine.calculate_performance_score(**values)

    def test_no_trade_result_cannot_win(self):
        metrics = self.score(profits=[], win_rate=0)
        self.assertEqual(metrics["score"], -1_000_000.0)

    def test_drawdown_and_liquidation_reduce_score(self):
        safe = self.score(max_drawdown=-5)
        risky = self.score(max_drawdown=-30, liquidations=8)
        self.assertGreater(safe["score"], risky["score"])

    def test_score_exposes_quality_metrics(self):
        metrics = self.score()
        self.assertEqual(metrics["profit_factor"], 2.0)
        self.assertIn("expectancy_percent", metrics)
        self.assertIn("calmar_ratio", metrics)


class ParameterSourceTests(unittest.TestCase):
    def test_config_and_best_sources_are_explicit(self):
        tune, description = resolve_parameter_source("config")
        self.assertIsNone(tune)
        self.assertEqual(description, "ma_strategy_config.py")

        with tempfile.TemporaryDirectory() as temp_dir:
            best_path = Path(temp_dir) / "best.json"
            best_path.write_text('{"leverage": 7, "fee_rate": 0.001}', encoding="utf-8")
            tune, description = resolve_parameter_source("best", best_params=best_path)

        self.assertEqual(tune["leverage"], 7)
        self.assertEqual(tune["fee_rate"], 0.001)
        self.assertEqual(description, str(best_path))

    def test_parameter_json_rejects_unknown_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bad.json"
            path.write_text('{"leverge": 7}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "leverge"):
                load_ma_strategy_tune(path)

    def test_optimizer_preserves_base_parameters_outside_profile(self):
        args = Namespace(
            output_dir=None, mode="grid", tests=1, workers=1, batch_size=2,
            chunksize=1, start="0", end="10", seed=3, log_every=0,
            elite_size=2, base_source="file", base_params=None,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            args.output_dir = temp_dir
            args.base_params = str(Path(temp_dir) / "base.json")
            Path(args.base_params).write_text('{"leverage": 7}', encoding="utf-8")

            def fake_strategy(tune, start, end):
                self.assertEqual(tune["leverage"], 7)
                return {"score": tune["ema_16_period"]}

            with patch("optimize._init_worker"), patch("optimize.ma_strategy", fake_strategy):
                best = run_optimization(args, grid={"ema_16_period": [10]})

        self.assertEqual(best["params"]["leverage"], 7)
        self.assertEqual(best["params"]["ema_16_period"], 10)

    def test_resume_does_not_repeat_completed_grid_candidates(self):
        args = Namespace(
            output_dir=None, mode="grid", tests=2, workers=1, batch_size=2,
            chunksize=1, start="0", end="10", seed=3, log_every=0,
            elite_size=2, resume=False,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            args.output_dir = temp_dir

            def fake_strategy(tune, start, end):
                return {"score": tune["ema_16_period"]}

            with patch("optimize._init_worker"), patch("optimize.ma_strategy", fake_strategy):
                run_optimization(args, grid={"ema_16_period": [10, 12]})

            args.resume = True
            with patch("optimize._init_worker"), patch(
                "optimize.ma_strategy", side_effect=AssertionError("candidate repeated")
            ):
                run_optimization(args, grid={"ema_16_period": [10, 12]})

            with (Path(temp_dir) / "optimization_results.csv").open(
                encoding="utf-8"
            ) as results_file:
                rows = list(csv.DictReader(results_file))

        self.assertEqual(len(rows), 2)

    def test_validation_score_penalizes_train_only_performance(self):
        stable = _robust_validation_score({"score": 100}, {"score": 90}, 0.5)
        overfit = _robust_validation_score({"score": 200}, {"score": 90}, 0.5)
        self.assertGreater(stable, overfit)


if __name__ == "__main__":
    unittest.main()
