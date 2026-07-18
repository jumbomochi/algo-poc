# Durable Paper Ledger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make PostgreSQL the durable sleeve/order ledger, size paper capital from IB NetLiquidation, hydrate restart-safe strategy state, and mutate positions only from idempotent IB fills.

**Architecture:** IB owns account NAV, aggregate positions, open orders, and executions; PostgreSQL owns sleeve attribution, intents, reservations, immutable fills, and materialized sleeve positions. Redis Streams transports deterministic recommendations and enriched fills, while startup reconciliation blocks new entries whenever broker and database filled state disagree.

**Tech Stack:** Python 3.12+, SQLAlchemy 2, Alembic, PostgreSQL 16, Redis Streams, Pydantic 2, ib_insync, pytest.

## Global Constraints

- All migrations are additive; implementation must not repair, reset, delete, or overwrite current paper data.
- Paper defaults to deployment fraction `1.0` with no cap.
- Live defaults to fraction `0.0` and cap `0.0`; both must be positive before live entries are permitted.
- Scheduled entry publication defaults to disabled in both modes until the
  operator completes reconciliation and explicitly enables the selected mode.
- `deployment_fraction` must remain within `[0.0, 1.0]`; a configured cap must be non-negative.
- IB executions are the only events allowed to create positions, change filled quantity, or change cash because of trading.
- PostgreSQL aggregate filled quantities must reconcile to IB before new entries are published.
- Reconciliation mismatches block entries but not reduction exits or kill liquidation.
- Repair application remains interactive, TTY-only, backed up, and run only by the human operator.
- Candidate replacement stays disabled until ten-year and walk-forward validation passes the documented performance and turnover gates.
- Preserve the six existing sleeve weights and existing trading surfaces.

---

### Task 1: Add mode-specific capital configuration and pure sizing

**Files:**
- Modify: `shared/config.py`
- Modify: `config/default.yaml`
- Create: `shared/capital.py`
- Test: `tests/shared/test_capital.py`
- Test: `tests/shared/test_config.py`

**Interfaces:**
- Produces: `CapitalModeConfig`, `CapitalConfig`, `CapitalBudget`, and `calculate_capital_budget(net_liquidation, mode, config, sleeve_weights)`.
- Consumes: existing `AppConfig.mode` and six-sleeve `CAPITAL_ALLOCATIONS` mapping.

- [ ] **Step 1: Write failing configuration and sizing tests**

```python
def test_paper_defaults_to_full_nav_without_cap():
    cfg = CapitalConfig()
    budget = calculate_capital_budget(
        1_000_000.0, "paper", cfg, {"momentum": 0.6, "hedge": 0.4}
    )
    assert budget.deployable_capital == 1_000_000.0
    assert budget.sleeve_budgets == {"momentum": 600_000.0, "hedge": 400_000.0}

def test_cap_limits_fractional_budget():
    cfg = CapitalConfig(paper=CapitalModeConfig(
        deployment_fraction=0.5, max_deployable_usd=200_000.0
    ))
    assert calculate_capital_budget(1_000_000.0, "paper", cfg, {"x": 1.0}).deployable_capital == 200_000.0

def test_live_is_disabled_by_default():
    with pytest.raises(CapitalDisabledError, match="fraction and cap"):
        calculate_capital_budget(1_000_000.0, "live", CapitalConfig(), {"x": 1.0})

def test_fraction_outside_unit_interval_is_invalid():
    with pytest.raises(ValidationError):
        CapitalModeConfig(deployment_fraction=1.01)
```

- [ ] **Step 2: Run the tests and verify the missing-interface failures**

Run: `pytest tests/shared/test_capital.py tests/shared/test_config.py -v`  
Expected: FAIL because `CapitalConfig` and `shared.capital` do not exist.

- [ ] **Step 3: Implement configuration and capital calculation**

```python
class CapitalModeConfig(BaseModel):
    deployment_fraction: float = Field(ge=0.0, le=1.0)
    max_deployable_usd: float | None = Field(default=None, ge=0.0)
    entries_enabled: bool = False

class CapitalConfig(BaseModel):
    paper: CapitalModeConfig = Field(default_factory=lambda: CapitalModeConfig(
        deployment_fraction=1.0, max_deployable_usd=None, entries_enabled=False
    ))
    live: CapitalModeConfig = Field(default_factory=lambda: CapitalModeConfig(
        deployment_fraction=0.0, max_deployable_usd=0.0, entries_enabled=False
    ))
```

```python
@dataclass(frozen=True)
class CapitalBudget:
    net_liquidation: float
    deployment_fraction: float
    max_deployable_usd: float | None
    deployable_capital: float
    sleeve_budgets: dict[str, float]

def calculate_capital_budget(net_liquidation, mode, config, sleeve_weights):
    selected = config.live if mode == "live" else config.paper
    if net_liquidation <= 0:
        raise ValueError("IB NetLiquidation must be positive")
    if mode == "live" and (
        selected.deployment_fraction <= 0
        or selected.max_deployable_usd is None
        or selected.max_deployable_usd <= 0
    ):
        raise CapitalDisabledError("live fraction and cap must both be positive")
    fractional = net_liquidation * selected.deployment_fraction
    deployable = min(fractional, selected.max_deployable_usd) \
        if selected.max_deployable_usd is not None else fractional
    if not math.isclose(sum(sleeve_weights.values()), 1.0, abs_tol=1e-6):
        raise ValueError("sleeve weights must sum to 1.0")
    return CapitalBudget(
        net_liquidation=net_liquidation,
        deployment_fraction=selected.deployment_fraction,
        max_deployable_usd=selected.max_deployable_usd,
        deployable_capital=deployable,
        sleeve_budgets={k: deployable * v for k, v in sleeve_weights.items()},
    )
```

