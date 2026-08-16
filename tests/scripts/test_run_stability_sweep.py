"""The sweep driver that feeds ``research.evaluation.stability``.

Story KAN-39. ``parameter_stability`` is pure and cannot run a backtest, so the
grid has to be replayed by something outside ``research/``. This covers the
driver end to end for ``momentum.lookback_days`` on synthetic bars -- a real
sleeve, a real parameter, a real backtest per grid point, just a tiny panel.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from scripts.run_stability_sweep import SLEEVES, main, parse_grid
from shared.universe import BEAR_TICKERS


TICKERS = ["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL"]


def _write_bars(path, sessions: int = 60) -> None:
    """A panel with a stable cross-sectional ranking, so momentum trades.

    The drift is deliberately gentle. Entries are day limit orders priced at
    yesterday's close and worked against today's bar, so a panel that gaps up
    faster than the intraday range never fills anything and the whole sweep
    reads zero.
    """
    start = date(2024, 1, 2)
    bars = {}
    for rank, ticker in enumerate(TICKERS):
        drift = 1.0 + 0.0008 * (len(TICKERS) - rank)
        price = 100.0
        rows = []
        for i in range(sessions):
            price *= drift
            # A deterministic wobble so the trailing stop has something to bite.
            wobble = 1.0 + (0.01 if i % 7 == 0 else -0.003)
            close = price * wobble
            rows.append({
                "date": (start + timedelta(days=i)).isoformat(),
                "open": round(close * 0.999, 4),
                "high": round(close * 1.01, 4),
                "low": round(close * 0.99, 4),
                "close": round(close, 4),
                "volume": 1_000_000,
            })
        bars[ticker] = rows
    path.write_text(json.dumps({"bars": bars}))


class TestGridParsing:
    def test_a_comma_separated_grid_becomes_floats(self):
        assert parse_grid("100,126,152") == [100.0, 126.0, 152.0]

    def test_whitespace_and_trailing_commas_are_tolerated(self):
        assert parse_grid(" 100 , 126 , 152 , ") == [100.0, 126.0, 152.0]

    def test_a_duplicated_grid_point_is_rejected(self):
        # A repeated value silently shrinks the neighborhood.
        with pytest.raises(ValueError, match="duplicate"):
            parse_grid("100,126,126")

    def test_a_non_numeric_grid_point_is_rejected(self):
        with pytest.raises(ValueError, match="numeric"):
            parse_grid("100,about a hundred")


class TestSleeveRegistry:
    def test_momentum_defaults_match_the_shipped_backtest_call_site(self):
        # scripts/run_backtest.py:2318-2328 -- if the shipped sleeve is
        # re-tuned and this table is not, the sweep measures a surface nobody
        # trades.
        momentum = SLEEVES["momentum"]
        assert momentum.defaults["lookback_days"] == 126
        assert momentum.defaults["top_n"] == 5
        assert momentum.defaults["position_size_pct"] == 0.12
        assert momentum.defaults["trailing_stop_pct"] == 0.10

    def test_momentum_carries_the_bear_tickers_the_shipped_sleeve_holds(self):
        # run_backtest passes bear_tickers=BEAR_TICKERS. Dropping it changes
        # the sleeve's exit behaviour, so the swept surface would belong to a
        # strategy nobody trades.
        assert SLEEVES["momentum"].fixed_kwargs["bear_tickers"] == BEAR_TICKERS

    def test_an_unknown_sleeve_is_rejected(self, tmp_path, capsys):
        with pytest.raises(SystemExit):
            main([
                "--sleeve", "no_such_sleeve",
                "--parameter", "lookback_days",
                "--grid", "5,10,15",
                "--center", "10",
                "--bars-from-json", str(tmp_path / "bars.json"),
                "--out", str(tmp_path / "out.json"),
            ])

    def test_a_parameter_the_sleeve_does_not_take_is_rejected(self, tmp_path):
        with pytest.raises(SystemExit):
            main([
                "--sleeve", "momentum",
                "--parameter", "surprise_threshold_pct",
                "--grid", "5,10,15",
                "--center", "10",
                "--bars-from-json", str(tmp_path / "bars.json"),
                "--out", str(tmp_path / "out.json"),
            ])


class TestSweepEndToEnd:
    def test_momentum_lookback_sweep_writes_a_mapping_file(self, tmp_path, capsys):
        bars_path = tmp_path / "bars.json"
        _write_bars(bars_path)
        out_path = tmp_path / "stability" / "momentum-lookback_days.json"

        exit_code = main([
            "--sleeve", "momentum",
            "--parameter", "lookback_days",
            "--grid", "10,15,20",
            "--center", "15",
            "--capital", "100000",
            "--bars-from-json", str(bars_path),
            "--out", str(out_path),
        ])

        assert exit_code == 0
        artifact = json.loads(out_path.read_text())

        assert artifact["sleeve"] == "momentum"
        assert artifact["parameter"] == "lookback_days"
        assert artifact["center"] == 15.0
        assert artifact["metric"] == "sharpe_ratio"
        # The mapping parameter_stability consumes: one metric per grid point.
        assert sorted(artifact["results"]) == ["10.0", "15.0", "20.0"]
        assert all(isinstance(v, float) for v in artifact["results"].values())
        # Every grid point actually replayed the backtest, and the sleeve
        # actually traded -- a sweep of three flat zeros proves nothing.
        assert len(artifact["runs"]) == 3
        assert all("total_trades" in run for run in artifact["runs"])
        assert any(v != 0.0 for v in artifact["results"].values())

    def test_the_artifact_carries_the_stability_verdict(self, tmp_path):
        bars_path = tmp_path / "bars.json"
        _write_bars(bars_path)
        out_path = tmp_path / "momentum-lookback_days.json"

        main([
            "--sleeve", "momentum",
            "--parameter", "lookback_days",
            "--grid", "10,15,20",
            "--center", "15",
            "--bars-from-json", str(bars_path),
            "--out", str(out_path),
        ])

        verdict = json.loads(out_path.read_text())["stability"]

        assert verdict["parameter"] == "lookback_days"
        assert verdict["center"] == 15.0
        assert verdict["neighborhood"] == [10.0, 20.0]
        assert isinstance(verdict["is_plateau"], bool)
        assert verdict["verdict_reason"]

    def test_a_static_universe_sweep_is_flagged_as_survivorship_biased(
        self, tmp_path, capsys
    ):
        bars_path = tmp_path / "bars.json"
        _write_bars(bars_path)
        out_path = tmp_path / "out.json"

        main([
            "--sleeve", "momentum",
            "--parameter", "lookback_days",
            "--grid", "10,15,20",
            "--center", "15",
            "--bars-from-json", str(bars_path),
            "--out", str(out_path),
        ])

        artifact = json.loads(out_path.read_text())
        assert artifact["point_in_time_universe"] is False
        # A stability surface measured on survivors is a surface for a
        # strategy that could not have been traded; say so, loudly.
        assert "SURVIVORSHIP" in capsys.readouterr().out

    def test_a_membership_calendar_scopes_the_sweep(self, tmp_path):
        bars_path = tmp_path / "bars.json"
        _write_bars(bars_path)
        snapshots = tmp_path / "membership.json"
        snapshots.write_text(json.dumps({"2024-01-02": TICKERS}))
        out_path = tmp_path / "out.json"

        main([
            "--sleeve", "momentum",
            "--parameter", "lookback_days",
            "--grid", "10,15,20",
            "--center", "15",
            "--bars-from-json", str(bars_path),
            "--universe-snapshots", str(snapshots),
            "--out", str(out_path),
        ])

        artifact = json.loads(out_path.read_text())
        assert artifact["point_in_time_universe"] is True
        assert sorted(artifact["tickers"]) == sorted(TICKERS)
        assert any(v != 0.0 for v in artifact["results"].values())

    def test_a_center_outside_the_grid_is_rejected_before_any_backtest_runs(
        self, tmp_path
    ):
        bars_path = tmp_path / "bars.json"
        _write_bars(bars_path)
        out_path = tmp_path / "out.json"

        with pytest.raises(SystemExit):
            main([
                "--sleeve", "momentum",
                "--parameter", "lookback_days",
                "--grid", "10,20",
                "--center", "15",
                "--bars-from-json", str(bars_path),
                "--out", str(out_path),
            ])

        assert not out_path.exists()
