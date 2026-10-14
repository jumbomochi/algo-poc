from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable

from backtest.metrics import BacktestMetrics
from backtest.portfolio_context import (
    HeldPosition,
    PendingOrder,
    PortfolioContext,
    RunnerBook,
)
from backtest.simulator import SimulatedExecutor
from research.shadow import CandidateObserver
from services.risk_management.funding import (
    DEFAULT_SLEEVE_CASH_BUFFER_BPS,
    size_to_sleeve_cash,
    sleeve_buy_cost_usd,
)
from shared.universe import MembershipCalendar


logger = logging.getLogger(__name__)

# Consecutive trading sessions a held ticker may print no bar before the
# position is written off as delisted. A name whose bars simply stop *is* what a
# delisting looks like in daily data; leaving the position open would keep its
# last close in NAV forever and keep the trade out of the win-rate/expectancy
# statistics, which is survivorship bias moved from the universe into the trade
# stats. Five sessions distinguishes a delisting from an ordinary data gap.
DELISTING_STALE_SESSIONS = 5


@dataclass
class BacktestResult:
    """Container for backtest output."""

    trades: list[dict] = field(default_factory=list)
    portfolio_values: list[float] = field(default_factory=list)
    dates: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    shadow_candidates: list[dict] = field(default_factory=list)
    # Lots still held when the last session ended. ``trades`` records closed
    # round-trips only, so without this a position that never exited leaves no
    # trace — and any per-trade cost statistic silently drops it.
    open_positions: list[dict] = field(default_factory=list)
    # KAN-111: entries the sleeve's own cash could not fully pay for —
    # ``downsized`` were cut to fit, ``skipped`` could not afford the smallest
    # order. Non-zero only for a sleeve whose sizing outruns its cash.
    sleeve_cash: dict = field(
        default_factory=lambda: {"downsized": 0, "skipped": 0}
    )
    # KAN-113: the cash injections actually applied, by the session they were
    # applied on (``run(cash_flows=...)``). Empty for every run without flows.
    # A withdrawal larger than the replay's cash is clamped, so this can differ
    # from what was asked; ``portfolio_values`` (and so ``metrics``) include
    # these steps — callers that compare returns remove them.
    cash_flows_applied: dict = field(default_factory=dict)
    # KAN-116: what happened to every buy signal the runner saw on or after
    # ``trade_start_date``: refused by risk, skipped for sleeve cash, queued,
    # and of the queued ones how many filled and how many expired unfilled.
    entry_funnel: dict = field(
        default_factory=lambda: {
            "signals": 0, "risk_refused": 0, "whole_share_skipped": 0,
            "cash_skipped": 0, "queued": 0, "filled": 0, "expired": 0,
        }
    )


