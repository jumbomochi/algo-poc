"""The paper run's IB client ids must be its own, and disjoint from the backtest's.

``scripts/run_paper.py`` reuses ``run_backtest.fetch_bars_from_ib`` to pull bars.
That function declares ``client_id: int = 10``, and the paper run's call site
omitted the argument — so the daily run presented the *backtest's* client id on
its historical-data connection, while ``--ib-client-id`` (58) covered only the
broker snapshot and the order path.

IB refuses a second connection presenting a client id already in use, and the
weekly backtest refresh is Tuesday 05:00 SGT with a six-hour bound, so it can
hold id 10 until ~11:00. A paper run started in that window — which is exactly a
catch-up after a missed 04:15 slot — fails to fetch bars and exits with
"ERROR: No data fetched. Is IB Gateway running?", naming the wrong cause.

``deploy/launchd/run_backtest_refresh.sh`` asserted the opposite as settled fact,
and load-bearingly: the whole ``REFRESH_TIMEOUT`` design rests on the two jobs
contending only for the pacing budget and never for identity.

    (No clientId collision — the backtest uses 10, run_paper 58/59 — but they
    contend for the same historical-data pacing budget.)

These guards read the ids out of the source rather than restating them, so the
comment and the code cannot drift apart again. Everything here is AST-level: no
IB connection, and no import of the scripts, which would pull ib_insync and the
service packages for no benefit.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RUN_PAPER = REPO / "scripts/run_paper.py"
RUN_BACKTEST = REPO / "scripts/run_backtest.py"
REFRESH = REPO / "deploy/launchd/run_backtest_refresh.sh"

FETCHER = "fetch_bars_from_ib"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def _backtest_fetcher_default_client_id() -> int:
    """The default `run_backtest.fetch_bars_from_ib` hands out."""
    for node in ast.walk(_tree(RUN_BACKTEST)):
        if isinstance(node, ast.FunctionDef) and node.name == FETCHER:
            args = node.args
            names = [a.arg for a in args.args]
            assert "client_id" in names, names
            # Defaults align to the tail of args.args.
            offset = len(names) - len(args.defaults)
            default = args.defaults[names.index("client_id") - offset]
            assert isinstance(default, ast.Constant), ast.dump(default)
            return int(default.value)
    raise AssertionError(f"{FETCHER} not found in {RUN_BACKTEST}")


def _paper_fetch_calls() -> list[ast.Call]:
    return [
        node
        for node in ast.walk(_tree(RUN_PAPER))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == FETCHER
    ]


def _paper_client_id_default() -> int:
    """The default of run_paper's own --ib-client-id argparse option."""
    for node in ast.walk(_tree(RUN_PAPER)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "add_argument":
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        if node.args[0].value != "--ib-client-id":
            continue
        for kw in node.keywords:
            if kw.arg == "default" and isinstance(kw.value, ast.Constant):
                return int(kw.value.value)
    raise AssertionError("--ib-client-id default not found in run_paper.py")


def _paper_client_id_expressions() -> list[str]:
    """Every `client_id=` value the paper run passes anywhere, unparsed."""
    out: list[str] = []
    for node in ast.walk(_tree(RUN_PAPER)):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "client_id":
                    out.append(ast.unparse(kw.value))
    return out


def test_the_bar_fetch_passes_a_client_id_at_all():
    """The defect. Omitting it silently adopts the backtest's default."""
    calls = _paper_fetch_calls()
    assert calls, f"no {FETCHER} call found in run_paper.py"
    for call in calls:
        kwargs = {kw.arg for kw in call.keywords}
        assert "client_id" in kwargs, (
            f"{FETCHER} called without client_id at run_paper.py:{call.lineno} — "
            f"it will inherit run_backtest's default"
        )


def test_the_bar_fetch_uses_the_runs_own_client_id_option():
    """Not merely *a* literal: it must be the id the operator can override."""
    for call in _paper_fetch_calls():
        for kw in call.keywords:
            if kw.arg == "client_id":
                assert "ib_client_id" in ast.unparse(kw.value), (
                    f"run_paper.py:{call.lineno} passes a client_id that is not "
                    f"derived from --ib-client-id: {ast.unparse(kw.value)}"
                )


def test_the_paper_and_backtest_client_ids_are_disjoint():
    """The invariant run_backtest_refresh.sh's timeout design rests on.

    The paper run uses its option and that option + 1, so both must clear the
    backtest's default.
    """
    backtest_id = _backtest_fetcher_default_client_id()
    paper_id = _paper_client_id_default()
    paper_ids = {paper_id, paper_id + 1}
    assert backtest_id not in paper_ids, (
        f"backtest client id {backtest_id} collides with run_paper's {sorted(paper_ids)}; "
        f"the Tuesday 05:00-11:00 refresh window would block a paper catch-up"
    )


def test_no_paper_client_id_is_a_bare_literal_colliding_with_the_backtest():
    """Belt and braces: a hardcoded 10 anywhere in run_paper reintroduces it."""
    backtest_id = _backtest_fetcher_default_client_id()
    literals = [e for e in _paper_client_id_expressions() if e.isdigit()]
    assert str(backtest_id) not in literals, (
        f"run_paper.py passes a literal client_id={backtest_id}, the backtest's id"
    )


def test_the_refresh_wrapper_states_the_ids_the_code_actually_uses():
    """The comment is load-bearing — it justifies REFRESH_TIMEOUT — so it must
    describe the code rather than an intention."""
    # The claim wraps across comment lines, so strip the leaders and rejoin
    # before matching — otherwise a reflow silently disarms this guard.
    text = " ".join(
        re.sub(r"^\s*#\s?", "", line) for line in REFRESH.read_text().splitlines()
    )
    match = re.search(
        r"No clientId collision\s*[—-]\s*the backtest uses (\d+),\s*run_paper (\d+)/(\d+)",
        text,
    )
    assert match, "run_backtest_refresh.sh no longer states the client ids it relies on"
    stated_backtest, stated_a, stated_b = (int(g) for g in match.groups())
    paper_id = _paper_client_id_default()
    assert stated_backtest == _backtest_fetcher_default_client_id()
    assert {stated_a, stated_b} == {paper_id, paper_id + 1}
