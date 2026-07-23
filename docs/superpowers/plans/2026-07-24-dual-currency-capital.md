# Dual-Currency Capital Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the SGD-base IB account safe to use for USD equity sizing, execution, accounting, and SGD performance reporting without permitting negative USD cash.

**Architecture:** Read one coherent broker snapshot containing SGD NAV, SGD-per-USD FX, and settled USD cash. Convert only the NAV-derived trading budget to USD, keep all sleeve/order economics in USD, retain consolidated performance in SGD, and fail closed when currency or funding data is invalid.

**Tech Stack:** Python 3.12+, ib_insync, Pydantic 2, SQLAlchemy 2, Alembic, PostgreSQL 16, Redis Streams, Prometheus, pytest.

## Global Constraints

- Base/reporting currency is exactly `SGD`; trading currency is exactly `USD`.
- FX direction is always `fx_base_per_trading`, meaning SGD per 1 USD.
- New buys must never create negative settled USD cash or use margin/SGD collateral as a substitute.
- Attributed exits and kill liquidation remain available when entries are blocked.
- SGD-to-USD conversion order placement and automatic FX hedging are out of scope.
- Schema changes are additive; do not reset paper state or infer legacy position ownership.
- Scheduled paper and all live entries remain disabled throughout implementation and rollout.
- Use TDD for every behavior change and commit after every task.

---

## File map

**Currency and broker boundary**

- Modify `shared/config.py`: currency policy and live-entry safety validation.
- Modify `config/default.yaml`: explicit SGD/USD defaults.
- Modify `shared/broker_state.py`: currency-explicit immutable broker snapshot.
- Modify `services/execution/ib_account.py`: async SGD NAV, USD FX, and USD settled-cash extraction.
- Modify `tests/shared/test_config.py` and `tests/services/execution/test_ib_account.py`.

**Capital and persistence**

- Modify `shared/capital.py`: base-to-trading conversion and FX-age validation.
- Modify `shared/models/order_ledger.py`, `shared/models/portfolio_config.py`, and `shared/models/equity_snapshot.py`: explicit currency fields.
- Create `shared/models/currency.py`: immutable recorded SGD-to-USD funding event.
- Modify `shared/models/__init__.py`.
- Create `migrations/versions/f6c2d9a84b31_add_dual_currency_accounting.py`.
- Create `tests/migrations/test_dual_currency_migration.py`.
- Modify `scripts/run_paper.py` and `tests/scripts/test_run_paper_capital.py`.

**Funding and execution safety**

- Create `services/risk_management/funding.py`: pure settled-USD cash gate and conservative commission estimate.
- Create `tests/services/risk_management/test_funding.py`.
- Modify `shared/order_ledger.py`: account-wide active buy reservations.
- Modify `services/risk_management/runner.py` and `scripts/run_paper.py`: invoke the gate before buy approval.
- Modify `tests/services/risk_management/test_runner.py` and `tests/scripts/test_run_paper_gate.py`.

**Fill economics and reporting**

- Modify `shared/schemas/messages.py`, `services/execution/ib_executor.py`, `services/execution/runner.py`, and `services/portfolio_accounting/projector.py`: preserve and translate commission currency.
- Modify `tests/services/execution/test_ib_executor.py`, `tests/services/execution/test_runner.py`, and `tests/services/portfolio_accounting/test_projector.py`.
- Create `shared/fx_performance.py` and `tests/shared/test_fx_performance.py`: exact USD-to-SGD attribution.
- Modify `scripts/paper_state.py` and `scripts/run_paper.py`: persist and display both return views.
- Modify `shared/observability.py` and `tests/shared/test_observability.py`.

---

### Task 0: Preserve the existing async IB hotfix

**Files:**

- Modify: `services/execution/ib_account.py:50`
- Modify: `tests/services/execution/test_ib_account.py:11-82`

**Interfaces:**

- Consumes: ib_insync `IB.accountSummaryAsync() -> Awaitable[list[AccountValue]]`
- Produces: `IBAccountReader.snapshot()` that never invokes blocking `accountSummary()` inside an event loop.

- [ ] **Step 1: Confirm the existing regression assertions**

The test double must forbid the synchronous call and expose the async call:

```python
ib.accountSummary.side_effect = AssertionError(
    "sync accountSummary is forbidden inside the async reader"
)
ib.accountSummaryAsync = AsyncMock(return_value=[nav_row])
```

- [ ] **Step 2: Run the focused regression**

Run:

```bash
.venv/bin/pytest tests/services/execution/test_ib_account.py -q
```

Expected: `5 passed`.

- [ ] **Step 3: Confirm the production call is asynchronous**

The reader must contain:

```python
summary = list(await _resolve(self._ib.accountSummaryAsync()))
```

- [ ] **Step 4: Commit only the hotfix files**

```bash
git add services/execution/ib_account.py tests/services/execution/test_ib_account.py
git diff --cached --check
git commit -m "fix: use async IB account summary API"
```

---

### Task 1: Add explicit currency configuration and snapshot types

**Files:**