Add `capital: CapitalConfig` to `AppConfig` and explicit paper/live values to `config/default.yaml`.

- [ ] **Step 4: Run focused tests**

Run: `pytest tests/shared/test_capital.py tests/shared/test_config.py -v`  
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add shared/config.py shared/capital.py config/default.yaml tests/shared/test_capital.py tests/shared/test_config.py
git commit -m "feat: size deployable capital from broker NAV"
```

### Task 2: Add the additive PostgreSQL ledger schema

**Files:**
- Create: `shared/models/order_ledger.py`
- Modify: `shared/models/portfolio.py`
- Modify: `shared/models/__init__.py`
- Create: `migrations/versions/8b6f2c1d4a90_add_durable_order_ledger.py`
- Test: `tests/shared/test_order_ledger_models.py`

**Interfaces:**
- Produces: `OrderIntent`, `ExecutionFill`, `CapitalSnapshot`, `CapitalAdjustment`, `ReconciliationReport`, and `OrderStatus`.
- Consumes: SQLAlchemy `Base` and existing `Position`/`PortfolioConfig`.

- [ ] **Step 1: Write failing model tests**

```python
def test_execution_id_is_unique(session):
    session.add_all([make_fill("exec-1"), make_fill("exec-1")])
    with pytest.raises(IntegrityError):
        session.commit()

def test_recommendation_id_is_unique(session):
    session.add_all([make_intent("rec-1"), make_intent("rec-1")])
    with pytest.raises(IntegrityError):
        session.commit()

def test_position_accepts_broker_contract_identity(session):
    position = Position(
        ticker="BRK B", portfolio="quality_value", quantity=1.0,
        avg_entry_price=500.0, current_price=500.0, peak_price=500.0,
        highest_price_since_entry=500.0, opened_at=NOW, status="open",
        con_id=12345, exchange="SMART", currency="USD",
    )
    session.add(position)
    session.commit()
    assert position.con_id == 12345
```

- [ ] **Step 2: Verify RED**

Run: `pytest tests/shared/test_order_ledger_models.py -v`  
Expected: FAIL because ledger models and contract columns are absent.

- [ ] **Step 3: Implement models and migration**

Define `OrderStatus` as a `StrEnum` with exactly the nine states in the design. Use strings in PostgreSQL plus check constraints so SQLite model tests behave consistently. Key constraints:

```python
UniqueConstraint("recommendation_id", name="uq_order_intent_recommendation")
UniqueConstraint("account_id", "execution_id", name="uq_execution_fill_account_exec")
Index("ix_order_intent_active", "status", "portfolio")
Index("ix_execution_fill_order", "account_id", "ib_order_id")
```

Add nullable `con_id`, `exchange`, and `currency` columns to existing positions so the migration is additive. New fill-created positions must populate all three in application code.

- [ ] **Step 4: Verify model metadata and Alembic chain**

Run: `pytest tests/shared/test_order_ledger_models.py tests/shared/test_models.py -v`  
Expected: PASS.  
Run: `alembic heads`  
Expected: one head, `8b6f2c1d4a90`.

- [ ] **Step 5: Commit**

```bash
git add shared/models migrations/versions/8b6f2c1d4a90_add_durable_order_ledger.py tests/shared/test_order_ledger_models.py
git commit -m "feat: add durable order and fill ledger schema"
```

### Task 3: Implement conditional lifecycle transitions and reservations

**Files:**
- Create: `shared/order_ledger.py`
- Test: `tests/shared/test_order_ledger.py`

**Interfaces:**
- Produces: `OrderLedger.create_intent`, `transition`, `record_submission`, `active_reservations`, `load_pending_orders`, and `mark_published`.
- Consumes: Task 2 ledger models and a SQLAlchemy `Session`.

- [ ] **Step 1: Write failing lifecycle tests**

```python
def test_terminal_intent_cannot_transition(session):
    ledger = OrderLedger(session)
    ledger.create_intent(make_proposal("rec-1"))
    ledger.transition("rec-1", OrderStatus.RISK_REJECTED, reason="sector")
    with pytest.raises(InvalidOrderTransition):
        ledger.transition("rec-1", OrderStatus.APPROVED)

def test_active_buy_reservation_uses_unfilled_notional(session):
    ledger = OrderLedger(session)
    ledger.create_intent(make_proposal("rec-1", quantity=10, price=100))
    ledger.transition("rec-1", OrderStatus.APPROVED)
    intent = ledger.get("rec-1")
    intent.filled_quantity = 4
    session.flush()
    assert ledger.active_reservations("momentum") == pytest.approx(600.0)

