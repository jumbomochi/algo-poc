"""Guards on the per-epoch drill runbook (KAN-32).

``docs/operations/drill-runbook.md`` is a *paste-able* procedure, not a
description of one. Its whole acceptance criterion is "precise enough to repeat
next epoch without re-deriving anything" (AC6), and a runbook rots in ways prose
review does not catch:

* **A command it tells the operator to paste stops parsing.** A renamed flag
  turns a drill step into an ``error: unrecognized arguments`` at 3am with a
  real position open. Every fenced ``python -m scripts.ops.…`` / ``python
  scripts/…`` line is therefore fed to the real ``argparse`` parser of the
  script it names.
* **The routing premise flips.** Drill 2 is routed through the *broker-stop
  verifier* rather than the risk service's software scan for one reason, pinned
  below: ``load_open_positions`` filters the drill tag out and
  ``BrokerStopManager`` does not. If either changes, the drill's mechanism
  changes with it and the runbook is silently wrong.
* **A precondition stops being one.** The runbook opens by turning
  ``execution.broker_stops_enabled`` on, which is only a step while the shipped
  default is off. And it leans on two "fires immediately after a restart"
  properties to make the drill deterministic instead of a 30-minute wait.
* **A literal drifts.** The tag, the two drill types, and the DLQ key are
  spelled out in the runbook; all three are owned by code.

The account-wide-flatten assertion guards the sharpest edge in the procedure:
``flatten_paper_account.py`` closes *every* position on the paper account, so
reaching for it to unwind a drill would flatten the six graded sleeves. The
runbook has to keep saying so for as long as that script has no symbol filter.
"""

from __future__ import annotations

import argparse
import importlib
import re
import shlex
from pathlib import Path

import pytest

from shared.config import ExecutionConfig
from shared.models.evidence import DRILL_TYPE_VALUES
from shared.redis_client import DEAD_LETTER_SUFFIX
from shared.universe import DRILL_PORTFOLIO

RUNBOOK = Path("docs/operations/drill-runbook.md")
OPS_INDEX = Path("docs/operations/README.md")
ISOLATION = Path("docs/operations/drill-evidence-isolation.md")


@pytest.fixture(scope="module")
def runbook() -> str:
    return RUNBOOK.read_text()


def _fenced_commands(text: str) -> list[str]:
    """Every shell line inside a fenced block, continuations joined."""
    blocks = re.findall(r"```bash\n(.*?)```", text, flags=re.DOTALL)
    commands: list[str] = []
    for block in blocks:
        joined = block.replace("\\\n", " ")
        for line in joined.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            commands.append(line)
    return commands


#: ``python -m scripts.ops.x`` / ``python scripts/x.py`` invocations, mapped to
#: the module whose parser must accept the rest of the line.
_INVOCATION = re.compile(
    r"^python3? +(?:-m +(?P<module>[\w.]+)|(?P<path>scripts/[\w/]+\.py))\b"
    r"(?P<args>.*)$"
)

#: Parser factory per module, or None where the parser is built inside
#: ``main()`` and cannot be reached without running it. Naming them here rather
#: than guessing keeps the failure "this script changed its parser entry point",
#: not "the test's heuristic broke". Every module gets the flag-literal check
#: below regardless; the factory just buys a stricter one on top.
_PARSER_FACTORY = {
    "scripts.ops.record_epoch": "_build_parser",
    "scripts.ops.resolve_alert": None,
    "scripts.ops.go_live_gate": None,
    "scripts.ops.flatten_paper_account": None,
    "scripts.divergence_monitor": None,
    "scripts.reconcile_paper": "_parser",
    "scripts.run_paper": None,
}

#: Module -> source path, for the flag-literal check.
_SOURCE = {
    module: Path(module.replace(".", "/") + ".py") for module in _PARSER_FACTORY
}


def test_runbook_exists_and_is_indexed(runbook: str) -> None:
    """A runbook nobody can find is not a runbook (AC6)."""
    assert runbook.strip(), "drill-runbook.md is empty"
    index = OPS_INDEX.read_text()
    assert "drill-runbook.md" in index, (
        "docs/operations/README.md must list drill-runbook.md — the index is "
        "how an operator finds it next epoch"
    )


def test_runbook_covers_both_drills(runbook: str) -> None:
    """Both drill types, spelled exactly as ``record_epoch.py drill`` wants."""
    for drill_type in DRILL_TYPE_VALUES:
        assert f"--type {drill_type}" in runbook, (
            f"the runbook must give the literal recording command for "
            f"{drill_type!r}; DrillType is the source of truth for the spelling"
        )


