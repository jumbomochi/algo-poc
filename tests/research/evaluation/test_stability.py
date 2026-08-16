"""Parameter stability: an edge that lives on one parameter value is not an edge.

Story KAN-39 / direction doc D10. ``parameter_stability`` reads a precomputed
{parameter value -> metric} surface and decides whether the shipped value sits
on a plateau or on a knife's edge. Every case here is synthetic on purpose --
the analysis is pure arithmetic and must be provable without running a
backtest.
"""
from __future__ import annotations

import builtins
import io
import os

import pytest

from research.evaluation.stability import StabilityReport, parameter_stability


class TestVerdict:
    def test_a_knife_edge_center_is_not_a_plateau(self):
        report = parameter_stability(
            {100.0: 0.1, 126.0: 2.0, 152.0: 0.1},
            center=126.0,
        )

        assert report.is_plateau is False
        # The reason has to carry the number that decided it, or an operator
        # reading the artifact cannot tell a marginal fail from a blowout.
        assert "95.0%" in report.verdict_reason

    def test_a_flat_surface_is_a_plateau(self):
        report = parameter_stability(
            {100.0: 0.9, 126.0: 1.0, 152.0: 1.1},
            center=126.0,
        )

        assert report.is_plateau is True
        assert report.relative_degradation == pytest.approx(0.0)

    def test_a_neighborhood_mean_exactly_at_the_tolerance_passes(self):
        # mean(0.6, 0.8) = 0.7, exactly 30% below the center. The boundary is
        # inclusive, and binary float dust in (1.0 - 0.7) must not flip it.
        report = parameter_stability(
            {100.0: 0.6, 126.0: 1.0, 152.0: 0.8},
            center=126.0,
            plateau_tolerance=0.30,
        )

        assert report.relative_degradation == pytest.approx(0.30)
        assert report.is_plateau is True

    def test_one_point_worse_than_the_tolerance_fails(self):
        # mean(0.6, 0.79) = 0.695 -- 30.5% below the center, past the boundary.
        report = parameter_stability(
            {100.0: 0.6, 126.0: 1.0, 152.0: 0.79},
            center=126.0,
            plateau_tolerance=0.30,
        )

        assert report.relative_degradation == pytest.approx(0.305)
        assert report.is_plateau is False

    def test_a_negative_neighbor_fails_even_when_the_mean_is_close(self):
        # mean(2.05, -0.05) = 1.0 -- dead level with the center, yet one
        # neighboring parameter value loses money. That is not a plateau.
        report = parameter_stability(
            {100.0: 2.05, 126.0: 1.0, 152.0: -0.05},
            center=126.0,
        )

        assert report.relative_degradation == pytest.approx(0.0)
        assert report.is_plateau is False
        assert "152" in report.verdict_reason
        assert "-0.05" in report.verdict_reason


class TestRejectedInput:
    def test_an_empty_surface_raises(self):
        with pytest.raises(ValueError, match="center"):
            parameter_stability({}, center=126.0)

    def test_a_center_absent_from_the_surface_raises(self):
        with pytest.raises(ValueError, match="center"):
            parameter_stability({100.0: 0.5, 152.0: 0.5}, center=126.0)

    def test_an_empty_neighborhood_raises(self):
        with pytest.raises(ValueError, match="neighbou?rs"):
            parameter_stability({126.0: 1.0}, center=126.0)

    def test_a_single_point_neighborhood_raises(self):
        # One neighbor cannot distinguish a plateau from a slope.
        with pytest.raises(ValueError, match="neighbou?rs"):
            parameter_stability({126.0: 1.0, 152.0: 0.9}, center=126.0)

    def test_a_zero_center_metric_raises(self):
        with pytest.raises(ValueError, match="zero"):
            parameter_stability(
                {100.0: 0.1, 126.0: 0.0, 152.0: 0.1},
                center=126.0,
            )

    def test_a_negative_tolerance_raises(self):
        with pytest.raises(ValueError, match="tolerance"):
            parameter_stability(
                {100.0: 0.9, 126.0: 1.0, 152.0: 1.1},
                center=126.0,
                plateau_tolerance=-0.1,
            )


class TestReportArithmetic:
    def test_the_report_carries_the_surface_it_judged(self):
        report = parameter_stability(
            {152.0: 0.8, 100.0: 0.6, 126.0: 1.0},
            center=126.0,
            plateau_tolerance=0.40,
        )

        assert isinstance(report, StabilityReport)
        assert report.center == 126.0
        assert report.center_metric == pytest.approx(1.0)
        # Neighbors only, ascending by parameter value -- the center is
        # reported separately and must not be averaged into its own baseline.
        assert report.neighborhood == [100.0, 152.0]
        assert report.metrics == [pytest.approx(0.6), pytest.approx(0.8)]
        assert report.neighborhood_mean == pytest.approx(0.7)
        assert report.neighborhood_std == pytest.approx(0.1414213562373095)
        assert report.relative_degradation == pytest.approx(0.30)
        assert report.is_plateau is True

    def test_degradation_is_scaled_by_the_magnitude_of_the_center(self):
        # |center| in the denominator: a center of -1.0 whose neighbors average
        # -2.0 has degraded by 100% of the center's magnitude, not -100%.
        report = parameter_stability(
            {100.0: -2.0, 126.0: -1.0, 152.0: -2.0},
            center=126.0,
        )

        assert report.relative_degradation == pytest.approx(1.0)
        assert report.is_plateau is False

    def test_the_parameter_name_is_carried_through(self):
        report = parameter_stability(
            {100.0: 0.9, 126.0: 1.0, 152.0: 1.1},
            center=126.0,
            parameter="lookback_days",
        )

        assert report.parameter == "lookback_days"


class TestPurity:
    def test_the_analysis_touches_no_files_and_runs_no_backtest(
        self, tmp_path, monkeypatch
    ):
        """AC6: pure arithmetic -- no data files, no backtest, no cwd."""
        monkeypatch.chdir(tmp_path)

        def _no_open(*args, **kwargs):  # pragma: no cover - failure path
            raise AssertionError(f"parameter_stability opened {args!r}")

        monkeypatch.setattr(builtins, "open", _no_open)
        # Path.read_text() reaches io.open, not builtins.open -- patching only
        # the latter would let the sibling modules' file access slip through.
        monkeypatch.setattr(io, "open", _no_open)
        monkeypatch.setattr(os, "listdir", _no_open)
        monkeypatch.setattr(os, "scandir", _no_open)

        report = parameter_stability(
            {100.0: 0.9, 126.0: 1.0, 152.0: 1.1},
            center=126.0,
        )

        # The guards have done their job; lift them before inspecting the
        # directory, because iterdir() itself goes through os.listdir (3.12)
        # or os.scandir (3.13) depending on the interpreter.
        monkeypatch.undo()

        assert report.is_plateau is True
        assert list(tmp_path.iterdir()) == []