def test_create_intent_is_idempotent(session):
    ledger = OrderLedger(session)
    first = ledger.create_intent(make_proposal("rec-1"))
    second = ledger.create_intent(make_proposal("rec-1"))
    assert first.id == second.id
```

- [ ] **Step 2: Verify RED**

Run: `pytest tests/shared/test_order_ledger.py -v`  
Expected: FAIL because `OrderLedger` is missing.

- [ ] **Step 3: Implement repository with compare-and-set transitions**

Use a transition map and lock with
`select(OrderIntent).where(OrderIntent.recommendation_id == recommendation_id).with_for_update()`
on PostgreSQL. Keep the same API under SQLite tests:

```python
ALLOWED_TRANSITIONS = {
    OrderStatus.PROPOSED: {OrderStatus.RISK_REJECTED, OrderStatus.APPROVED},
    OrderStatus.APPROVED: {OrderStatus.SUBMISSION_FAILED, OrderStatus.SUBMITTED},
    OrderStatus.SUBMITTED: {
        OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED,
        OrderStatus.CANCELLED, OrderStatus.EXPIRED,
    },
    OrderStatus.PARTIALLY_FILLED: {
        OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED,
        OrderStatus.CANCELLED, OrderStatus.EXPIRED,
    },
}
```

`active_reservations(portfolio)` sums `(requested_quantity - filled_quantity) * limit_price` for buy intents in `APPROVED`, `SUBMITTED`, or `PARTIALLY_FILLED`.

- [ ] **Step 4: Verify GREEN**

Run: `pytest tests/shared/test_order_ledger.py -v`  
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add shared/order_ledger.py tests/shared/test_order_ledger.py
git commit -m "feat: persist order lifecycle and reservations"
```

### Task 4: Gate projected exposure instead of current exposure

**Files:**
- Modify: `services/risk_management/engine.py`
- Modify: `backtest/_portfolio_state.py`
- Test: `tests/services/risk_management/test_engine.py`
- Test: `tests/services/risk_management/test_stop_loss.py`

**Interfaces:**
- Produces: `RiskEngine.check_entry(ticker, quantity, price, sector, portfolio, existing_lots=0, reserved_notional=0.0)` that scales to total, sector, and position headroom.
- Consumes: current market value through `PortfolioState.total_exposure_pct`, `nav` as the exposure basis, and reservation value from Task 3.

- [ ] **Step 1: Write projected-exposure regression tests**

```python
def test_entry_is_scaled_to_total_exposure_headroom():
    engine = RiskEngine(position_entry_limit_pct=50, total_exposure_limit_pct=100)
    state = make_portfolio(nav=10_000, total_exposure_pct=90)
    decision = engine.check_entry("AAPL", 20, 100, "Tech", state)
    assert decision.approved
    assert decision.adjusted_quantity == pytest.approx(10)

def test_pending_reservation_consumes_headroom():
    engine = RiskEngine(position_entry_limit_pct=50, total_exposure_limit_pct=100)
    state = make_portfolio(nav=10_000, total_exposure_pct=80)
    decision = engine.check_entry(
        "AAPL", 20, 100, "Tech", state, reserved_notional=1_500
    )
    assert decision.adjusted_quantity == pytest.approx(5)

def test_no_trade_when_projected_headroom_is_zero():
    state = make_portfolio(nav=10_000, total_exposure_pct=100)
    assert not RiskEngine(total_exposure_limit_pct=100).check_entry(
        "AAPL", 1, 100, "Tech", state
    ).approved
```

- [ ] **Step 2: Verify RED**

Run: `pytest tests/services/risk_management/test_engine.py -v`  
Expected: FAIL because the current check ignores proposed and reserved notional.

- [ ] **Step 3: Implement headroom scaling**

```python
current_value = portfolio.nav * portfolio.total_exposure_pct / 100.0
limit_value = portfolio.nav * self.total_exposure_limit_pct / 100.0
headroom = max(0.0, limit_value - current_value - reserved_notional)
allowed_value = min(
    quantity * price,
    headroom,
    portfolio.nav * self.position_entry_limit_pct / 100.0,
)
adjusted_quantity = round(allowed_value / price, 4) if price > 0 else 0.0
```

Apply the equivalent projected-value check to sector exposure. Preserve decision precedence and exit behaviour.

- [ ] **Step 4: Run the entire risk suite**

Run: `pytest tests/services/risk_management/ -v`  
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/risk_management/engine.py backtest/_portfolio_state.py tests/services/risk_management
git commit -m "fix: gate entries on projected exposure"
```

### Task 5: Enrich fill identity and persist execution submissions

**Files:**
- Modify: `shared/schemas/messages.py`
- Modify: `services/execution/ib_executor.py`
- Modify: `services/execution/runner.py`
- Modify: `services/execution/order_manager.py`
- Test: `tests/shared/test_schemas.py`
- Test: `tests/services/execution/test_runner.py`
- Test: `tests/services/execution/test_partial_fills.py`

**Interfaces:**
- Produces: enriched `FillMessage` fields and durable execution callbacks into `OrderLedger`.
- Consumes: `OrderLedger` from Task 3 and IB execution attributes `execId`, `cumQty`, `acctNumber`, and contract `conId`.

- [ ] **Step 1: Write failing enrichment and restart tests**

```python
def test_fill_message_round_trips_execution_identity():
    fill = FillMessage(
        ticker="AAPL", timestamp=NOW, side="buy", quantity=2,
        cumulative_quantity=2, fill_price=100, commission=0.2,
        recommendation_id="rec-1", order_id="9", execution_id="e-1",
        account_id="DUN551088", portfolio="momentum", con_id=265598,
        exchange="SMART", currency="USD",
    )
    assert FillMessage.from_stream_dict(fill.to_stream_dict()) == fill