def test_drill_tag_literal_matches_code(runbook: str) -> None:
    assert DRILL_PORTFOLIO in runbook, (
        f"the runbook must name the drill tag {DRILL_PORTFOLIO!r} verbatim — "
        "it is what the operator pastes into --portfolio-tag and into SQL"
    )


def test_dlq_key_literal_matches_code(runbook: str) -> None:
    """AC2's 'no DLQ entry' check needs the real key, not a remembered one."""
    assert f"stream:approved_orders{DEAD_LETTER_SUFFIX}" in runbook


@pytest.mark.parametrize("command", _fenced_commands(RUNBOOK.read_text()))
def test_every_pasted_command_parses(command: str) -> None:
    """Each in-repo script invocation is accepted by that script's parser.

    Commands the operator is told to fill in (``$STOP``-style placeholders) are
    skipped: the shell substitution is the point, and argparse would see the
    literal. Everything else must parse exactly as written.
    """
    match = _INVOCATION.match(command)
    if match is None:
        return  # docker/psql/redis-cli lines are checked by eye, not here
    module = match.group("module") or match.group("path").replace(
        "/", "."
    ).removesuffix(".py")
    if module not in _PARSER_FACTORY:
        pytest.fail(
            f"{command!r} invokes {module}, which this guard does not know how "
            "to parse. Add it to _PARSER_FACTORY so the command stays checked."
        )
    args = [tok for tok in shlex.split(match.group("args")) if "$" not in tok]

    # Always: every flag the runbook pastes is spelled somewhere in the script.
    # Cheap, and it is the check that survives a parser built inside main().
    source = _SOURCE[module].read_text()
    for token in args:
        if token.startswith("--"):
            assert token.split("=")[0] in source, (
                f"the runbook pastes {token} to {module}, which does not "
                f"define it: {command!r}"
            )

    factory = _PARSER_FACTORY[module]
    if factory is None:
        return
    parser: argparse.ArgumentParser = getattr(
        importlib.import_module(module), factory
    )()
    try:
        parser.parse_args(args)
    except SystemExit as exc:  # argparse exits 2 on a bad flag or subcommand
        pytest.fail(f"the runbook pastes a command {module} rejects: {command!r} ({exc})")


_SELECT = re.compile(
    r"SELECT\s+(?P<cols>[\w\s,]+?)\s+FROM\s+(?P<table>\w+)", re.IGNORECASE
)
_INSERT = re.compile(
    r"INSERT\s+INTO\s+(?P<table>\w+)\s*\((?P<cols>[\w\s,]+)\)", re.IGNORECASE
)
_UPDATE = re.compile(r"UPDATE\s+(?P<table>\w+)\s+SET\s+(?P<col>\w+)", re.IGNORECASE)


def _sql_statements(text: str) -> list[str]:
    """The psql payloads, line continuations joined."""
    body = text.replace("\\\n", " ")
    return re.findall(r'psql[^"]*-c\s+"(.*?)"', body, flags=re.DOTALL)


def test_runbook_sql_matches_the_schema(runbook: str) -> None:
    """Every table and column the operator pastes exists in the ORM metadata.

    Hand-written SQL in a document is exactly where a renamed column goes
    unnoticed until an operator hits it mid-drill with a real position open —
    and the first draft of this runbook had four wrong names (``order_intents``
    has ``action`` not ``side``; ``execution_fills`` has ``symbol``/``price``
    not ``ticker``/``fill_price``; ``divergence_daily`` keys on ``sleeve``).
    """
    import shared.models  # noqa: F401  (registers every table)
    from shared.models.base import Base

    statements = _sql_statements(runbook)
    assert statements, "no psql commands found — the extraction regex broke"

    checked = 0
    for sql in statements:
        for pattern, group in ((_SELECT, "cols"), (_INSERT, "cols"), (_UPDATE, "col")):
            for match in pattern.finditer(sql):
                table_name = match.group("table")
                table = Base.metadata.tables.get(table_name)
                assert table is not None, (
                    f"the runbook queries table {table_name!r}, which no model "
                    f"declares: {sql[:80]!r}"
                )
                columns = {c.name for c in table.columns}
                for column in match.group(group).replace("\n", " ").split(","):
                    column = column.strip()
                    if not column or column.upper() in {"COUNT(*)", "*"}:
                        continue
                    assert column in columns, (
                        f"the runbook selects {column!r} from {table_name!r}, "
                        f"which has no such column"
                    )
                    checked += 1
    assert checked > 10, f"only {checked} columns checked — the regex stopped matching"