class BacktestRunner:
    """Replays historical data day-by-day through a signal/risk pipeline.

    Tracks portfolio state, positions, and trades. Uses SimulatedExecutor
    for order fills.

    **Execution model: next-bar fills.** Signals are computed from bars up to
    and including ``close[t]``, and the resulting orders are queued for the
    following session — entries fill against ``open[t+1]`` (or the limit price
    if only the intraday low reaches it) and exits fill at ``open[t+1]``.
    Entries are day orders: an unreachable limit expires with the session
    rather than resting. This replaced same-bar filling, which let a decision
    taken on the close trade at that same day's low (entries) or that same
    day's open (exits) — findings 4.2 and 4.3 of the 2026-08-06 review.

    **Entries are funded from cash (KAN-111).** After the risk engine approves
    an entry it is capped at the cash the sleeve holds when it decides, net of
    the entries it has already queued that session, with
    ``services.risk_management.funding.size_to_sleeve_cash`` — the function the
    paper run uses, so live, the rolling shadow and the backtest size alike.
    Cash raised by exits decided the same session does not count: live books
    sell proceeds only when the sell fills, and the buy can fill first. Before
    this the sim could run a sleeve's cash negative up to the risk engine's
    150% exposure limit — free, interest-less leverage that live can never
    take, because the fill projector refuses any fill that overdraws a sleeve.

    Usage:
        runner = BacktestRunner(executor=executor, initial_capital=100_000)
        result = runner.run(bars_by_ticker, signals_fn, risk_engine)
    """

    def __init__(
        self,
        executor: SimulatedExecutor,
        initial_capital: float = 100_000.0,
        *,
        whole_shares: bool = False,
        skip_ledger: Any = None,
        cash_buffer_bps: float = DEFAULT_SLEEVE_CASH_BUFFER_BPS,
    ) -> None:
        self.executor = executor
        self.initial_capital = initial_capital
        # Whole-share mode has to bite *after* risk sizing, not only at the
        # signal. ``RiskEngine.check_entry`` returns an ``adjusted_quantity``
        # floored to four decimals when it caps an entry, and live that
        # fraction still has to clear ``ib_executor._effective_quantity``.
        # Truncating only the signal would leave the backtest booking 0.0001
        # -share lots that no broker would fill.
        self.whole_shares = whole_shares
        self.skip_ledger = skip_ledger
        # Live's ``currency.sleeve_cash_buffer_bps``. The sim fills a limit buy
        # at up to ``limit * (1 + slippage)``, so the buffer it applies is never
        # below that ticker's slippage — otherwise a funded entry could still
        # take cash below zero here. At the defaults (25 bps buffer, slippage
        # 10-25 bps) the two are the same number.
        self.cash_buffer_bps = float(cash_buffer_bps)

    def run(
        self,
        bars_by_ticker: dict[str, list[dict]],
        signals_fn: Callable[[str, list[dict]], dict | None],
        risk_engine: Any,
        trade_start_date: Any = None,
        *,
        candidate_observer: CandidateObserver | None = None,
        portfolio_name: str = "",
        membership: MembershipCalendar | None = None,
        delisting_stale_sessions: int = DELISTING_STALE_SESSIONS,
        cash_flows: dict | None = None,
        cash_flow_basis: tuple[Any, float] | None = None,
        book: RunnerBook | None = None,
    ) -> BacktestResult:
        """Run a backtest over the provided bar data.

        Args:
            bars_by_ticker: Map of ticker -> list of bar dicts (sorted by date).
                Each bar has keys: date, open, high, low, close.
            signals_fn: Callable(ticker, bars_so_far) -> signal dict or None.
                Signal dict keys: action ("buy"/"sell"), ticker, limit_price,
                quantity, sector (for buys).
            risk_engine: Object with check_entry(ticker, quantity, price,
                sector, portfolio) -> decision with .approved, .adjusted_quantity.
            trade_start_date: If set, only allow new buy entries on or after this
                date. Earlier bars are still fed to signals_fn for indicator
                warm-up. Sell signals for existing positions are always processed.
            candidate_observer: Optional observational hook for raw buy candidates.
            portfolio_name: Portfolio identity supplied to candidate_observer.
            membership: Optional point-in-time universe. When supplied, a ticker
                only produces signals on dates when it was a member, and an open
                position in a ticker that leaves the universe (index removal or
                delisting) is exited at the next open. When omitted the whole of
                ``bars_by_ticker`` is treated as tradable on every date, which
                carries survivorship bias if those bars came from a
                present-day ticker list.
            delisting_stale_sessions: Consecutive sessions a held ticker may
                print no bar before the position is written off at its last
                close with ``exit_reason: "delisted"``. See
                ``DELISTING_STALE_SESSIONS``.
            cash_flows: KAN-113. ``{session: signed USD}`` cash injected into
                (or, negative, withdrawn from) the book, so a replay can carry
                the capital flows the live sleeve received. Applied on the
                first replayed session on or after the key, after that
                session's fills and before its decisions: the cash sizes the
                entries decided at that session's close (KAN-111's cash cap
                sees it) and is in that session's end-of-day value — the same
                session a live snapshot first includes the flow. A withdrawal
                larger than the cash on hand is clamped to that cash (never
                below zero): the replay cannot sell to fund it. The applied
                amounts are in ``BacktestResult.cash_flows_applied``. ``None``
                or empty leaves the run exactly as without it.
            cash_flow_basis: KAN-105. ``(anchor_session, basis_nav)``: the
                ``cash_flows`` are sized against a book worth ``basis_nav`` on
                ``anchor_session`` (live's), so each one is multiplied by this
                run's own end-of-day NAV on that session over ``basis_nav``
                before it is applied. A flow keeps its size *relative to the
                book* when the replay's NAV on the anchor differs from live's —
                which it does after a warm-up, because the warm-up trades.
                Every flow must apply strictly after the anchor (the scale is
                not known until its close); one that would apply on or before
                it raises ``ValueError``. Scaling inside the run, rather than a
                first pass to learn the NAV, mattered because a sleeve's signal
                function was stateful: a second pass over the same function
                started with the first pass's book in its private state (until
                KAN-116, which moved that state into ``book``).
            book: KAN-116. A :class:`backtest.portfolio_context.RunnerBook`
                the sleeve's signal function was built with as its
                ``portfolio_context``. Before each session's decisions it is
                bound to a ``PortfolioContext`` of this run's own filled lots
                (and its still-working orders), so the signal function sees
                what actually filled — as live does — instead of a private
                record of what it *asked* for. A buy that does not fill
                leaves no lot behind and is simply re-evaluated next session.
                Pass it whenever ``signals_fn`` was built with one: an
                unbound book raises on its first read. ``None`` only for a
                signal function that reads no positions (a test stub).

        Returns:
            BacktestResult with trades, portfolio_values, and metrics.
        """
        cash = self.initial_capital
        cash_limited = {"downsized": 0, "skipped": 0}
        funnel = BacktestResult().entry_funnel
        pending_flows = sorted((cash_flows or {}).items())
        flows_applied: dict = {}
        # KAN-105: this run's NAV over live's on the basis session, learned at
        # that session's close. ``None`` until then.
        flow_scale: float | None = None if cash_flow_basis else 1.0
        positions: dict[str, list[_Lot]] = {}
        trades: list[dict] = []
        portfolio_values: list[float] = [self.initial_capital]
        dates: list = []

        # Collect all unique dates across tickers, sorted
        all_dates = collect_sorted_dates(bars_by_ticker)

        # Build date-indexed bar lookups for each ticker
        bars_by_date: dict[str, dict] = {}
        bars_history: dict[str, list[dict]] = {t: [] for t in bars_by_ticker}
        for ticker, bars in bars_by_ticker.items():
            for bar in bars:
                bars_by_date[(ticker, bar["date"])] = bar

        # Orders decided on the previous session, waiting for the next open.
        pending_entries: dict[str, _PendingEntry] = {}
        pending_exits: dict[str, str] = {}  # ticker -> exit_reason
        # Last close seen per ticker, used to mark — and ultimately to write
        # off — a position in a name that stops printing bars.
        last_close: dict[str, float] = {}
        # Consecutive sessions since each ticker's last print. Only tracked once
        # a ticker has printed at least one bar, so names that have not listed
        # yet are not mistaken for delistings.
        stale_sessions: dict[str, int] = {}

        def close_position(ticker: str, fill: dict, exit_reason: str) -> float:
            """Record trades for every lot of ``ticker`` and return the cash raised."""
            proceeds = 0.0
            for lot in positions.pop(ticker, []):
                lot_fill = dict(fill)
                lot_fill["quantity"] = lot.quantity
                lot_fill["commission"] = self.executor.cost_model.commission_for(
                    lot.quantity
                )
                exit_value = lot_fill["fill_price"] * lot.quantity
                pnl = (
                    exit_value
                    - lot.entry_price * lot.quantity
                    - lot.entry_commission
                    - lot_fill["commission"]
                )
                proceeds += exit_value - lot_fill["commission"]
                trades.append({
                    "ticker": ticker,
                    "entry_date": lot.entry_date,
                    "exit_date": lot_fill["date"],
                    "entry_price": lot.entry_price,
                    "exit_price": lot_fill["fill_price"],
                    "quantity": lot.quantity,
                    "pnl": pnl,
                    "entry_commission": lot.entry_commission,
                    "exit_commission": lot_fill["commission"],
                    "entry_signals": lot.entry_signals,
                    "exit_reason": exit_reason,
                })
            return proceeds

        for current_date in all_dates:
            traded_today = {
                ticker for ticker in bars_by_ticker
                if (ticker, current_date) in bars_by_date
            }

            # --- Phase 0: age every ticker that has printed before. ---
            for ticker in stale_sessions:
                stale_sessions[ticker] = (
                    0 if ticker in traded_today else stale_sessions[ticker] + 1
                )

            # --- Phase 1a: write off holdings whose bars have stopped. ---
            # There is no future bar to fill against, so the position is closed
            # at its last observed close. Without this the position never
            # closes: it stays out of the trade statistics while its stale mark
            # props up NAV, and any queued order for it leaks reservations.
            for ticker, stale in sorted(stale_sessions.items()):
                if stale < delisting_stale_sessions:
                    continue
                if ticker not in positions and ticker not in pending_entries:
                    continue
                pending_entries.pop(ticker, None)
                # A removal already decided is the real reason; the delisting is
                # only why it had to fill at the last close instead of an open.
                exit_reason = pending_exits.pop(ticker, None) or "delisted"
                if ticker in positions:
                    mark = last_close.get(ticker)
                    if mark is None:
                        continue
                    cash += close_position(
                        ticker,
                        self.executor.fill_terminal_exit(
                            quantity=0.0,
                            price=mark,
                            exit_date=current_date,
                            ticker=ticker,
                        ),
                        exit_reason,
                    )

            # --- Phase 1b: fill exits queued yesterday, at today's open. ---
            # Exits settle before entries so freed cash is available the same
            # session, matching how the live sleeves sequence their orders.
            for ticker, exit_reason in list(pending_exits.items()):
                bar = bars_by_date.get((ticker, current_date))
                if bar is None:
                    # The ticker did not trade today; a market exit stays live
                    # until it can actually be worked (or until Phase 1a writes
                    # the position off as delisted).
                    continue
                del pending_exits[ticker]
                if ticker not in positions:
                    continue
                cash += close_position(
                    ticker,
                    self.executor.fill_market_exit(
                        quantity=0.0, bar=bar, ticker=ticker
                    ),
                    exit_reason,
                )

            # --- Phase 1c: work yesterday's entries, then expire them. ---
            for ticker, order in pending_entries.items():
                bar = bars_by_date.get((ticker, current_date))
                if bar is None:
                    continue
                fill = self.executor.try_fill_limit_entry(
                    limit_price=order.limit_price,
                    quantity=order.quantity,
                    bar=bar,
                    ticker=ticker,
                )
                if fill is None:
                    continue

                funnel["filled"] += 1
                cash -= fill["fill_price"] * fill["quantity"] + fill["commission"]
                positions.setdefault(ticker, []).append(
                    _Lot(
                        ticker=ticker,
                        quantity=fill["quantity"],
                        entry_price=fill["fill_price"],
                        entry_date=fill["date"],
                        entry_commission=fill["commission"],
                        entry_signals=order.entry_signals,
                    )
                )
            # Day-order semantics: an entry gets exactly one session, whether or
            # not its ticker printed a bar in it. Carrying it over would keep
            # its notional reserved against cash indefinitely.
            pending_entries.clear()

            # --- Phase 2a: bookkeeping for everything that printed today. ---
            for ticker in bars_by_ticker:
                bar = bars_by_date.get((ticker, current_date))
                if bar is None:
                    continue
                bars_history[ticker].append(bar)
                last_close[ticker] = bar["close"]
                stale_sessions.setdefault(ticker, 0)

            # --- Phase 2b: point-in-time universe gate. ---
            # Driven by what we can act on rather than by what printed a bar, so
            # a holding dropped from the index on a day it happens not to trade
            # is still queued for exit.
            non_members: set[str] = set()
            if membership is not None:
                gate_tickers = traded_today | set(positions) | set(pending_exits)
                for ticker in sorted(gate_tickers):
                    if membership.contains(ticker, current_date):
                        continue
                    non_members.add(ticker)
                    # Out of the point-in-time universe: not tradable, and any
                    # holding has to be liquidated (index removal / delisting).
                    if ticker in positions and ticker not in pending_exits:
                        pending_exits[ticker] = "universe_removal"

            # --- Phase 2b': capital flows (KAN-113), before today's decisions. ---
            while pending_flows and pending_flows[0][0] <= current_date:
                _, amount = pending_flows.pop(0)
                if flow_scale is None:
                    raise ValueError(
                        f"cash flow applied on {current_date}, on or before "
                        f"the basis session {cash_flow_basis[0]}: it cannot be "
                        "scaled to this book before that session's close"
                    )
                amount = float(amount) * flow_scale
                if amount < 0:
                    amount = -min(-amount, max(cash, 0.0))
                cash += amount
                flows_applied[current_date] = (
                    flows_applied.get(current_date, 0.0) + amount
                )

            # --- Phase 2c': KAN-116, hand the sleeve its own book. ---
            # Built once per session, before any decision, exactly as live
            # builds one PortfolioContext per sleeve per run: every ticker's
            # decision this session reads the same book, and an exit queued for
            # one ticker does not change what another ticker's replacement
            # logic sees as held until it fills.
            if book is not None:
                book.bind(
                    _runner_context(
                        positions, pending_exits, self.initial_capital
                    )
                )

            # --- Phase 2c: decide, using bars up to and including today's close. ---
            for ticker in bars_by_ticker:
                if ticker not in traded_today or ticker in non_members:
                    continue

                signal = signals_fn(ticker, bars_history[ticker])
                if signal is None:
                    continue

                action = signal.get("action")

                if action == "sell" and ticker in positions:
                    pending_exits.setdefault(
                        ticker, signal.get("exit_reason", "unknown")
                    )

                elif action == "buy":
                    # Skip new entries before trade_start_date
                    if trade_start_date is not None and current_date < trade_start_date:
                        continue
                    funnel["signals"] += 1
                    limit_price = signal["limit_price"]
                    quantity = signal["quantity"]
                    sector = signal.get("sector", "Unknown")

                    # A queued entry is a committed lot and committed cash that
                    # has not settled yet — both have to be visible to risk.
                    pending_entry = pending_entries.get(ticker)
                    existing_lots = len(positions.get(ticker, [])) + (
                        1 if pending_entry is not None else 0
                    )
                    portfolio_state = _make_simple_portfolio(
                        cash, positions, bars_by_date, current_date, last_close
                    )
                    decision = risk_engine.check_entry(
                        ticker, quantity, limit_price, sector, portfolio_state,
                        existing_lots=existing_lots,
                        reserved_notional=sum(
                            order.notional for order in pending_entries.values()
                        ),
                    )

                    if candidate_observer is not None:
                        try:
                            signal_snapshot = deepcopy(signal)
                            candidate_observer.observe(
                                portfolio=portfolio_name,
                                ticker=ticker,
                                as_of=current_date,
                                signal=signal_snapshot,
                                risk_approved=bool(decision.approved),
                                risk_reason=str(decision.reason),
                            )
                        except Exception:
                            logger.exception(
                                "Research shadow observer failed; trading result is unchanged"
                            )

                    if not decision.approved:
                        funnel["risk_refused"] += 1
                        continue

                    order_quantity = decision.adjusted_quantity
                    if self.whole_shares:
                        truncated = float(int(order_quantity))
                        if truncated <= 0:
                            funnel["whole_share_skipped"] += 1
                            if self.skip_ledger is not None:
                                self.skip_ledger.record(
                                    ticker=ticker,
                                    current_date=current_date,
                                    fractional_quantity=order_quantity,
                                    price=limit_price,
                                )
                            continue
                        order_quantity = truncated

                    cost_model = self.executor.cost_model
                    buffer_bps = max(
                        self.cash_buffer_bps, cost_model.slippage_bps_for(ticker)
                    )
                    funding = size_to_sleeve_cash(
                        quantity=order_quantity,
                        price=limit_price,
                        sleeve_cash_usd=cash,
                        committed_usd=sum(
                            order.cash_cost for order in pending_entries.values()
                        ),
                        per_share=cost_model.commission_per_share,
                        minimum=cost_model.commission_minimum,
                        buffer_bps=buffer_bps,
                        whole_shares=self.whole_shares,
                    )
                    if not funding.approved:
                        cash_limited["skipped"] += 1
                        funnel["cash_skipped"] += 1
                        continue
                    if funding.downsized:
                        cash_limited["downsized"] += 1
                        order_quantity = funding.quantity

                    funnel["queued"] += 1
                    pending_entries[ticker] = _PendingEntry(
                        limit_price=limit_price,
                        quantity=order_quantity,
                        entry_signals=signal.get("signals", {}),
                        cash_cost=sleeve_buy_cost_usd(
                            order_quantity,
                            limit_price,
                            per_share=cost_model.commission_per_share,
                            minimum=cost_model.commission_minimum,
                            buffer_bps=buffer_bps,
                        ),
                    )

            # End of day: compute portfolio value and update peak prices
            nav = cash
            for ticker, lots in positions.items():
                bar = bars_by_date.get((ticker, current_date))
                mark = bar["close"] if bar is not None else last_close.get(ticker)
                for lot in lots:
                    if mark is None:
                        nav += lot.entry_price * lot.quantity
                        continue
                    nav += mark * lot.quantity
                    if bar is not None:
                        lot.peak_price = max(lot.peak_price, mark)
            portfolio_values.append(nav)
            dates.append(current_date)
            if (
                flow_scale is None
                and current_date >= cash_flow_basis[0]
            ):
                flow_scale = nav / float(cash_flow_basis[1])

        # Entries still queued after the last session neither filled nor expired.
        funnel["expired"] = (
            funnel["queued"] - funnel["filled"] - len(pending_entries)
        )

        # Compute metrics
        metrics = BacktestMetrics.compute(
            portfolio_values=portfolio_values,
            trades=trades,
        )
        try:
            shadow_records = getattr(candidate_observer, "records", [])
            shadow_candidates = [record.to_dict() for record in shadow_records]
        except Exception:
            logger.exception(
                "Research shadow export failed; trading result is unchanged"
            )
            shadow_candidates = []

        open_positions = [
            {
                "ticker": ticker,
                "entry_date": lot.entry_date,
                "entry_price": lot.entry_price,
                "quantity": lot.quantity,
                "entry_commission": lot.entry_commission,
            }
            for ticker, lots in positions.items()
            for lot in lots
        ]

        return BacktestResult(
            trades=trades,
            portfolio_values=portfolio_values,
            dates=dates,
            metrics=metrics,
            shadow_candidates=shadow_candidates,
            open_positions=open_positions,
            sleeve_cash=cash_limited,
            cash_flows_applied=flows_applied,
            entry_funnel=funnel,
        )