@pytest.mark.asyncio
async def test_submission_persists_order_id_before_return(runner, ledger):
    await runner.process_approved_order(make_approved_order(recommendation_id="rec-1"))
    assert ledger.get("rec-1").status == OrderStatus.SUBMITTED
    assert ledger.get("rec-1").ib_order_id == "order-001"

def test_setup_reloads_pending_order_attribution(runner, ledger):
    seed_submitted_intent(ledger, recommendation_id="rec-1", ib_order_id="9")
    runner.restore_pending_orders()
    assert runner._pending_orders["9"].portfolio == "momentum"

@pytest.mark.asyncio
async def test_broker_cancellation_releases_reservation(runner, ledger):
    seed_submitted_intent(ledger, recommendation_id="rec-1", ib_order_id="9")
    await runner.handle_ib_order_status({
        "order_id": "9", "status": "Cancelled", "reason": "cancelled at IB"
    })
    assert ledger.get("rec-1").status == OrderStatus.CANCELLED
    assert ledger.active_reservations("momentum") == 0
```

- [ ] **Step 2: Verify RED**

Run: `pytest tests/shared/test_schemas.py tests/services/execution/test_runner.py tests/services/execution/test_partial_fills.py -v`  
Expected: FAIL because execution identity and durable pending-order restoration are absent.

- [ ] **Step 3: Enrich IB payload and execution runner**

The IB fill payload must contain:

```python
{
    "execution_id": str(fill.execution.execId),
    "account_id": str(fill.execution.acctNumber),
    "order_id": order_id,
    "con_id": int(fill.contract.conId),
    "ticker": ticker,
    "exchange": fill.contract.exchange or "SMART",
    "currency": fill.contract.currency or "USD",
    "side": side,
    "quantity": float(fill.execution.shares),
    "cumulative_quantity": float(fill.execution.cumQty),
    "fill_price": float(fill.execution.price),
    "commission": float(getattr(fill.commissionReport, "commission", 0.0) or 0.0),
    "order_done": trade.isDone(),
}
```

Inject a session-backed `OrderLedger` into `ExecutionServiceRunner`. On submit, persist `SUBMITTED` plus IB order ID before acknowledging the approved-order Redis message. On skip/failure, persist `SUBMISSION_FAILED`. Restore active order attribution during setup. Populate `FillMessage.portfolio` from the persisted intent, never from an in-memory-only map. Register an IB order-status handler: broker `Cancelled` and `ApiCancelled` transition to `CANCELLED`, `Inactive` with a rejection reason transitions to `SUBMISSION_FAILED` before any fill or `CANCELLED` after a partial fill, and an unfilled day order missing after its trading session transitions to `EXPIRED` only when IB completed-order history confirms expiry.

- [ ] **Step 4: Run execution and schema tests**

Run: `pytest tests/shared/test_schemas.py tests/services/execution/ -v`  
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add shared/schemas/messages.py services/execution tests/shared/test_schemas.py tests/services/execution
git commit -m "feat: persist IB order and execution identity"
```

### Task 6: Add the idempotent fill projector

**Files:**
- Create: `services/portfolio_accounting/__init__.py`
- Create: `services/portfolio_accounting/projector.py`
- Create: `services/portfolio_accounting/runner.py`
- Create: `services/portfolio_accounting/Dockerfile`
- Modify: `scripts/paper_state.py`
- Test: `tests/services/portfolio_accounting/__init__.py`
- Test: `tests/services/portfolio_accounting/test_projector.py`
- Test: `tests/services/portfolio_accounting/test_runner.py`

**Interfaces:**
- Produces: `FillProjector.apply(fill: FillMessage) -> bool`; returns `False` for an already-applied execution.
- Consumes: enriched fills from Task 5, Task 2 models, and Task 3 lifecycle rules.

- [ ] **Step 1: Write failing projector tests**

```python
def test_replayed_buy_fill_changes_cash_once(projector, session):
    fill = make_fill(execution_id="e-1", quantity=10, price=100, commission=1)
    assert projector.apply(fill) is True
    assert projector.apply(fill) is False
    assert get_position(session, "momentum", "AAPL").quantity == 10
    assert get_cash(session, "momentum") == pytest.approx(8_999)

def test_partial_fills_weight_average_and_complete_intent(projector, ledger):
    seed_submitted_intent(ledger, quantity=10)
    projector.apply(make_fill("e-1", quantity=4, cumulative=4, price=100))
    assert ledger.get("rec-1").status == OrderStatus.PARTIALLY_FILLED
    projector.apply(make_fill("e-2", quantity=6, cumulative=10, price=110))
    assert ledger.get("rec-1").status == OrderStatus.FILLED
    assert get_position(session, "momentum", "AAPL").avg_entry_price == pytest.approx(106)

def test_unknown_fill_is_audited_without_position_mutation(projector, session):
    fill = make_fill("e-unknown", recommendation_id="unknown")
    with pytest.raises(UnattributedFillError):
        projector.apply(fill)
    assert count_positions(session) == 0
    assert count_execution_fills(session) == 1
```