def test_runbook_quotes_order_status_in_the_enum_casing(runbook: str) -> None:
    """``OrderStatus`` is uppercase; a lowercase 'pass' value would never match."""
    from shared.models.order_ledger import OrderStatus

    for status in (OrderStatus.SUBMITTED, OrderStatus.SUBMISSION_FAILED, OrderStatus.FILLED):
        assert status.value in runbook, (
            f"the runbook must quote {status.value!r} exactly — an operator "
            "comparing against a lowercased value sees a false failure"
        )


def test_broker_stops_flag_is_still_off_by_default(runbook: str) -> None:
    """Enabling the flag is a real step only while the default is off.

    Drill 2 exercises the broker-native stop (KAN-19/20). With the flag off the
    verifier is inert, so the runbook tells the operator to turn it on and
    rebuild. If the default ever flips, that step becomes a confusing no-op and
    the runbook must be re-written rather than left to read wrong.
    """
    assert ExecutionConfig().broker_stops_enabled is False
    assert "broker_stops_enabled" in runbook


def test_software_stop_scan_cannot_see_a_drill_position() -> None:
    """The fact that routes Drill 2 through the broker stop.

    ``load_open_positions`` is what fills the risk service's in-memory book, and
    it drops every ``_``-prefixed portfolio — so the software stop-loss scan
    never evaluates a ``__drill__`` position and the spec's original "set
    highest_price_since_entry and wait for the scan" mechanism cannot fire on
    that path. ``load_liquidation_targets`` deliberately keeps the row, which is
    what lets a kill still flatten it.
    """
    from shared.universe import is_excluded_portfolio

    assert is_excluded_portfolio(DRILL_PORTFOLIO)

    source = Path("shared/position_loader.py").read_text()
    assert '~Position.portfolio.startswith("_", autoescape=True)' in source, (
        "load_open_positions no longer excludes synthetic portfolios — the "
        "runbook's Drill 2 routing note is out of date"
    )
    assert (
        "startswith" not in Path("shared/liquidation.py").read_text()
    ), "load_liquidation_targets must keep returning drill rows so a kill can flatten them"


def test_broker_stop_verifier_does_see_a_drill_position() -> None:
    """The other half of the same fact.

    ``BrokerStopManager._open_positions`` filters on status/quantity/account
    only. A ``__drill__`` position therefore gets a real GTC stop, which is the
    mechanism Drill 2 drives — and, separately, is correct: a real position
    needs real protection whatever it is tagged.
    """
    source = Path("services/execution/broker_stops.py").read_text()
    body = source[source.index("def _open_positions"):]
    body = body[: body.index("def _next_stop_id")]
    assert "portfolio" not in body, (
        "BrokerStopManager._open_positions now filters on portfolio — if it "
        "excludes the drill tag, Drill 2 has no mechanism at all"
    )


def test_restart_makes_both_sweeps_immediate() -> None:
    """Why the runbook says 'restart execution' instead of 'wait 30 minutes'.

    Both timers start at ``None``, so the first loop iteration after a restart
    runs the sweep rather than waiting out an interval. That is what turns two
    otherwise unobservable background scans into drill steps.
    """
    source = Path("services/execution/runner.py").read_text()
    assert "self._last_halt_sweep_at: float | None = None" in source
    assert "self._last_stop_verification_at: float | None = None" in source


def test_flatten_tool_is_still_account_wide(runbook: str) -> None:
    """The runbook's loudest warning must stay true.

    ``flatten_paper_account.py`` takes no symbol or portfolio filter: running it
    to unwind a drill closes the six graded sleeves too. The moment it grows one,
    the warning becomes wrong advice.
    """
    source = Path("scripts/ops/flatten_paper_account.py").read_text()
    assert '--symbol' not in source and '--portfolio' not in source, (
        "flatten_paper_account.py grew a filter — the runbook's "
        "'this flattens the graded book too' warning needs rewriting"
    )
    assert "flatten_paper_account" in runbook


def test_isolation_contract_records_the_positions_reader() -> None:
    """``load_open_positions`` belongs in the exclusion contract's reader table.

    The table listed only the cash and peak_nav queries; the positions query
    excludes too, and its exclusion is the one with a behavioural consequence
    (no software stop-loss on a drill position). An undocumented reader is how
    the next drill designer repeats the same wrong assumption.
    """
    text = ISOLATION.read_text()
    assert "load_open_positions" in text
