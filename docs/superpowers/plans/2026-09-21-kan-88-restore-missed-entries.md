# KAN-88 — Restore Missed Entries From an IB Statement

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the operator a tool that rebuilds the 15 entry fills IB really
executed on 2026-09-18 but the blinded executor never booked, from an IB
Account Management statement, through the same `FillProjector` path a live
fill takes — so the positions, cash, trades and intents all land correctly and
`reconcile_paper.py --report` flips back to `entries_allowed: true`.

**Architecture:** Nothing new decides anything. KAN-87's `plan_sweep` already
encodes every rule this needs — skip an execution already in `execution_fills`,
refuse one with no `order_intents` row, defer one whose commission cannot be
expressed in USD, un-expire an intent terminalized on absence, and build a
`FillMessage` the projector accepts. The only thing it hardcodes is *where the
execution came from*. So this work parameterizes that one value, adds a parser
that turns statement rows into the `SweptExecution` records `plan_sweep`
already eats, and wraps both in an operator CLI with the refusal-and-
confirmation discipline `restore_missed_exit.py` established. The book is
never written by this agent — the CLI is handed over and the operator runs it.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x, Alembic, pytest
(`asyncio_mode = "auto"`), stdlib `csv`.

**Spec:** https://huiliang.atlassian.net/browse/KAN-88 (the issue description
is the spec; its 8 acceptance criteria are the contract)

## Global Constraints