- Modify: `shared/config.py:109-144`
- Modify: `config/default.yaml:1-12`
- Modify: `shared/broker_state.py:34-43`
- Modify: `tests/shared/test_config.py`
- Modify: `tests/scripts/test_run_paper_capital.py`
- Modify: `tests/scripts/test_run_paper_gate.py`

**Interfaces:**

- Produces: `CurrencyConfig`, `AppConfig.currency`, and the currency-explicit `BrokerAccountSnapshot` fields consumed by Tasks 2-9.

- [ ] **Step 1: Write failing configuration tests**

Add to `tests/shared/test_config.py`:

```python
def test_currency_defaults_are_sgd_base_and_usd_trading():
    cfg = AppConfig()
    assert cfg.currency.expected_base_currency == "SGD"
    assert cfg.currency.trading_currency == "USD"
    assert cfg.currency.max_fx_age_seconds == 300
    assert cfg.currency.minimum_settled_usd_reserve == 0.0


def test_live_entries_require_positive_usd_reserve():
    with pytest.raises(ValidationError, match="settled USD reserve"):
        AppConfig(
            mode="live",
            capital=CapitalConfig(
                live=CapitalModeConfig(
                    deployment_fraction=0.1,
                    max_deployable_usd=10_000,
                    entries_enabled=True,
                )
            ),
        )
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
.venv/bin/pytest tests/shared/test_config.py -q
```

Expected: FAIL because `AppConfig.currency` does not exist.

- [ ] **Step 3: Implement `CurrencyConfig` and live validation**

Add to `shared/config.py` and include `model_validator` in the Pydantic imports:

```python
class CurrencyConfig(BaseModel):
    expected_base_currency: Literal["SGD"] = "SGD"
    trading_currency: Literal["USD"] = "USD"
    max_fx_age_seconds: int = Field(default=300, gt=0)
    minimum_settled_usd_reserve: float = Field(default=0.0, ge=0.0)
    commission_per_share_usd: float = Field(default=0.005, ge=0.0)
    minimum_commission_usd: float = Field(default=1.0, ge=0.0)
```

Add the field and validator to `AppConfig`:

```python
currency: CurrencyConfig = Field(default_factory=CurrencyConfig)

@model_validator(mode="after")
def validate_live_currency_safety(self) -> AppConfig:
    if (
        self.capital.live.entries_enabled
        and self.currency.minimum_settled_usd_reserve <= 0
    ):
        raise ValueError("live entries require a positive settled USD reserve")
    return self
```

Add to `config/default.yaml`:

```yaml
currency:
  expected_base_currency: SGD
  trading_currency: USD
  max_fx_age_seconds: 300
  minimum_settled_usd_reserve: 0.0
  commission_per_share_usd: 0.005
  minimum_commission_usd: 1.0
```

- [ ] **Step 4: Make `BrokerAccountSnapshot` currency explicit**

Replace the ambiguous NAV field with:

```python
@dataclass(frozen=True)
class BrokerAccountSnapshot:
    account_id: str
    mode: str
    base_currency: str
    trading_currency: str
    net_liquidation_base: float
    fx_base_per_trading: float
    net_liquidation_trading_equivalent: float
    settled_cash_trading: float
    fx_source: str
    fx_captured_at: datetime
    positions: dict[int, BrokerPosition] = field(default_factory=dict)
    open_orders: dict[str, BrokerOpenOrder] = field(default_factory=dict)
    captured_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
```

Update test fixtures to provide `SGD`, `USD`, an FX rate, USD-equivalent NAV,
settled USD cash, and an aware FX timestamp. Do not add a compatibility
property named `net_liquidation`; compiler/test failures must expose every
ambiguous caller.

- [ ] **Step 5: Run configuration and broker-state consumers**

Run:

```bash
.venv/bin/pytest tests/shared/test_config.py tests/shared/test_capital.py tests/scripts/test_run_paper_capital.py tests/scripts/test_run_paper_gate.py -q
```

Expected: PASS after all constructors use explicit fields.

- [ ] **Step 6: Commit**

```bash
git add shared/config.py config/default.yaml shared/broker_state.py tests
git diff --cached --check
git commit -m "feat: define SGD base and USD trading currencies"
```

---

### Task 2: Read a coherent SGD NAV, USD FX, and USD cash snapshot

**Files:**

- Modify: `services/execution/ib_account.py`
- Modify: `scripts/run_paper.py:780-800,980-993`
- Modify: `tests/services/execution/test_ib_account.py`

**Interfaces:**

- Consumes: `IB.accountSummaryAsync()` rows including `NetLiquidation` and `$LEDGER:ALL` values.
- Produces: `IBAccountReader(ib, expected_mode, expected_base_currency, trading_currency).snapshot() -> BrokerAccountSnapshot`.

- [ ] **Step 1: Add the observed SGD regression fixture**

Set the fake summary to:

```python
ib.accountSummaryAsync = AsyncMock(return_value=[
    SimpleNamespace(
        account="DUN551088",
        tag="NetLiquidation",
        value="1001757.23",
        currency="SGD",
    ),
    SimpleNamespace(
        account="All",
        tag="ExchangeRate",
        value="1.2928304",
        currency="USD",
    ),
    SimpleNamespace(
        account="All",
        tag="SettledCash",
        value="25000.00",
        currency="USD",
    ),
])
```

