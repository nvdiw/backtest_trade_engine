"""Shared optimizer defaults and stage weights; no strategy imports."""
import os


DEFAULT_DEVELOPMENT_START = "2023-01-01"

DEFAULT_AUTO_VALIDATION_START = "2023-07-01"

DEFAULT_AUTO_DISCOVERY_START = "2024-01-01"

DEFAULT_DEVELOPMENT_END = "2025-04-01"

DEFAULT_RESEARCH_END = "2026-01-01"

DEFAULT_HOLDOUT_START = "2026-01-01"

DEFAULT_ROLLING_DEVELOPMENT_MONTHS = 24

DEFAULT_ROLLING_OOS_MONTHS = 8

DEFAULT_ROLLING_EMBARGO_MONTHS = 1

DEFAULT_ROLLING_HOLDOUT_MONTHS = 5

DEFAULT_ROLLING_STRESS_MONTHS = 3

DEFAULT_ROLLING_VALIDATION_MONTHS = 6

DEFAULT_OUTPUT_DIR = os.path.join("outputs", "optimize")

AUTO_STAGE_ORDER = ("discovery", "validation", "stress", "walk_forward", "final")

AUTO_STAGE_WEIGHTS = {
    # Recent Discovery evidence is intentionally stronger than older regimes.
    "discovery": 0.35,
    "validation": 0.15,
    "stress": 0.10,
    "walk_forward": 0.30,
    "final": 0.10,
}

DEFAULT_EXECUTION_SCENARIOS = {
    "base": {},
    "adverse": {
        "fee_rate": 0.0007,
        "slippage_rate": 0.0002,
        "funding_rate_per_8h": 0.0001,
        "maintenance_margin_rate": 0.005,
        "liquidation_fee_rate": 0.002,
    },
    "severe": {
        "fee_rate": 0.0010,
        "slippage_rate": 0.0005,
        "funding_rate_per_8h": 0.0003,
        "maintenance_margin_rate": 0.010,
        "liquidation_fee_rate": 0.005,
    },
}
