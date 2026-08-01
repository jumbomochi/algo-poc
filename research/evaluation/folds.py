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


# Fraction of an axis's length reserved as the initial ("anchor") training
# block before the first test/validate segment begins. Because this is a
# *fraction* of the available span rather than a fixed offset, the anchor --
# and therefore the first fold's training width -- grows as more history
# (`n`) becomes available. This is what fixes the original defect, where the
# first fold's training window was pinned to a constant regardless of
# n_dates.
_ANCHOR_RATIO = 0.5


def _expanding_folds(n: int, k: int, gap: int, what: str) -> list[tuple[int, int, int]]:
    """Split the half-open axis [0, n) into `k` anchored/expanding folds.

    Each fold reuses all data from 0 up to its own `train_end` (anchored /
    expanding scheme -- training never rolls forward, it only grows), followed
    by a `gap`-wide purge/embargo buffer, followed by a contiguous
    test/validate segment. The `k` segments partition [anchor, n) so that
    together with fold 0..k-1 every later date is covered exactly once by some
    segment.

    Returns a list of `(train_end, seg_start, seg_end)` tuples, one per fold,
    in increasing order. Raises ValueError (message contains "not enough
    dates") if `n` is too small to carve out `k` non-empty, properly
    purged/embargoed folds with a non-empty initial training block.
    """
    if k <= 0:
        return []

    anchor = max(1, round(n * _ANCHOR_RATIO))
    remaining = n - anchor
    seg_size = remaining // k if k > 0 else 0

    if anchor <= gap or remaining < k or seg_size < 1:
        raise ValueError(
            f"not enough dates: n={n} is too small for {what} "
            f"(k={k}, gap={gap})"
        )

    folds: list[tuple[int, int, int]] = []
    for j in range(k):
        seg_start = anchor + j * seg_size
        seg_end = n if j == k - 1 else anchor + (j + 1) * seg_size
        train_end = seg_start - gap
        if train_end <= 0:
            raise ValueError(
                f"not enough dates: n={n} is too small for {what} "
                f"(k={k}, gap={gap})"
            )
        folds.append((train_end, seg_start, seg_end))
    return folds


def nested_walk_forward(
    n_dates: int, n_outer: int, n_inner: int, horizon: int, embargo: int
) -> list[OuterFold]:
    gap = horizon + embargo

    outer_raw = _expanding_folds(n_dates, n_outer, gap, what="outer folds")

    outer_folds: list[OuterFold] = []
    for train_end, test_start, test_end in outer_raw:
        # Recurse the same anchored/expanding scheme *inside* the outer-train
        # span: the inner axis is [0, train_end), so the inner anchor (and
        # thus fold-0 inner training width) also scales with however much
        # outer-train data is available -- which itself scales with n_dates.
        inner_raw = _expanding_folds(train_end, n_inner, gap, what="inner folds")
        inner = tuple(
            InnerFold(train=(0, it_end), validate=(v_start, v_end))
            for it_end, v_start, v_end in inner_raw
        )
        outer_folds.append(
            OuterFold(train=(0, train_end), test=(test_start, test_end), inner=inner)
        )
    return outer_folds
