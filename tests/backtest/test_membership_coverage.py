"""Coverage floor for a point-in-time baseline (direction doc D14).

Removing survivorship bias by scoring against a point-in-time universe only
works if the historical members can actually be priced. A member whose bars
cannot be pulled is silently skipped, and a silently-skipped delisting is
survivorship bias walking back in through the side door. These tests pin the
measurement and the 5% floor that turns it into a gate.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from backtest.membership import (
    COVERAGE_BLOCKED,
    COVERAGE_OK,
    DEFAULT_COVERAGE_FLOOR_PCT,
    CoverageReport,
    measure_coverage,
    priced_days_from_bars,
)
from shared.universe import MembershipCalendar


START = date(2020, 1, 6)


def _sessions(count: int, start: date = START) -> list[date]:
    """``count`` consecutive weekday-ish dates — spacing is irrelevant here."""
    return [start + timedelta(days=i) for i in range(count)]


def _calendar(members: list[str], start: date = START) -> MembershipCalendar:
    """A calendar whose membership never changes, effective from ``start``."""
    return MembershipCalendar({start: members})


class TestMembershipDayArithmetic:
    def test_exclusions_are_weighted_per_session_not_per_ticker(self):
        """AC1: a name unpriceable for half the sessions costs half its days.

        Five members over ten sessions is 50 membership-days. One member that
        can only be priced for five of them loses 5 days, not 10 — 10%, not the
        20% a per-ticker count would report.
        """
        sessions = _sessions(10)
        membership = _calendar(["AAA", "BBB", "CCC", "DDD", "EEE"])
        priced = {t: set(sessions) for t in ("AAA", "BBB", "CCC", "DDD")}
        priced["EEE"] = set(sessions[:5])

        report = measure_coverage(
            membership, sessions=sessions, priced_tickers=priced
        )

        assert report.total_membership_days == 50
        assert report.excluded_membership_days == 5
        assert report.excluded_pct == pytest.approx(10.0)

    def test_a_name_that_left_the_index_early_costs_fewer_days(self):
        """Membership-days, not ticker-count: a short tenure weighs less.

        DDD is a member for the first two sessions only and cannot be priced at
        all. Its whole tenure is 2 of the 22 membership-days in the window, so
        the exclusion is 9.09% — a per-ticker count would have called it 25%.
        """
        sessions = _sessions(10)
        membership = MembershipCalendar({
            sessions[0]: ["AAA", "BBB", "CCC", "DDD"],
            sessions[2]: ["AAA", "BBB", "CCC"],
        })

        report = measure_coverage(
            membership,
            sessions=sessions,
            priced_tickers={"AAA", "BBB", "CCC"},
        )

        assert report.total_membership_days == 4 * 2 + 3 * 8
        assert report.excluded_membership_days == 2
        assert report.excluded_pct == pytest.approx(2 / 32 * 100)

    def test_sessions_before_the_first_snapshot_contribute_nothing(self):
        """No membership history means no members — not a free pass either."""
        sessions = _sessions(4, start=date(2019, 12, 30)) + _sessions(6)
        membership = _calendar(["AAA", "BBB"])

        report = measure_coverage(
            membership, sessions=sessions, priced_tickers={"AAA", "BBB"}
        )

        assert report.total_membership_days == 12
        assert report.state == COVERAGE_OK

    def test_no_membership_days_at_all_is_blocked_not_a_pass(self):
        """A window that never overlaps the membership verified nothing."""
        membership = _calendar(["AAA"], start=date(2025, 1, 2))

        report = measure_coverage(
            membership, sessions=_sessions(5), priced_tickers={"AAA"}
        )

        assert report.total_membership_days == 0
        assert report.state == COVERAGE_BLOCKED


class TestFloorBoundary:
    def _report_at(self, excluded_days: int) -> CoverageReport:
        """1000 membership-days, ``excluded_days`` of them unpriceable."""
        sessions = _sessions(100)
        membership = _calendar([f"T{i:02d}" for i in range(10)])
        priced = {f"T{i:02d}": set(sessions) for i in range(10)}
        # Strip whole sessions off one ticker until the count is reached.
        priced["T00"] = set(sessions[excluded_days:])
        return measure_coverage(
            membership, sessions=sessions, priced_tickers=priced
        )

    def test_just_under_the_floor_is_ok(self):
        report = self._report_at(49)
        assert report.excluded_pct == pytest.approx(4.9)
        assert report.state == COVERAGE_OK

    def test_exactly_at_the_floor_is_ok(self):
        """The direction doc says "≤ 5%", so 5.0 passes."""
        report = self._report_at(50)
        assert report.excluded_pct == pytest.approx(5.0)
        assert report.state == COVERAGE_OK

    def test_just_over_the_floor_is_blocked(self):
        report = self._report_at(51)
        assert report.excluded_pct == pytest.approx(5.1)
        assert report.state == COVERAGE_BLOCKED

    def test_floor_is_recorded_and_configurable(self):
        sessions = _sessions(10)
        membership = _calendar(["AAA", "BBB"])
        report = measure_coverage(
            membership,
            sessions=sessions,
            priced_tickers={"AAA"},
            floor_pct=60.0,
        )

        assert report.excluded_pct == pytest.approx(50.0)
        assert report.floor_pct == pytest.approx(60.0)
        assert report.state == COVERAGE_OK
        assert self._report_at(51).floor_pct == pytest.approx(
            DEFAULT_COVERAGE_FLOOR_PCT
        )


class TestExcludedTickers:
    def test_names_every_dropped_ticker_with_its_membership_day_count(self):
        sessions = _sessions(10)
        membership = _calendar(["AAA", "BBB", "CCC"])
        priced = {"AAA": set(sessions), "BBB": set(sessions[:7])}

        report = measure_coverage(
            membership, sessions=sessions, priced_tickers=priced
        )

        assert report.excluded_tickers == {"BBB": 3, "CCC": 10}
        assert report.excluded_membership_days == 13

    def test_complete_coverage_reports_an_empty_dict(self):
        sessions = _sessions(10)
        membership = _calendar(["AAA", "BBB"])

        report = measure_coverage(
            membership, sessions=sessions, priced_tickers={"AAA", "BBB"}
        )

        assert report.excluded_tickers == {}
        assert report.excluded_membership_days == 0
        assert report.excluded_pct == pytest.approx(0.0)
        assert report.state == COVERAGE_OK

    def test_always_tradable_instruments_are_not_index_membership_days(self):
        """The ETF sleeves are not constituents, so they are not the denominator."""
        sessions = _sessions(10)
        membership = MembershipCalendar({START: ["AAA"]}, always=["SPY", "XLK"])

        report = measure_coverage(
            membership, sessions=sessions, priced_tickers={"AAA"}
        )

        assert report.total_membership_days == 10
        assert report.excluded_tickers == {}


class TestReportSerialisation:
    def test_to_dict_carries_every_field_the_artifact_needs(self):
        sessions = _sessions(10)
        membership = _calendar(["AAA", "BBB"])

        payload = measure_coverage(
            membership, sessions=sessions, priced_tickers={"AAA"}
        ).to_dict()

        assert set(payload) == {
            "total_membership_days",
            "excluded_membership_days",
            "excluded_pct",
            "excluded_tickers",
            "floor_pct",
            "state",
        }
        assert payload["state"] == COVERAGE_BLOCKED

    def test_excluded_tickers_are_ordered_for_a_stable_artifact(self):
        sessions = _sessions(3)
        membership = _calendar(["ZZZ", "AAA", "MMM"])

        report = measure_coverage(
            membership, sessions=sessions, priced_tickers=set()
        )

        assert list(report.excluded_tickers) == ["AAA", "MMM", "ZZZ"]


class TestPricedDaysFromBars:
    def test_derives_the_priceable_sessions_from_loaded_bars(self):
        sessions = _sessions(3)
        bars = {
            "AAA": [{"date": d, "close": 1.0} for d in sessions],
            "BBB": [{"date": sessions[0], "close": 1.0}],
        }

        priced = priced_days_from_bars(bars)

        assert priced["AAA"] == set(sessions)
        assert priced["BBB"] == {sessions[0]}

    def test_a_member_with_no_bars_at_all_loses_every_day(self):
        sessions = _sessions(4)
        membership = _calendar(["AAA", "DEAD"])
        bars = {"AAA": [{"date": d, "close": 1.0} for d in sessions]}

        report = measure_coverage(
            membership,
            sessions=sessions,
            priced_tickers=priced_days_from_bars(bars),
        )

        assert report.excluded_tickers == {"DEAD": 4}
        assert report.state == COVERAGE_BLOCKED