Assert:

```python
assert snapshot.base_currency == "SGD"
assert snapshot.trading_currency == "USD"
assert snapshot.net_liquidation_base == pytest.approx(1_001_757.23)
assert snapshot.fx_base_per_trading == pytest.approx(1.2928304)
assert snapshot.net_liquidation_trading_equivalent == pytest.approx(774_855.87)
assert snapshot.settled_cash_trading == pytest.approx(25_000)
```

Add parameterized failures for duplicate/missing NAV, FX, and settled USD cash;
wrong NAV currency; non-finite values; non-positive NAV/FX; and multiple
managed accounts.

- [ ] **Step 2: Verify the real regression is RED**

Run:

```bash
.venv/bin/pytest tests/services/execution/test_ib_account.py -q
```

Expected: FAIL because SGD NAV is filtered out and explicit fields are absent.

- [ ] **Step 3: Implement exact-row selection and conversion**

Add these helpers:

```python
def _matching_rows(
    rows: list[Any],
    *,
    tag: str,
    currency: str,
    account_id: str,
    allow_all: bool = False,
) -> list[Any]:
    accepted_accounts = {"", account_id}
    if allow_all:
        accepted_accounts.add("All")
    return [
        row for row in rows
        if str(getattr(row, "tag", "")) == tag
        and str(getattr(row, "currency", "")) == currency
        and str(getattr(row, "account", account_id)) in accepted_accounts
    ]


def _one_float(rows: list[Any], *, label: str) -> float:
    if len(rows) != 1:
        raise AccountValidationError(f"expected exactly one {label} value")
    try:
        value = float(rows[0].value)
    except (TypeError, ValueError) as exc:
        raise AccountValidationError(f"invalid {label} value") from exc
    if not math.isfinite(value):
        raise AccountValidationError(f"invalid {label} value")
    return value
```

Construct the values inside `snapshot()`:

```python
captured_at = datetime.now(timezone.utc)
nav_base = _one_float(
    _matching_rows(
        summary,
        tag="NetLiquidation",
        currency=self._expected_base_currency,
        account_id=account_id,
    ),
    label=f"{self._expected_base_currency} NetLiquidation",
)
fx = _one_float(
    _matching_rows(
        summary,
        tag="ExchangeRate",
        currency=self._trading_currency,
        account_id=account_id,
        allow_all=True,
    ),
    label=f"{self._trading_currency} ExchangeRate",
)
settled_cash = _one_float(
    _matching_rows(
        summary,
        tag="SettledCash",
        currency=self._trading_currency,
        account_id=account_id,
        allow_all=True,
    ),
    label=f"{self._trading_currency} SettledCash",
)
if nav_base <= 0 or fx <= 0:
    raise AccountValidationError("NAV and FX rate must be positive")
```

Return the explicit snapshot with
`net_liquidation_trading_equivalent=nav_base / fx`,
`fx_source="$LEDGER:ALL/ExchangeRate"`, and
`fx_captured_at=captured_at`.

- [ ] **Step 4: Pass currency config from the daily runner**

Change `read_broker_snapshot` to accept `expected_base_currency` and
`trading_currency`, then construct:

```python
return await IBAccountReader(
    ib,
    expected_mode=mode,
    expected_base_currency=expected_base_currency,
    trading_currency=trading_currency,
).snapshot()
```

Pass `_config.currency.expected_base_currency` and
`_config.currency.trading_currency` from `main()`.

- [ ] **Step 5: Verify focused and integration tests**

Run:

```bash
.venv/bin/pytest tests/services/execution/test_ib_account.py tests/scripts/test_run_paper_capital.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add services/execution/ib_account.py scripts/run_paper.py tests/services/execution/test_ib_account.py tests/scripts/test_run_paper_capital.py
git diff --cached --check
git commit -m "feat: read SGD NAV and USD account funding"
```

---

### Task 3: Convert capital and persist explicit currency fields

**Files:**

- Modify: `shared/capital.py`
- Modify: `shared/models/order_ledger.py`
- Modify: `shared/models/portfolio_config.py`
- Modify: `shared/models/equity_snapshot.py`
- Create: `shared/models/currency.py`
- Modify: `shared/models/__init__.py`
- Create: `migrations/versions/f6c2d9a84b31_add_dual_currency_accounting.py`
- Create: `tests/migrations/test_dual_currency_migration.py`
- Modify: `tests/shared/test_capital.py`

**Interfaces:**

- Produces: `calculate_capital_budget(snapshot, mode, capital_config, currency_config, sleeve_weights, now=None) -> CapitalBudget`.
- Produces: additive ORM fields consumed by the paper runner, risk service, fill projector, and reporting tasks.

- [ ] **Step 1: Write failing capital-conversion tests**

