"""Shared helpers for analytics-engine tests: synthetic series builders."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np


def make_points(start: date, days: int, base: float, noise: float = 0.0, seed: int = 7,
                change_at: int | None = None, change_to: float | None = None,
                gap: tuple[int, int] | None = None) -> list[tuple[date, float | None]]:
    rng = np.random.default_rng(seed)
    pts = []
    for i in range(days):
        d = start + timedelta(days=i)
        v = base
        if change_at is not None and i >= change_at:
            v = change_to if change_to is not None else base
        if gap and gap[0] <= i < gap[1]:
            pts.append((d, None))
            continue
        pts.append((d, v + float(rng.normal(0, noise)) if noise else v))
    return pts
