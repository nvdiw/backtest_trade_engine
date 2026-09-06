"""Strategy plug-in contract used by :mod:`optimize`.

The optimizer deliberately knows as little as possible about a strategy.  A
strategy only needs a callable accepting ``tune``, ``start`` and ``end`` and
returning a mapping containing at least ``score`` and ``closed_trades``.

Optional attributes may live beside the strategy callable::

    param_grid = {"period": [7, 14, 21]}
    PARAMETER_PROFILES = {"focused": param_grid, "full": param_grid}
    def build_strategy_config(tune=None): ...
    def required_indicator_warmup(config): ...
    def is_valid_candidate(params): ...
    def canonicalize_candidate(params, baseline): ...

CLI specifications use ``module:function``.  ``ma`` is kept as a built-in
alias for the existing project strategy.
"""

from __future__ import annotations

import importlib
import inspect
import json
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Mapping

from market_data import MarketDataSource


StrategyCallable = Callable[..., Mapping[str, Any]]


def _callable_accepts(function: Callable[..., Any], name: str) -> bool:
    # Optimizer tests and user integrations may wrap a strategy in a callable
    # proxy. When it exposes a concrete side-effect callable, inspect that real
    # target instead of the proxy's broad ``**kwargs`` signature.
    side_effect = getattr(function, "side_effect", None)
    if callable(side_effect):
        function = side_effect
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return True
    return name in signature.parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


def _plain_config_values(config: Any) -> dict[str, Any]:
    if config is None:
        return {}
    if isinstance(config, Mapping):
        return dict(config)
    fields = getattr(config, "__dataclass_fields__", None)
    if fields:
        return {name: getattr(config, name) for name in fields}
    values = vars(config) if hasattr(config, "__dict__") else {}
    return {name: value for name, value in values.items() if not name.startswith("_")}