```python
def test_sgd_nav_builds_usd_sleeve_budgets():
    snapshot = make_snapshot(
        net_liquidation_base=1_001_757.23,
        fx_base_per_trading=1.2928304,
        net_liquidation_trading_equivalent=774_855.87,
        settled_cash_trading=25_000,
    )
    budget = calculate_capital_budget(
        snapshot,
        "paper",
        CapitalConfig(),
        CurrencyConfig(),
        {"momentum": 0.6, "hedge": 0.4},
        now=snapshot.captured_at,
    )
    assert budget.deployable_capital == pytest.approx(774_855.87)
    assert budget.sleeve_budgets["momentum"] == pytest.approx(464_913.522)
    assert budget.net_liquidation_base == pytest.approx(1_001_757.23)


def test_stale_fx_blocks_capital_calculation():
    snapshot = make_snapshot(
        fx_captured_at=datetime.now(timezone.utc) - timedelta(seconds=301)
    )
    with pytest.raises(ValueError, match="FX quote is stale"):
        calculate_capital_budget(
            snapshot,
            "paper",
            CapitalConfig(),
            CurrencyConfig(max_fx_age_seconds=300),
            {"x": 1.0},
        )
```

- [ ] **Step 2: Verify RED**

Run:

```bash
.venv/bin/pytest tests/shared/test_capital.py -q
```

Expected: FAIL on the old scalar-NAV interface.

- [ ] **Step 3: Implement `CapitalBudget` and conversion**

Use this public shape:

```python
@dataclass(frozen=True)
class CapitalBudget:
    base_currency: str
    trading_currency: str
    net_liquidation_base: float
    net_liquidation_trading_equivalent: float
    fx_base_per_trading: float
    fx_captured_at: datetime
    fractional_base: float
    deployment_fraction: float
    max_deployable_usd: float | None
    settled_cash_trading: float
    deployable_capital: float
    sleeve_budgets: dict[str, float]
```

The function computes:

```python
fractional_base = snapshot.net_liquidation_base * selected.deployment_fraction
fractional_trading = fractional_base / snapshot.fx_base_per_trading
deployable = (
    min(fractional_trading, selected.max_deployable_usd)
    if selected.max_deployable_usd is not None
    else fractional_trading
)
```

Reject a negative FX age, an age over `max_fx_age_seconds`, non-matching
configured currencies, non-positive NAV/FX, and sleeve weights that do not sum
to one.

- [ ] **Step 4: Add ORM fields and the funding event model**

Add nullable compatibility columns to `CapitalSnapshot`:

```python
base_currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
trading_currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
net_liquidation_base: Mapped[float | None] = mapped_column(Float, nullable=True)
net_liquidation_trading_equivalent: Mapped[float | None] = mapped_column(Float, nullable=True)
fx_base_per_trading: Mapped[float | None] = mapped_column(Float, nullable=True)
fx_captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
fractional_base: Mapped[float | None] = mapped_column(Float, nullable=True)
settled_cash_trading: Mapped[float | None] = mapped_column(Float, nullable=True)
```

Add `currency="USD"` to `PortfolioConfig`, add USD/SGD valuation fields to
`EquitySnapshot`, and add `commission_currency` plus `commission_trading` to
`ExecutionFill`.

Create `CurrencyConversion` in `shared/models/currency.py` with account ID,
source/target currency and amounts, executed rate, fee amount/currency,
operator/source, and timezone-aware execution timestamp. This task creates the
audit interface only; no code places FX orders.

Use this exact model interface:

```python
class CurrencyConversion(Base):
    __tablename__ = "currency_conversions"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[str] = mapped_column(String(50), nullable=False)
    source_currency: Mapped[str] = mapped_column(String(8), nullable=False)
    source_amount: Mapped[float] = mapped_column(Float, nullable=False)
    target_currency: Mapped[str] = mapped_column(String(8), nullable=False)
    target_amount: Mapped[float] = mapped_column(Float, nullable=False)
    fx_base_per_trading: Mapped[float] = mapped_column(Float, nullable=False)
    fee_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    fee_currency: Mapped[str] = mapped_column(String(8), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    operator: Mapped[str | None] = mapped_column(String(100), nullable=True)
    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
```

- [ ] **Step 5: Write the additive migration and migration test**

Revision `f6c2d9a84b31` must revise `d8f10a4b72c3`. It adds the fields above,
sets existing `portfolio_config.currency` rows to `USD`, creates the
`currency_conversions` table, and performs no deletes or ownership updates.

The migration test upgrades an initial-head SQLite/PostgreSQL-compatible test
schema through the new revision, verifies all tables/columns, and asserts an
existing portfolio row remains unchanged except for `currency="USD"`.

- [ ] **Step 6: Run model and migration tests**

Run:

```bash
.venv/bin/pytest tests/shared/test_capital.py tests/migrations/test_dual_currency_migration.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add shared/capital.py shared/models migrations/versions/f6c2d9a84b31_add_dual_currency_accounting.py tests/shared/test_capital.py tests/migrations/test_dual_currency_migration.py
git diff --cached --check
git commit -m "feat: persist dual-currency capital state"
```

---

### Task 4: Wire dual-currency capital into the daily preparation

**Files:**

- Modify: `scripts/run_paper.py:287-340,1000-1055`
- Modify: `tests/scripts/test_run_paper_capital.py`

**Interfaces:**

- Consumes: Task 3 `CapitalBudget`.
- Produces: currency-complete `CapitalSnapshot` rows and USD sleeve budgets.