@dataclass
class _PendingEntry:
    """A limit buy decided on one session's close, working the next session."""

    limit_price: float
    quantity: float
    entry_signals: dict = field(default_factory=dict)
    #: Worst-case cash this entry can take when it fills (KAN-111): notional
    #: at the limit grown by the slippage buffer, plus commission.
    cash_cost: float = 0.0

    @property
    def notional(self) -> float:
        return self.limit_price * self.quantity


@dataclass
class _Lot:
    """Individual lot within a position."""

    ticker: str
    quantity: float
    entry_price: float
    entry_date: Any
    entry_commission: float
    entry_signals: dict = field(default_factory=dict)
    peak_price: float = 0.0

    def __post_init__(self):
        if self.peak_price == 0.0:
            self.peak_price = self.entry_price


def _runner_context(
    positions: dict[str, list[_Lot]],
    pending_exits: dict[str, str],
    sleeve_budget: float,
) -> PortfolioContext:
    """KAN-116: this run's filled lots as a live-shaped ``PortfolioContext``.

    Field for field what ``PaperTradingState.build_portfolio_context`` reads
    from the ``positions`` table: total quantity, quantity-weighted fill price,
    the high-water mark since entry (the runner raises ``peak_price`` at each
    close after decisions, as live's ``update_peak_prices`` does after its
    signals), and the first fill's session. Entries queued yesterday have
    already filled or expired by the time decisions run, so the only working
    orders are exits that could not fill (no bar) — shown as pending sells,
    which ``_context_exit_quantity`` reads to avoid a duplicate exit.
    """
    held = {}
    for ticker, lots in positions.items():
        quantity = sum(lot.quantity for lot in lots)
        if not lots or quantity <= 0:
            continue
        held[ticker] = HeldPosition(
            quantity=quantity,
            avg_entry_price=sum(lot.entry_price * lot.quantity for lot in lots) / quantity,
            peak_price=max(lot.peak_price for lot in lots),
            entry_date=min(lot.entry_date for lot in lots),
        )
    pending = {
        ticker: PendingOrder(
            ticker=ticker,
            action="sell",
            quantity=held[ticker].quantity,
            limit_price=None,
            recommendation_id=f"backtest-exit-{ticker}",
        )
        for ticker in pending_exits
        if ticker in held
    }
    return PortfolioContext(
        positions=held,
        pending_orders=pending,
        sleeve_budget=float(sleeve_budget),
        reserved_notional=0.0,
    )


