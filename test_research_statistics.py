import unittest

from research_statistics import (
    deflated_sharpe_ratio,
    grid_ordinal_position,
    moving_block_bootstrap_ci,
    parameter_plateau_scores,
    performance_from_returns,
    probability_of_backtest_overfitting,
)


class ResearchStatisticsTests(unittest.TestCase):
    def test_return_metrics_use_cagr_and_drawdown(self):
        metrics = performance_from_returns([0.01, -0.005, 0.02, -0.002], 12)
        self.assertGreater(metrics["total_return"], 0)
        self.assertLess(metrics["maximum_drawdown"], 0)
        self.assertIsNotNone(metrics["sharpe"])

    def test_block_bootstrap_is_reproducible(self):
        first = moving_block_bootstrap_ci([1, -1, 2, -1] * 5, samples=100, seed=7)
        second = moving_block_bootstrap_ci([1, -1, 2, -1] * 5, samples=100, seed=7)
        self.assertEqual(first, second)
        self.assertLessEqual(first["lower"], first["estimate"])
        self.assertGreaterEqual(first["upper"], first["estimate"])

    def test_more_trials_raise_deflated_benchmark(self):
        few = deflated_sharpe_ratio(0.5, 100, [0.1, 0.2])
        many = deflated_sharpe_ratio(0.5, 100, [i / 100 for i in range(30)])
        self.assertGreaterEqual(many["expected_maximum_sharpe"], few["expected_maximum_sharpe"])

    def test_pbo_detects_unstable_selection(self):
        report = probability_of_backtest_overfitting([
            [10, 10, -10, -10],
            [-10, -10, 10, 10],
            [1, 1, 1, 1],
        ])
        self.assertIsNotNone(report["pbo"])
        self.assertGreater(report["combinations"], 0)

    def test_plateau_prefers_supported_candidate(self):
        records = [
            {"candidate_id": "left", "params": {"x": 1}, "robust_score": 90},
            {"candidate_id": "middle", "params": {"x": 2}, "robust_score": 100},
            {"candidate_id": "right", "params": {"x": 3}, "robust_score": 92},
            {"candidate_id": "spike", "params": {"x": 5}, "robust_score": 110},
        ]
        scores = parameter_plateau_scores(records, {"x": [1, 2, 3, 4, 5]})
        self.assertGreater(scores["middle"]["plateau_stability"], scores["spike"]["plateau_stability"])

    def test_plateau_handles_refined_values_between_grid_points(self):
        records = [
            {"candidate_id": "refined", "params": {"x": 2.5}, "robust_score": 10},
            {"candidate_id": "grid", "params": {"x": 3}, "robust_score": 9},
        ]
        scores = parameter_plateau_scores(records, {"x": [1, 3, 5]})
        self.assertEqual(
            scores["refined"]["neighbour_method"],
            "nearest_grid_configurations",
        )
        self.assertIsNotNone(scores["refined"]["median_grid_distance"])
        self.assertGreater(scores["refined"]["plateau_stability"], 0)
        self.assertEqual(grid_ordinal_position([1, 3, 5], 2), 0.5)


if __name__ == "__main__":
    unittest.main()
