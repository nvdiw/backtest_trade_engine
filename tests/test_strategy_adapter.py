import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

from strategy_adapter import load_grid_source, normalize_grid, resolve_strategy


class StrategyAdapterTests(unittest.TestCase):
    def test_ma_alias_resolves_existing_strategy(self):
        adapter = resolve_strategy("ma")
        self.assertEqual(adapter.identifier, "ma_strategy:ma_strategy")
        self.assertIn("entry_score_threshold", adapter.default_values())
        self.assertGreater(adapter.warmup_candles(), 0)

    def test_optimizer_uses_one_maximum_warmup_for_the_whole_grid(self):
        rsi = resolve_strategy("rsi")
        self.assertEqual(
            rsi.maximum_optimizer_warmup({"period_rsi": [7, 14, 21]}, {}),
            22,
        )

    def test_generic_strategy_and_discovered_grid(self):
        module = types.ModuleType("temporary_demo_strategy")
        module.param_grid = {"period": [5, 10]}
        module.demo = lambda **kwargs: {"score": 1, "closed_trades": 1}
        sys.modules[module.__name__] = module
        try:
            adapter = resolve_strategy("temporary_demo_strategy:demo")
            result = adapter.evaluate(tune={"period": 5}, start=0, end=10)
            self.assertEqual(result["score"], 1)
            self.assertEqual(adapter.discovered_profiles()["full"]["period"], [5, 10])
        finally:
            sys.modules.pop(module.__name__, None)

    def test_json_grid_can_contain_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "grid.json"
            path.write_text(json.dumps({"small": {"x": [1, 2]}}), encoding="utf-8")
            self.assertEqual(load_grid_source(path)["small"]["x"], [1, 2])

    def test_grid_rejects_empty_values(self):
        with self.assertRaises(ValueError):
            normalize_grid({"x": []})


if __name__ == "__main__":
    unittest.main()
