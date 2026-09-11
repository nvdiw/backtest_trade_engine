# Code map

Start with the module owning the behavior below. Use `rg -n '^def |^class '`
to locate functions before reading a large file. `optimize.py` re-exports its
previous public and private entry points for existing scripts and tests.

| Responsibility | Read first |
| --- | --- |
| Optimizer CLI and help | `optimizer_cli.py` |
| Date defaults and stage weights | `optimizer_defaults.py` |
| Grid enumeration, mutation, candidate generation | `optimizer_candidates.py` |
| Learning labels, importance, surrogate trees | `optimizer_learning.py` |
| Stage combination, ACCEPT/WATCH/REJECT and ranking | `optimizer_selection.py` |
| Per-direction sample gate | `optimizer_policy.py` |
| Profit learning and directional evidence | `optimizer_evidence.py` |
| Auto execution, checkpoints, resume, research orchestration | `optimize.py` |
| Campaign status and rejection summaries | `campaign_reporting.py` |
| Compact candidate comparison | `optimize_overview.py` |
| Pulse configuration and search grids | `pulse_strategy_config.py` |
| Pulse indicators and entry filters | `pulse_features.py` |
| Pulse event loop and entry/exit timing | `pulse_strategy.py` |
| Risk-budgeted quantity caps | `risk_sizing.py` |
| Shared fills, fees, margin and liquidation | `trade_engine.py` |
| Pulse sizing diagnostics | `pulse_diagnostics.py` |
| Chart explanations | `pulse_reason_text.py` |

## Selection versus strategy

The refactor preserves Pulse signal formulas, next-open execution, stops, exits,
fees, and the original minimum-of-four position sizing formula. Selection changes
are separate: Pulse Auto requires 30 closed trades per enabled side in Final,
scaled upward to an integer for each stage's duration relative to Final. Disabled
sides are exempt. This is a sample-coverage heuristic, not a profitability target.

New and resumed stage records use the same gate. Rejected samples retain their
raw metrics and receive a zero learning label. Version 3 of the surrogate cache
rebuilds old labels from bounded historical observations. On the first Pulse
resume under this policy, old importance/mutation guidance is reset and the
migration is recorded in `auto_state.json`. Historical sparse finalists remain
visible for review but cannot serve as elite parents under the new sample gate.
Raw historical CSV files retain their original scores; current in-memory
selection rechecks them. Historical stage statistics describe the original run.

Disjoint walk-forward trade counts are summed. Overlapping funnel stages are
never added together to establish Final sample coverage. `qualified_finalists`
counts ACCEPT decisions; `observed_finalists` counts all stored finalists.

## Leverage diagnostics

Leverage limits collateral usage, together with risk, gross exposure and cash.
Increasing it need not increase quantity once another cap binds. Pulse reports
`long/short_average_gross_exposure`, `average_margin_fraction`, `filled_entries`,
and `sizing_{risk,exposure,margin,cash}_entries`. Gross exposure is notional divided
by account equity; margin fraction is collateral divided by account equity.
These are per-filled-entry averages, not time-weighted exposure.

## Verification and active runs

Run `python -m unittest discover -s tests -q`.
`tests/test_optimizer_policy.py` covers sample gates, resumed stages, qualification,
walk-forward counts, and risk-limited leverage. Existing Pulse/engine tests cover
signals, execution, liquidation and margin-limited leverage.

Code edits do not reload the parent process of an already-running campaign.
For the September 11 campaign, stop its own terminal with Ctrl+C, wait for its
checkpoint message, then resume from the project directory:

```powershell
python optimize.py pulse --auto --resume --timeframe 1m --cycles 300 --output-dir outputs/pulse/optimize/2026-09-11 --workers 4
```

The September 12 diagnostic replay is in `outputs/pulse_leverage_audit.json`.
It compares candidate `c000097-000003` on its stored Final range, with no writes
to the active campaign. Its 120 original result fields and 47,327 trace events
matched the previous strategy implementation. Leverage changes were diagnostic
counterfactuals; they were not substituted into campaign parameters.