@dataclass(frozen=True)
class StrategyAdapter:
    """Resolved strategy and its optional optimizer hooks."""

    name: str
    module_name: str
    function_name: str
    function: StrategyCallable
    module: ModuleType
    config_builder: Callable[..., Any] | None = None
    tune_loader: Callable[[str | Path], Mapping[str, Any]] | None = None
    warmup_calculator: Callable[[Any], int] | None = None
    candidate_validator: Callable[[Mapping[str, Any]], bool] | None = None
    candidate_canonicalizer: Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]] | None = None

    @property
    def identifier(self) -> str:
        return f"{self.module_name}:{self.function_name}"

    @property
    def data_file(self) -> Path | None:
        from runtime_settings import market_selection
        value, _ = market_selection(getattr(self.module, "DATA_FILE", None), None)
        return Path(value) if value else None

    @property
    def timeframe(self) -> str | None:
        from runtime_settings import market_selection
        _, value = market_selection(None, getattr(self.module, "TIMEFRAME", None))
        return str(value) if value else None

    def market_data_source(self, override=None) -> MarketDataSource | None:
        path = Path(override) if override else self.data_file
        return MarketDataSource(path, self.timeframe) if path else None

    def default_values(self, tune: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if self.config_builder is None:
            return dict(tune or {})
        return _plain_config_values(self.config_builder(dict(tune or {})))

    def load_tune(self, path: str | Path) -> dict[str, Any]:
        if self.tune_loader is not None:
            return dict(self.tune_loader(path))
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("base parameter JSON must contain an object")
        return payload

    def validate_candidate(self, params: Mapping[str, Any]) -> bool:
        return bool(self.candidate_validator(params)) if self.candidate_validator else True

    def canonicalize_candidate(
        self, params: Mapping[str, Any], baseline: Mapping[str, Any]
    ) -> dict[str, Any]:
        if self.candidate_canonicalizer is None:
            return dict(params)
        return dict(self.candidate_canonicalizer(dict(params), dict(baseline)))

    def warmup_candles(self, tune: Mapping[str, Any] | None = None) -> int:
        if self.warmup_calculator is None:
            return 0
        config = self.config_builder(dict(tune or {})) if self.config_builder else dict(tune or {})
        return max(0, int(self.warmup_calculator(config)))

    def maximum_optimizer_warmup(
        self,
        grid: Mapping[str, list[Any]],
        base_tune: Mapping[str, Any] | None = None,
    ) -> int:
        if self.warmup_calculator is None:
            return 0
        custom = getattr(self.module, "maximum_optimizer_warmup", None)
        if callable(custom):
            return max(0, int(custom(grid, dict(base_tune or {}))))

        # Generic plug-in fallback: try the all-maximum tune plus every
        # individual grid value. Strategy-specific hooks can model coupled
        # periods (for example ATR + ATR-MA) exactly.
        base = dict(base_tune or {})
        tunes = [base]
        maximums = {
            key: max(values) for key, values in grid.items()
            if values and all(isinstance(value, (int, float)) for value in values)
        }
        tunes.append({**base, **maximums})
        tunes.extend(
            {**base, key: value}
            for key, values in grid.items()
            for value in values
        )
        warmups = []
        for tune in tunes:
            try:
                warmups.append(self.warmup_candles(tune))
            except (KeyError, TypeError, ValueError):
                continue
        return max(warmups, default=0)

    def preload(
        self,
        start: Any,
        end: Any,
        tune: Mapping[str, Any],
        use_warmup: bool,
        indicator_warmup_candles: int | None = None,
    ) -> None:
        warmup = (
            max(0, int(indicator_warmup_candles))
            if use_warmup and indicator_warmup_candles is not None
            else self.warmup_candles(tune) if use_warmup else 0
        )
        custom = getattr(self.module, "preload_optimizer_data", None)
        if callable(custom):
            kwargs = {
                "start": start,
                "end": end,
                "tune": dict(tune),
                "use_indicator_warmup": use_warmup,
            }
            if _callable_accepts(custom, "indicator_warmup_candles"):
                kwargs["indicator_warmup_candles"] = warmup
            custom(**kwargs)
            return
        source = self.market_data_source()
        if source is not None:
            from trade_engine import TradeEngine

            TradeEngine.load_market_data(
                start=start,
                end=end,
                warmup_candles=warmup,
                data_file=source.data_file,
                timeframe=source.timeframe,
            )
            return
        if self.module_name == "ma_strategy":
            from trade_engine import TradeEngine

            TradeEngine.load_market_data(
                start=start,
                end=end,
                warmup_candles=warmup,
            )

    def evaluate(
        self,
        *,
        tune: Mapping[str, Any],
        start: Any,
        end: Any,
        use_indicator_warmup: bool = True,
        indicator_warmup_candles: int | None = None,
        research: bool = False,
    ) -> Mapping[str, Any]:
        kwargs: dict[str, Any] = {
            "tune": {**dict(tune), "optimize": True},
            "start": start,
            "end": end,
        }
        if not use_indicator_warmup and _callable_accepts(self.function, "use_indicator_warmup"):
            kwargs["use_indicator_warmup"] = False
        if (
            use_indicator_warmup
            and indicator_warmup_candles is not None
            and _callable_accepts(self.function, "indicator_warmup_candles")
        ):
            kwargs["indicator_warmup_candles"] = max(
                self.warmup_candles(tune), int(indicator_warmup_candles)
            )
        if research and _callable_accepts(self.function, "research"):
            kwargs["research"] = True
        result = self.function(**kwargs)
        if not isinstance(result, Mapping):
            raise TypeError(
                f"strategy {self.identifier} returned {type(result).__name__}; expected a mapping"
            )
        return result

    def discovered_profiles(self) -> dict[str, dict[str, list[Any]]]:
        profiles = getattr(self.module, "PARAMETER_PROFILES", None)
        if isinstance(profiles, Mapping):
            return normalize_profiles(profiles)
        for attribute in ("param_grid", "PARAM_GRID", "FULL_PARAM_GRID"):
            grid = getattr(self.module, attribute, None)
            if isinstance(grid, Mapping):
                normalized = normalize_grid(grid)
                return {"focused": normalized, "full": normalized}
        return {}


def _optional_callable(module: ModuleType, *names: str) -> Callable[..., Any] | None:
    for name in names:
        value = getattr(module, name, None)
        if callable(value):
            return value
    return None


def resolve_strategy(specification: str | None = None) -> StrategyAdapter:
    """Resolve ``ma`` or a ``module:function`` strategy specification."""
    specification = (specification or "ma").strip()
    aliases = {
        "ma": "ma_strategy:ma_strategy",
        "ma_strategy": "ma_strategy:ma_strategy",
        "rsi": "rsi_strategy:rsi_strategy",
        "rsi_strategy": "rsi_strategy:rsi_strategy",
    }
    resolved = aliases.get(specification, specification)
    if ":" in resolved:
        module_name, function_name = resolved.rsplit(":", 1)
    else:
        module_name = function_name = resolved
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if specification in ("rsi", "rsi_strategy"):
            raise ModuleNotFoundError(
                "rsi_strategy.py is not present yet; add rsi_strategy(tune, start, end) "
                "and its param_grid, then use --strategy rsi"
            ) from exc
        raise
    function = getattr(module, function_name, None)
    if not callable(function):
        raise ValueError(f"strategy callable not found: {module_name}:{function_name}")

    if module_name == "ma_strategy":
        from strategy_config import build_ma_strategy_config, load_ma_strategy_tune

        config_builder = build_ma_strategy_config
        tune_loader = load_ma_strategy_tune
    else:
        config_builder = _optional_callable(
            module, "build_strategy_config", f"build_{function_name}_config"
        )
        tune_loader = _optional_callable(module, "load_strategy_tune", "load_tune")

    return StrategyAdapter(
        name=specification,
        module_name=module_name,
        function_name=function_name,
        function=function,
        module=module,
        config_builder=config_builder,
        tune_loader=tune_loader,
        warmup_calculator=_optional_callable(module, "required_indicator_warmup"),
        candidate_validator=_optional_callable(module, "is_valid_candidate"),
        candidate_canonicalizer=_optional_callable(module, "canonicalize_candidate"),
    )


def normalize_grid(grid: Mapping[str, Any]) -> dict[str, list[Any]]:
    if not grid:
        raise ValueError("param_grid cannot be empty")
    normalized: dict[str, list[Any]] = {}
    for key, values in grid.items():
        if not isinstance(key, str) or not key:
            raise ValueError("every param_grid key must be a non-empty string")
        if isinstance(values, (str, bytes)) or not hasattr(values, "__iter__"):
            values = [values]
        values = list(values)
        if not values:
            raise ValueError(f"param_grid[{key!r}] cannot be empty")
        normalized[key] = values
    return normalized


def normalize_profiles(profiles: Mapping[str, Any]) -> dict[str, dict[str, list[Any]]]:
    return {str(name): normalize_grid(grid) for name, grid in profiles.items()}


def load_grid_source(source: str | Path) -> dict[str, dict[str, list[Any]]]:
    """Load a grid/profile mapping from JSON or ``module:attribute``."""
    source_text = str(source)
    path = Path(source_text)
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
    else:
        if ":" not in source_text:
            raise FileNotFoundError(
                f"parameter grid source not found: {source_text}; expected JSON or module:attribute"
            )
        module_name, attribute = source_text.rsplit(":", 1)
        module = importlib.import_module(module_name)
        payload = getattr(module, attribute, None)
        if payload is None:
            raise ValueError(f"grid attribute not found: {source_text}")
    if not isinstance(payload, Mapping):
        raise ValueError("parameter grid source must contain a mapping")
    # A mapping whose values are mappings is a profile collection; otherwise it
    # is one grid exposed under both conventional profile names.
    if payload and all(isinstance(value, Mapping) for value in payload.values()):
        return normalize_profiles(payload)
    grid = normalize_grid(payload)
    return {"focused": grid, "full": grid}
