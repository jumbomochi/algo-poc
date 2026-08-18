#!/usr/bin/env python3
"""Daily divergence monitor: live paper-trading vs. latest backtest.

For each paper-trading portfolio (and the aggregate), this script:

1. Loads live equity history from the ``equity_snapshots`` table.
2. Loads the corresponding daily equity series from the most recent
   ``output/backtest_multi_*.json``.
3. Aligns the two by date, takes the last N (default 30) trading days, and
   computes return divergence + daily-returns correlation + realized
   slippage/commission from the ``trades`` table.
4. Prints a console table, writes a JSON report, and optionally emits a
   Prometheus textfile for ``node_exporter`` to scrape.
5. Exits non-zero if any portfolio's divergence breaches the threshold, or if
   the baseline backtest is not comparable to live at all — so cron/launchd
   jobs can alert. See ``exit_code_for`` for the contract.

This is **not** a kill switch. It surfaces divergence so a human can decide
whether to investigate, disable a sleeve, or carry on. Automated sleeve
disable on persistent breach can be layered on later (see notes in
``docs/strategies/mean-reversion-failure-analysis.md`` for the phase plan).

Usage:
    python scripts/divergence_monitor.py
    python scripts/divergence_monitor.py --backtest output/backtest_multi_20260526_235302.json
    python scripts/divergence_monitor.py --window 60 --threshold 0.30
    python scripts/divergence_monitor.py --portfolio momentum
    python scripts/divergence_monitor.py --prometheus-textfile /var/lib/node_exporter/textfile/divergence.prom
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from glob import glob
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backtest.divergence import (
    ASSUMED_SLIPPAGE_BPS,
    DEFAULT_THRESHOLD,
    DEFAULT_WINDOW_DAYS,
    ExecutionModel,
    PortfolioDivergenceReport,
    aggregate_reports,
    any_breach,
    build_report,
    execution_model_from_backtest_config,
)
from scripts.paper_state import PaperTradingState
from shared.config import load_config
from shared.models.evidence import DivergenceDaily
from shared.universe import is_excluded_portfolio


# ---------------------------------------------------------------------------
# Exit-code contract (mirrored in deploy/launchd/run_divergence.sh)
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_BREACH = 1
EXIT_ERROR = 2
# The monitor ran but could not judge anything: the baseline backtest is not
# like-for-like with live execution, so every report was forced to NO_DATA.
# This needs its own code — folding it into EXIT_OK made a blind monitor
# indistinguishable from a healthy one in the daily log.
EXIT_BASELINE_NOT_COMPARABLE = 3
# The baseline is comparable and the numbers are real, but the artifact they
# were computed against is old (KAN-56). Distinct from 3: the monitor is not
# blind, it is scoring today's equity against expectations that stopped being
# refreshed. Between 2026-07-28 and 2026-08-18 that was silently true for
# three weeks — the weekly refresh missed twice, once without alerting at all.
EXIT_BASELINE_STALE = 4


# ---------------------------------------------------------------------------
# Baseline staleness (KAN-56)
# ---------------------------------------------------------------------------

#: Two missed weekly refreshes. One miss is a bad week (IB's data farm, a
#: reboot); two is a broken job, and by then the baseline no longer describes
#: the regime the live book is trading in.
DEFAULT_MAX_BASELINE_AGE_DAYS = 14

#: The stable token in the warning line. Kept as a constant because both a
#: human grepping ~/ibc/logs and the Telegram renderer key off it, and neither
#: should have to track edits to the prose around it.
BASELINE_STALE_WARNING = "BASELINE_STALE"

#: ``backtest_multi_20260728_053111.json`` — the date the backtest was *run*.
_BASELINE_STAMP_RE = re.compile(r"(\d{8})_(\d{6})")


@dataclass(frozen=True)
class BaselineAge:
    """How old the artifact being scored against is, and how we know.

    Read at the point of *consumption* rather than production on purpose. Over
    the 2026-07-28 → 2026-08-18 gap two different jobs failed in two different
    ways (a host that was down at the calendar slot, then an unreachable
    gateway) and only one of them told anyone. The artifact's own age is the
    one signal that is identical under every one of those causes, including
    the ones nobody has thought of yet.
    """

    path: str
    age_days: int | None
    max_age_days: int
    #: ``filename`` | ``mtime`` | ``unknown`` — worth carrying, because the
    #: fallback is materially weaker evidence and the operator should be able
    #: to see which one produced the number.
    source: str

    @property
    def is_stale(self) -> bool:
        # An unknown age is never stale: the caller has already hard-errored on
        # a missing artifact, and guessing here would turn "cannot tell" into a
        # confident wrong verdict.
        return self.age_days is not None and self.age_days > self.max_age_days

    def warning_line(self) -> str | None:
        if not self.is_stale:
            return None
        return (
            f"{BASELINE_STALE_WARNING}: {Path(self.path).name} is "
            f"{self.age_days} days old (threshold {self.max_age_days}, age from "
            f"{self.source}). The weekly refresh has not produced a newer "
            "baseline; divergence is being scored against stale expectations. "
            "See deploy/launchd/README.md, 'Weekly backtest refresh'."
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "age_days": self.age_days,
            "max_age_days": self.max_age_days,
            "source": self.source,
            "stale": self.is_stale,
        }


def baseline_age(
    backtest_path: str,
    max_age_days: int = DEFAULT_MAX_BASELINE_AGE_DAYS,
    *,
    today: date | None = None,
) -> BaselineAge:
    """Age the baseline artifact, preferring its filename stamp to its mtime.

    The name records when the backtest was run; the mtime records when the file
    was last touched. Restoring a backup, rsyncing ``output/`` or a plain
    ``cp -r`` rewrites the mtime, which would make a three-week-old baseline
    look like today's — the one direction in which this check must never err.
    The mtime is the fallback for a hand-named artifact passed via
    ``--backtest``, which has no stamp to read.
    """
    today = today or date.today()
    name = Path(backtest_path).name

    match = _BASELINE_STAMP_RE.search(name)
    if match:
        try:
            generated = datetime.strptime(match.group(1), "%Y%m%d").date()
        except ValueError:
            generated = None
        if generated is not None:
            return BaselineAge(
                path=backtest_path,
                age_days=(today - generated).days,
                max_age_days=max_age_days,
                source="filename",
            )

    try:
        mtime = datetime.fromtimestamp(Path(backtest_path).stat().st_mtime).date()
    except OSError:
        return BaselineAge(
            path=backtest_path,
            age_days=None,
            max_age_days=max_age_days,
            source="unknown",
        )
    return BaselineAge(
        path=backtest_path,
        age_days=(today - mtime).days,
        max_age_days=max_age_days,
        source="mtime",
    )


def exit_code_for(
    reports: list[PortfolioDivergenceReport],
    execution_model: ExecutionModel | None = None,
    baseline: BaselineAge | None = None,
) -> int:
    """Map the run's outcome onto the process exit code.

    Precedence is worst-outage-first: a breach outranks everything (it is the
    louder signal, and its alert names the sleeves); a non-comparable baseline
    outranks a stale one (BLIND means no drift detection is running at all,
    stale means it is running against old expectations). A genuine NO_DATA on a
    good baseline — no overlapping live history yet — is not a fault and stays
    at EXIT_OK.

    ``baseline`` is optional so the two-argument contract every existing caller
    uses keeps working; omitting it simply means staleness is not judged.
    """
    if any_breach(reports):
        return EXIT_BREACH
    if execution_model is not None and not execution_model.is_like_for_like:
        return EXIT_BASELINE_NOT_COMPARABLE
    if any(not r.baseline_comparable for r in reports):
        return EXIT_BASELINE_NOT_COMPARABLE
    if baseline is not None and baseline.is_stale:
        return EXIT_BASELINE_STALE
    return EXIT_OK


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def find_latest_backtest_json(output_dir: str = "output") -> str | None:
    """Return the path to the most recent ``backtest_multi_*.json``, or None."""
    candidates = sorted(glob(f"{output_dir}/backtest_multi_*.json"))
    return candidates[-1] if candidates else None


def load_backtest_equity_series(
    backtest_path: str,
) -> tuple[dict[str, dict[date, float]], dict[date, float]]:
    """Load per-portfolio and aggregate equity series from a backtest results JSON.

    ``portfolio_values`` has ``len(dates) + 1`` entries — the first is the
    pre-day-0 initial capital. We drop it so that
    ``portfolio_values[i+1]`` aligns with ``dates[i]`` (end-of-day equity).

    Returns:
        ``(per_portfolio, aggregate)`` where each maps ``date -> equity``.
    """
    with open(backtest_path) as f:
        data = json.load(f)

    dates_iso = data["aggregate"]["dates"]
    dates = [date.fromisoformat(d) for d in dates_iso]

    def _series(pv: list[float]) -> dict[date, float]:
        # End-of-day values aligned to dates.
        eod = pv[1:] if len(pv) == len(dates) + 1 else pv
        return dict(zip(dates, eod))

    per_portfolio = {
        name: _series(p["portfolio_values"])
        for name, p in data["portfolios"].items()
    }
    aggregate = _series(data["aggregate"]["portfolio_values"])
    return per_portfolio, aggregate


def load_backtest_execution_model(backtest_path: str) -> ExecutionModel:
    """Read the execution assumptions the baseline backtest ran under.

    A baseline that filled same-bar, or that modelled no per-order commission
    floor, is not something live execution can match — the monitor reports its
    figures but refuses to grade live against it (finding 4.6).
    """
    with open(backtest_path) as f:
        data = json.load(f)
    return execution_model_from_backtest_config(data.get("config"))


def load_live_equity_series(state: PaperTradingState, portfolio: str) -> dict[date, float]:
    """Pull live daily equity for a portfolio from ``equity_snapshots``."""
    rows = state.get_equity_history(portfolio)
    return {
        date.fromisoformat(r["date"]): float(r["equity"])
        for r in rows
    }


def load_live_aggregate_series(
    per_portfolio: dict[str, dict[date, float]],
) -> dict[date, float]:
    """Sum per-portfolio equity across dates that appear in every portfolio.

    Restricting to fully-overlapping dates avoids spurious aggregate dips
    when one sleeve happens to be missing a snapshot for a given day.
    """
    if not per_portfolio:
        return {}
    common_dates = set.intersection(*(set(s.keys()) for s in per_portfolio.values()))
    return {
        d: sum(s[d] for s in per_portfolio.values())
        for d in sorted(common_dates)
    }


# ---------------------------------------------------------------------------
# Console rendering
# ---------------------------------------------------------------------------


STATUS_GLYPHS = {"OK": "✓", "WARNING": "⚠", "BREACH": "✗", "NO_DATA": "·"}


def _fmt_pct(v: float | None, decimals: int = 2) -> str:
    return f"{v * 100:+.{decimals}f}%" if v is not None else "—"


def _fmt_money(v: float | None) -> str:
    return f"${v:,.2f}" if v is not None else "—"


def _fmt_corr(v: float | None) -> str:
    return f"{v:+.3f}" if v is not None else "—"


def print_report_table(
    reports: list[PortfolioDivergenceReport],
    window_days: int,
    threshold: float,
    execution_model: ExecutionModel | None = None,
) -> None:
    print("=" * 110)
    print(f"  DIVERGENCE MONITOR — window {window_days} days, threshold {threshold:.0%}")
    if execution_model is not None:
        print(
            f"  Baseline execution: {execution_model.fill_model} fills, "
            f"{execution_model.slippage_bps:.0f} bps slippage, "
            f"max(${execution_model.commission_minimum:.2f}, "
            f"${execution_model.commission_per_share}/share) commission"
            + ("" if execution_model.is_like_for_like else "  [NOT LIKE-FOR-LIKE]")
        )
    print("=" * 110)
    print()
    header = (
        f"  {'':2}  {'Portfolio':<22}{'Days':>6}{'Live':>10}{'Backtest':>10}"
        f"{'Δ pp':>10}{'Δ rel':>10}{'Corr':>8}{'Slip bps':>11}{'Trades':>8}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for r in reports:
        glyph = STATUS_GLYPHS.get(r.status, "?")
        print(
            f"  {glyph:<2}  {r.portfolio:<22}{r.days_compared:>6}"
            f"{_fmt_pct(r.live_return, 1):>10}"
            f"{_fmt_pct(r.backtest_return, 1):>10}"
            f"{_fmt_pct(r.absolute_divergence_pp, 2):>10}"
            f"{_fmt_pct(r.relative_divergence, 1):>10}"
            f"{_fmt_corr(r.daily_correlation):>8}"
            f"{(f'{r.realized_slippage_bps:.1f}' if r.realized_slippage_bps is not None else '—'):>11}"
            f"{r.live_trades_in_window:>8}"
        )
    print()

    # Surface anything non-OK in plain English at the bottom.
    actionable = [
        r for r in reports
        if r.status in ("WARNING", "BREACH") or not r.baseline_comparable
    ]
    assumed_slippage = (
        execution_model.slippage_bps
        if execution_model is not None
        else ASSUMED_SLIPPAGE_BPS
    )
    for r in actionable:
        print(f"  [{r.status}] {r.portfolio}:")
        for note in r.notes:
            print(f"     • {note}")
        if (
            r.realized_slippage_bps is not None
            and r.realized_slippage_bps > 1.5 * assumed_slippage
        ):
            print(
                f"     • Realized slippage {r.realized_slippage_bps:.1f} bps "
                f"exceeds the {assumed_slippage:.0f} bps backtest assumption."
            )
        if r.assumed_commission_total > 0 and r.realized_commission_total > 1.5 * r.assumed_commission_total:
            print(
                f"     • Realized commission ${r.realized_commission_total:.2f} is "
                f"{r.realized_commission_total / r.assumed_commission_total:.1f}× "
                f"the ${r.assumed_commission_total:.2f} assumed."
            )
    if actionable:
        print()


# ---------------------------------------------------------------------------
# JSON / Prometheus output
# ---------------------------------------------------------------------------


def write_json_report(
    reports: list[PortfolioDivergenceReport],
    output_path: str,
    backtest_path: str,
    window_days: int,
    threshold: float,
    execution_model: ExecutionModel | None = None,
    baseline: BaselineAge | None = None,
) -> None:
    """Write the full report to a JSON file for historical tracking / Grafana."""

    def _serialize(r: PortfolioDivergenceReport) -> dict[str, Any]:
        d = asdict(r)
        # asdict() turns dates into date objects; serialize as ISO strings.
        d["window_start"] = r.window_start.isoformat() if r.window_start else None
        d["window_end"] = r.window_end.isoformat() if r.window_end else None
        return d

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "backtest_source": backtest_path,
        "window_days": window_days,
        "threshold": threshold,
        "reports": [_serialize(r) for r in reports],
    }
    if execution_model is not None:
        payload["baseline_execution_model"] = asdict(execution_model)
    if baseline is not None:
        # Read back by scripts/ops/divergence_alert.py to render the exit-4
        # Telegram, so the message can name the age instead of saying "stale".
        payload["baseline_staleness"] = baseline.as_dict()
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"  Report saved to {output_path}")


def write_prometheus_textfile(
    reports: list[PortfolioDivergenceReport],
    textfile_path: str,
    baseline: BaselineAge | None = None,
) -> None:
    """Write Prometheus-format gauges for ``node_exporter`` textfile collector.

    Atomic-write into a ``.prom`` file. node_exporter's textfile collector
    will pick it up on next scrape; no metrics HTTP server needed.
    """
    from prometheus_client import CollectorRegistry, Gauge, write_to_textfile

    registry = CollectorRegistry()
    g_abs = Gauge(
        "algo_poc_divergence_absolute_pp",
        "Absolute return divergence (live - backtest) in decimal (0.01 = 1 pp).",
        ["portfolio"], registry=registry,
    )
    g_rel = Gauge(
        "algo_poc_divergence_relative",
        "Relative return divergence: (live - backtest) / |backtest|.",
        ["portfolio"], registry=registry,
    )
    g_corr = Gauge(
        "algo_poc_divergence_correlation",
        "Pearson correlation of daily returns over the window.",
        ["portfolio"], registry=registry,
    )
    g_slip = Gauge(
        "algo_poc_realized_slippage_bps",
        "Realized slippage in basis points, weighted by notional.",
        ["portfolio"], registry=registry,
    )
    g_comm = Gauge(
        "algo_poc_realized_commission_dollars",
        "Realized commission dollars over the window.",
        ["portfolio"], registry=registry,
    )
    g_status = Gauge(
        "algo_poc_divergence_status",
        "Status (0=OK, 1=WARNING, 2=BREACH, 3=NO_DATA).",
        ["portfolio"], registry=registry,
    )

    # Unlabelled: there is exactly one baseline per run, and it is a property
    # of the run rather than of any portfolio. Exported for the day a scraper
    # exists on this host — the exit code, not this gauge, is what currently
    # reaches a human (see EXIT_BASELINE_STALE).
    g_age = Gauge(
        "algo_poc_divergence_baseline_age_days",
        "Age in days of the backtest baseline the run was scored against.",
        registry=registry,
    )

    status_code = {"OK": 0, "WARNING": 1, "BREACH": 2, "NO_DATA": 3}

    if baseline is not None and baseline.age_days is not None:
        g_age.set(baseline.age_days)

    for r in reports:
        if r.absolute_divergence_pp is not None:
            g_abs.labels(portfolio=r.portfolio).set(r.absolute_divergence_pp)
        if r.relative_divergence is not None:
            g_rel.labels(portfolio=r.portfolio).set(r.relative_divergence)
        if r.daily_correlation is not None:
            g_corr.labels(portfolio=r.portfolio).set(r.daily_correlation)
        if r.realized_slippage_bps is not None:
            g_slip.labels(portfolio=r.portfolio).set(r.realized_slippage_bps)
        g_comm.labels(portfolio=r.portfolio).set(r.realized_commission_total)
        g_status.labels(portfolio=r.portfolio).set(status_code.get(r.status, -1))

    Path(textfile_path).parent.mkdir(parents=True, exist_ok=True)
    write_to_textfile(textfile_path, registry)
    print(f"  Prometheus textfile written to {textfile_path}")


# ---------------------------------------------------------------------------
# Evidence store — the durable copy of each verdict (KAN-27)
# ---------------------------------------------------------------------------


def baseline_id_for(backtest_path: str) -> str:
    """The identity of the baseline a verdict was scored against.

    The file's basename, not its full path: the same artifact read from a
    worktree, a deployed checkout, or a backup directory is the same baseline,
    and the path would fragment the ``(sleeve, session_date, baseline_id)``
    key that makes a re-run idempotent.
    """
    return Path(backtest_path).name


# Generous for two INSERTs, and short next to the job's window. The verdict
# reaches the operator only once this process exits — the launchd wrapper sends
# the Telegram message from the exit code — so a write blocked on a lock would
# otherwise swallow a BREACH rather than merely lose a row.
PERSIST_STATEMENT_TIMEOUT_MS = 30_000


def persist_engine_kwargs(db_url: str) -> dict[str, Any]:
    """Engine options for the short-lived connection that writes the verdicts.

    Postgres-only: ``statement_timeout`` is a server setting sqlite (the test
    store) would reject. Applied to a connection of its own rather than to the
    read engine, so bounding the write cannot change how the reporting half of
    the run behaves.
    """
    if db_url.startswith("postgresql"):
        return {
            "connect_args": {
                "options": f"-c statement_timeout={PERSIST_STATEMENT_TIMEOUT_MS}"
            }
        }
    return {}


# A float that round-tripped through the DB should compare equal, but a hair of
# drift must not be read as "a different threshold" and block a real update.
_THRESHOLD_TOLERANCE = 1e-9


def _same_pins(
    existing: DivergenceDaily, window_sessions: int, threshold: float
) -> bool:
    """Was the stored verdict scored under the same window and threshold?"""
    return (
        existing.window_sessions == window_sessions
        and abs(existing.threshold - threshold) <= _THRESHOLD_TOLERANCE
    )


def persist_divergence_rows(
    session: Session,
    reports: list[PortfolioDivergenceReport],
    *,
    baseline_id: str,
    window_sessions: int,
    threshold: float,
) -> int:
    """Write one ``divergence_daily`` row per report, and return how many.

    The row is dated by the session the verdict *covers* — the last aligned
    session in the window — never by the wall clock, so a late or catch-up run
    cannot mis-date the evidence the epoch clock counts.

    Two rules the caller must not "fix":

    * A ``NO_DATA`` verdict IS written. "The monitor ran and could not judge"
      is a recorded observation, and D11 pauses the clock on it. Absence means
      something else entirely — the monitor did not run — and KAN-26's
      arithmetic depends on being able to tell the two apart.
    * A report with no aligned session (live and baseline share no dates) is
      skipped. There is no session to date it by, and stamping today's would
      claim a verdict for a session that was never scored, which the blindness
      rule would read as a working monitor.

    Idempotent against ``uq_divergence_daily_sleeve_date_baseline``: a same-day
    re-run on the same baseline updates its row in place. A hand-rolled
    select-then-update rather than a dialect-specific upsert, matching
    ``PaperTradingState.record_equity_snapshot``.
    """
    now = datetime.now(timezone.utc)
    written = 0
    for report in reports:
        if report.window_end is None:
            print(
                f"  ⚠ Not recording '{report.portfolio}': no aligned session to "
                f"date the verdict by (live and baseline share no dates), so the "
                f"run is recorded as blind by absence rather than as a verdict."
            )
            continue
        existing = session.execute(
            select(DivergenceDaily).where(
                DivergenceDaily.sleeve == report.portfolio,
                DivergenceDaily.session_date == report.window_end,
                DivergenceDaily.baseline_id == baseline_id,
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                DivergenceDaily(
                    sleeve=report.portfolio,
                    session_date=report.window_end,
                    status=report.status,
                    baseline_id=baseline_id,
                    window_sessions=window_sessions,
                    threshold=threshold,
                    metric_value=report.relative_divergence,
                    created_at=now,
                )
            )
        elif not _same_pins(existing, window_sessions, threshold):
            # An ad-hoc run with a different --window/--threshold lands on the
            # same key as the canonical one — window_end does not move with
            # --window — but it is a different observation, not a correction.
            # Overwriting would let an exploratory command silently clear a
            # firing breach streak the capital ladder gates on.
            print(
                f"  ⚠ Not recording '{report.portfolio}' for {report.window_end}: "
                f"the stored verdict was scored at window {existing.window_sessions}/"
                f"threshold {existing.threshold:g} and this run used "
                f"{window_sessions}/{threshold:g}. A differently-pinned verdict is a "
                f"different observation, so the recorded one stands; re-run with the "
                f"canonical pins to update it."
            )
            continue
        else:
            # A re-run of the same observation: the verdict may legitimately
            # differ (a late or corrected snapshot landed), and the freshest
            # scoring of that session is the one the store should hold.
            existing.status = report.status
            existing.metric_value = report.relative_divergence
            existing.created_at = now
        written += 1
    return written


ALERTS_STREAM = "stream:alerts"

# A DB error echoes the DSN, and the DSN carries the paper database password.
# stream:alerts fans out to Telegram, which is not a secret store. Same strip
# as scripts/run_paper.py and scripts/ops/divergence_alert.py: greedy to the
# LAST '@', because the password is whatever secrets.sh returned and may itself
# contain '@' or whitespace. Over-redacting is the safe direction.
_DSN_CREDENTIAL = re.compile(r"(?P<prefix>[a-zA-Z][\w+.-]*://[^\s/@]*:).*@")

# The store is a side effect of a run whose real job is the verdict; a wedged
# Redis must not hold the 04:45 job open past its window.
ALERT_SOCKET_TIMEOUT_SECONDS = 5


def _redact(text: str) -> str:
    return _DSN_CREDENTIAL.sub(r"\g<prefix>***@", text)


def _redis_from_url(url: str, **kwargs: Any) -> Any:
    """Single seam for the one Redis connection this script opens."""
    import redis as redis_sync

    return redis_sync.Redis.from_url(url, **kwargs)


def emit_persist_failure_alert(error: BaseException, *, redis_url: str) -> bool:
    """Page the operator that today's verdicts were computed but never stored.

    This is the only operator-visible signal for the failure: the exit code is
    deliberately unchanged (a store outage must not mask a BREACH), and the
    launchd wrapper stays silent on exit 0. Best-effort — a failure here is
    printed and swallowed, because the run's own verdict still has to be
    delivered.
    """
    from shared.schemas.messages import AlertMessage

    message = _redact(
        f"divergence_monitor.py: could not persist today's divergence verdicts "
        f"({error}). The verdicts were computed and reported, but the evidence "
        f"store has no rows for this session — the epoch clock will read the "
        f"gap as a blind session until it is re-run."
    )
    try:
        conn = _redis_from_url(
            redis_url,
            socket_connect_timeout=ALERT_SOCKET_TIMEOUT_SECONDS,
            socket_timeout=ALERT_SOCKET_TIMEOUT_SECONDS,
        )
        try:
            alert = AlertMessage(
                timestamp=datetime.now(timezone.utc),
                event_type="divergence_persist_failed",
                priority="high",
                message=message,
                context={"script": "divergence_monitor.py"},
            )
            conn.xadd(ALERTS_STREAM, alert.to_stream_dict())
        finally:
            conn.close()
        return True
    except Exception as alert_error:
        print(
            "WARNING: could not raise the divergence-persistence alert "
            f"({_redact(str(alert_error))})"
        )
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    # Load default DB URL from config (may fail if config file missing, that's OK).
    try:
        _config = load_config("config/default.yaml")
        default_db_url = _config.database.url
        default_redis_url = _config.redis.url
    except Exception:
        # Honour ALGO_DATABASE_URL even when the config file can't be loaded, so
        # a missing/unreadable config can't silently fall back to the wrong DB
        # (the launchd wrapper always exports ALGO_DATABASE_URL).
        default_db_url = os.environ.get(
            "ALGO_DATABASE_URL", "postgresql://algo:algo@localhost:5432/algo_poc"
        )
        default_redis_url = os.environ.get("ALGO_REDIS_URL", "redis://localhost:6379/0")

    parser = argparse.ArgumentParser(
        description="Compare live paper-trading equity to backtest expectations."
    )
    parser.add_argument(
        "--backtest", default=None,
        help="Path to backtest results JSON. Default: latest output/backtest_multi_*.json",
    )
    parser.add_argument(
        "--window", type=int, default=DEFAULT_WINDOW_DAYS,
        help=f"Rolling window size in trading days (default {DEFAULT_WINDOW_DAYS}).",
    )
    parser.add_argument(
        "--threshold", type=float, default=DEFAULT_THRESHOLD,
        help=f"Relative divergence warning threshold (default {DEFAULT_THRESHOLD}).",
    )
    parser.add_argument(
        "--max-baseline-age-days", type=int, default=DEFAULT_MAX_BASELINE_AGE_DAYS,
        help=(
            "Warn (and exit "
            f"{EXIT_BASELINE_STALE}) when the baseline artifact is older than "
            f"this many days (default {DEFAULT_MAX_BASELINE_AGE_DAYS}, i.e. two "
            "missed weekly refreshes). 0 disables the check."
        ),
    )
    parser.add_argument(
        "--portfolio", default=None,
        help="Limit comparison to a single named portfolio.",
    )
    parser.add_argument(
        "--output", default=None,
        help="Path to write JSON output. Default: output/divergence_<YYYYMMDD>.json",
    )
    parser.add_argument(
        "--no-output", action="store_true",
        help="Skip writing the JSON output file.",
    )
    parser.add_argument(
        "--prometheus-textfile", default=None,
        help="Write Prometheus gauges to this .prom file for node_exporter to scrape.",
    )
    parser.add_argument("--db-url", default=default_db_url, help="PostgreSQL database URL.")
    parser.add_argument(
        "--redis-url", default=default_redis_url,
        help="Redis URL used only to alert if the verdicts cannot be stored.",
    )
    args = parser.parse_args()

    # --- Resolve inputs ---
    backtest_path = args.backtest or find_latest_backtest_json()
    if backtest_path is None:
        print("ERROR: No backtest JSON found. Pass --backtest or run scripts/run_backtest.py first.")
        return EXIT_ERROR
    if not Path(backtest_path).is_file():
        print(f"ERROR: Backtest file not found: {backtest_path}")
        return EXIT_ERROR
    print(f"  Backtest source: {backtest_path}")

    # Staleness is judged here, at the point of consumption, rather than by the
    # job that produces the artifact — a producer that never ran cannot report
    # that it never ran, which is precisely how the 2026-07-28 → 2026-08-18 gap
    # stayed quiet. 0 disables the check for an ad-hoc run against a
    # deliberately old baseline.
    baseline: BaselineAge | None = None
    if args.max_baseline_age_days > 0:
        baseline = baseline_age(backtest_path, args.max_baseline_age_days)
        stale_warning = baseline.warning_line()
        if stale_warning:
            print(f"  ⚠ {stale_warning}")

    bt_per_portfolio, bt_aggregate = load_backtest_equity_series(backtest_path)
    execution_model = load_backtest_execution_model(backtest_path)
    if not execution_model.is_like_for_like:
        print(
            "  ⚠ Baseline is not like-for-like with live: "
            + "; ".join(execution_model.unmet_requirements())
            + ". Divergence will be reported as NO_DATA; re-run the baseline "
            "per docs/operations/backtest-baseline.md."
        )

    # --- Open DB and load paper state ---
    # SQLAlchemy lazy-connects, so the actual connection attempt happens inside
    # PaperTradingState.load() when it runs its first SELECT. Wrap both phases.
    try:
        engine = create_engine(args.db_url)
        session_factory = sessionmaker(bind=engine)
        session: Session = session_factory()
    except Exception as e:
        print(f"ERROR: Failed to construct DB engine ({args.db_url}): {e}")
        return EXIT_ERROR

    try:
        state = PaperTradingState.load(session)
    except ValueError:
        print("ERROR: No paper trading state in DB. Run scripts/run_paper.py --init first.")
        return EXIT_ERROR
    except Exception as e:
        # Auth failure, network unreachable, missing tables, etc.
        print(f"ERROR: Could not load paper state from DB ({args.db_url}):")
        print(f"       {type(e).__name__}: {e}")
        print(
            "       Check that the DB is running, credentials are correct, "
            "and migrations have run (`alembic upgrade head`)."
        )
        return EXIT_ERROR

    portfolios = state.get_portfolio_names()
    if args.portfolio:
        if args.portfolio not in portfolios:
            print(
                f"ERROR: Portfolio '{args.portfolio}' not in DB. "
                f"Available: {', '.join(portfolios)}"
            )
            return EXIT_ERROR
        portfolios = [args.portfolio]

    # --- Build per-portfolio reports ---
    reports: list[PortfolioDivergenceReport] = []
    live_series_by_portfolio: dict[str, dict[date, float]] = {}
    for name in portfolios:
        # Synthetic portfolios ("__drill__", "_aggregate", "__liquidation__")
        # are never divergence input: a drill's fills exist to prove the safety
        # machinery works, not to be scored against a strategy baseline. Today
        # such a sleeve is also absent from the backtest JSON and would fall
        # through the branch below, but that is incidental — the exclusion is
        # explicit here so it cannot silently stop working if a same-named
        # sleeve ever appears in a baseline.
        if is_excluded_portfolio(name):
            print(
                f"  ⚠ Skipping '{name}': excluded portfolio (synthetic/drill "
                f"tag — never scored against a backtest sleeve; see "
                f"docs/operations/drill-evidence-isolation.md)."
            )
            continue
        live = load_live_equity_series(state, name)
        live_series_by_portfolio[name] = live
        if name not in bt_per_portfolio:
            print(
                f"  ⚠ Skipping '{name}': not present in backtest "
                f"(likely a sleeve that was dropped — see "
                f"docs/strategies/ for the rationale)."
            )
            continue
        trades = state.get_trades(name)
        report = build_report(
            portfolio=name,
            live=live,
            backtest=bt_per_portfolio[name],
            trades=trades,
            window_days=args.window,
            threshold=args.threshold,
            execution_model=execution_model,
        )
        reports.append(report)

    # Everything scored so far is a real sleeve. The aggregate is appended
    # below and must never be persisted (D15: the store holds observations,
    # not derived truth — the digest recomputes the roll-up from these rows),
    # so the sleeve list is captured here rather than filtered by name later.
    sleeve_reports = list(reports)

    # --- Aggregate report (only over sleeves that exist in both) ---
    if not args.portfolio:
        comparable = {
            n: s for n, s in live_series_by_portfolio.items()
            if n in bt_per_portfolio
        }
        live_total = load_live_aggregate_series(comparable)
        all_trades = state.get_all_trades()
        # Filter to only trades from portfolios that are in the comparison.
        all_trades = [t for t in all_trades if t["portfolio"] in comparable]
        agg_report = aggregate_reports(
            reports=reports,
            live_total=live_total,
            backtest_total=bt_aggregate,
            all_trades=all_trades,
            window_days=args.window,
            threshold=args.threshold,
            execution_model=execution_model,
        )
        reports.append(agg_report)

    # --- Output ---
    print()
    print_report_table(
        reports,
        window_days=args.window,
        threshold=args.threshold,
        execution_model=execution_model,
    )

    if not args.no_output:
        output_path = args.output or f"output/divergence_{date.today().strftime('%Y%m%d')}.json"
        write_json_report(
            reports, output_path,
            backtest_path=backtest_path,
            window_days=args.window,
            threshold=args.threshold,
            execution_model=execution_model,
            baseline=baseline,
        )

    if args.prometheus_textfile:
        write_prometheus_textfile(reports, args.prometheus_textfile, baseline=baseline)

    # --- Persist the verdicts (last, and unable to change the exit code) ---
    # Last because the JSON report and the .prom file are what the launchd
    # wrapper's alert renderer reads back: a slow or wedged store must not cost
    # the operator the message about a BREACH.
    # The write gets a connection of its own — bounded by a statement timeout,
    # and with an identity map that holds none of the ORM objects the reporting
    # half loaded, so it can only ever commit the rows built below.
    try:
        persist_session: Session = sessionmaker(
            bind=create_engine(args.db_url, **persist_engine_kwargs(args.db_url))
        )()
        try:
            written = persist_divergence_rows(
                persist_session,
                sleeve_reports,
                baseline_id=baseline_id_for(backtest_path),
                window_sessions=args.window,
                threshold=args.threshold,
            )
            persist_session.commit()
        finally:
            # Closing rolls back anything uncommitted, so a partial write can
            # never be left half-applied on the way out.
            persist_session.close()
        print(f"  Recorded {written} divergence verdict row(s) in the evidence store.")
    except Exception as e:
        # The monitor's job is to report divergence. A store outage is a real
        # problem — it makes the session look blind to the epoch clock — but it
        # is the operator's to fix, and swallowing the verdict to report it
        # would be strictly worse. Hence: alert, and leave the exit code alone.
        print(f"ERROR: could not persist divergence verdicts: {_redact(str(e))}")
        emit_persist_failure_alert(e, redis_url=args.redis_url)

    return exit_code_for(reports, execution_model, baseline=baseline)


if __name__ == "__main__":
    sys.exit(main())