- [ ] **Step 2: Verify RED**

Run: `pytest tests/services/portfolio_accounting/ -v`  
Expected: FAIL because the accounting service does not exist.

- [ ] **Step 3: Implement transactional fill projection**

`FillProjector.apply` must start one transaction, insert the unique immutable fill, lock the intent and sleeve position, apply commission, update quantity/weighted price/cash, advance lifecycle, and commit. Catch only the execution unique-constraint collision to return `False`. Refactor `PaperTradingState.record_fill` into a private accounting helper used by the projector; `run_paper.py` must no longer call it.

The runner consumes `stream:fills` with group `portfolio_accounting`, drains pending messages at startup, acknowledges only after `apply` commits, and sends malformed messages to the fill DLQ.

- [ ] **Step 4: Verify GREEN**

Run: `pytest tests/services/portfolio_accounting/ tests/scripts/test_paper_state.py -v`  
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/portfolio_accounting scripts/paper_state.py tests/services/portfolio_accounting tests/scripts/test_paper_state.py
git commit -m "feat: project IB fills into sleeve positions"
```

### Task 7: Replace auto-correct reconciliation with fail-closed reports

**Files:**
- Modify: `services/execution/reconciliation.py`
- Create: `shared/broker_state.py`
- Create: `services/execution/ib_account.py`
- Create: `scripts/reconcile_paper.py`
- Test: `tests/services/execution/test_reconciliation.py`
- Test: `tests/services/execution/test_ib_account.py`
- Test: `tests/scripts/test_reconcile_paper.py`

**Interfaces:**
- Produces: `BrokerAccountSnapshot`, `BrokerPosition`, `BrokerOpenOrder`, `IBAccountReader.snapshot()`, `ReconciliationResult.entries_allowed`, `build_repair_plan`, and TTY-only `apply_repair_plan`.
- Consumes: IB contract-keyed snapshots, filled DB positions, and active intents.

- [ ] **Step 1: Write failing fail-closed and repair-safety tests**

```python
def test_any_quantity_mismatch_blocks_entries():
    result = PositionReconciler(quantity_tolerance=1e-6).reconcile(
        broker_positions={265598: 100.0}, db_positions={265598: 100.01},
        broker_orders={}, db_orders={},
    )
    assert result.entries_allowed is False
    assert result.discrepancies[0].auto_correct is False

def test_open_order_missing_at_ib_blocks_entries():
    result = reconcile(broker_orders={}, db_orders={"9": submitted_intent("9")})
    assert result.entries_allowed is False

def test_apply_refuses_non_tty(monkeypatch, tmp_path):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(RepairRefusedError, match="TTY"):
        apply_repair_plan(session, plan_path=tmp_path / "plan.json")

def test_ib_only_position_requires_explicit_sleeve_mapping():
    plan = build_repair_plan(discrepancy_ib_only(con_id=265598, quantity=10))
    assert plan.actions == []
    assert plan.unresolved[0].reason == "sleeve_mapping_required"

@pytest.mark.asyncio
async def test_account_reader_returns_contract_keyed_snapshot(fake_ib):
    fake_ib.managedAccounts.return_value = ["DUN551088"]
    fake_ib.accountSummary.return_value = [tag("NetLiquidation", "1000000")]
    fake_ib.positions.return_value = [position(con_id=265598, symbol="AAPL", qty=10)]
    snapshot = await IBAccountReader(fake_ib, expected_mode="paper").snapshot()
    assert snapshot.account_id == "DUN551088"
    assert snapshot.net_liquidation == 1_000_000
    assert snapshot.positions[265598].quantity == 10
```

- [ ] **Step 2: Verify RED**

Run: `pytest tests/services/execution/test_reconciliation.py tests/services/execution/test_ib_account.py tests/scripts/test_reconcile_paper.py -v`  
Expected: FAIL because reconciliation still marks small mismatches auto-correctable and no safe CLI exists.

- [ ] **Step 3: Implement contract-keyed reconciliation and report-only CLI**

Remove the 5% auto-correction rule. Compare positions by `(account_id, con_id)` and open orders by `(account_id, ib_order_id)` using an absolute quantity tolerance of `1e-6`. Compare every active intent's filled quantity with the sum of its immutable execution fills. Persist result JSON in `ReconciliationReport`. `IBAccountReader` obtains exactly one managed account, validates its `DU`/`U` prefix against mode, and returns NetLiquidation, contract-keyed positions, and open orders. The CLI defaults to report-only and writes plans under `output/reconciliation/` without altering rows.

The `--apply-plan` path must:

1. refuse non-TTY stdin;
2. refuse unresolved mappings;
3. call existing `dump_paper_state` before mutation;
4. require the exact confirmation string `APPLY PAPER REPAIR`;
5. verify the plan account starts with `DU`;
6. apply only the actions serialized in the reviewed plan;
7. commit once and print a post-apply reconciliation instruction.

Do not execute `--apply-plan` during implementation or verification.

- [ ] **Step 4: Run reconciliation safety tests**

Run: `pytest tests/services/execution/test_reconciliation.py tests/services/execution/test_ib_account.py tests/scripts/test_reconcile_paper.py tests/scripts/test_run_paper_reset.py -v`  
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/execution/reconciliation.py services/execution/ib_account.py shared/broker_state.py scripts/reconcile_paper.py tests/services/execution/test_reconciliation.py tests/services/execution/test_ib_account.py tests/scripts/test_reconcile_paper.py
git commit -m "feat: fail closed on broker ledger divergence"
```

