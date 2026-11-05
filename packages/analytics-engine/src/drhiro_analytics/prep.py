"""Series preparation (spec §3.2 prep.py)."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from drhiro_analytics.types import SeriesInput


def to_frame(s: SeriesInput) -> pd.Series:
    """DatetimeIndex daily, NaN for None."""
    idx = [d for d, _ in s.points]
    vals = [np.nan if v is None else float(v) for _, v in s.points]
    return pd.Series(vals, index=pd.DatetimeIndex(idx), name=s.key)


def weekday_adjust(x: pd.Series) -> pd.Series:
    """x - weekday_median + global_median (NaN-safe)."""
    global_median = x.median()
    weekday_medians = x.groupby(x.index.dayofweek).median()
    out = x.copy()
    for dow in range(7):
        m = weekday_medians.get(dow)
        if pd.isna(m):
            m = global_median
        out[x.index.dayofweek == dow] = x[x.index.dayofweek == dow] - m + global_median
    return out


def weekly_sum(x: pd.Series) -> pd.Series:
    """ISO-week sum; week NaN if < 4 days observed."""
    return x.resample("W-MON", label="left", closed="left").agg(lambda v: v.sum() if v.count() >= 4 else np.nan)


def smooth(x: pd.Series) -> pd.Series:
    return x.rolling(7, min_periods=4, center=True).median()


def coverage(x: pd.Series, start, end) -> float:
    """non-NaN days / total days in [start, end]."""
    idx = pd.date_range(start, end, freq="D")
    if len(idx) == 0:
        return 0.0
    x = x.reindex(idx)
    return float(x.notna().sum()) / len(idx)


def first_data_day(s: SeriesInput) -> date | None:
    for d, v in s.points:
        if v is not None:
            return d
    return None


def slice_range(x: pd.Series, start: date, end: date) -> pd.Series:
    return x.reindex(pd.date_range(start, end, freq="D"))


def add_days(d: date, n: int) -> date:
    return d + timedelta(days=n)
