"""The alert body has to carry what the exit code cannot.

A scalar exit code can express one condition. A run can be in several at once:
breaching on one sleeve while three others were never graded. Precedence sends
the breach, and before this the operator learned nothing about the ungraded
half of the book — which is the difference between "one sleeve drifted" and
"one sleeve drifted and we are only watching three of six".
"""
from __future__ import annotations

from scripts.ops.divergence_alert import render_alert


def _sleeve(name, status, *, comparable=True, notes=None):
    return {
        "portfolio": name, "status": status,
        "baseline_comparable": comparable,
        "absolute_divergence_pp": -0.048, "relative_divergence": -0.9,
        "notes": notes or [],
    }


def _report(*sleeves):
    return {"reports": list(sleeves), "backtest_source": "output/shadow_20260902.json"}


# --- exit 5: the new middle state -------------------------------------------


def test_exit_five_renders_a_message_at_all() -> None:
    """Silence on a degradation is how half a book goes unwatched unnoticed."""
    msg = render_alert(5, _report(
        _sleeve("momentum", "OK"),
        _sleeve("earnings_drift", "NO_DATA", comparable=False,
                notes=["no shadow curve for 'earnings_drift'"]),
    ))

    assert msg


def test_exit_five_says_how_much_of_the_book_was_watched() -> None:
    msg = render_alert(5, _report(
        _sleeve("momentum", "OK"),
        _sleeve("sector_rotation", "OK"),
        _sleeve("earnings_drift", "NO_DATA", comparable=False,
                notes=["no shadow curve for 'earnings_drift'"]),
    ))

    assert "2" in msg and "3" in msg, msg


def test_exit_five_names_the_ungraded_sleeves() -> None:
    msg = render_alert(5, _report(
        _sleeve("momentum", "OK"),
        _sleeve("earnings_drift", "NO_DATA", comparable=False,
                notes=["no shadow curve for 'earnings_drift'"]),
    ))

    assert "earnings_drift" in msg


def test_exit_five_does_not_claim_nothing_is_running() -> None:
    """The exact false statement exit 3's text makes when a partial reuses it."""
    msg = render_alert(5, _report(
        _sleeve("momentum", "OK"),
        _sleeve("earnings_drift", "NO_DATA", comparable=False),
    ))

    assert "No drift detection is running" not in msg


# --- exit 1: a breach must not hide the ungraded half ------------------------


def test_a_breach_alert_names_the_ungraded_sleeves_too() -> None:
    msg = render_alert(1, _report(
        _sleeve("momentum", "BREACH"),
        _sleeve("earnings_drift", "NO_DATA", comparable=False),
        _sleeve("tail_risk_hedge", "NO_DATA", comparable=False),
    ))

    assert "momentum" in msg
    assert "earnings_drift" in msg, msg
    assert "tail_risk_hedge" in msg, msg


def test_a_clean_breach_says_nothing_about_ungraded_sleeves() -> None:
    """No noise when the whole book was graded."""
    msg = render_alert(1, _report(
        _sleeve("momentum", "BREACH"),
        _sleeve("sector_rotation", "OK"),
    ))

    assert "ungraded" not in msg.lower(), msg


# --- wording that outlived the pinned feed -----------------------------------


def test_the_breach_text_is_not_specific_to_a_backtest_baseline() -> None:
    """The feed is the rolling shadow now. Telling an operator to look at a
    'backtest baseline' sends them to an artifact this run never read."""
    msg = render_alert(1, _report(_sleeve("momentum", "BREACH")))

    assert "backtest baseline" not in msg.lower(), msg