- Branch is stacked on `feat/kan-87-execution-sweep` (PR #183). Alembic head
  on that branch is `8f3a41bf29f8`; any new migration hangs off it.
- All modules open with `from __future__ import annotations`.
- **Never invent a price.** Every economic value comes from the statement.
  A missing or unparseable one is a refusal, never a default.
- **This agent never mutates the paper book.** Per repo CLAUDE.md, `--apply`
  is an operator action. The deliverable is the tool, its tests, and the
  runbook.
- Paper accounts only: an `account_id` not starting with `DU` is refused.
- Recovered fills must stay distinguishable from observed ones, forever.

---

### Task 1: Let a recovery name its own source

`plan_sweep` stamps every message it builds `RECOVERY_SOURCE_SWEEP`. A
statement repair must be countable *separately* from the nightly sweep —
15 fills recovered by hand on one day and 15 recovered by the sweep over
two weeks are different facts about the system's health.

**Files:**
- Modify: `services/execution/execution_sweep.py:28` (constants),
  `:76-78` (`plan_sweep` signature), `:148-150` (call to `_to_fill_message`),
  `:161-163` (`_to_fill_message` signature), `:208` (the stamped value)
- Test: `tests/services/execution/test_execution_sweep.py`

**Interfaces:**
- Produces:
  - `RECOVERY_SOURCE_STATEMENT: str = "ib_statement"`
  - `plan_sweep(executions: Sequence[SweptExecution], ledger: OrderLedger, *, recovery_source: str = RECOVERY_SOURCE_SWEEP) -> SweepOutcome`

- [ ] **Step 1: Write the failing test**

```python
def test_a_caller_can_name_the_recovery_source(session):
    """KAN-88 AC6. A statement repair and the nightly sweep must be
    countable apart: 15 fills rebuilt by hand on one day and 15 recovered
    by the sweep over two weeks say different things about the system."""
    ledger = OrderLedger(session)
    _intent(session, ledger, ib_order_id="189", recommendation_id="rec-189")
    outcome = plan_sweep(
        [_execution(ib_order_id="189", execution_id="stmt-189")],
        ledger,
        recovery_source=RECOVERY_SOURCE_STATEMENT,
    )
    assert [f.recovery_source for f in outcome.recovered] == ["ib_statement"]


def test_the_sweep_source_is_still_the_default(session):
    """The nightly sweep passes nothing and must be unaffected."""
    ledger = OrderLedger(session)
    _intent(session, ledger, ib_order_id="190", recommendation_id="rec-190")
    outcome = plan_sweep(
        [_execution(ib_order_id="190", execution_id="exec-190")], ledger
    )
    assert [f.recovery_source for f in outcome.recovered] == ["ib_execution_sweep"]
```

(Reuse whatever `_intent` / `_execution` helpers the existing module already
defines; do not add parallel ones.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/services/execution/test_execution_sweep.py -k recovery_source -v`
Expected: FAIL — `ImportError: cannot import name 'RECOVERY_SOURCE_STATEMENT'`

- [ ] **Step 3: Write minimal implementation**

```python
#: Written to ``ExecutionFill.recovery_source`` for a fill rebuilt from an
#: IB Account Management statement, weeks after the fact (KAN-88). Distinct
#: from the nightly sweep's value on purpose: the sweep recovering fills
#: every night is a worsening upstream problem, whereas a statement repair
#: is a one-off with a human behind it. Counting them together would hide
#: the first inside the second.
RECOVERY_SOURCE_STATEMENT = "ib_statement"


def plan_sweep(
    executions: Sequence[SweptExecution],
    ledger: OrderLedger,
    *,
    recovery_source: str = RECOVERY_SOURCE_SWEEP,
) -> SweepOutcome:
```

Thread `recovery_source` into `_to_fill_message(execution, intent,
commission_trading, recovery_source)` and stamp it there in place of the
constant. Keep the default so the nightly sweep's call site is untouched.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/services/execution/test_execution_sweep.py tests/scripts/test_run_paper_execution_sweep.py -q`
Expected: PASS, no regressions.

- [ ] **Step 5: Commit**

```bash
git add services/execution/execution_sweep.py tests/services/execution/test_execution_sweep.py
git commit -m "KAN-88: let a recovery name its own source"
```

---

### Task 2: Record *when* a fill was recovered, so the digest can see it

AC6 says every recovered fill is counted in the daily digest.
`pipeline_report_summary.py` bounds its recovered count on `executed_at`,
which for this repair is 2026-09-18 — days before the night the repair
actually runs. So the digest would count zero on the one night that matters,
which is the exact failure KAN-87's own comment at `:135-142` describes for
the sweep. `executed_at` cannot answer "what did we recover today"; only a
recovery timestamp can.

**Files:**
- Create: `migrations/versions/<rev>_add_execution_fill_recovered_at.py`
- Modify: `shared/models/order_ledger.py:149-152` (after `recovery_source`),
  `services/portfolio_accounting/projector.py:~213` (the `_fill_values` dict),
  `scripts/ops/pipeline_report_summary.py:145-150` (the count)
- Test: `tests/services/portfolio_accounting/test_projector.py`,
  `tests/deploy/test_pipeline_report.py`

**Interfaces:**
- Produces: `ExecutionFill.recovered_at: Mapped[datetime | None]`, set by
  `FillProjector` to the write time iff `fill.recovery_source` is not None.
  `FillMessage` is **not** changed — the projector stamps the clock, because
  the recovery time is a property of the write, not of the broker's record.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_recovered_fill_records_when_it_was_recovered(session):
    """KAN-88 AC6. executed_at is the broker's clock and can be weeks old;
    recovered_at is when the book learned, which is what the digest asks."""
    before = datetime.now(timezone.utc)
    _project(session, _fill(recovery_source="ib_statement",
                            timestamp=datetime(2026, 9, 18, 13, 31, tzinfo=timezone.utc)))
    row = session.scalars(select(ExecutionFill)).one()
    assert row.recovered_at is not None and row.recovered_at >= before
    assert row.executed_at.date() == date(2026, 9, 18)


def test_a_live_fill_has_no_recovery_timestamp(session):
    """A fill the callback delivered was never recovered; stamping it would
    make every ordinary fill look like a repair."""
    _project(session, _fill(recovery_source=None))
    assert session.scalars(select(ExecutionFill)).one().recovered_at is None
```

And in `tests/deploy/test_pipeline_report.py`, alongside the existing KAN-87
window test:

```python
def test_a_statement_repair_is_counted_on_the_night_it_is_applied(tmp_path):
    """KAN-88 AC6. The execution is days old — it is the REPAIR that is
    today's news. Counting on executed_at reported zero on exactly the night
    the operator did the work."""
    def seed(s):
        _fill(s, at=SINCE - timedelta(days=9),
              recovery_source="ib_statement",
              recovered_at=datetime.now(timezone.utc))
    _, sends, _ = _drive_wrapper(tmp_path, seed=seed)
    assert "1 recovered" in _message(sends)
```

Extend the local `_fill` helper in that module with `recovery_source=None,
recovered_at=None` keyword arguments.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/services/portfolio_accounting/test_projector.py -k recover -v`
Expected: FAIL — `AttributeError: 'ExecutionFill' object has no attribute 'recovered_at'`

- [ ] **Step 3: Write the migration**

```python
"""add execution_fills.recovered_at

Revision ID: <generated>
Revises: 8f3a41bf29f8
"""
revision = "<generated>"
down_revision = "8f3a41bf29f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "execution_fills",
        sa.Column("recovered_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("execution_fills", "recovered_at")
```

Generate the revision id with `python -c "import uuid; print(uuid.uuid4().hex[:12])"`.

- [ ] **Step 4: Add the column and stamp it**

In `shared/models/order_ledger.py`, directly after `recovery_source`:

```python
    #: When the book learned of this fill, set only when it was RECOVERED
    #: rather than observed. ``executed_at`` is the broker's clock and can be
    #: weeks older than the repair (KAN-88 rebuilt 2026-09-18 executions on
    #: 2026-09-21), so it cannot answer "what did we recover tonight" — the
    #: question the 04:52 digest exists to ask. ``None`` for every fill the
    #: live callback delivered.
    recovered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
```

In `FillProjector._fill_values`, beside the existing `recovery_source` entry:

```python
            "recovery_source": fill.recovery_source,
            # Stamped here, not carried on the message: the recovery time is
            # a property of the write, not of the broker's record.
            "recovered_at": (
                datetime.now(timezone.utc)
                if fill.recovery_source is not None else None
            ),
```

`_fill_values` is a `@staticmethod`, so this needs no instance state.
**Check `_IMMUTABLE_FILL_FIELDS` does not list `recovered_at`** — it must
not, for the same reason `recovery_source` is excluded (model comment
`order_ledger.py:146-148`): a live callback replaying an execution the
repair already recorded arrives with no recovery fields, and treating that
as an identity conflict would dead-letter a fill whose economics match.

- [ ] **Step 5: Widen the digest count**

In `scripts/ops/pipeline_report_summary.py`, replace the `recovery_source ==
RECOVERY_SOURCE_SWEEP` predicate:

```python
    # Any recovery, not just the sweep's: KAN-88 rebuilds fills from an IB
    # statement and those must be counted too (AC6).
    #
    # Bounded on recovered_at where it exists, falling back to executed_at.
    # A statement repair carries an executed_at days older than the window,
    # so bounding on the broker's clock alone reports zero on exactly the
    # night the operator did the work — the same blind spot the comment
    # above describes for the sweep. Rows written before recovered_at
    # existed keep the old behaviour through the coalesce.
    fills_recovered = session.scalar(
        select(func.count())
        .select_from(ExecutionFill)
        .where(
            func.coalesce(
                ExecutionFill.recovered_at, ExecutionFill.executed_at
            ) >= since - RECOVERED_LOOKBACK,
            ExecutionFill.recovery_source.is_not(None),
        )
    ) or 0
```

Drop the now-unused `RECOVERY_SOURCE_SWEEP` import if nothing else uses it.

- [ ] **Step 6: Run tests**

Run: `python -m pytest tests/services/portfolio_accounting/ tests/deploy/test_pipeline_report.py tests/shared/test_order_ledger_models.py -q`
Expected: PASS. (Note: 4 tests in `test_pipeline_report.py` fail on a
non-UTC host — see "Known baseline failures" at the foot of this plan. Run
that file with `TZ=UTC` and outside 00:00–00:37 SGT.)

- [ ] **Step 7: Commit**

```bash
git add migrations/versions shared/models/order_ledger.py \
        services/portfolio_accounting/projector.py \
        scripts/ops/pipeline_report_summary.py tests/
git commit -m "KAN-88: record when a fill was recovered, so the digest can see it"
```

---

### Task 3: Parse an IB statement into executions

The statement is the only surviving record of these fills — `reqExecutions`
stopped serving them on 2026-09-19. Everything economic in the repair comes
from this file, so the parser's job is to refuse anything it cannot read
rather than to cope.

**Files:**
- Create: `scripts/ops/restore_missed_entries.py` (parser half)
- Test: `tests/ops/test_restore_missed_entries.py`

**Interfaces:**
- Consumes: `SweptExecution` from `services.execution.execution_sweep`
- Produces:
  - `class StatementRefusedError(RuntimeError)`
  - `REQUIRED_COLUMNS: frozenset[str]`
  - `parse_statement(rows: Iterable[Mapping[str, str]], *, account_id: str | None = None) -> list[SweptExecution]`
  - `load_statement(path: Path, *, account_id: str | None = None) -> list[SweptExecution]`

Column mapping — IB Flex Query "Trades" field names, required exactly:

| Statement column | `SweptExecution` field |
|---|---|
| `ClientAccountID` | `account_id` |
| `TradeID` | `execution_id` |
| `IBOrderID` | `ib_order_id` |
| `ConID` | `con_id` |
| `Symbol` | `ticker` |
| `Exchange` | `exchange` |
| `CurrencyPrimary` | `currency` |
| `Buy/Sell` | `side` (`BUY`→`buy`, `SELL`→`sell`) |
| `Quantity` | `quantity` |
| `TradePrice` | `price` |
| `IBCommission` | `commission` (see below) |
| `IBCommissionCurrency` | `commission_currency` |
| `DateTime` | `executed_at` |

- [ ] **Step 1: Write the failing tests**

```python
def test_a_statement_row_becomes_an_execution():
    """AC2. Every economic value comes from the file, none is defaulted."""
    [execution] = parse_statement([_row()])
    assert execution.execution_id == "0000e0d5.68cb1234.01.01"
    assert execution.ib_order_id == "189"
    assert execution.con_id == 4391
    assert execution.ticker == "AMD"
    assert execution.side == "buy"
    assert execution.quantity == 5.0
    assert execution.price == 161.42
    assert execution.commission == 1.0
    assert execution.commission_currency == "USD"
    assert execution.executed_at == datetime(2026, 9, 18, 13, 31, 2, tzinfo=timezone.utc)


def test_a_missing_column_is_refused_by_name():
    """A statement exported with the wrong field set must not be silently
    half-read; the operator has to go back and re-export it."""
    row = _row(); del row["TradePrice"]
    with pytest.raises(StatementRefusedError, match="TradePrice"):
        parse_statement([row])


def test_a_commission_is_recorded_as_a_magnitude():
    """IB reports a commission charge as NEGATIVE. FillProjector._validate
    rejects a negative commission outright, so a verbatim copy would refuse
    every real row."""
    [execution] = parse_statement([_row(IBCommission="-1.00")])
    assert execution.commission == 1.0


def test_an_unreadable_price_is_refused_rather_than_defaulted():
    """Never invent a price -- a wrong one is a wrong cost basis forever."""
    with pytest.raises(StatementRefusedError, match="TradePrice"):
        parse_statement([_row(TradePrice="")])


def test_a_live_account_is_refused():
    """A live ledger is never reconstructed by a script."""
    with pytest.raises(StatementRefusedError, match="paper"):
        parse_statement([_row(ClientAccountID="U1234567")])


def test_a_row_for_another_account_is_refused():
    """A statement covering two accounts must not leak one into the other."""
    with pytest.raises(StatementRefusedError, match="DUN551088"):
        parse_statement([_row(ClientAccountID="DU9999999")], account_id="DUN551088")


def test_partial_executions_of_one_order_accumulate():
    """cumulative_quantity is what the projector advances the intent on; a
    per-row copy of quantity would terminalize a 2-of-5 fill as complete."""
    rows = [
        _row(TradeID="a", Quantity="2", **{"DateTime": "20260918;093102"}),
        _row(TradeID="b", Quantity="3", **{"DateTime": "20260918;093400"}),
    ]
    first, second = parse_statement(rows)
    assert (first.quantity, first.cumulative_quantity) == (2.0, 2.0)
    assert (second.quantity, second.cumulative_quantity) == (3.0, 5.0)


def test_both_ib_datetime_formats_are_read():
    """Flex serves 'YYYYMMDD;HHMMSS'; an Activity Statement CSV serves ISO."""
    a, = parse_statement([_row(**{"DateTime": "20260918;093102"})])
    b, = parse_statement([_row(**{"DateTime": "2026-09-18 09:31:02"})])
    assert a.executed_at == b.executed_at


def test_an_unparseable_datetime_is_refused():
    with pytest.raises(StatementRefusedError, match="DateTime"):
        parse_statement([_row(**{"DateTime": "last Tuesday"})])
```

Helper for the module:

```python
def _row(**overrides):
    """One AMD row from the 2026-09-18 batch, in IB Flex 'Trades' shape.

    Times are US/Eastern in the statement; 09:31:02 EDT is 13:31:02Z.
    """
    row = {
        "ClientAccountID": "DUN551088",
        "TradeID": "0000e0d5.68cb1234.01.01",
        "IBOrderID": "189",
        "ConID": "4391",
        "Symbol": "AMD",
        "Exchange": "NASDAQ",
        "CurrencyPrimary": "USD",
        "Buy/Sell": "BUY",
        "Quantity": "5",
        "TradePrice": "161.42",
        "IBCommission": "-1.00",
        "IBCommissionCurrency": "USD",
        "DateTime": "2026-09-18 13:31:02",
    }
    row.update(overrides)
    return row
```

Note the fixture uses a UTC-equivalent wall time so the two format tests
agree; document in the parser that `DateTime` is read as **UTC** and that a
statement exported in local time must be re-exported in UTC (Flex offers
this), because a silent 4-hour shift would file a fill on the wrong session.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/ops/test_restore_missed_entries.py -q`
Expected: FAIL — `ModuleNotFoundError`/`ImportError` for the new module.

- [ ] **Step 3: Write the parser**

Module docstring must state: what was lost and why (`Error 1101` left the
executor bound to Trade objects IB stopped pushing to), why `reqExecutions`
cannot reach back (one trading day, closed 2026-09-19), and that this file
is the only surviving record. Then:

```python
REQUIRED_COLUMNS = frozenset({
    "ClientAccountID", "TradeID", "IBOrderID", "ConID", "Symbol",
    "Exchange", "CurrencyPrimary", "Buy/Sell", "Quantity", "TradePrice",
    "IBCommission", "IBCommissionCurrency", "DateTime",
})

_SIDE = {"BUY": "buy", "SELL": "sell"}


def parse_statement(rows, *, account_id=None):
    parsed = []
    for index, row in enumerate(rows, start=1):
        missing = sorted(REQUIRED_COLUMNS - set(row))
        if missing:
            raise StatementRefusedError(
                f"row {index} is missing required column(s): "
                f"{', '.join(missing)}. Re-export the statement with the "
                "full Flex 'Trades' field set rather than filling them in "
                "by hand."
            )
        ...
    return _with_cumulative(parsed)
```

Each field gets its own named refusal. `_number(row, column, index)` raises
`StatementRefusedError` naming the column on a blank, non-numeric or
non-finite value. `_with_cumulative` groups by `ib_order_id`, sorts each
group by `(executed_at, execution_id)`, and assigns a running sum — the
returned list preserves that sorted order so a caller projecting in order
never advances an intent past a fill it has not applied.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/ops/test_restore_missed_entries.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/ops/restore_missed_entries.py tests/ops/test_restore_missed_entries.py
git commit -m "KAN-88: read an IB statement into broker executions"
```

---

### Task 4: The operator CLI — dry run, refuse, confirm, project, prove

**Files:**
- Modify: `scripts/ops/restore_missed_entries.py` (CLI half)
- Test: `tests/ops/test_restore_missed_entries.py`

**Interfaces:**
- Consumes: `parse_statement`/`load_statement` (Task 3), `plan_sweep` and
  `RECOVERY_SOURCE_STATEMENT` (Task 1), `FillProjector`, `OrderLedger`,
  `dump_paper_state` (from `scripts.run_paper`, the same import
  `scripts/reconcile_paper.py:23` uses)
- Produces:
  - `CONFIRMATION = "RESTORE MISSED ENTRIES"`
  - `class RecoveryRefusedError(RuntimeError)` — the apply-side refusal, kept
    distinct from Task 3's `StatementRefusedError` so a bad file and a bad
    confirmation are never confused in a traceback
  - `apply_recovery(session, outcome: SweepOutcome, *, confirm: str) -> list[str]`
    — returns the execution ids actually projected
  - `main(argv: list[str] | None = None) -> int`

CLI:

```
python scripts/ops/restore_missed_entries.py --statement <path.csv> \
    [--account DUN551088] [--database-url ...] [--artifact-dir output/repairs] [--apply]
```

- [ ] **Step 1: Write the failing tests**

```python
def test_a_statement_fill_opens_the_position(session):
    """AC1/AC2/AC3. The position, the trade at the statement price, and the
    cash movement all come out of the ordinary projector path."""
    _seed_expired_intent(session, ib_order_id="189", ticker="AMD",
                         con_id=4391, requested=5.0809)
    outcome = plan_sweep(parse_statement([_row()]), OrderLedger(session),
                         recovery_source=RECOVERY_SOURCE_STATEMENT)
    apply_recovery(session, outcome, confirm=CONFIRMATION)

    position = session.scalars(select(Position).where(Position.con_id == 4391)).one()
    assert position.quantity == 5.0
    trade = session.scalars(select(Trade).where(Trade.ticker == "AMD")).one()
    assert trade.entry_price == 161.42


def test_a_wrongly_expired_intent_is_restored_then_filled(session):
    """AC4. The 17 intents for orders 189-205 were terminalized EXPIRED with
    ABSENT_AT_IB_REASON by a restart on 2026-09-19. For the 15 that filled
    that is factually wrong, and _advance_intent returns early on a terminal
    status, so the projector alone would leave them EXPIRED forever."""
    _seed_expired_intent(session, ib_order_id="189", ticker="AMD",
                         con_id=4391, requested=5.0809)
    outcome = plan_sweep(parse_statement([_row()]), OrderLedger(session),
                         recovery_source=RECOVERY_SOURCE_STATEMENT)
    apply_recovery(session, outcome, confirm=CONFIRMATION)

    intent = session.scalars(select(OrderIntent)).one()
    # 5.0809 requested, 5 filled: a sub-one-share shortfall is whole-share
    # rounding, not a material partial, so FILLED is correct.
    assert intent.status == OrderStatus.FILLED.value
    assert intent.reason is None


def test_an_intent_expired_for_a_real_reason_is_left_alone(session):
    """restore_absent_terminalization refuses any reason but absence; the
    execution is reported untracked rather than forced through."""
    _seed_expired_intent(session, ib_order_id="189", ticker="AMD",
                         con_id=4391, requested=5.0809,
                         reason="cancelled by risk")
    outcome = plan_sweep(parse_statement([_row()]), OrderLedger(session),
                         recovery_source=RECOVERY_SOURCE_STATEMENT)
    assert outcome.recovered == ()
    assert outcome.untracked == ("189",)


def test_re_running_the_repair_changes_nothing(session):
    """AC7. Idempotent: the second run recognises the execution and skips."""
    _seed_expired_intent(session, ib_order_id="189", ticker="AMD",
                         con_id=4391, requested=5.0809)
    ledger = OrderLedger(session)
    apply_recovery(session,
                   plan_sweep(parse_statement([_row()]), ledger,
                              recovery_source=RECOVERY_SOURCE_STATEMENT),
                   confirm=CONFIRMATION)
    second = plan_sweep(parse_statement([_row()]), ledger,
                        recovery_source=RECOVERY_SOURCE_STATEMENT)
    assert second.recovered == () and second.already_recorded == 1
    assert apply_recovery(session, second, confirm=CONFIRMATION) == []

    assert session.scalar(select(func.count()).select_from(Position)) == 1
    assert session.scalar(select(func.count()).select_from(Trade)) == 1
    assert session.scalars(select(Position)).one().quantity == 5.0


def test_every_recovered_fill_is_marked(session):
    """AC6. Evidence rebuilt from a statement weeks later must never be
    indistinguishable from evidence observed on the day."""
    _seed_expired_intent(session, ib_order_id="189", ticker="AMD",
                         con_id=4391, requested=5.0809)
    apply_recovery(session,
                   plan_sweep(parse_statement([_row()]), OrderLedger(session),
                              recovery_source=RECOVERY_SOURCE_STATEMENT),
                   confirm=CONFIRMATION)
    fill = session.scalars(select(ExecutionFill)).one()
    assert fill.recovery_source == "ib_statement"
    assert fill.recovered_at is not None


def test_a_wrong_confirmation_writes_nothing(session):
    _seed_expired_intent(session, ib_order_id="189", ticker="AMD",
                         con_id=4391, requested=5.0809)
    outcome = plan_sweep(parse_statement([_row()]), OrderLedger(session),
                         recovery_source=RECOVERY_SOURCE_STATEMENT)
    with pytest.raises(RecoveryRefusedError, match="RESTORE MISSED ENTRIES"):
        apply_recovery(session, outcome, confirm="yes")
    assert session.scalar(select(func.count()).select_from(ExecutionFill)) == 0


def test_a_dry_run_writes_nothing(tmp_path, session_url, capsys):
    """Default is report-only; --apply is the only way to touch the book."""
    statement = tmp_path / "trades.csv"
    _write_csv(statement, [_row()])
    assert main(["--statement", str(statement), "--database-url", session_url]) == 0
    assert "Dry-run" in capsys.readouterr().out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/ops/test_restore_missed_entries.py -q`
Expected: FAIL — `apply_recovery` / `main` not defined.

- [ ] **Step 3: Write the CLI half**

`apply_recovery` mirrors `restore_missed_exit.apply_restore`'s discipline:

```python
def apply_recovery(session, outcome, *, confirm):
    if confirm != CONFIRMATION:
        raise RecoveryRefusedError(
            f"exact confirmation required: expected {CONFIRMATION!r}"
        )
    projector = FillProjector(session)
    applied = []
    for fill in outcome.recovered:
        # One transaction per fill, because FillProjector.apply owns its own
        # and refuses a session carrying pending work. A failure therefore
        # leaves the fills before it COMMITTED and correct -- which is right:
        # a booked purchase must never be rolled back to tidy up a later
        # row's failure. The caller reports how far it got.
        if projector.apply(fill):
            applied.append(fill.execution_id)
    return applied
```

`main`:
1. Parse args; `load_statement(path, account_id=args.account)`.
2. Open a session; `plan_sweep(executions, OrderLedger(session),
   recovery_source=RECOVERY_SOURCE_STATEMENT)`.
3. Print the plan — one line per execution with ticker, order id, quantity,
   price, commission and its disposition, then the totals
   (`recovered` / `already_recorded` / `untracked` / `deferred`) and the
   summed cost basis.
4. Refuse `--apply` when `outcome.untracked` or `outcome.deferred` is
   non-empty: a partial repair leaves the book half-right and
   reconciliation still fail-closed, and the operator should see why first.
5. `--apply` requires `sys.stdin.isatty()`, then `dump_paper_state(session,
   artifact_dir / f"paper_state_pre_entry_restore_{stamp}.json")` **before**
   prompting (the rollback path — AC's ROLLBACK section), then the
   `input()` confirmation, then `apply_recovery`.
6. Write a JSON artifact next to the dump recording: the statement path and
   its sha256, every execution id applied with its economics, the
   `recovered_at` stamp, and a dated note that `equity_snapshots` rows from
   the execution date to the repair date understate market value and
   overstate cash by the recovered cost basis and are **not** rewritten —
   per-position daily marks were never stored, so they cannot be
   recomputed (the same limitation `backfill_restored_equity.py:31-40`
   documents). This is AC3's "explicitly dated and explained" branch.
7. Print the next step: `python scripts/reconcile_paper.py --report` and
   confirm `entries_allowed: true` (AC5 — an operator verification, recorded
   in the PR, not asserted by a test).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/ops/test_restore_missed_entries.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/ops/restore_missed_entries.py tests/ops/test_restore_missed_entries.py
git commit -m "KAN-88: rebuild missed entries from an IB statement"
```

---

### Task 5: The runbook

The repair is an operator action this agent must not perform. Without a
runbook the tool is undeployable.

**Files:**
- Create: `docs/operations/restore-missed-entries.md`
- Modify: `docs/operations/live-topology.md` (link it from the recovery
  section KAN-87 added)

- [ ] **Step 1: Write the runbook**

Cover, in order: how to export the Flex Query (report the **Trades**
section, `DateTime` in UTC, the 13 required fields, date range covering
2026-09-18); the dry run and how to read its output; what each refusal
means and what to do about it; the `--apply` confirmation; the
post-conditions to check (`reconcile_paper.py --report` shows
`severity: ok` / `entries_allowed: true`); and the rollback (restore the
`paper_state_pre_entry_restore_*.json` dump).

State plainly that the equity series is **not** rewritten and why, and
point at the artifact that dates the correction.

- [ ] **Step 2: Commit**

```bash
git add docs/operations/
git commit -m "KAN-88: runbook for rebuilding missed entries from a statement"
```

---

## Known baseline failures (pre-existing, not this work)

`tests/deploy/test_pipeline_report.py` has 4 clock-dependent failures on a
non-UTC host, present on `feat/kan-87-execution-sweep` before this branch and
green in CI (which runs UTC):

1. `test_the_message_reports_the_documented_facts_in_order` —
   `run_pipeline_report.sh:47` builds `SINCE` with a `%z` offset that SQLite
   drops, so under `TZ=Asia/Singapore` the bound lands 8h in the future.
   Passes under `TZ=UTC`.
2. The three halt/escalation tests — `_seed_reconciliation("disabled")` seeds
   blocked readings at `now-37min` and `now-1d`, and `_session_key` buckets by
   `Asia/Singapore` date. Between 00:00 and 00:37 SGT both land in the same
   session, so `disabled_sessions` is 1 against `ESCALATE_AFTER_SESSIONS = 2`
   and nothing escalates.

Report on KAN-87; do not fix here.

## Acceptance criteria map

| AC | Where |
|---|---|
| 1 positions match IB exactly | Task 4 — `test_a_statement_fill_opens_the_position`; operator-verified via AC5 |
| 2 real fill price, never invented | Task 3 refusals; Task 4 trade-price assertion |
| 3 cash + realised P&L; equity dated and explained | Task 4 — projector path moves cash; artifact dates the equity note |
| 4 intents FILLED not EXPIRED | Task 4 — `test_a_wrongly_expired_intent_is_restored_then_filled` |
| 5 reconcile reports ok / entries_allowed | Task 4 step 7 + runbook; operator step, recorded in the PR |
| 6 marked `recovery_source`, counted in the digest | Tasks 1 + 2 |
| 7 idempotent | Task 4 — `test_re_running_the_repair_changes_nothing` |
| 8 pytest green | Final verification before the PR |