### Task 8: Hydrate strategy state and remove pre-fill mutation

**Files:**
- Create: `backtest/portfolio_context.py`
- Modify: `scripts/run_backtest.py`
- Modify: `scripts/run_paper.py`
- Modify: `scripts/paper_state.py`
- Test: `tests/scripts/test_run_paper_gate.py`
- Test: `tests/backtest/test_momentum_signals.py`
- Test: `tests/backtest/test_quality_value_signals.py`
- Test: `tests/backtest/test_thematic_momentum_signals.py`
- Test: `tests/backtest/test_earnings_drift_signals.py`

**Interfaces:**
- Produces: immutable `PortfolioContext`, `HeldPosition`, `PendingOrder`, and context-aware active strategy factories.
- Consumes: filled positions, pending intents, capital budgets, current prices, and regime.

- [ ] **Step 1: Write restart and no-prefill regression tests**

```python
def test_held_position_is_hold_after_new_factory_instance():
    context = PortfolioContext(positions={
        "AAPL": HeldPosition(quantity=10, avg_entry_price=100,
                             peak_price=120, entry_date=date(2026, 1, 1))
    }, pending_orders={}, sleeve_budget=100_000, reserved_notional=0)
    fn = make_momentum_signals_fn(
        bars_by_ticker={"AAPL": bars_ending_at(115)}, lookback_days=20,
        initial_capital=100_000, portfolio_context=context,
    )
    assert fn("AAPL", bars_ending_at(115)) is None

def test_hydrated_trailing_stop_emits_full_quantity_exit():
    context = context_with_position("AAPL", quantity=10, entry=100, peak=120)
    signal = make_momentum_signals_fn(
        bars_by_ticker={"AAPL": bars_ending_at(107)}, lookback_days=20,
        initial_capital=100_000, trailing_stop_pct=0.10,
        portfolio_context=context,
    )("AAPL", bars_ending_at(107))
    assert signal["action"] == "sell"
    assert signal["quantity"] == 10

def test_run_daily_does_not_create_position_before_fill(state):
    signals = run_daily(state, build_portfolio(always_buy), {"AAPL": make_bars()})
    assert len(signals) == 1
    assert state.get_positions("test_sleeve") == {}
    assert state.get_cash("test_sleeve") == 10_000

def test_pending_buy_suppresses_duplicate_recommendation():
    context = context_with_pending_buy("AAPL")
    assert make_momentum_signals_fn(
        bars_by_ticker={"AAPL": bars()}, lookback_days=20,
        initial_capital=100_000, portfolio_context=context,
    )("AAPL", bars()) is None
```

- [ ] **Step 2: Verify RED**

Run: `pytest tests/scripts/test_run_paper_gate.py tests/backtest/test_momentum_signals.py tests/backtest/test_quality_value_signals.py tests/backtest/test_thematic_momentum_signals.py tests/backtest/test_earnings_drift_signals.py -v`  
Expected: FAIL because strategies use fresh mutable closure state and `run_daily` records fills.

- [ ] **Step 3: Implement context-aware evaluation**

```python
@dataclass(frozen=True)
class HeldPosition:
    quantity: float
    avg_entry_price: float
    peak_price: float
    entry_date: date

@dataclass(frozen=True)
class PortfolioContext:
    positions: dict[str, HeldPosition]
    pending_orders: dict[str, PendingOrder]
    sleeve_budget: float
    reserved_notional: float
```

Add an optional `portfolio_context` argument to each active strategy factory. When supplied, derive held/pending state exclusively from that context and do not mutate closure tracking. Preserve the existing internal simulation state when the argument is absent so backtests remain functional. In `run_daily`, remove both buy and sell calls to `state.record_fill`; update only safe market marks/snapshots. Build each sleeve context from PostgreSQL and Task 3 reservations before factory creation.

- [ ] **Step 4: Run active strategy and paper tests**

