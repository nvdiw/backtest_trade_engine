"""Optimizer command-line options, separate from execution."""
import argparse
import os
from runtime_settings import add_runtime_arguments
from optimizer_defaults import (
    DEFAULT_DEVELOPMENT_START,
    DEFAULT_AUTO_VALIDATION_START,
    DEFAULT_AUTO_DISCOVERY_START,
    DEFAULT_DEVELOPMENT_END,
    DEFAULT_RESEARCH_END,
    DEFAULT_HOLDOUT_START,
    DEFAULT_ROLLING_DEVELOPMENT_MONTHS,
    DEFAULT_ROLLING_OOS_MONTHS,
    DEFAULT_ROLLING_EMBARGO_MONTHS,
    DEFAULT_ROLLING_HOLDOUT_MONTHS,
    DEFAULT_ROLLING_STRESS_MONTHS,
    DEFAULT_ROLLING_VALIDATION_MONTHS,
    DEFAULT_OUTPUT_DIR,
    AUTO_STAGE_ORDER,
    AUTO_STAGE_WEIGHTS,
    DEFAULT_EXECUTION_SCENARIOS
)


class _OptimizerHelpFormatter(
    argparse.ArgumentDefaultsHelpFormatter,
    argparse.RawDescriptionHelpFormatter,
):
    """Keep command examples readable while still showing option defaults."""

    def _get_help_string(self, action):
        help_text = action.help
        if (
            "%(default)" not in help_text
            and action.default not in (None, False, argparse.SUPPRESS)
        ):
            help_text += " (default: %(default)s)"
        return help_text