- [ ] **Step 1: Write a failing persistence test**

Use the SGD fixture and assert:

```python
stored = session.scalar(select(CapitalSnapshot))
assert stored.base_currency == "SGD"
assert stored.trading_currency == "USD"
assert stored.net_liquidation_base == pytest.approx(1_001_757.23)
assert stored.net_liquidation_trading_equivalent == pytest.approx(774_855.87)
assert stored.fx_base_per_trading == pytest.approx(1.2928304)
assert stored.settled_cash_trading == pytest.approx(25_000)
assert stored.deployable_capital == pytest.approx(774_855.87)
```

- [ ] **Step 2: Verify RED**

Run:

```bash
.venv/bin/pytest tests/scripts/test_run_paper_capital.py -q
```

Expected: FAIL because `prepare_daily_run` still persists the ambiguous scalar.

- [ ] **Step 3: Persist every explicit field**

Call the new capital interface and construct `CapitalSnapshot` with all Task 3
fields. Keep legacy `net_liquidation` equal to the USD-equivalent NAV and
legacy `deployable_capital` in USD so existing risk consumers remain
dimensionally correct during migration.

Replace the generic dollar output with:

```python
print(
    f"Broker NAV: SGD {capital.net_liquidation_base:,.2f} "
    f"(USD {capital.net_liquidation_trading_equivalent:,.2f}); "
    f"FX: {capital.fx_base_per_trading:.7f} SGD/USD; "
    f"settled USD: {capital.settled_cash_trading:,.2f}; "
    f"deployable USD: {capital.deployable_capital:,.2f}; "
    f"reconciliation: {preparation.reconciliation.severity}; "
    f"entries: {'disabled' if entries_disabled else 'enabled'}"
)
```

- [ ] **Step 4: Verify daily preparation and reconciliation**

Run:

```bash
.venv/bin/pytest tests/scripts/test_run_paper_capital.py tests/scripts/test_reconcile_paper.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/run_paper.py tests/scripts/test_run_paper_capital.py
git diff --cached --check
git commit -m "feat: prepare paper runs from USD-equivalent capital"
```

---

### Task 5: Enforce account-wide settled USD funding

**Files:**

- Create: `services/risk_management/funding.py`
- Create: `tests/services/risk_management/test_funding.py`
- Modify: `shared/order_ledger.py`
- Modify: `services/risk_management/runner.py`
- Modify: `scripts/run_paper.py`
- Modify: `tests/services/risk_management/test_runner.py`
- Modify: `tests/scripts/test_run_paper_gate.py`

**Interfaces:**

- Produces: `estimate_commission_usd(quantity, per_share, minimum) -> float`.
- Produces: `check_settled_usd_funding(...) -> FundingDecision`.
- Produces: `OrderLedger.active_buy_reservations_for_account(account_id, exclude_recommendation_id=None) -> float`.

- [ ] **Step 1: Write failing pure funding tests**

```python
def test_buy_is_rejected_when_reservations_and_buffer_exceed_cash():
    decision = check_settled_usd_funding(
        order_notional_usd=900,
        settled_cash_usd=1_000,
        active_reservations_usd=50,
        estimated_commission_usd=1,
        minimum_reserve_usd=100,
    )
    assert decision.approved is False
    assert decision.required_usd == pytest.approx(1_051)
    assert "settled USD cash" in decision.reason


def test_cash_gate_ignores_margin_and_scales_nothing():
    decision = check_settled_usd_funding(
        order_notional_usd=800,
        settled_cash_usd=1_000,
        active_reservations_usd=0,
        estimated_commission_usd=1,
        minimum_reserve_usd=100,
    )
    assert decision.approved is True
```

- [ ] **Step 2: Verify RED**

Run:

```bash
.venv/bin/pytest tests/services/risk_management/test_funding.py -q
```

Expected: import failure because the module does not exist.

- [ ] **Step 3: Implement the pure gate**

```python
@dataclass(frozen=True)
class FundingDecision:
    approved: bool
    required_usd: float
    remaining_usd: float
    reason: str


def estimate_commission_usd(
    quantity: float, *, per_share: float, minimum: float
) -> float:
    return max(float(minimum), abs(float(quantity)) * float(per_share))


def check_settled_usd_funding(
    *,
    order_notional_usd: float,
    settled_cash_usd: float,
    active_reservations_usd: float,
    estimated_commission_usd: float,
    minimum_reserve_usd: float,
) -> FundingDecision:
    required = (
        max(0.0, order_notional_usd)
        + max(0.0, active_reservations_usd)
        + max(0.0, estimated_commission_usd)
        + max(0.0, minimum_reserve_usd)
    )
    remaining = settled_cash_usd - required
    approved = remaining >= 0
    return FundingDecision(
        approved=approved,
        required_usd=required,
        remaining_usd=remaining,
        reason=("settled USD cash available" if approved else "insufficient settled USD cash"),
    )
```

- [ ] **Step 4: Add account-wide reservation aggregation**

Sum remaining quantities times intent limit prices for active BUY intents in
the same account. Include `APPROVED`, `SUBMITTED`, and `PARTIALLY_FILLED`, plus
published `PROPOSED` rows. Exclude the current recommendation when the risk
service revalidates it.