Run: `pytest tests/backtest/test_momentum_signals.py tests/backtest/test_sector_rotation_signals.py tests/backtest/test_quality_value_signals.py tests/backtest/test_earnings_drift_signals.py tests/backtest/test_thematic_momentum_signals.py tests/backtest/test_tail_risk_hedge_signals.py tests/scripts/test_run_paper_gate.py -v`  
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backtest/portfolio_context.py scripts/run_backtest.py scripts/run_paper.py scripts/paper_state.py tests/backtest tests/scripts/test_run_paper_gate.py
git commit -m "fix: hydrate paper strategies from filled positions"
```

### Task 9: Wire reconciliation, capital snapshots, intent outbox, and corrected sizing

**Files:**
- Modify: `scripts/run_paper.py`
- Modify: `services/risk_management/runner.py`
- Modify: `shared/observability.py`
- Test: `tests/scripts/test_run_paper_capital.py`
- Test: `tests/scripts/test_run_paper_gate.py`
- Test: `tests/services/risk_management/test_runner.py`

**Interfaces:**
- Produces: daily startup sequence from the approved design and deterministic unpublished-intent replay.
- Consumes: Tasks 1, 3, 4, 7, and 8.

- [ ] **Step 1: Write failing orchestration tests**

```python
def test_one_million_nav_builds_one_million_of_sleeve_budgets():
    result = prepare_daily_run(
        broker_snapshot=matching_snapshot(net_liquidation=1_000_000),
        config=paper_config(fraction=1.0, cap=None), session=session,
    )
    assert sum(result.capital.sleeve_budgets.values()) == pytest.approx(1_000_000)

def test_reconciliation_mismatch_filters_buys_but_keeps_sells():
    result = run_daily(
        state, portfolios, bars_by_ticker,
        reconciliation=failed_reconciliation(),
    )
    assert all(signal["action"] == "sell" for signal in result.signals)

def test_signal_creates_intent_without_position_mutation():
    run_and_publish_once(
        state=state, portfolios=portfolios, bars_by_ticker=bars_by_ticker,
        ledger=ledger, redis=redis,
    )
    assert ledger.get("sleeve-2026-07-18-momentum-AAPL-buy").status == OrderStatus.PROPOSED
    assert state.get_positions("momentum") == {}

def test_unpublished_intent_is_replayed_with_same_id():
    seed_unpublished_intent("rec-1")
    publish_unpublished_intents(session, redis)
    assert redis.xadd_payload["recommendation_id"] == "rec-1"
```

- [ ] **Step 2: Verify RED**

Run: `pytest tests/scripts/test_run_paper_capital.py tests/scripts/test_run_paper_gate.py tests/services/risk_management/test_runner.py -v`  
Expected: FAIL because the runner neither snapshots broker NAV nor persists lifecycle transitions.

- [ ] **Step 3: Implement orchestration and risk transitions**

Add `--entries-disabled` and keep it enabled during rollout verification. The runner must fetch broker state, reconcile, calculate Task 1 budgets, insert `CapitalSnapshot`, build hydrated contexts, calculate projected risk with reservations, create deterministic intents, and publish unpublished intents. Risk management receives a session-backed `OrderLedger`: every rejection records `RISK_REJECTED`; every approval records `APPROVED` before publishing `ApprovedOrderMessage`.

Change quality-value size from `0.10` to `0.06` and thematic momentum from `0.15` to `0.135` in both paper and backtest portfolio builders. Emit metrics for deployable capital, per-sleeve budget, reserved notional, lifecycle counts, and reconciliation status.

- [ ] **Step 4: Run orchestration suites**

Run: `pytest tests/scripts/ tests/services/risk_management/ tests/shared/test_observability.py -v`  
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/run_paper.py services/risk_management/runner.py shared/observability.py tests/scripts tests/services/risk_management tests/shared/test_observability.py
git commit -m "feat: orchestrate reconciled broker NAV paper runs"
```

### Task 10: Complete-universe ranking and replacement validation harness

**Files:**
- Create: `backtest/ranked_selection.py`
- Modify: `scripts/run_backtest.py`
- Modify: `scripts/run_paper.py`
- Create: `scripts/validate_replacement_policy.py`
- Test: `tests/backtest/test_ranked_selection.py`
- Test: `tests/backtest/test_quality_value_signals.py`
- Test: `tests/backtest/test_thematic_momentum_signals.py`

**Interfaces:**
- Produces: `rank_complete_universe`, `ReplacementPolicy`, and comparative validation JSON.
- Consumes: complete daily feature maps and current `PortfolioContext`.

- [ ] **Step 1: Write failing ranking and policy tests**

```python
def test_ranking_is_independent_of_input_order():
    scores = {"A": 1.0, "B": 3.0, "C": 2.0}
    assert rank_complete_universe(scores, top_n=2) == ["B", "C"]
    assert rank_complete_universe(dict(reversed(list(scores.items()))), top_n=2) == ["B", "C"]

def test_technical_only_policy_never_sells_on_rank_drop():
    actions = target_deltas(
        held={"A"}, selected={"B"}, scores={"A": 1, "B": 2},
        policy=ReplacementPolicy.TECHNICAL_ONLY,
    )
    assert actions == []

def test_margin_policy_replaces_only_above_threshold():
    assert target_deltas(
        held={"A"}, selected={"B"}, scores={"A": 1.0, "B": 1.2},
        policy=ReplacementPolicy.SCORE_MARGIN, score_margin=0.25,
    ) == []
```

- [ ] **Step 2: Verify RED**

