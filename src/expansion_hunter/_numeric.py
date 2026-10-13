"""Exact arithmetic shortcuts for the bounded native float candle windows."""

from __future__ import annotations

from math import isfinite
from statistics import mean as statistics_mean


def float_mean(values):
    """Keep statistics.mean rounding while avoiding its generic Fraction work.

    Float denominators are powers of two. Accumulating their integer ratios
    therefore gives the same exact sum and final rounding as statistics.mean.
    fmean rounds the sum before division and can change a threshold by one ULP.
    Preserve the generic implementation for empty, mixed, or nonfinite inputs.
    """
    values = tuple(values)
    count = len(values)
    if not count:
        return statistics_mean(values)
    total, denominator = 0, 1
    for value in values:
        if type(value) is not float or not isfinite(value):
            return statistics_mean(values)
        numerator, value_denominator = value.as_integer_ratio()
        if value_denominator > denominator:
            total *= value_denominator // denominator
            denominator = value_denominator
        total += numerator * (denominator // value_denominator)
    return total / (denominator * count)