def collect_sorted_dates(bars_by_ticker: dict[str, list[dict]]) -> list:
    """Collect all unique dates across tickers, sorted chronologically.

    Public because the coverage measurement in ``scripts/run_backtest`` has to
    count membership-days over exactly the sessions this runner replays; two
    independent derivations of "the session list" would drift.
    """
    dates = set()
    for bars in bars_by_ticker.values():
        for bar in bars:
            dates.add(bar["date"])
    return sorted(dates)


def _make_simple_portfolio(
    cash: float,
    positions: dict[str, list[_Lot]],
    bars_by_date: dict,
    current_date: Any,
    last_close: dict[str, float] | None = None,
) -> Any:
    """Build a minimal portfolio-like object for risk engine calls.

    The risk engine expects a portfolio with nav, peak_nav, positions,
    sector_exposure, total_exposure_pct, margin_utilization_pct.
    For backtesting we provide simplified values.
    """
    from backtest._portfolio_state import SimplePortfolioState

    nav = cash
    all_positions = {}
    for ticker, lots in positions.items():
        total_qty = sum(lot.quantity for lot in lots)
        all_positions[ticker] = {"quantity": total_qty}
        bar = bars_by_date.get((ticker, current_date))
        if bar is not None:
            mark = bar["close"]
        else:
            mark = (last_close or {}).get(ticker)
        for lot in lots:
            nav += (mark if mark is not None else lot.entry_price) * lot.quantity

    # Exposure = market value / nav. Was hard-coded 0.0, which silently
    # disabled the RiskEngine's total-exposure limit in every backtest —
    # sleeves could lever internally (cash below zero) bounded only by
    # per-position caps and max-lots.
    market_value = nav - cash
    exposure_pct = (market_value / nav) * 100.0 if nav > 0 else 0.0

    return SimplePortfolioState(
        nav=nav,
        peak_nav=nav,
        positions=all_positions,
        sector_exposure={},
        total_exposure_pct=exposure_pct,
        margin_utilization_pct=0.0,
    )