Run: `pytest tests/backtest/test_ranked_selection.py tests/backtest/test_quality_value_signals.py tests/backtest/test_thematic_momentum_signals.py -v`  
Expected: FAIL because quality ranking is populated incrementally and replacement policies do not exist.

- [ ] **Step 3: Implement deterministic ranking and offline policy comparison**

```python
class ReplacementPolicy(StrEnum):
    TECHNICAL_ONLY = "technical_only"
    WEAKEST = "weakest"
    SCORE_MARGIN = "score_margin"

def rank_complete_universe(scores: dict[str, float], top_n: int) -> list[str]:
    return [ticker for ticker, _ in sorted(
        scores.items(), key=lambda item: (-item[1], item[0])
    )[:top_n]]
```

Precompute the complete quality score map by date, as thematic ranking already does. The validation script runs all three policies on identical bars and writes return, Sharpe, max drawdown, trade count, annual turnover, and walk-forward metrics to JSON. Scheduled paper config remains `technical_only` unless the score-margin or weakest variant improves walk-forward Sharpe without increasing max drawdown and stays below `2.0x` annual turnover.

- [ ] **Step 4: Run ranking and backtest unit tests**

Run: `pytest tests/backtest/test_ranked_selection.py tests/backtest/test_quality_value_signals.py tests/backtest/test_thematic_momentum_signals.py -v`  
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backtest/ranked_selection.py scripts/run_backtest.py scripts/run_paper.py scripts/validate_replacement_policy.py tests/backtest
git commit -m "feat: validate ranked candidate replacement policies"
```

### Task 11: Wire services, operations, and final verification

**Files:**
- Modify: `docker-compose.yml`
- Modify: `shared/models/__init__.py`
- Modify: `docs/strategies/portfolio-2026-05.md`
- Create: `docs/operations/durable-paper-ledger.md`
- Modify: `docs/operations/divergence-monitor.md`
- Test: `tests/contracts/test_durable_paper_pipeline.py`

**Interfaces:**
- Produces: runnable accounting service, operational runbook, and end-to-end contract coverage.
- Consumes: all prior tasks.

- [ ] **Step 1: Write failing end-to-end contract test**

```python
def test_recommendation_to_fill_is_durable_and_idempotent(system):
    system.publish_recommendation(make_sleeve_buy("rec-1"))
    system.risk_once()
    system.execution_submit_once(order_id="9")
    assert system.positions() == []
    system.publish_fill(make_fill("e-1", recommendation_id="rec-1"))
    system.accounting_once()
    system.accounting_once()  # replay
    assert system.position_quantity("momentum", "AAPL") == 10
    assert system.intent_status("rec-1") == "FILLED"

def test_mismatch_blocks_entry_not_exit(system):
    system.seed_broker_db_mismatch()
    assert system.allowed_actions([buy_signal(), sell_signal()]) == [sell_signal()]
```

- [ ] **Step 2: Verify RED**

Run: `pytest tests/contracts/test_durable_paper_pipeline.py -v`  
Expected: FAIL until the accounting service and orchestration are wired together.

- [ ] **Step 3: Wire compose and document exact operator sequence**

Add `portfolio-accounting` to Compose, dependent on migrations, PostgreSQL, Redis, and execution. Its environment uses the existing database and Redis URLs. Document these commands without executing destructive repair:

```bash
alembic upgrade head
python scripts/reconcile_paper.py --report
python scripts/run_paper.py --entries-disabled
python scripts/reconcile_paper.py --apply-plan output/reconciliation/repair-plan-20260718T000000Z.json
```

The runbook must state that the timestamped filename is an example, and that the
operator—not an agent—selects the actual generated plan and executes the apply
command interactively. Document rollback as application rollback only; do not
recommend downgrading the additive migration while ledger rows exist.

- [ ] **Step 4: Run fresh verification**

Run: `pytest tests/contracts/test_durable_paper_pipeline.py -v`  
Expected: PASS.  
Run: `pytest`  
Expected: all tests pass with zero failures.  
Run: `alembic heads`  
Expected: exactly one head, `8b6f2c1d4a90`.  
Run: `/opt/homebrew/bin/uv build`  
Expected: wheel and source distribution build successfully.

- [ ] **Step 5: Generate read-only rollout evidence**

Run: `python scripts/reconcile_paper.py --report`  
Expected: a mismatch report for the current ledger and no database mutations.  
Run: `python scripts/run_paper.py --status`  
Expected: current persisted state prints successfully.  
Do not run `--apply-plan`, `--reset`, or a published paper cycle during implementation verification.

- [ ] **Step 6: Commit**

```bash
git add docker-compose.yml shared/models/__init__.py docs/strategies/portfolio-2026-05.md docs/operations tests/contracts/test_durable_paper_pipeline.py
git commit -m "docs: operationalize durable paper ledger rollout"
```

## Implementation completion gate

Before presenting the work as complete:

- Confirm every production behavior was introduced by a test that first failed for the expected reason.
- Confirm `git diff --check` is clean.
- Confirm the full test suite and package build pass from the feature worktree.
- Confirm the report-only reconciliation command performed no mutations.
- Confirm live deployment remains disabled in default configuration.
- Report that the current paper database still requires the operator-reviewed interactive repair before scheduled entries can resume.