def build_parser():
    parser = argparse.ArgumentParser(
        prog="optimize.py",
        formatter_class=_OptimizerHelpFormatter,
        description="""Search and validate robust strategy parameters.

Choose one search path:
  smart  Budgeted adaptive search (recommended for normal experiments).
  grid   Every combination in a profile; usually only practical for tiny grids.
  auto   Continuous model-guided development search with successive halving,
         walk-forward validation, stress tests, and development-period finalists. Existing state is
         resumed automatically. It runs until Ctrl+C unless --auto-cycles is set.

Dates are inclusive at START and exclusive at END. A candle index may be used
instead of a YYYY-MM-DD date.""",
        epilog="""recommended examples:
  Inspect profiles and estimate a run without starting it:
    python optimize.py --list-profiles
    python optimize.py --mode smart --profile focused --tests 5000 --dry-run

  Start a fast adaptive search from ma_strategy_config.py:
    python optimize.py --mode smart --profile focused --base-source config `
      --tests 5000 -w 8 --output-dir outputs/optimize/focused_run

  Train on one period and use the next period as inner validation:
    python optimize.py --mode smart --profile signal --tests 10000 -w 8 --date-policy fixed `
      --start 2023-01-01 --end 2024-01-01 `
      --validation-start 2024-01-01 --validation-end 2025-04-01 `
      --validation-top 30 --min-trades 50 --max-drawdown 35 `
      --output-dir outputs/optimize/validated_signal

  Refine an existing winner, then resume the same interrupted run:
    python optimize.py --mode smart --profile exit --base-source best `
      --base-params outputs/optimize/best_params.json --tests 5000 -w 8 `
      --output-dir outputs/optimize/refine_exit
    python optimize.py --mode smart --profile exit --base-source best `
      --base-params outputs/optimize/best_params.json --tests 5000 -w 8 `
      --output-dir outputs/optimize/refine_exit --resume

  Run two auto cycles (omit --auto-cycles to run until Ctrl+C):
    python optimize.py --auto --auto-cycles 2 -w 16 `
      --output-dir outputs/optimize/auto_two_cycles

  Resume the auto campaign with exactly the same settings (--resume is optional
  when the checkpoint already exists):
    python optimize.py --auto --auto-cycles 2 -w 16 `
      --output-dir outputs/optimize/auto_two_cycles --resume

Tips:
  * Start with --dry-run. Full built-in grids can contain enormous combinations.
  * Use a new --output-dir for a new experiment; use --resume only for the same run.
  * Plain --auto detects and resumes a compatible checkpoint in --output-dir.
  * A new campaign warm-starts from a compatible existing --base-params winner.
  * --date-policy auto derives recent leak-resistant ranges from the latest candle.
  * Use --date-policy fixed when explicit date flags must be preserved exactly.
  * For trustworthy selection, use --research, freeze its recommendation, then peek once with --sealed-holdout.
  * Raw score is preserved; cross-range comparisons use a candle-count annualized score.
  * Auto learns from normalized Discovery ranks and later funnel outcomes, not raw scale.
  * Candidate selection balances predicted quality, uncertainty, diversity, and randomness.
  * Numeric mutations learn a preferred direction and local step inside the configured bounds.
  * Auto mode defaults to profile=full; non-auto mode defaults to profile=focused.""",
    )

    search = parser.add_argument_group("search mode and parameter scope")
    search.add_argument(
        "--strategy", default="ma", metavar="NAME|MODULE:FUNCTION",
        help=(
            "strategy callable (built-in alias 'ma', or module:function; the callable "
            "must accept tune/start/end and return result metrics)"
        ),
    )
    search.add_argument(
        "--param-grid", metavar="JSON|MODULE:ATTRIBUTE",
        help=(
            "external parameter grid or profile collection; otherwise the strategy's "
            "param_grid/PARAMETER_PROFILES is discovered"
        ),
    )
    search.add_argument(
        "--auto", action="store_true",
        help="use the resumable staged auto campaign (overrides --mode)",
    )
    search.add_argument(
        "--mode", choices=("smart", "grid"), default="smart",
        help="non-auto search algorithm",
    )
    search.add_argument(
        "--tests", type=int, default=5000, metavar="N",
        help="candidate budget in smart mode; ignored by grid and auto",
    )
    search.add_argument(
        "--profile", default=None, metavar="NAME",
        help="parameter profile name (default: full in auto mode, focused otherwise)",
    )
    search.add_argument(
        "--base-source", "--params-source", choices=("config", "best", "file"), default="config",
        help="fixed/base values come from the selected strategy config or --base-params JSON",
    )
    search.add_argument(
        "--base-params", "--params-file", default=os.path.join("outputs", "optimize", "best_params.json"),
        metavar="PATH", help="JSON read when --base-source is best or file",
    )

    execution = parser.add_argument_group("execution and reproducibility")
    execution.add_argument(
        "-w", "--workers", type=int, default=min(8, os.cpu_count() or 1), metavar="N",
        help="parallel worker processes",
    )
    execution.add_argument(
        "--batch-size", type=int, default=0, metavar="N",
        help="candidates evaluated before adapting/checkpointing; 0 selects automatically",
    )
    execution.add_argument(
        "--chunksize", type=int, default=0, metavar="N",
        help="tasks sent to each worker at once; 0 selects automatically",
    )
    execution.add_argument(
        "--elite-size", type=int, default=20, metavar="N",
        help="top candidates that guide smart search",
    )
    execution.add_argument(
        "--seed", type=int, default=42, metavar="N",
        help="random seed for reproducible smart/auto candidate generation",
    )
    execution.add_argument(
        "--data-file", metavar="PATH",
        help=(
            "fallback market CSV for audit/date discovery when a strategy does not "
            "expose DATA_FILE; strategy-owned DATA_FILE is authoritative"
        ),
    )
    execution.add_argument(
        "--data-audit", choices=("strict", "warn", "off"), default="strict",
        help="pre-run market-data gate; gaps/zero volume remain warnings in strict mode",
    )
    execution.add_argument(
        "--refresh-auto-report", action="store_true",
        help=(
            "re-evaluate saved Auto finalists for monthly analytics and rebuild "
            "the colored report/snapshot"
        ),
    )

    ranges = parser.add_argument_group("standard search ranges and robustness")
    ranges.add_argument(
        "--date-policy", choices=("auto", "fixed"), default="auto",
        help=(
            "auto derives and freezes recent ranges from the latest candle; "
            "fixed uses the explicit date options below"
        ),
    )
    ranges.add_argument(
        "--rolling-development-months", type=int,
        default=DEFAULT_ROLLING_DEVELOPMENT_MONTHS, metavar="N",
        help="recent history ending at the latest candle (default: 24 months)",
    )
    ranges.add_argument(
        "--rolling-oos-months", type=int, default=DEFAULT_ROLLING_OOS_MONTHS,
        metavar="N", help="reporting-only OOS reservation before the embargo",
    )
    ranges.add_argument(
        "--rolling-embargo-months", type=int,
        default=DEFAULT_ROLLING_EMBARGO_MONTHS, metavar="N",
        help="unused calendar months between research OOS and sealed holdout",
    )
    ranges.add_argument(
        "--rolling-holdout-months", type=int,
        default=DEFAULT_ROLLING_HOLDOUT_MONTHS, metavar="N",
        help="latest calendar months reserved as the sealed holdout",
    )
    ranges.add_argument(
        "--rolling-stress-months", type=int,
        default=DEFAULT_ROLLING_STRESS_MONTHS, metavar="N",
        help="small historical stability slice immediately before the recent window",
    )
    ranges.add_argument(
        "--rolling-validation-months", type=int,
        default=DEFAULT_ROLLING_VALIDATION_MONTHS, metavar="N",
        help="rolling Validation duration after Stress and before Discovery",
    )
    ranges.add_argument(
        "--start", default=DEFAULT_DEVELOPMENT_START, metavar="DATE|INDEX",
        help="inclusive training start",
    )
    ranges.add_argument(
        "--end", default=DEFAULT_DEVELOPMENT_END, metavar="DATE|INDEX",
        help="exclusive search end; later dates are reserved for walk-forward/holdout",
    )
    ranges.add_argument(
        "--validation-start", metavar="DATE|INDEX",
        help="inclusive inner-validation start; requires --validation-end",
    )
    ranges.add_argument(
        "--validation-end", metavar="DATE|INDEX",
        help="exclusive inner-validation end; requires --validation-start",
    )
    ranges.add_argument(
        "--validation-top", type=int, default=20, metavar="N",
        help="training finalists re-tested on inner validation (used for selection)",
    )
    ranges.add_argument(
        "--overfit-penalty", type=float, default=0.25, metavar="FLOAT",
        help="penalty when training score exceeds validation score",
    )
    ranges.add_argument(
        "--min-trades", type=int, default=0, metavar="N",
        help="disqualify candidates with fewer closed trades (0 disables)",
    )
    ranges.add_argument(
        "--max-drawdown", type=float, metavar="PERCENT",
        help="disqualify candidates above this absolute drawdown percentage",
    )

    output = parser.add_argument_group("output, checkpoints, and planning")
    output.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, metavar="PATH",
                        help="directory for reports; CLI default: outputs/<strategy>/optimize (research/holdout for those modes)")
    output.add_argument("--resume", nargs="?", const=True, default=False, metavar="FOLDER",
                        help="resume with saved settings; optionally give a campaign path or unique folder name")
    output.add_argument("--log-every", type=int, default=10, metavar="N",
                        help="print progress every N completed tests (0 is silent)")
    output.add_argument("--top-n", type=int, default=20, metavar="N",
                        help="ranked candidates saved to top_results.json")
    output.add_argument(
        "--excel-top", type=int, default=5000,
        metavar="N", help="top candidates included in XLSX (0 disables XLSX)",
    )
    output.add_argument("--list-profiles", action="store_true",
                        help="show profile parameter counts/grid sizes and exit")
    output.add_argument("--dry-run", action="store_true",
                        help="print the resolved plan without running backtests")

    research = parser.add_argument_group(
        "nested walk-forward research (reporting-only OOS validation)"
    )
    research.add_argument(
        "--research", action="store_true",
        help="run nested chronological walk-forward instead of a normal/auto search",
    )
    research.add_argument(
        "--wf-start", default=DEFAULT_DEVELOPMENT_START, metavar="DATE|INDEX",
        help="earliest nested walk-forward training candle",
    )
    research.add_argument(
        "--wf-end", default=DEFAULT_RESEARCH_END, metavar="DATE|INDEX|latest",
        help=(
            "exclusive development end, not the dataset end; later candles "
            "through --holdout-end stay sealed"
        ),
    )
    research.add_argument("--wf-train-months", type=float, default=24.0, metavar="N")
    research.add_argument("--wf-validation-months", type=float, default=3.0, metavar="N")
    research.add_argument("--wf-test-months", type=float, default=2.0, metavar="N")
    research.add_argument("--wf-step-months", type=float, default=2.0, metavar="N")
    research.add_argument(
        "--wf-rolling", action="store_true",
        help="use a fixed rolling train window instead of anchored expanding history",
    )
    research.add_argument(
        "--wf-purge-candles", type=int, default=0, metavar="N",
        help="unused candles between train/validation/test boundaries",
    )
    research.add_argument(
        "--research-tests", type=int, default=500, metavar="N",
        help="fixed candidate pool evaluated independently inside every fold",
    )
    research.add_argument(
        "--research-seeds", metavar="JSON",
        help=(
            "optional Auto snapshot/top-results JSON; compatible winners are "
            "inserted before deterministic Halton candidates"
        ),
    )
    research.add_argument(
        "--allow-research-seed-overlap", action="store_true",
        help=(
            "allow unverifiable/overlapping seed history for diagnostics; the "
            "research_seed_provenance gate remains false"
        ),
    )
    research.add_argument(
        "--research-validation-top", type=int, default=50, metavar="N",
        help="training finalists evaluated on each inner validation window",
    )
    research.add_argument(
        "--research-pbo-candidates", type=int, default=20, metavar="N",
        help="fixed candidates retained across validation blocks for CSCV/PBO",
    )
    research.add_argument("--bootstrap-samples", type=int, default=1000, metavar="N")
    research.add_argument("--bootstrap-confidence", type=float, default=0.95, metavar="RATIO")
    research.add_argument("--min-oos-folds", type=int, default=4, metavar="N")
    research.add_argument(
        "--min-fold-trades", type=int, default=5, metavar="N",
        help="minimum closed trades required in every train/validation/OOS evaluation",
    )
    research.add_argument(
        "--min-total-oos-trades", type=int, default=30, metavar="N",
        help="minimum closed trades across the stitched reporting-only OOS folds",
    )
    research.add_argument(
        "--max-oos-liquidations", type=int, default=0, metavar="N",
        help="maximum liquidations allowed across all reporting-only OOS folds",
    )
    research.add_argument(
        "--research-max-drawdown", type=float, default=40.0, metavar="PERCENT",
        help="per-window drawdown gate used during nested selection",
    )
    research.add_argument(
        "--max-oos-drawdown", type=float, default=40.0, metavar="PERCENT",
        help="maximum drawdown allowed on the stitched OOS return series",
    )
    research.add_argument("--min-positive-fold-ratio", type=float, default=0.60, metavar="RATIO")
    research.add_argument("--min-dsr-probability", type=float, default=0.95, metavar="RATIO")
    research.add_argument("--max-pbo", type=float, default=0.20, metavar="RATIO")
    research.add_argument(
        "--min-parameter-consensus", type=float, default=0.50, metavar="RATIO",
        help="minimum mean modal frequency across mutable parameters and fold winners",
    )
    research.add_argument(
        "--max-parameter-spread", type=float, default=0.35, metavar="RATIO",
        help="maximum mean normalized grid spread across mutable parameters",
    )
    research.add_argument(
        "--require-positive-ci", action="store_true",
        help="require the bootstrap lower bound of periodic OOS return to exceed zero",
    )
    research.add_argument(
        "--allow-nonpositive-ci", dest="require_positive_ci", action="store_false",
        help="diagnostic override: do not reject a result whose bootstrap lower bound is non-positive",
    )
    research.set_defaults(require_positive_ci=True)
    research.add_argument(
        "--sealed-holdout", action="store_true",
        help="evaluate frozen parameters once on the sealed range and record its consumption",
    )
    research.add_argument("--holdout-params", metavar="PATH")
    research.add_argument(
        "--holdout-start", default=DEFAULT_HOLDOUT_START, metavar="DATE|INDEX",
        help="inclusive sealed range start; by default this is also --wf-end",
    )
    research.add_argument(
        "--holdout-end", default="latest", metavar="DATE|INDEX|latest",
        help="exclusive sealed range end; latest means one interval after the final candle",
    )
    research.add_argument(
        "--holdout-min-trades", type=int, default=20, metavar="N",
        help="minimum closed trades required in every sealed cost scenario",
    )
    research.add_argument(
        "--holdout-max-drawdown", type=float, default=30.0, metavar="PERCENT",
        help="maximum absolute drawdown allowed in every sealed cost scenario",
    )
    research.add_argument(
        "--cost-scenarios", metavar="JSON",
        help="scenario-name to strategy-tune overrides; MA gets base/adverse/severe defaults",
    )
    research.add_argument(
        "--allow-holdout-repeat", action="store_true",
        help="allow a repeat but mark it contaminated and never call it unseen",
    )

    auto = parser.add_argument_group("auto campaign (used only with --auto)")
    auto.add_argument(
        "--auto-tests", type=int, default=2000,
        metavar="N",
        help="new discovery candidates generated in every auto cycle",
    )
    auto.add_argument(
        "--auto-validation-top", type=int, default=500,
        metavar="N",
        help="discovery finalists sent to the independent validation range",
    )
    auto.add_argument(
        "--auto-stress-top", type=int, default=250,
        metavar="N",
        help="validation finalists sent to the older stress range",
    )
    auto.add_argument(
        "--auto-final-top", type=int, default=100,
        metavar="N",
        help="stress finalists tested on the complete development range",
    )
    auto.add_argument(
        "--auto-hall-size", type=int, default=100,
        metavar="N",
        help="maximum robust winners retained across all auto cycles",
    )
    auto.add_argument(
        "--auto-cycles", "--cycles", type=int, default=0,
        metavar="N",
        help="stop after N completed cycles (0 runs until Ctrl+C)",
    )
    auto.add_argument(
        "--auto-discovery-start", default=DEFAULT_AUTO_DISCOVERY_START,
        metavar="DATE|INDEX",
        help="start of the recent discovery range",
    )
    auto.add_argument(
        "--auto-validation-start", default=DEFAULT_AUTO_VALIDATION_START,
        metavar="DATE|INDEX",
        help="start of validation; it ends at auto-discovery-start",
    )
    auto.add_argument(
        "--auto-stress-start", default=DEFAULT_DEVELOPMENT_START,
        metavar="DATE|INDEX",
        help="inclusive start of the older stability-only stress slice",
    )
    auto.add_argument(
        "--auto-stress-end", default=None, metavar="DATE|INDEX",
        help=(
            "exclusive end of the older stability slice; in fixed mode defaults "
            "to --auto-validation-start"
        ),
    )
    auto.add_argument(
        "--auto-end", default=DEFAULT_DEVELOPMENT_END,
        metavar="DATE|INDEX|latest",
        help=(
            "exclusive candidate-search end; auto policy sets this immediately "
            "after the latest candle"
        ),
    )
    auto.add_argument(
        "--auto-importance-target",
        choices=("objective_score", "total_profit", "total_profit_percent"),
        default="objective_score",
        help="metric used to learn which parameters deserve more mutations",
    )
    auto.add_argument(
        "--auto-advanced-min-candidates", type=int, default=64, metavar="N",
        help="minimum cycle size that activates halving/surrogate/walk-forward",
    )
    auto.add_argument(
        "--auto-halving-rungs", type=int, default=2, metavar="N",
        help="cheap expanding Discovery rungs before full Discovery (0 disables)",
    )
    auto.add_argument(
        "--auto-halving-keep", type=float, default=0.25, metavar="RATIO",
        help="fraction promoted after each cheap Discovery rung",
    )
    auto.add_argument(
        "--auto-surrogate-min-samples", type=int, default=64, metavar="N",
        help="historical full-Discovery samples required before Extra Trees is used",
    )
    auto.add_argument(
        "--auto-surrogate-pool", type=int, default=8, metavar="MULTIPLIER",
        help="unevaluated quality/uncertainty/diversity pool relative to --auto-tests",
    )
    auto.add_argument(
        "--auto-surrogate-trees", type=int, default=64, metavar="N",
        help="trees learning normalized ranks and robust funnel outcomes",
    )
    auto.add_argument(
        "--auto-surrogate-max-samples", type=int, default=10_000, metavar="N",
        help="representative historical samples retained for tree training",
    )
    search.add_argument('--seed-campaign', metavar='FOLDER',
                        help='seed a NEW Auto campaign with prior finalists and representative history; incompatible scores are reevaluated')
    auto.add_argument(
        '--auto-learning-target', choices=('rank', 'profit-evidence'), default='rank',
        help='profit-evidence learns net return, monthly downside and failed candidates; persists across resume',
    )
    auto.add_argument('--auto-trade-count-policy', choices=('fixed', 'duration'), default='fixed',
                      help='duration scales the trade gate to each stage relative to Discovery')
    auto.add_argument(
        "--auto-walk-forward-folds", type=int, default=3, metavar="N",
        help="disjoint pre-Discovery time folds (0 disables; minimum enabled value is 2)",
    )
    auto.add_argument(
        "--auto-walk-forward-top", type=int, default=150, metavar="N",
        help="Stress finalists evaluated on every walk-forward fold",
    )
    auto.add_argument(
        "--auto-walk-forward-stability-penalty", type=float, default=0.15,
        metavar="FLOAT",
        help="penalty multiplier applied to score variation across folds",
    )
    auto.add_argument(
        "--staged", action="store_true",
        help="optimize Signal, Exit, Risk, RSI, and Scale in consecutive phases",
    )
    auto.add_argument(
        "--stage-cycles", type=int, default=10, metavar="N",
        help="completed auto cycles allocated to each staged parameter phase",
    )
    auto.add_argument(
        "--snapshot-cycles", type=int, default=50, metavar="N",
        help="completed auto cycles between ranked Top-N snapshots (default: 50)",
    )
    auto.add_argument(
        "--snapshot-top", type=int, default=100, metavar="N",
        help="ranked candidates and standalone parameter JSON files per snapshot",
    )
    auto.add_argument(
        "--random-audit-tests", type=int, default=500, metavar="N",
        help="total random-window backtests after every staged snapshot",
    )
    auto.add_argument(
        "--random-audit-top", type=int, default=10, metavar="N",
        help="staged finalists compared on identical random windows",
    )
    auto.add_argument(
        "--random-audit-earliest", default=DEFAULT_DEVELOPMENT_START, metavar="DATE|INDEX",
        help="earliest allowed random-window candle",
    )
    auto.add_argument(
        "--random-audit-recent-start", default=DEFAULT_AUTO_DISCOVERY_START, metavar="DATE|INDEX",
        help="start boundary used for the recent-window quota",
    )
    auto.add_argument(
        "--random-audit-recent-ratio", type=float, default=0.70, metavar="RATIO",
        help="fraction of random windows starting on recent data",
    )
    auto.add_argument(
        "--random-audit-min-months", type=int, default=6, metavar="N",
        help="minimum random audit window duration",
    )
    auto.add_argument(
        "--random-audit-max-months", type=int, default=12, metavar="N",
        help="maximum random audit window duration",
    )
    parser.add_argument('--directional', action='store_true',
                        help='MA: optimize long/short separately; use side phases with --staged')
    parser.add_argument('--autopilot', action='store_true',
                        help='MA: start staged directional Auto with automatic snapshot and audit workbooks')
    add_runtime_arguments(parser, data_file=False)
    return parser
