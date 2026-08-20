"""The capture universe has to exist inside the container that captures.

KAN-58. ``shared.universe`` resolves the membership snapshot from a path
pinned to the repo root, which inside the image is the WORKDIR. If ``data/``
is not copied into the image the lookup raises at startup — and because
data_ingestion is started only by docker compose with
``restart: unless-stopped``, that is a crash loop rather than a degraded
service. Neither the test suite nor CI builds images, so this file is the only
thing standing between that and a deploy.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SNAPSHOT_REL = "data/universe/sp500_membership.json"


def test_the_snapshot_is_where_shared_universe_looks_for_it():
    from shared.universe import MEMBERSHIP_SNAPSHOT_PATH

    assert MEMBERSHIP_SNAPSHOT_PATH == REPO / "data" / "universe" / "sp500_membership.json"
    assert MEMBERSHIP_SNAPSHOT_PATH.is_file()


def test_the_data_ingestion_image_copies_the_membership_snapshot():
    """The service resolves its capture universe at startup, so a missing
    snapshot is a boot failure, not a missing feature."""
    dockerfile = (REPO / "services/data_ingestion/Dockerfile").read_text()
    assert "COPY data/universe/" in dockerfile, (
        "services/data_ingestion/Dockerfile must COPY data/universe/ — without "
        "it shared.universe cannot resolve the membership snapshot inside the "
        "image and the container crash-loops on startup"
    )


def test_the_snapshot_path_resolves_relative_to_the_package_not_the_cwd():
    """The launchd jobs run from ~/ibc and the container from /app, so a
    cwd-relative path resolves differently in each."""
    from shared.universe import MEMBERSHIP_SNAPSHOT_PATH

    assert MEMBERSHIP_SNAPSHOT_PATH.is_absolute()


def test_the_digest_and_the_runner_expect_the_same_universe():
    """The shortfall alarm is a comparison, so the two sides have to be
    counting the same thing. They disagreed by 41 names — the sleeves' sector,
    thematic and inverse ETFs, which no membership snapshot lists — which
    rendered a nonsensical '544/503' on a healthy day and, because the
    shortfall test was one-sided, stayed silent while every ETF failed.
    """
    from shared.config import AppConfig
    from shared.universe import capture_expected_universe, resolve_watchlist

    config = AppConfig()
    digest_expected = len(
        capture_expected_universe(
            config.universe.watchlist_source,
            config.universe.custom_tickers,
            config.universe.capture_source,
        )
    )

    # What DataIngestionRunner.run_cycle counts: the trading watchlist plus the
    # capture-only remainder.
    from shared.universe import resolve_capture_universe

    tickers = resolve_watchlist(
        config.universe.watchlist_source, config.universe.custom_tickers
    )
    capture_only = [t for t in resolve_capture_universe(config.universe.capture_source)
                    if t not in set(tickers)]
    runner_expected = len(tickers) + len(capture_only)

    assert digest_expected == runner_expected


def test_the_expected_universe_covers_names_outside_the_index():
    """The regression that made the two sides disagree: the trading watchlist
    is not a subset of the index."""
    from shared.universe import (
        capture_expected_universe,
        resolve_capture_universe,
        resolve_watchlist,
    )

    expected = set(capture_expected_universe("sleeves", [], "membership"))
    index = set(resolve_capture_universe("membership"))
    sleeves = set(resolve_watchlist("sleeves", []))

    assert sleeves - index, "precondition: some sleeve names are not index members"
    assert (sleeves - index) <= expected
    assert expected == sleeves | index


@pytest.mark.parametrize("source", ["custom", "sp5000", "sleeve"])
def test_a_capture_source_that_cannot_name_a_universe_is_rejected(source):
    """A capture source that resolves to nothing looks exactly like a healthy
    zero-capture day, and the bars it failed to keep cannot be fetched back.
    ``custom`` is rejected because it carries no tickers of its own here."""
    from shared.universe import resolve_capture_universe

    with pytest.raises(ValueError):
        resolve_capture_universe(source)
