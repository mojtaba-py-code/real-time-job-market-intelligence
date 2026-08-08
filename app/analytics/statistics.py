"""Statistical primitives shared by every analytics engine.

Pure functions, no I/O, no database. Keeping the mathematics here means the
trend classifier and the emerging-technology detector can be tested against
known series instead of against a live corpus.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date, timedelta

from app.models.enums import TrendDirection

#: Below this many observations a trend statement is not defensible.
MIN_TREND_SAMPLES = 5


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolation percentile (``q`` in ``[0, 100]``)."""
    if not values:
        return None
    if not 0.0 <= q <= 100.0:
        raise ValueError("q must be between 0 and 100")
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * (q / 100.0)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[int(position)])
    weight = position - lower
    return float(ordered[lower] * (1 - weight) + ordered[upper] * weight)


def median(values: Sequence[float]) -> float | None:
    """Middle value of a sample."""
    return percentile(values, 50)


def mean(values: Sequence[float]) -> float | None:
    """Arithmetic mean, or ``None`` for an empty sample."""
    return sum(values) / len(values) if values else None


def standard_deviation(values: Sequence[float]) -> float:
    """Population standard deviation."""
    if len(values) < 2:
        return 0.0
    average = sum(values) / len(values)
    variance = sum((value - average) ** 2 for value in values) / len(values)
    return math.sqrt(variance)


def coefficient_of_variation(values: Sequence[float]) -> float:
    """Relative dispersion: standard deviation divided by the mean."""
    average = mean(values)
    if not average:
        return 0.0
    return standard_deviation(values) / abs(average)


def percent_change(current: float, previous: float) -> float | None:
    """Percentage change from ``previous`` to ``current``.

    Returns ``None`` when the baseline is zero: "infinite growth" is not a
    number a dashboard should print.
    """
    if previous == 0:
        return None
    return ((current - previous) / previous) * 100.0


def z_score(value: float, values: Sequence[float]) -> float | None:
    """How many standard deviations ``value`` sits from the sample mean."""
    if len(values) < 2:
        return None
    deviation = standard_deviation(values)
    if deviation == 0:
        return None
    average = mean(values) or 0.0
    return (value - average) / deviation


def moving_average(values: Sequence[float], window: int) -> list[float]:
    """Trailing simple moving average.

    The first ``window - 1`` points use the partial window they have, so the
    output always has the same length as the input.
    """
    if window <= 1 or not values:
        return list(values)
    result: list[float] = []
    running = 0.0
    for index, value in enumerate(values):
        running += value
        if index >= window:
            running -= values[index - window]
        divisor = min(index + 1, window)
        result.append(running / divisor)
    return result


def linear_slope(values: Sequence[float]) -> float:
    """Least-squares slope of a series against its index."""
    n = len(values)
    if n < 2:
        return 0.0
    mean_x = (n - 1) / 2
    mean_y = sum(values) / n
    numerator = sum((index - mean_x) * (value - mean_y) for index, value in enumerate(values))
    denominator = sum((index - mean_x) ** 2 for index in range(n))
    return numerator / denominator if denominator else 0.0


def classify_trend(
    *,
    change_pct: float | None,
    series: Sequence[float],
    rising_threshold: float = 10.0,
    declining_threshold: float = -10.0,
    volatility_threshold: float = 0.6,
    min_samples: int = MIN_TREND_SAMPLES,
) -> TrendDirection:
    """Turn a change and a series into a defensible trend label.

    Order matters: an insufficient sample is reported as such, a very noisy
    series is called volatile rather than rising or declining, and only then is
    the direction decided.
    """
    non_zero = [value for value in series if value > 0]
    if len(non_zero) < min_samples or change_pct is None:
        return TrendDirection.INSUFFICIENT_DATA

    # A series that swings wildly has no meaningful direction, even if the
    # endpoints happen to differ.
    if (
        coefficient_of_variation(series) > volatility_threshold
        and abs(change_pct) < rising_threshold * 3
    ):
        return TrendDirection.VOLATILE

    if change_pct >= rising_threshold:
        return TrendDirection.RISING
    if change_pct <= declining_threshold:
        return TrendDirection.DECLINING
    return TrendDirection.STABLE


def fill_missing_days(
    points: Sequence[tuple[date, int]], *, start: date, end: date
) -> list[tuple[date, int]]:
    """Expand a sparse daily series into a dense one, filling gaps with zero.

    Without this, a skill that was not mentioned on a given day would simply be
    missing from the series and every moving average would be wrong.
    """
    known = dict(points)
    dense: list[tuple[date, int]] = []
    current = start
    while current <= end:
        dense.append((current, known.get(current, 0)))
        current += timedelta(days=1)
    return dense


def share(part: float, whole: float) -> float:
    """Safe ratio in ``[0, 1]``."""
    if whole <= 0:
        return 0.0
    return max(0.0, min(1.0, part / whole))


def confidence_from_sample(size: int, *, full_confidence_at: int = 50) -> float:
    """Map a sample size onto a confidence in ``[0, 1]``.

    Small samples produce small confidences, which is the honest way to report
    a trend derived from a handful of postings.
    """
    if size <= 0:
        return 0.0
    return round(min(1.0, math.log1p(size) / math.log1p(full_confidence_at)), 4)


__all__ = [
    "MIN_TREND_SAMPLES",
    "classify_trend",
    "coefficient_of_variation",
    "confidence_from_sample",
    "fill_missing_days",
    "linear_slope",
    "mean",
    "median",
    "moving_average",
    "percent_change",
    "percentile",
    "share",
    "standard_deviation",
    "z_score",
]
