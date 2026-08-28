"""Market-data quality checks and reproducibility fingerprints.

The module is deliberately independent from the optimizer and strategy modules so
that every optimization entry point can run the same gate before spending any
compute.  ``audit_market_data`` accepts either a CSV path or an already loaded
``pandas.DataFrame``.
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import enum
import gzip
import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd


AUDIT_SCHEMA_VERSION = 1

REQUIRED_MARKET_COLUMNS = (
    "Open time",
    "Close time",
    "Open",
    "High",
    "Low",
    "Close",
    "Volume",
)

DEFAULT_ISSUE_POLICIES: Mapping[str, str] = {
    "file_read_error": "fail",
    "missing_required_columns": "fail",
    "malformed_rows": "fail",
    "missing_required_values": "fail",
    "invalid_timestamps": "fail",
    "invalid_candle_time_range": "fail",
    "non_monotonic_timestamps": "fail",
    "duplicate_timestamps": "fail",
    "nonfinite_values": "fail",
    "negative_values": "fail",
    "ohlc_inconsistent": "fail",
    # Real exchange exports can legitimately contain both of these.  They must
    # remain visible, but are warnings unless a campaign elects to reject them.
    "time_gaps": "warn",
    "zero_volume": "warn",
}

_VALID_ACTIONS = frozenset(("fail", "warn", "ignore"))
_PRICE_COLUMNS = ("Open", "High", "Low", "Close")
_NUMERIC_COLUMNS = (*_PRICE_COLUMNS, "Volume")


class MarketDataAuditError(ValueError):
    """Raised when a report contains one or more issues configured as failures."""

    def __init__(self, report: "AuditReport") -> None:
        self.report = report
        summary = "; ".join(
            f"{issue.code} ({issue.count})" for issue in report.errors
        )
        super().__init__(f"Market-data audit failed: {summary}")


@dataclass(frozen=True)
class AuditConfig:
    """Configuration for :func:`audit_market_data`.

    ``policies`` overrides individual entries in ``DEFAULT_ISSUE_POLICIES``.
    Supported actions are ``fail``, ``warn`` and ``ignore``.  Unknown issue codes
    use ``default_policy``.
    """

    required_columns: tuple[str, ...] = REQUIRED_MARKET_COLUMNS
    open_time_column: str = "Open time"
    close_time_column: str = "Close time"
    expected_interval: str | dt.timedelta | pd.Timedelta = "15min"
    policies: Mapping[str, str] = field(default_factory=dict)
    default_policy: str = "fail"
    max_examples: int = 10
    compute_data_fingerprint: bool = True

    def __post_init__(self) -> None:
        default_policy = str(self.default_policy).lower()
        if default_policy not in _VALID_ACTIONS:
            raise ValueError(
                f"default_policy must be one of {sorted(_VALID_ACTIONS)}"
            )
        object.__setattr__(self, "default_policy", default_policy)

        normalized_policies = {
            str(code): str(action).lower() for code, action in self.policies.items()
        }
        invalid = {
            code: action
            for code, action in normalized_policies.items()
            if action not in _VALID_ACTIONS
        }
        if invalid:
            raise ValueError(f"Invalid audit policies: {invalid}")
        object.__setattr__(self, "policies", normalized_policies)

        if self.max_examples < 0:
            raise ValueError("max_examples cannot be negative")
        interval = pd.Timedelta(self.expected_interval)
        if interval <= pd.Timedelta(0):
            raise ValueError("expected_interval must be positive")

    @property
    def interval(self) -> pd.Timedelta:
        return pd.Timedelta(self.expected_interval)

    def policy_for(self, issue_code: str) -> str:
        return self.policies.get(
            issue_code,
            DEFAULT_ISSUE_POLICIES.get(issue_code, self.default_policy),
        )


@dataclass(frozen=True)
class AuditIssue:
    code: str
    severity: str
    count: int
    message: str
    examples: tuple[Mapping[str, Any], ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "count": int(self.count),
            "message": self.message,
            "examples": [_json_safe(dict(example)) for example in self.examples],
            "details": _json_safe(dict(self.details)),
        }


@dataclass
class AuditReport:
    source: str
    row_count: int
    issues: list[AuditIssue] = field(default_factory=list)
    data_sha256: str | None = None
    expected_interval_seconds: float = 900.0
    schema_version: int = AUDIT_SCHEMA_VERSION

    @property
    def errors(self) -> list[AuditIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def warnings(self) -> list[AuditIssue]:
        return [issue for issue in self.issues if issue.severity == "warning"]

    @property
    def passed(self) -> bool:
        return not self.errors

    def issue(self, code: str) -> AuditIssue | None:
        return next((issue for issue in self.issues if issue.code == code), None)

    def raise_for_errors(self) -> "AuditReport":
        if self.errors:
            raise MarketDataAuditError(self)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source": self.source,
            "row_count": int(self.row_count),
            "passed": self.passed,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "expected_interval_seconds": self.expected_interval_seconds,
            "data_sha256": self.data_sha256,
            "issues": [issue.to_dict() for issue in self.issues],
        }

    def summary(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        issue_text = ", ".join(
            f"{issue.code}={issue.count} ({issue.severity})"
            for issue in self.issues
        )
        return f"{status}: {self.row_count} rows" + (
            f"; {issue_text}" if issue_text else "; no issues"
        )


@dataclass(frozen=True)
class RunFingerprints:
    data_sha256: str
    code_sha256: str
    config_sha256: str
    combined_sha256: str

    def to_dict(self) -> dict[str, str]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class _CsvStructure:
    malformed_count: int = 0
    examples: tuple[Mapping[str, Any], ...] = ()
    parse_error: str | None = None


def audit_market_data(
    source: str | Path | pd.DataFrame,
    config: AuditConfig | None = None,
) -> AuditReport:
    """Audit market candles and return a serializable :class:`AuditReport`.

    Call ``report.raise_for_errors()`` to use the result as a hard pre-run gate.
    A CSV path gets an exact byte-level SHA256; a DataFrame gets a deterministic
    content fingerprint.
    """

    config = config or AuditConfig()
    source_label = "<dataframe>" if isinstance(source, pd.DataFrame) else str(source)
    report = AuditReport(
        source=source_label,
        row_count=0,
        expected_interval_seconds=config.interval.total_seconds(),
    )

    structure = _CsvStructure()
    try:
        if isinstance(source, pd.DataFrame):
            frame = source.copy(deep=False)
            if config.compute_data_fingerprint:
                report.data_sha256 = fingerprint_dataframe(frame)
        else:
            path = Path(source)
            structure = _scan_csv_structure(path, config.max_examples)
            frame = pd.read_csv(path, on_bad_lines="skip", low_memory=False)
            if config.compute_data_fingerprint:
                report.data_sha256 = sha256_file(path)
    except Exception as exc:
        _append_issue(
            report,
            config,
            "file_read_error",
            1,
            f"Could not read market data: {exc}",
            examples=({"error": str(exc)},),
        )
        return report

    report.row_count = len(frame)

    if structure.malformed_count:
        _append_issue(
            report,
            config,
            "malformed_rows",
            structure.malformed_count,
            "CSV rows do not have the same field count as the header.",
            examples=structure.examples,
        )
    if structure.parse_error:
        _append_issue(
            report,
            config,
            "malformed_rows",
            1,
            f"CSV structure scan failed: {structure.parse_error}",
            examples=({"error": structure.parse_error},),
        )

    missing_columns = [
        column for column in config.required_columns if column not in frame.columns
    ]
    if missing_columns:
        _append_issue(
            report,
            config,
            "missing_required_columns",
            len(missing_columns),
            "Required market-data columns are missing.",
            examples=tuple({"column": column} for column in missing_columns),
        )

    available_required = [
        column for column in config.required_columns if column in frame.columns
    ]
    if available_required:
        missing_mask = frame[available_required].isna().any(axis=1)
        missing_rows = frame.index[missing_mask].tolist()
        if missing_rows:
            _append_issue(
                report,
                config,
                "missing_required_values",
                len(missing_rows),
                "Rows contain missing values in required columns.",
                examples=_row_examples(
                    frame, missing_rows, available_required, config.max_examples
                ),
            )

    _audit_timestamps(frame, report, config)
    numeric = _audit_numeric_values(frame, report, config)
    _audit_ohlc(numeric, frame, report, config)
    return report


def _audit_timestamps(
    frame: pd.DataFrame, report: AuditReport, config: AuditConfig
) -> None:
    timestamp_columns = [
        column
        for column in (config.open_time_column, config.close_time_column)
        if column in frame.columns
    ]
    parsed: dict[str, pd.Series] = {}
    invalid_by_column: dict[str, int] = {}
    invalid_rows: set[Any] = set()
    for column in timestamp_columns:
        values = pd.to_datetime(frame[column], errors="coerce", utc=True)
        parsed[column] = values
        invalid_mask = values.isna()
        invalid_by_column[column] = int(invalid_mask.sum())
        invalid_rows.update(frame.index[invalid_mask].tolist())

    invalid_count = sum(invalid_by_column.values())
    if invalid_count:
        _append_issue(
            report,
            config,
            "invalid_timestamps",
            invalid_count,
            "Timestamp values are missing or cannot be parsed.",
            examples=_row_examples(
                frame,
                list(invalid_rows),
                timestamp_columns,
                config.max_examples,
            ),
            details={"by_column": invalid_by_column},
        )

    open_times = parsed.get(config.open_time_column)
    if open_times is not None:
        valid_open = open_times.dropna()
        duplicate_mask = valid_open.duplicated(keep=False)
        duplicate_rows = valid_open.index[duplicate_mask].tolist()
        if duplicate_rows:
            _append_issue(
                report,
                config,
                "duplicate_timestamps",
                len(duplicate_rows),
                "Multiple candles have the same open timestamp.",
                examples=_timestamp_examples(
                    open_times, duplicate_rows, config.max_examples
                ),
                details={
                    "unique_duplicated_timestamps": int(
                        valid_open[duplicate_mask].nunique()
                    )
                },
            )

        diffs_in_order = valid_open.diff()
        backwards_rows = diffs_in_order.index[
            diffs_in_order < pd.Timedelta(0)
        ].tolist()
        if backwards_rows:
            _append_issue(
                report,
                config,
                "non_monotonic_timestamps",
                len(backwards_rows),
                "Open timestamps move backwards in file order.",
                examples=_timestamp_examples(
                    open_times, backwards_rows, config.max_examples
                ),
            )

        # Gap detection is performed on sorted unique timestamps so duplicate or
        # out-of-order rows do not create false gap events.
        ordered = pd.Series(valid_open.unique()).sort_values(ignore_index=True)
        ordered_diffs = ordered.diff()
        gap_positions = np.flatnonzero(ordered_diffs > config.interval)
        if len(gap_positions):
            gap_examples: list[Mapping[str, Any]] = []
            estimated_missing = 0
            for position in gap_positions:
                gap = ordered_diffs.iloc[position]
                estimated = max(
                    1, int(math.ceil(gap / config.interval)) - 1
                )
                estimated_missing += estimated
                if len(gap_examples) < config.max_examples:
                    gap_examples.append(
                        {
                            "previous_open_time": ordered.iloc[position - 1].isoformat(),
                            "next_open_time": ordered.iloc[position].isoformat(),
                            "gap_seconds": float(gap.total_seconds()),
                            "estimated_missing_candles": estimated,
                        }
                    )
            _append_issue(
                report,
                config,
                "time_gaps",
                len(gap_positions),
                "Open timestamps contain gaps larger than the expected interval.",
                examples=tuple(gap_examples),
                details={
                    "estimated_missing_candles": estimated_missing,
                    "expected_interval_seconds": config.interval.total_seconds(),
                },
            )

    close_times = parsed.get(config.close_time_column)
    if open_times is not None and close_times is not None:
        valid_pair = open_times.notna() & close_times.notna()
        invalid_range = valid_pair & (close_times <= open_times)
        bad_rows = frame.index[invalid_range].tolist()
        if bad_rows:
            _append_issue(
                report,
                config,
                "invalid_candle_time_range",
                len(bad_rows),
                "Candle close time is not later than its open time.",
                examples=_row_examples(
                    frame,
                    bad_rows,
                    (config.open_time_column, config.close_time_column),
                    config.max_examples,
                ),
            )


def _audit_numeric_values(
    frame: pd.DataFrame, report: AuditReport, config: AuditConfig
) -> dict[str, pd.Series]:
    numeric: dict[str, pd.Series] = {}
    nonfinite_by_column: dict[str, int] = {}
    nonfinite_rows: set[Any] = set()
    negative_by_column: dict[str, int] = {}
    negative_rows: set[Any] = set()

    for column in _NUMERIC_COLUMNS:
        if column not in frame.columns:
            continue
        values = pd.to_numeric(frame[column], errors="coerce")
        numeric[column] = values
        finite_mask = pd.Series(
            np.isfinite(values.to_numpy(dtype=float, na_value=np.nan)),
            index=values.index,
        )
        bad_mask = ~finite_mask
        nonfinite_by_column[column] = int(bad_mask.sum())
        nonfinite_rows.update(frame.index[bad_mask].tolist())

        negative_mask = finite_mask & (values < 0)
        negative_by_column[column] = int(negative_mask.sum())
        negative_rows.update(frame.index[negative_mask].tolist())

    nonfinite_count = sum(nonfinite_by_column.values())
    if nonfinite_count:
        _append_issue(
            report,
            config,
            "nonfinite_values",
            nonfinite_count,
            "Numeric market-data values contain NaN, infinity, or non-numeric text.",
            examples=_row_examples(
                frame,
                list(nonfinite_rows),
                tuple(numeric),
                config.max_examples,
            ),
            details={"by_column": nonfinite_by_column},
        )

    negative_count = sum(negative_by_column.values())
    if negative_count:
        _append_issue(
            report,
            config,
            "negative_values",
            negative_count,
            "Price or volume values are negative.",
            examples=_row_examples(
                frame,
                list(negative_rows),
                tuple(numeric),
                config.max_examples,
            ),
            details={"by_column": negative_by_column},
        )

    volume = numeric.get("Volume")
    if volume is not None:
        finite_volume = np.isfinite(volume.to_numpy(dtype=float, na_value=np.nan))
        zero_mask = pd.Series(finite_volume, index=volume.index) & (volume == 0)
        zero_rows = frame.index[zero_mask].tolist()
        if zero_rows:
            _append_issue(
                report,
                config,
                "zero_volume",
                len(zero_rows),
                "Candles with zero traded volume were found.",
                examples=_row_examples(
                    frame, zero_rows, ("Volume",), config.max_examples
                ),
            )
    return numeric


def _audit_ohlc(
    numeric: Mapping[str, pd.Series],
    frame: pd.DataFrame,
    report: AuditReport,
    config: AuditConfig,
) -> None:
    if not all(column in numeric for column in _PRICE_COLUMNS):
        return
    prices = pd.DataFrame({column: numeric[column] for column in _PRICE_COLUMNS})
    finite = np.isfinite(prices.to_numpy(dtype=float)).all(axis=1)
    consistent = (
        (prices["Low"] <= prices["Open"])
        & (prices["Low"] <= prices["Close"])
        & (prices["Low"] <= prices["High"])
        & (prices["High"] >= prices["Open"])
        & (prices["High"] >= prices["Close"])
    )
    bad_mask = pd.Series(finite, index=prices.index) & ~consistent
    bad_rows = frame.index[bad_mask].tolist()
    if bad_rows:
        _append_issue(
            report,
            config,
            "ohlc_inconsistent",
            len(bad_rows),
            "OHLC bounds are inconsistent (Low <= Open/Close <= High is violated).",
            examples=_row_examples(
                frame, bad_rows, _PRICE_COLUMNS, config.max_examples
            ),
        )


def _append_issue(
    report: AuditReport,
    config: AuditConfig,
    code: str,
    count: int,
    message: str,
    *,
    examples: Sequence[Mapping[str, Any]] = (),
    details: Mapping[str, Any] | None = None,
) -> None:
    action = config.policy_for(code)
    if action == "ignore" or count <= 0:
        return
    report.issues.append(
        AuditIssue(
            code=code,
            severity="error" if action == "fail" else "warning",
            count=int(count),
            message=message,
            examples=tuple(examples[: config.max_examples]),
            details=details or {},
        )
    )


def _row_examples(
    frame: pd.DataFrame,
    row_indices: Sequence[Any],
    columns: Sequence[str],
    limit: int,
) -> tuple[Mapping[str, Any], ...]:
    examples: list[Mapping[str, Any]] = []
    existing_columns = [column for column in columns if column in frame.columns]
    for row_index in row_indices[:limit]:
        values = frame.loc[row_index, existing_columns]
        # A non-unique DataFrame index can return a DataFrame.  Keeping the row
        # label is sufficient in that unusual case and avoids ambiguous output.
        if isinstance(values, pd.DataFrame):
            payload: dict[str, Any] = {"row_index": _json_safe(row_index)}
        else:
            payload = {"row_index": _json_safe(row_index)}
            if isinstance(values, pd.Series):
                payload.update(
                    {column: _json_safe(value) for column, value in values.items()}
                )
            elif existing_columns:
                payload[existing_columns[0]] = _json_safe(values)
        examples.append(payload)
    return tuple(examples)


def _timestamp_examples(
    timestamps: pd.Series, row_indices: Sequence[Any], limit: int
) -> tuple[Mapping[str, Any], ...]:
    return tuple(
        {
            "row_index": _json_safe(row_index),
            "open_time": _json_safe(timestamps.loc[row_index]),
        }
        for row_index in row_indices[:limit]
    )


def _open_csv_text(path: Path):
    if path.suffix.lower() == ".gz":
        return gzip.open(path, "rt", encoding="utf-8-sig", newline="")
    return path.open("r", encoding="utf-8-sig", newline="")


def _scan_csv_structure(path: Path, max_examples: int) -> _CsvStructure:
    malformed_count = 0
    examples: list[Mapping[str, Any]] = []
    try:
        with _open_csv_text(path) as csv_file:
            reader = csv.reader(csv_file, strict=True)
            try:
                header = next(reader)
            except StopIteration:
                return _CsvStructure(
                    malformed_count=1,
                    examples=({"line_number": 1, "reason": "empty CSV"},),
                )
            expected_fields = len(header)
            for row in reader:
                if len(row) == expected_fields:
                    continue
                malformed_count += 1
                if len(examples) < max_examples:
                    examples.append(
                        {
                            "line_number": reader.line_num,
                            "expected_fields": expected_fields,
                            "actual_fields": len(row),
                        }
                    )
    except (csv.Error, UnicodeError, OSError) as exc:
        return _CsvStructure(
            malformed_count=malformed_count,
            examples=tuple(examples),
            parse_error=str(exc),
        )
    return _CsvStructure(malformed_count, tuple(examples))


def sha256_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    """Return the exact byte-level SHA256 of a file."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as file_obj:
        while True:
            chunk = file_obj.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint_dataframe(frame: pd.DataFrame, include_index: bool = True) -> str:
    """Return a deterministic content/schema fingerprint for a DataFrame."""

    digest = hashlib.sha256()
    schema = {
        "columns": [str(column) for column in frame.columns],
        "dtypes": [str(dtype) for dtype in frame.dtypes],
        "include_index": bool(include_index),
        "rows": len(frame),
    }
    digest.update(_canonical_json(schema).encode("utf-8"))
    hashes = pd.util.hash_pandas_object(
        frame, index=include_index, categorize=False
    ).to_numpy(dtype="uint64", copy=False)
    digest.update(hashes.astype("<u8", copy=False).tobytes())
    return digest.hexdigest()