Add tests proving reservations from every sleeve consume the same account USD
cash and reservations from another account do not.

Implement the query as:

```python
def active_buy_reservations_for_account(
    self,
    account_id: str,
    *,
    exclude_recommendation_id: str | None = None,
) -> float:
    remaining = OrderIntent.requested_quantity - OrderIntent.filled_quantity
    statement = select(
        func.coalesce(func.sum(remaining * OrderIntent.limit_price), 0.0)
    ).where(
        OrderIntent.account_id == account_id,
        func.upper(OrderIntent.action) == "BUY",
        OrderIntent.limit_price.is_not(None),
        or_(
            OrderIntent.status.in_((
                OrderStatus.APPROVED.value,
                OrderStatus.SUBMITTED.value,
                OrderStatus.PARTIALLY_FILLED.value,
            )),
            and_(
                OrderIntent.status == OrderStatus.PROPOSED.value,
                OrderIntent.published_at.is_not(None),
            ),
        ),
    )
    if exclude_recommendation_id is not None:
        statement = statement.where(
            OrderIntent.recommendation_id != exclude_recommendation_id
        )
    return float(self.session.scalar(statement) or 0.0)
```

- [ ] **Step 5: Gate local paper signals and risk-service buys**

Before `RiskEngine.check_entry`, calculate order notional and commission, load
account-wide reservations and the latest matching capital snapshot, and call
`check_settled_usd_funding`. Persist a risk rejection and publish the existing
entry-rejection alert when funding fails.

The local `run_daily` path receives `settled_cash_trading`, account-wide
reservations, commission settings, and the minimum reserve. Sells bypass this
gate.

- [ ] **Step 6: Verify service and local gates**

Run:

```bash
.venv/bin/pytest tests/services/risk_management/test_funding.py tests/services/risk_management/test_runner.py tests/scripts/test_run_paper_gate.py -q
```

Expected: PASS, including a test where available margin is large but settled
USD cash is insufficient.

- [ ] **Step 7: Commit**

```bash
git add services/risk_management/funding.py shared/order_ledger.py services/risk_management/runner.py scripts/run_paper.py tests/services/risk_management tests/scripts/test_run_paper_gate.py
git diff --cached --check
git commit -m "feat: block buys without settled USD funding"
```

---

### Task 6: Preserve commission currency and USD trading cost

**Files:**

- Modify: `shared/schemas/messages.py:110-129`
- Modify: `services/execution/ib_executor.py:232-249`
- Modify: `services/execution/runner.py:330-366`
- Modify: `services/portfolio_accounting/projector.py`
- Modify: `tests/services/execution/test_ib_executor.py`
- Modify: `tests/services/execution/test_runner.py`
- Modify: `tests/services/portfolio_accounting/test_projector.py`

**Interfaces:**

- Produces: fill fields `commission_currency`, `commission_trading`, and `commission_fx_base_per_trading`.
- Consumes: cached IB `$LEDGER:ALL/ExchangeRate` rows from the connected executor.

- [ ] **Step 1: Write failing fill-message and projector tests**

For a USD commission:

```python
fill = make_fill(
    commission=1.25,
    commission_currency="USD",
    commission_trading=1.25,
)
assert projector.apply(fill) is True
stored = session.scalar(select(ExecutionFill))
assert stored.commission_currency == "USD"
assert stored.commission_trading == pytest.approx(1.25)
```

For an SGD commission at `1.25 SGD/USD`:

```python
fill = make_fill(
    commission=1.25,
    commission_currency="SGD",
    commission_trading=1.0,
    commission_fx_base_per_trading=1.25,
)
assert projector.apply(fill) is True
assert get_cash(session) == pytest.approx(8_999.0)
```

Reject an SGD commission missing its conversion and any unsupported currency.

- [ ] **Step 2: Verify RED**

Run:

```bash
.venv/bin/pytest tests/services/execution/test_ib_executor.py tests/services/execution/test_runner.py tests/services/portfolio_accounting/test_projector.py -q
```

Expected: FAIL because commission currency fields do not exist.

- [ ] **Step 3: Enrich the executor callback**

Read `fill.commissionReport.currency`. USD maps one-to-one. SGD divides by the
current positive USD `ExchangeRate` row from `self._ib.accountValues()`. The
payload includes the original amount/currency, translated USD commission, and
the rate used. Unsupported or missing conversion data leaves
`commission_trading=None`, which the projector rejects and audits.

Use one pure conversion helper:

```python
def _commission_in_usd(
    amount: float,
    currency: str,
    *,
    fx_base_per_trading: float | None,
) -> float | None:
    if currency == "USD":
        return amount
    if currency == "SGD" and fx_base_per_trading is not None:
        if math.isfinite(fx_base_per_trading) and fx_base_per_trading > 0:
            return amount / fx_base_per_trading
    return None
```

The executor selects exactly one account-value row whose tag is
`ExchangeRate` and currency is `USD`; duplicate or invalid rows yield no
conversion rather than a guessed rate.

- [ ] **Step 4: Persist original and translated commission values**

