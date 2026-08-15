#!/usr/bin/env python3
"""Render the Telegram alert text for a divergence-monitor run.

KAN-43. Pure rendering — this module never sends anything; the launchd wrapper
(``deploy/launchd/run_divergence.sh``) owns delivery via the shared
``telegram()`` helper. Split this way so the message content is unit-testable
without a network stub, and so a rendering failure degrades to the wrapper's
generic fallback text rather than to silence.

Input is the JSON report ``scripts/divergence_monitor.py`` already writes plus
the tail of the run's log; the monitor itself is deliberately untouched.

Usage:
    python scripts/ops/divergence_alert.py --exit-code 1 \\
        --report output/divergence_20260816.json \\
        --log ~/ibc/logs/divergence_20260816.log
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
from pathlib import Path
from typing import Any

# This script is executed by path from the launchd wrapper (`$VENV
# scripts/ops/divergence_alert.py`), which puts `scripts/ops/` — not the repo
# root — on sys.path. Pin the repo root explicitly so the import resolves to
# THIS checkout rather than to whatever tree an editable install happens to
# point at.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backtest.divergence import ExecutionModel  # noqa: E402

DOC_HINT = "Regenerate per docs/operations/backtest-baseline.md"


def _fmt_pp(v: float | None) -> str:
    """Render a decimal return difference as percentage points (0.01 = 1 pp)."""
    return f"{v * 100:+.2f} pp" if v is not None else "—"


def _fmt_pct(v: float | None) -> str:
    return f"{v * 100:+.1f}%" if v is not None else "—"


def _execution_model(report: dict[str, Any] | None) -> ExecutionModel | None:
    """Rebuild the baseline's ExecutionModel from the report, if it recorded one.

    Only known fields are passed through, so a report written by an older
    monitor (or a newer one) still constructs instead of raising.
    """
    if not report:
        return None
    payload = report.get("baseline_execution_model")
    if not isinstance(payload, dict) or "fill_model" not in payload:
        return None
    known = {f.name for f in dataclasses.fields(ExecutionModel)}
    try:
        return ExecutionModel(**{k: v for k, v in payload.items() if k in known})
    except TypeError:
        return None


def _breach_lines(report: dict[str, Any] | None) -> list[str]:
    """One line per BREACH sleeve. WARNING sleeves are deliberately omitted:
    only BREACH counts toward the ladder (D11), and alerting on WARNING would
    train the operator to ignore the channel."""
    lines: list[str] = []
    for r in (report or {}).get("reports", []):
        if r.get("status") != "BREACH":
            continue
        lines.append(
            f"• {r.get('portfolio', '?')}: live {_fmt_pct(r.get('live_return'))} "
            f"vs backtest {_fmt_pct(r.get('backtest_return'))} "
            f"(Δ {_fmt_pp(r.get('absolute_divergence_pp'))}, "
            f"{_fmt_pct(r.get('relative_divergence'))} relative)"
        )
    return lines


def _blind_reasons(report: dict[str, Any] | None) -> list[str]:
    """Why the monitor could not score, in the monitor's own words.

    Prefers ``ExecutionModel.unmet_requirements()`` so the message says which
    requirement failed rather than just "blind"; falls back to the per-sleeve
    notes when the report predates the recorded execution model.
    """
    model = _execution_model(report)
    if model is not None and not model.is_like_for_like:
        return [f"• {reason}" for reason in model.unmet_requirements()]
    reasons: list[str] = []
    for r in (report or {}).get("reports", []):
        if r.get("baseline_comparable", True):
            continue
        for note in r.get("notes", []):
            reasons.append(f"• {r.get('portfolio', '?')}: {note}")
    return reasons


# `scheme://user:password@host` — the monitor prints the full DSN when it
# cannot reach the DB (divergence_monitor.py:443,453), and that DSN carries the
# live Postgres password. Telegram is not a secret store, so the password is
# stripped before the line leaves this process.
#
# The password is NOT percent-encoded: run_divergence.sh interpolates whatever
# `secrets.sh` returned, and an operator may legally have typed '@' or a space.
# So the match must run to the LAST '@' on the line (greedy `.*`) and must not
# exclude whitespace — an earlier non-greedy `[^\s@]*@` leaked the tail of
# `p@ssw0rd` and missed `pa ss` entirely. Over-redacting is the safe direction;
# under-redacting ships a live credential.
_DSN_CREDENTIAL = re.compile(r"(?P<prefix>[a-zA-Z][\w+.-]*://[^\s/@]*:).*@")

# Telegram's sendMessage caps a body at 4096 characters and answers HTTP 400
# beyond it; the wrapper discards curl's status, so an over-long alert would be
# dropped in silence — the exact failure this job exists to remove. The body is
# now data-dependent (one line per breaching sleeve, or a whole exception
# string on exit 2), so it is bounded here rather than hoped about.
MAX_MESSAGE_CHARS = 3500
_TRUNCATION_NOTE = "\n… truncated — see the log for the full detail."


def _redact(text: str) -> str:
    return _DSN_CREDENTIAL.sub(r"\g<prefix>***@", text)


def _cap(text: str) -> str:
    if len(text) <= MAX_MESSAGE_CHARS:
        return text
    keep = MAX_MESSAGE_CHARS - len(_TRUNCATION_NOTE)
    return text[:keep].rstrip() + _TRUNCATION_NOTE


def _last_error_line(log_tail: str) -> str:
    errors = [
        line.strip() for line in log_tail.splitlines()
        if line.strip().startswith("ERROR")
    ]
    return _redact(errors[-1]) if errors else ""


def render_alert(
    exit_code: int,
    report: dict[str, Any] | None,
    log_tail: str = "",
) -> str | None:
    """Return the alert text for this run, or None when nothing should be sent.

    Exit 0 is silence by design — alerting on a healthy day is what trains an
    operator to ignore the channel. Every other code MUST yield a message, even
    when the report is missing or malformed: a renderer that can go quiet
    reintroduces exactly the blindness this story removes.
    """
    if exit_code == 0:
        return None
    return _cap(_render(exit_code, report, log_tail))


def _render(
    exit_code: int,
    report: dict[str, Any] | None,
    log_tail: str,
) -> str:
    if exit_code == 1:
        lines = _breach_lines(report)
        body = "\n".join(lines) if lines else (
            "• (no per-sleeve detail in the report — see the log)"
        )
        return (
            "🚨 Divergence BREACH — live paper equity has diverged from the "
            f"backtest baseline.\n{body}"
        )

    if exit_code == 2:
        detail = _last_error_line(log_tail) or "no ERROR line in the log tail"
        return f"🚨 Divergence monitor HARD ERROR (exit 2).\n{detail}"

    if exit_code == 3:
        reasons = _blind_reasons(report)
        body = "\n".join(reasons) if reasons else (
            "• reason not recorded in the report — see the log"
        )
        return (
            "⚠️ Divergence monitor is BLIND (exit 3): the baseline backtest is "
            "not comparable to live, so every report is forced to NO_DATA. No "
            f"drift detection is running.\n{body}\n{DOC_HINT}"
        )

    return f"🚨 Divergence monitor returned UNEXPECTED exit code {exit_code}."


def _load_report(
    path: str | None,
    not_before: float | None = None,
) -> dict[str, Any] | None:
    """Load the run's JSON report, or None if it is missing or stale.

    ``not_before`` is the epoch second the run started. Exit 1 does not by
    itself prove a breach — ``divergence_monitor.py`` ends in
    ``sys.exit(main())``, so any uncaught exception also exits 1 — and a report
    left by an earlier run that day would then be narrated as though this run
    had computed it. An older file is treated as absent, which downgrades the
    message to the generic body rather than to silence.
    """
    if not path:
        return None
    try:
        p = Path(path)
        if not_before is not None and p.stat().st_mtime < not_before:
            return None
        with p.open() as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _load_log_tail(
    path: str | None,
    offset: int = 0,
    max_bytes: int = 65536,
) -> str:
    """Read the log from ``offset`` on — the byte count at which this run began.

    A day's log holds every run against that date, so without the offset the
    last ``ERROR:`` line could belong to an earlier run entirely.
    """
    if not path:
        return ""
    try:
        p = Path(path)
        size = p.stat().st_size
        start = max(offset, size - max_bytes) if size > offset + max_bytes else offset
        with p.open("rb") as f:
            if start:
                f.seek(start)
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render a divergence alert.")
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--report", default=None)
    parser.add_argument("--log", default=None)
    parser.add_argument(
        "--not-before", type=float, default=None,
        help="Epoch second this run started; an older report is treated as stale.",
    )
    parser.add_argument(
        "--log-offset", type=int, default=0,
        help="Byte offset in the log at which this run began.",
    )
    args = parser.parse_args(argv)

    text = render_alert(
        args.exit_code,
        _load_report(args.report, args.not_before),
        _load_log_tail(args.log, args.log_offset),
    )
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