def fingerprint_data(source: str | Path | pd.DataFrame) -> str:
    """Fingerprint a raw data file or in-memory DataFrame."""

    if isinstance(source, pd.DataFrame):
        return fingerprint_dataframe(source)
    return sha256_file(source)


def fingerprint_config(config: Any) -> str:
    """Hash config-like data using stable key ordering and JSON normalization."""

    payload = _canonical_json(config).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def fingerprint_code(
    paths: str | Path | Iterable[str | Path],
    *,
    root: str | Path | None = None,
    extensions: Sequence[str] | None = (".py",),
) -> str:
    """Hash code files together with their stable relative paths.

    Directory inputs are traversed recursively.  By default only ``.py`` files
    are included; pass ``extensions=None`` to include every file.
    """

    if isinstance(paths, (str, Path)):
        requested = [Path(paths)]
    else:
        requested = [Path(path) for path in paths]

    files: set[Path] = set()
    normalized_extensions = (
        None
        if extensions is None
        else {extension.lower() for extension in extensions}
    )
    for requested_path in requested:
        if requested_path.is_dir():
            candidates = requested_path.rglob("*")
        else:
            candidates = (requested_path,)
        for candidate in candidates:
            if not candidate.is_file():
                continue
            if (
                normalized_extensions is not None
                and candidate.suffix.lower() not in normalized_extensions
            ):
                continue
            files.add(candidate.resolve())

    if not files:
        raise ValueError("No code files matched the supplied paths/extensions")

    if root is not None:
        root_path = Path(root).resolve()
    else:
        common_parent = Path(
            __import__("os").path.commonpath([str(path.parent) for path in files])
        )
        root_path = common_parent.resolve()

    entries: list[tuple[str, Path]] = []
    for file_path in files:
        try:
            identifier = file_path.relative_to(root_path).as_posix()
        except ValueError:
            # This is only possible with an explicit root.  Retain an unambiguous
            # normalized identifier without making the hash depend on drive case.
            identifier = file_path.as_posix().lower()
        entries.append((identifier, file_path))

    digest = hashlib.sha256()
    for identifier, file_path in sorted(entries):
        digest.update(identifier.encode("utf-8"))
        digest.update(b"\0")
        with file_path.open("rb") as file_obj:
            while True:
                chunk = file_obj.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def build_run_fingerprints(
    data_source: str | Path | pd.DataFrame,
    code_paths: str | Path | Iterable[str | Path],
    config: Any,
    *,
    code_root: str | Path | None = None,
) -> RunFingerprints:
    """Build data/code/config hashes plus a single combined run identifier."""

    parts = {
        "data_sha256": fingerprint_data(data_source),
        "code_sha256": fingerprint_code(code_paths, root=code_root),
        "config_sha256": fingerprint_config(config),
    }
    combined = hashlib.sha256(_canonical_json(parts).encode("utf-8")).hexdigest()
    return RunFingerprints(**parts, combined_sha256=combined)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _json_safe(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _json_safe(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _json_safe(dataclasses.asdict(value))
    if isinstance(value, enum.Enum):
        return _json_safe(value.value)
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_json_safe(item) for item in value]
        return sorted(normalized, key=lambda item: repr(item))
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, (dt.datetime, dt.date, dt.time, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, (dt.timedelta, pd.Timedelta)):
        return float(pd.Timedelta(value).total_seconds())
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    if isinstance(value, float):
        if math.isnan(value):
            return "__NaN__"
        if math.isinf(value):
            return "__Infinity__" if value > 0 else "__-Infinity__"
    if callable(value):
        module = getattr(value, "__module__", "")
        qualname = getattr(value, "__qualname__", getattr(value, "__name__", repr(value)))
        return f"{module}.{qualname}".strip(".")
    if value is pd.NA:
        return None
    return value


__all__ = [
    "AUDIT_SCHEMA_VERSION",
    "REQUIRED_MARKET_COLUMNS",
    "DEFAULT_ISSUE_POLICIES",
    "AuditConfig",
    "AuditIssue",
    "AuditReport",
    "MarketDataAuditError",
    "RunFingerprints",
    "audit_market_data",
    "build_run_fingerprints",
    "fingerprint_code",
    "fingerprint_config",
    "fingerprint_data",
    "fingerprint_dataframe",
    "sha256_file",
]