Extend `FillMessage`, the runner mapping, projector immutable comparison, and
`ExecutionFill`. Pass only `commission_trading` into USD virtual sleeve cash
and P&L calculations. Preserve the original commission fields for audit.

- [ ] **Step 5: Verify fill idempotency and economics**

Run:

```bash
.venv/bin/pytest tests/services/execution/test_ib_executor.py tests/services/execution/test_runner.py tests/services/portfolio_accounting/test_projector.py tests/scripts/test_paper_state.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add shared/schemas/messages.py services/execution services/portfolio_accounting shared/models/order_ledger.py tests/services tests/scripts/test_paper_state.py
git diff --cached --check
git commit -m "feat: preserve fill commission currencies"
```

---

### Task 7: Add USD strategy and SGD investor attribution

**Files:**

- Create: `shared/fx_performance.py`
- Create: `tests/shared/test_fx_performance.py`
- Modify: `scripts/paper_state.py`
- Modify: `scripts/run_paper.py`
- Modify: `tests/scripts/test_paper_state.py`

**Interfaces:**

- Produces: `FxAttribution` and `attribute_usd_equity_to_sgd(...)`.
- Preserves: legacy `EquitySnapshot.equity`, `cash`, and `market_value` as USD for divergence compatibility.

- [ ] **Step 1: Write the attribution invariant test**

```python
def test_fx_attribution_reconciles_total_sgd_change():
    result = attribute_usd_equity_to_sgd(
        starting_usd_equity=100_000,
        ending_usd_equity=110_000,
        starting_fx_base_per_trading=1.25,
        ending_fx_base_per_trading=1.30,
        external_flow_sgd=0,
    )
    assert result.security_pnl_sgd == pytest.approx(12_500)
    assert result.fx_translation_pnl_sgd == pytest.approx(5_500)
    assert result.total_pnl_sgd == pytest.approx(18_000)
    assert result.ending_equity_sgd == pytest.approx(143_000)
```

- [ ] **Step 2: Verify RED**

Run:

```bash
.venv/bin/pytest tests/shared/test_fx_performance.py -q
```

Expected: import failure because the module does not exist.

- [ ] **Step 3: Implement exact decomposition**

```python
@dataclass(frozen=True)
class FxAttribution:
    starting_equity_sgd: float
    ending_equity_sgd: float
    security_pnl_sgd: float
    fx_translation_pnl_sgd: float
    external_flow_sgd: float
    total_pnl_sgd: float
    investor_return_sgd: float | None


def attribute_usd_equity_to_sgd(
    *,
    starting_usd_equity: float,
    ending_usd_equity: float,
    starting_fx_base_per_trading: float,
    ending_fx_base_per_trading: float,
    external_flow_sgd: float,
) -> FxAttribution:
    starting = starting_usd_equity * starting_fx_base_per_trading
    ending = ending_usd_equity * ending_fx_base_per_trading
    security = (ending_usd_equity - starting_usd_equity) * starting_fx_base_per_trading
    fx = ending_usd_equity * (
        ending_fx_base_per_trading - starting_fx_base_per_trading
    )
    total = ending - starting - external_flow_sgd
    return FxAttribution(
        starting_equity_sgd=starting,
        ending_equity_sgd=ending,
        security_pnl_sgd=security,
        fx_translation_pnl_sgd=fx,
        external_flow_sgd=external_flow_sgd,
        total_pnl_sgd=total,
        investor_return_sgd=(total / starting if starting > 0 else None),
    )
```

Validate finite amounts and positive FX rates. Assert in tests that security +
FX - external flow equals total within `1e-9` tolerance.

- [ ] **Step 4: Persist both valuation views**

Keep `EquitySnapshot.equity`, `cash`, and `market_value` in USD. Populate the
new trading/base currency, FX, and translated SGD fields when recording daily
snapshots. The divergence monitor continues reading legacy USD equity.

Add an SGD investor section to the daily report without changing the USD
strategy report used by divergence.

- [ ] **Step 5: Verify accounting and divergence compatibility**

Run:

```bash
.venv/bin/pytest tests/shared/test_fx_performance.py tests/scripts/test_paper_state.py tests/backtest/test_divergence.py tests/scripts/test_divergence_monitor.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add shared/fx_performance.py scripts/paper_state.py scripts/run_paper.py tests/shared/test_fx_performance.py tests/scripts/test_paper_state.py
git diff --cached --check
git commit -m "feat: report USD strategy and SGD investor returns"
```

---

### Task 8: Make observability currency explicit

**Files:**

- Modify: `shared/observability.py:62-118`
- Modify: `tests/shared/test_observability.py`
- Modify: `scripts/run_paper.py`

**Interfaces:**

- Preserves: existing `_usd` gauges for USD values only.
- Produces: SGD NAV, USD-equivalent NAV, SGD-per-USD FX, and settled USD cash gauges.

- [ ] **Step 1: Write failing metric tests**

