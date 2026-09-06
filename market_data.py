"""Strategy-owned market-data loading and date/index resolution.

Every strategy may expose ``DATA_FILE`` and ``TIMEFRAME``.  This module keeps
the CSV contract in one place so engines and optimizers do not need hard-coded
knowledge of BTC, a filename, or a candle interval.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


MARKET_COLUMNS = (
    "Open time",
    "Close time",
    "Open",
    "High",
    "Low",
    "Close",
    "Volume",
)


def _path_key(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve())


def _timeframe_delta(timeframe: str | None) -> pd.Timedelta | None:
    if timeframe is None or str(timeframe).strip().lower() in {"", "auto", "infer"}:
        return None
    text = str(timeframe).strip().lower()
    aliases = {
        "m": "min",
        "h": "h",
        "d": "d",
    }
    if text[-1:] in aliases and text[:-1].isdigit():
        text = text[:-1] + aliases[text[-1]]
    delta = pd.Timedelta(text)
    if delta <= pd.Timedelta(0):
        raise ValueError("TIMEFRAME must be a positive pandas-compatible interval")
    return delta


@lru_cache(maxsize=16)
def _open_times(path_key: str) -> pd.Series:
    values = pd.read_csv(
        path_key,
        usecols=["Open time"],
        parse_dates=["Open time"],
    )["Open time"]
    if values.empty:
        raise ValueError(f"market-data file contains no candles: {path_key}")
    if values.isna().any():
        raise ValueError(f"market-data file contains invalid Open time values: {path_key}")
    return values


def clear_market_data_cache() -> None:
    """Clear timestamp caches after a candle file is replaced in-place."""
    _open_times.cache_clear()
    _inferred_interval.cache_clear()
    _load_market_window.cache_clear()


@lru_cache(maxsize=16)
def _inferred_interval(path_key):
    times = _open_times(path_key)
    deltas = times.diff().dropna()
    if (deltas <= pd.Timedelta(0)).any():
        raise ValueError('Open time must be strictly increasing without duplicates')
    if deltas.empty:
        return None
    counts = deltas.value_counts()
    interval = counts.index[0]
    if counts.iloc[0] / len(deltas) < .8:
        raise ValueError('Ambiguous or mixed candle spacing; provide one consistent timeframe per file')
    if ((deltas.to_numpy(dtype='timedelta64[ns]').astype('int64') % interval.value) != 0).any():
        raise ValueError('Mixed candle spacing: gaps must be whole multiples of the base interval')
    return interval


def timeframe_label(interval):
    for suffix, unit in (('d', 86400), ('h', 3600), ('m', 60), ('s', 1)):
        seconds = interval.total_seconds()
        if seconds >= unit and seconds % unit == 0:
            return f'{int(seconds // unit)}{suffix}'
    return str(interval)


@dataclass(frozen=True)
class MarketDataSource:
    """A reusable OHLCV source owned by a strategy plug-in."""

    data_file: str | Path
    timeframe: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "data_file", Path(self.data_file).expanduser().resolve())
        _timeframe_delta(self.timeframe)

    @property
    def path_key(self) -> str:
        return _path_key(self.data_file)

    def open_times(self) -> pd.Series:
        return _open_times(self.path_key)

    def inferred_interval(self) -> pd.Timedelta:
        inferred = _inferred_interval(self.path_key)
        if inferred is None:
            declared = _timeframe_delta(self.timeframe)
            if declared is None:
                raise ValueError("cannot infer timeframe from fewer than two valid candles")
            return declared
        return inferred

    def interval(self) -> pd.Timedelta:
        declared = _timeframe_delta(self.timeframe)
        inferred = self.inferred_interval()
        if declared is not None and inferred != declared:
            raise ValueError(
                f"declared TIMEFRAME {self.timeframe!r} does not match the detected "
                f"candle interval {inferred} in {self.data_file}"
            )
        return declared or inferred

    def coverage(self) -> dict[str, Any]:
        times = self.open_times()
        interval = self.interval()
        return {
            "data_file": str(self.data_file),
            "timeframe": timeframe_label(interval),
            "first_candle": times.iloc[0].strftime("%Y-%m-%d %H:%M:%S"),
            "last_candle": times.iloc[-1].strftime("%Y-%m-%d %H:%M:%S"),
            "end_exclusive": (times.iloc[-1] + interval).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "interval_seconds": float(interval.total_seconds()),
        }

    def resolve_index(self, value: Any) -> int:
        if isinstance(value, (int, np.integer)):
            return int(value)
        target = pd.to_datetime(value)
        return int(self.open_times().searchsorted(target))

    def format_bound(self, value: Any) -> str:
        index = self.resolve_index(value) if not isinstance(value, int) else int(value)
        times = self.open_times()
        if 0 <= index < len(times):
            timestamp = times.iloc[index]
        elif index == len(times):
            timestamp = times.iloc[-1] + self.interval()
        else:
            return str(value)
        return timestamp.strftime("%Y-%m-%d %H:%M")

    def month_start_indices(self, start: int, end: int) -> list[int]:
        window = self.open_times().iloc[int(start):int(end)]
        month_keys = window.dt.strftime("%Y-%m")
        keep = ~month_keys.duplicated()
        return [int(index) for index in window.index[keep]]

    def load(self, start: Any, end: Any, warmup_candles: int = 0) -> dict[str, Any]:
        interval = self.interval()
        start_index = self.resolve_index(start)
        end_index = self.resolve_index(end)
        if end_index <= start_index:
            raise ValueError("end must resolve to a candle after start")
        if start_index < 0 or end_index > len(self.open_times()):
            raise ValueError('Requested candle indices are outside the dataset')
        warmup_candles = max(0, int(warmup_candles))
        data_start = max(0, start_index - warmup_candles)
        return _load_market_window(
            self.path_key,
            timeframe_label(interval),
            data_start,
            start_index,
            end_index,
        )


@lru_cache(maxsize=4)
def _load_market_window(
    path_key: str,
    timeframe: str,
    data_start: int,
    start_index: int,
    end_index: int,
) -> dict[str, Any]:
    rows_to_read = end_index - data_start
    frame = pd.read_csv(
        path_key,
        skiprows=range(1, data_start + 1),
        nrows=rows_to_read,
        usecols=list(MARKET_COLUMNS),
    )
    if frame.empty:
        raise ValueError("the selected start/end range contains no candles")

    open_time_values = frame["Open time"].astype(str).tolist()
    close_time_values = (
        pd.to_datetime(frame["Close time"], utc=True, format="mixed", errors="raise")
        + pd.Timedelta(milliseconds=1)
    ).dt.strftime("%Y-%m-%d %H:%M:%S.%f").tolist()
    open_time_ns = np.asarray(
        pd.to_datetime(
            frame["Open time"], utc=True, format="mixed", errors="raise"
        ).array.asi8,
        dtype=np.int64,
    )
    warmup_offset = start_index - data_start
    history = {
        "open_prices": frame["Open"].to_numpy(dtype=float),
        "close_prices": frame["Close"].to_numpy(dtype=float),
        "open_times": open_time_values,
        "close_times": close_time_values,
        "low_prices": frame["Low"].to_numpy(dtype=float),
        "high_prices": frame["High"].to_numpy(dtype=float),
        "volume_prices": frame["Volume"].to_numpy(dtype=float),
        "open_time_ns": open_time_ns,
    }
    source = MarketDataSource(path_key, timeframe)
    return {
        "data_file": path_key,
        "timeframe": timeframe,
        "start": start_index,
        "end": end_index,
        "data_start": data_start,
        "warmup_offset": warmup_offset,
        "month_starts": source.month_start_indices(start_index, end_index),
        **{name: values[warmup_offset:] for name, values in history.items()},
        **{f"history_{name}": values for name, values in history.items()},
    }


__all__ = [
    "MARKET_COLUMNS",
    "MarketDataSource",
    "clear_market_data_cache",
]
