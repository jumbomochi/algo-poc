# research/evaluation/folds.py
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InnerFold:
    train: tuple[int, int]
    validate: tuple[int, int]


@dataclass(frozen=True)
class OuterFold:
    train: tuple[int, int]
    test: tuple[int, int]
    inner: tuple[InnerFold, ...]


def _segment_bounds(n: int, n_segments: int) -> list[tuple[int, int]]:
    size = n // n_segments
    bounds: list[tuple[int, int]] = []
    start = 0
    for i in range(n_segments):
        end = n if i == n_segments - 1 else start + size
        bounds.append((start, end))
        start = end
    return bounds


def _inner_folds(start: int, end: int, n_inner: int, gap: int) -> list[InnerFold]:
    length = end - start
    if length <= 0 or n_inner <= 0:
        return []
    segs = _segment_bounds(length, n_inner + 1)
    folds: list[InnerFold] = []
    for j in range(n_inner):
        v_start = start + segs[j + 1][0]
        v_end = start + segs[j + 1][1]
        it_end = max(start, v_start - gap)
        folds.append(InnerFold(train=(start, it_end), validate=(v_start, v_end)))
    return folds


def nested_walk_forward(
    n_dates: int, n_outer: int, n_inner: int, horizon: int, embargo: int
) -> list[OuterFold]:
    gap = horizon + embargo

    # The first (smallest) outer-train span must itself be large enough to
    # host n_inner nested inner folds, each needing a full `gap` of
    # purge+embargo separation from its validation slice. An outer-train span
    # of gap * (n_inner + 1) is the minimum for that; reserving one extra
    # `gap` as the anchor (before the first outer test starts) guarantees the
    # first outer fold's train_end (= test_start - gap) meets that minimum.
    anchor = gap * (n_inner + 2)
    if n_dates < anchor + n_outer:
        raise ValueError("not enough dates for the requested outer/inner folds")

    remaining = n_dates - anchor
    test_size = remaining // n_outer
    outer_folds: list[OuterFold] = []
    for k in range(n_outer):
        test_start = anchor + k * test_size
        test_end = n_dates if k == n_outer - 1 else anchor + (k + 1) * test_size
        train_end = test_start - gap
        inner = _inner_folds(0, train_end, n_inner, gap)
        outer_folds.append(
            OuterFold(train=(0, train_end), test=(test_start, test_end), inner=tuple(inner))
        )
    return outer_folds