```python
metrics.net_liquidation_sgd.set(1_001_757.23)
metrics.net_liquidation_usd.set(774_855.87)
metrics.fx_sgd_per_usd.set(1.2928304)
metrics.settled_cash_usd.set(25_000)

assert metrics.net_liquidation_sgd._value.get() == pytest.approx(1_001_757.23)
assert metrics.net_liquidation_usd._value.get() == pytest.approx(774_855.87)
assert metrics.fx_sgd_per_usd._value.get() == pytest.approx(1.2928304)
assert metrics.settled_cash_usd._value.get() == pytest.approx(25_000)
```

- [ ] **Step 2: Verify RED**

Run:

```bash
.venv/bin/pytest tests/shared/test_observability.py -q
```

Expected: FAIL because the gauges are absent.

- [ ] **Step 3: Add explicit gauges and populate them**

Add these `TradingMetrics` fields and Prometheus names:

```text
algo_net_liquidation_sgd
algo_net_liquidation_usd
algo_fx_sgd_per_usd
algo_settled_cash_usd
```

Construct them as:

```python
net_liquidation_sgd=Gauge(
    "algo_net_liquidation_sgd",
    "Broker account net liquidation in SGD",
    registry=target_registry,
),
net_liquidation_usd=Gauge(
    "algo_net_liquidation_usd",
    "Broker account net liquidation translated to USD",
    registry=target_registry,
),
fx_sgd_per_usd=Gauge(
    "algo_fx_sgd_per_usd",
    "SGD required for one USD in the broker snapshot",
    registry=target_registry,
),
settled_cash_usd=Gauge(
    "algo_settled_cash_usd",
    "Settled USD cash before active order reservations",
    registry=target_registry,
),
```

Set them only from the matching explicit capital fields in
`prepare_daily_run`.

- [ ] **Step 4: Verify and commit**

Run:

```bash
.venv/bin/pytest tests/shared/test_observability.py tests/scripts/test_run_paper_capital.py -q
```

Expected: PASS.

```bash
git add shared/observability.py scripts/run_paper.py tests/shared/test_observability.py tests/scripts/test_run_paper_capital.py
git diff --cached --check
git commit -m "feat: expose currency-explicit trading metrics"
```

---

### Task 9: End-to-end verification and controlled paper rollout

**Files:**

- Modify only if a failing test identifies a defect in a Task 0-8 file.
- Operational outputs: `output/` and `/Users/huiliang/ibc/logs/` are read-only for verification; do not delete or overwrite historical files.

**Interfaces:**

- Consumes: all Task 0-8 interfaces.
- Produces: verified images and an entries-disabled paper-run result or a precise fail-closed reconciliation report.

- [ ] **Step 1: Run focused currency and safety suites**

```bash
.venv/bin/pytest \
  tests/services/execution/test_ib_account.py \
  tests/shared/test_capital.py \
  tests/scripts/test_run_paper_capital.py \
  tests/services/risk_management/test_funding.py \
  tests/services/risk_management/test_runner.py \
  tests/services/portfolio_accounting/test_projector.py \
  tests/shared/test_fx_performance.py \
  tests/shared/test_observability.py -q
```

Expected: PASS.

- [ ] **Step 2: Run full verification**

```bash
.venv/bin/python -c 'import asyncio, pytest, sys; asyncio.set_event_loop(asyncio.new_event_loop()); sys.exit(pytest.main(["-q"]))'
uvx ruff check shared services scripts tests
git diff --check
```

Expected: all tests pass, Ruff reports no new violations in touched files, and
the diff check is clean. If repository-wide Ruff exposes an unrelated existing
violation, rerun Ruff on the touched files and record both outputs.

- [ ] **Step 3: Apply the new migration only after backup verification**

Confirm the latest `/Users/huiliang/ibc/logs/db_backup_YYYYMMDD.log` reports
`Backup OK`. Then run:

```bash
env ALGO_DATABASE_URL=postgresql://algo:algo@127.0.0.1:55432/algo_poc .venv/bin/alembic upgrade head
```

Verify:

```sql
SELECT version_num FROM alembic_version;
```

Expected: `f6c2d9a84b31`.

- [ ] **Step 4: Rebuild images without restarting services**

```bash
docker compose build
docker image inspect algo-poc-execution:latest algo-poc-risk-management:latest
```

Expected: both images have fresh creation timestamps. Do not recreate running
containers until the migration and image verification are complete.

- [ ] **Step 5: Execute one entries-disabled paper run**

Run the standard non-reset wrapper:

```bash
/Users/huiliang/ibc/run_paper.sh
```

Expected currency lines include SGD NAV, USD equivalent, SGD/USD FX, settled
USD cash, and USD deployable capital. Because legacy positions are unowned,
the acceptable first result is a fail-closed reconciliation report. No new buy
order may be submitted.

- [ ] **Step 6: Verify broker and database state read-only**

Confirm:

- zero unexpected resting orders at IB
- one currency-complete capital snapshot if daily preparation committed
- the reconciliation report identifies legacy `account_id IS NULL` rows
- no buy intent advanced beyond a safe rejected/proposed state
- USD strategy and SGD investor values have explicit units

- [ ] **Step 7: Stop at the human repair gate**

Generate the report-only reconciliation plan. Do not apply ownership updates.
The human operator must run the interactive repair command after reviewing the
plan and backup. After that separate human action, rerun the wrapper with
entries disabled and verify reconciliation before considering entry enablement.
