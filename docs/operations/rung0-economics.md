# Rung-0 economics: is the six-sleeve split viable at USD 3.7k?

**Status:** measured; **decision logged 2026-08-17 — see §9** (KAN-34 / direction-doc D8).
**Run date:** 2026-08-17. **Window:** 2016-08-08 → 2026-08-03 (2,510 sessions).
**Gates:** epoch v2's restructure decision (KAN-33).

The capital ladder arms Rung 0 at 5,000 SGD ≈ USD 3,700, split six ways by the
committed sleeve weights. The direction doc flagged this as an open question
and asked for the drag number. The drag number is here — but it is not the
finding. **The finding is that four of the six sleeves cannot open positions at
this capital at all, and one of them cannot open a single position in ten
years.** A sleeve that never fills has no edge to measure and no baseline worth
re-tuning.

---

## 1. What was run

Three arms, identical in every respect except capital and sizing mode. All use
the committed cost model (`$0.005`/share, **`$1.00` per-order floor**,
liquidity-tiered slippage) and next-open fills.

| Arm | Command | Role |
|---|---|---|
| **A** | `--capital 3700 --whole-shares` | treatment — Rung 0 |
| **B** | `--capital 100000 --whole-shares` | **control** |
| **C** | `--capital 100000` (fractional) | historical reference only |

Whole-share mode is on in *both* A and B deliberately (AC 4c): with rounding
held constant, the A↔B difference is attributable to capital rather than to the
sizing change. Arm C is the shape every published baseline has been run in, and
is reported only so the two are not confused.

> **Universe caveat — read before quoting any number here.** These runs replay
> the cached 138-ticker static universe from
> `output/backtest_multi_20260804_075705.json`, **not** the point-in-time
> membership calendar KAN-23 delivered. No PIT-universe bar set exists yet:
> building one needs ~830 names fetched from IB Gateway, which is an operator
> step measured in hours. So these numbers carry survivorship bias and the
> per-sleeve returns are indicative.
>
> The headline is nonetheless robust to that, because fill feasibility is a
> comparison between a sleeve's position budget and a share price — it does not
> depend on which names were in the index in 2018. Survivorship bias flatters
> returns; it does not make a $34 budget able to buy a $170 share. **Re-run on
> the PIT universe before the decision is logged** (§6), and expect the
> feasibility percentages to move by a point or two, not to reverse.

---

## 2. Position sizes at Rung 0

Recomputed from the committed weights in `scripts/run_backtest.py:main()`
(mirrored in `scripts/run_paper.py:96-103`), at USD 3,700:

| Sleeve | weight | sleeve capital | `position_size_pct` | **position size** | affordable names¹ |
|---|---:|---:|---:|---:|---:|
| `quality_value` | 0.1538 | $569.06 | 0.06 | **$34.14** | 11 / 138 (8.0%) |
| `earnings_drift` | 0.1923 | $711.51 | 0.08 | **$56.92** | 26 / 138 (18.8%) |
| `thematic_momentum` | 0.1410 | $521.70 | 0.135 | **$70.43** | 32 / 138 (23.2%) |
| `momentum` | 0.2308 | $853.96 | 0.12 | **$102.48** | 44 / 138 (31.9%) |
| `sector_rotation` | 0.1538 | $569.06 | 0.20 | **$113.81** | 50 / 138 (36.2%) |
| `tail_risk_hedge` | 0.1283 | $474.71 | 0.25 | **$118.68** | 54 / 138 (39.1%) |

¹ Names whose latest close is at or below the position budget, i.e. where one
whole share can be bought. The universe's median latest close is **$171.61** —
above every sleeve's entire per-position budget.

These reproduce the direction doc's $34–119 range exactly.

---

## 3. Fill feasibility — the headline

Per sleeve: entry signals that reached sizing, and how many were rejected
because the budget could not buy one whole share.

### Arm A — USD 3,700, whole shares

| Sleeve | signals sized | **unfillable** | **% unfillable** | closed trades | open at end |
|---|---:|---:|---:|---:|---:|
| `quality_value` | 7,395 | **7,395** | **100.0%** | **0** | 0 |
| `tail_risk_hedge` | 5,956 | 5,952 | 99.9% | 3 | 0 |
| `earnings_drift` | 2,904 | 2,768 | 95.3% | 105 | 0 |
| `momentum` | 5,952 | 5,665 | 95.2% | 233 | 0 |
| `thematic_momentum` | 2,426 | 2,071 | 85.4% | 269 | 0 |
| `sector_rotation` | 741 | 606 | 81.8% | 104 | 2 |

### Arm B — USD 100,000, whole shares (control)

| Sleeve | signals sized | unfillable | % unfillable | closed trades | open at end |
|---|---:|---:|---:|---:|---:|
| `quality_value` | 128 | 13 | 10.2% | 71 | 17 |
| `earnings_drift` | 1,258 | 413 | 32.8% | 626 | 0 |
| `thematic_momentum` | 978 | 3 | 0.3% | 716 | 6 |
| `momentum` | 584 | 2 | 0.3% | 464 | 7 |
| `sector_rotation` | 169 | 12 | 7.1% | 117 | 4 |
| `tail_risk_hedge` | 449 | 56 | 12.5% | 292 | 1 |

**`quality_value` posts 7,395 entry signals and executes none of them.** Its
$34.14 budget clears one share of 8% of the universe, and the composite
quality-value score does not preferentially rank cheap shares. At Rung 0 the
sleeve is a no-op that still consumes 15.4% of the account.

`tail_risk_hedge` is effectively as dead — 3 trades in ten years — and worse
than its $118.68 headline suggests, because the regime allocation multiplies
the budget again (a 0.10 weight on `GLD` in a bear regime is an $11.87
position).

The two survivors, `momentum` and `thematic_momentum`, survive by *degrading*:
they buy whichever few names happen to be cheap enough, which is a different
strategy from the one that was backtested.

---

## 4. Round-trip commission drag

Per AC 4b: `(entry_commission + exit_commission) / entry_notional × 10000`,
averaged over **closed trades only**. Positions still open at the run's end are
excluded and counted in §3 above.

| Sleeve | **Arm A drag (bps)** | Arm B drag (bps) | A ÷ B |
|---|---:|---:|---:|
| `sector_rotation` | 236 | 17 | 13.6× |
| `momentum` | 264 | 11 | 23.4× |
| `earnings_drift` | 489 | 27 | 18.4× |
| `tail_risk_hedge` | 523 | 25 | 21.2× |
| `thematic_momentum` | 632 | 12 | 54.0× |
| `quality_value` | — (no trades) | 27 | — |

**Round-trip drag at Rung 0 is 236–632 bps: 2.4% to 6.3% of notional per
completed trade.** The direction doc quotes 84–293 bps. That figure is the
**one-way** number — commission is charged on entry *and* exit, so the doc's
range must be doubled to 168–586 bps before it is comparable, and the measured
range is worse still than the doubled estimate because the surviving trades
skew toward the smaller sleeves.

The aggregate statement of the same fact:

| | Arm A (3,700) |
|---|---:|
| Gross P&L before commission | **+$1,766.35** |
| Commissions paid | **−$1,428.00** |
| Net P&L | **+$338.35** |
| Commissions as % of starting capital | **38.6%** |

**Commission consumes 81% of gross profit over ten years.** Per sleeve the
picture is starker: `thematic_momentum` earns **+$118.90 gross** and pays
**$538.00** in commission, finishing at **−$419.10**. It is not a losing
strategy at Rung 0; it is a winning strategy sold to the broker.

---

## 5. Return vs the control

| | Arm A (3,700, whole) | **Arm B (100k, whole — control)** | Arm C (100k, fractional) |
|---|---:|---:|---:|
| Total return | **+9.07%** | **+84.33%** | +83.87% |
| Sharpe | 0.22 | 0.88 | 0.87 |
| Max drawdown | 11.56% | 12.27% | 12.35% |
| Win rate | 26.3% | 40.0% | — |
| Total trades | 714 | 2,286 | 2,298 |

Per sleeve (total return %):

| Sleeve | Arm A | Arm B |
|---|---:|---:|
| `momentum` | +75.48 | +201.15 |
| `sector_rotation` | +34.98 | +90.73 |
| `quality_value` | 0.00 (no trades) | +9.71 |
| `tail_risk_hedge` | −1.82 | −19.64 |
| `earnings_drift` | −11.27 | +42.21 |
| `thematic_momentum` | **−80.33** | +119.59 |

**Rung 0 retains about one-ninth of the control's ten-year return** (9.07% vs
84.33%), on a strategy that loses two of its six sleeves entirely and inverts
the sign on two more. B and C landing within 0.5 points of each other (84.33%
vs 83.87%) confirms the control is doing its job: whole-share rounding is
nearly free at $100k, so the A↔B gap is capital, not rounding.

---

## 6. Incidental finding: the fractional model books lots no broker would fill

Arm C's mean drag reads 115,428 bps for `earnings_drift` against a median of 13
bps. The outliers are trades of **0.0001 shares** — $0.0031 of notional paying
$2.00 of commission.

They come from the risk engine, not the sizing sites. `RiskEngine.check_entry`
caps an oversized entry and returns `adjusted_quantity` floored to four decimal
places with a `0.0001` minimum (`services/risk_management/engine.py:93,95-101`),
and the backtest has always booked that verbatim. Live, that quantity meets
`ib_executor._effective_quantity` (`services/execution/ib_executor.py:142-162`)
and is truncated or refused — so **every published fractional baseline contains
dust trades that live trading could never place.** They are individually tiny
and do not move the return, but they corrupt any per-trade cost statistic.

KAN-34 closes this on the whole-share path: truncation is applied after risk
sizing as well as at the signal (`backtest/runner.py`), which is why arm B's
`earnings_drift` shows 413 unfillable signals even at $100k: with the sleeve's
exposure limit nearly consumed, risk approves only the residual headroom, and
that sliver is a fraction of a share (median **0.14 shares**; e.g. `NOW` at
$64.40 approved for 0.2131 shares — $13.72 of room). Fractionally that books a
lot; live it is an `OrderSkippedError`. On the fractional path the behaviour is
unchanged by design. Making whole-share the default is a separate decision
(out of scope here) but the evidence now favours it.

---

## 7. Recommendation

The direction doc offers three options. Measured against these numbers:

**(a) Accept the drag with a capital-specific baseline — not viable.** This
option assumes the sleeves trade and merely trade expensively. They do not
trade: `quality_value` fills 0 of 7,395 signals and `tail_risk_hedge` 4 of
5,956. There is no baseline to regenerate for a sleeve with no trades, and
D16 would require a second baseline artifact plus a second set of monitor pins
to describe a book that is 100%-cash in two of six sleeves.

**(b) Raise Rung-0 funding — effective, and the cost is knowable.** Feasibility
is governed by `sleeve_capital × position_size_pct ≥ share_price`. Holding the
weights, the binding constraint is `quality_value` at 0.1538 × 0.06 = 0.92% of
the account per position; clearing a $171.61 median share needs roughly
**$18.6k**, and clearing most of the universe comfortably needs more. That is
five times Rung 0 and outside the ladder's intent.

**(c) Concentrate on fewer sleeves — recommended.** This is the only option
that fits the ladder's capital. Concentration raises per-position budget
directly, and the run says which sleeves earn their slot: `momentum`
(+75% even while skipping 95% of its signals) and `sector_rotation` (+35%,
lowest drag at 236 bps, and already the second-largest budget). Folding the
other four sleeves' 61.5% of capital into those two lifts `momentum` to
**$266.45** and `sector_rotation` to **$295.92** per position — one whole share
of **66.7%** and **71.0%** of the universe respectively, against 31.9% and
36.2% today.

**Recommendation: (c), concentrating Rung 0 on `momentum` and
`sector_rotation`, with `thematic_momentum` as the candidate third slot** — it
is gross-profitable (+$118.90) and only fees kill it, so a larger budget may
recover it, whereas `quality_value`'s $34 budget is the wrong side of an order
of magnitude.

Two constraints on adopting this:

- **The sleeves being dropped are not being judged.** Dropping
  `quality_value` at Rung 0 says its position budget is too small at this
  capital, not that its edge is absent — it is +9.71% in the control. Whatever
  is dropped must be re-admitted at a higher rung rather than retired.
- **Losing `tail_risk_hedge` removes the crash hedge.** It is the sleeve the
  crash-entry-freeze logic exempts. Concentrating on two momentum-family
  sleeves leaves Rung 0 directionally long with no hedge, which is a risk
  posture change, not just an allocation change, and belongs in the IPS
  amendment that accompanies the decision.

---

> **Outcome:** the operator adopted (c) and went further — **one sleeve,
> `momentum`**, not two. The reasoning, the conditions attached, and the
> risk-posture consequences are in **§9**; this section records what was
> recommended, §9 records what was decided.

---

## 8. Operator steps — not run by the agent

1. **Regenerate the PIT-universe bar set and re-run all three arms**, so the
   decision is logged against a non-survivorship-biased universe. Needs IB
   Gateway on `127.0.0.1:7497`; hours, and it contends with the 04:15 paper run
   for historical-data pacing.

   ```bash
   python scripts/run_backtest.py --years 10 \
       --universe-snapshots data/universe/sp500_membership.json \
       --output-dir output
   # then, reusing those bars:
   python scripts/run_backtest.py --capital 3700 --whole-shares \
       --bars-from-json output/backtest_multi_<TS>.json \
       --universe-snapshots data/universe/sp500_membership.json
   python scripts/run_backtest.py --capital 100000 --whole-shares \
       --bars-from-json output/backtest_multi_<TS>.json \
       --universe-snapshots data/universe/sp500_membership.json
   ```

2. ~~**Log the restructure decision**~~ — **DONE 2026-08-17, see §9.** The
   decision was taken on fill feasibility and commission arithmetic, both of
   which are independent of the universe caveat in §1, so it did not wait on
   step 1. D16's capital-specific divergence baseline is still owed and is
   condition §9.6(2): **one** baseline artifact and one set of monitor pins,
   not the two that option (a) would have required. That work is not a no-op
   and should be sequenced before Rung 0 arms.

## 9. Decision (D8) — Rung 0 runs one sleeve: `momentum`

**Status:** logged 2026-08-17. **Decides:** direction-doc D8 (OPEN since
2026-08-11). **Closes:** KAN-34 AC 6. **Unblocks:** KAN-33 (epoch v2).

### 9.1 The decision

Rung 0 (5,000 SGD ≈ USD 3,700) is allocated **100% to `momentum`**. The other
five sleeves are **suspended at Rung 0, not retired** — they are re-admitted at
higher rungs by between-epoch amendment (§9.6(4)).

This is option **(c) concentrate**, taken one step further than §7's
recommendation of two sleeves.

The **sleeve weights of record are unchanged.** `CAPITAL_ALLOCATIONS` and
`ACTIVE_SLEEVES` (`shared/universe.py`, `scripts/run_paper.py:104-111`) continue
to describe the six-sleeve book; Rung 0 runs a *capital overlay* on top of them,
so the `tests/shared/test_universe.py` invariant that the two agree is untouched.

### 9.2 Why one sleeve, at these numbers

Recomputed from the same universe as §2 (138 tickers, median latest close
$171.61), holding `position_size_pct` at its committed 0.12:

| Allocation | momentum position size | universe fillable¹ | round-trip drag² |
|---|---:|---:|---:|
| Six-way (today) | $102.48 | 31.9% (44/138) | 264 bps *(measured, §4)* |
| Two-way (§7's recommendation) | $266.45 | 66.7% (92/138) | ~75 bps |
| **One sleeve (this decision)** | **$444.00** | **87.7% (121/138)** | **~45 bps** |

¹ Names where one whole share fits the position budget. ² $1.00 commission floor
× 2 legs ÷ position notional; the floor binds at every one of these sizes.

Median capital utilisation at $444 is **86.8%** — once a name is affordable,
whole-share rounding wastes ~13% of the position budget, against a six-way
budget that buys nothing at all in two sleeves.

**Why not §7's two sleeves.** Two is materially better than six and materially
worse than one. The tie-breaker is not return, it is what Rung 0 is *for*: the
IPS names the smoke test's purpose as validating execution, reconciliation,
slippage, and the daily ops loop — "success = the operational gates behave, not
a P&L target" (`investment-policy-statement.md`, deployment path item 1).
Diversification buys return smoothing that a ~2-month window at this size cannot
measure; fill rate buys exercised code paths, which is the actual deliverable.

### 9.3 Why `momentum`, and on what grounds it was picked

Selected on **operational criteria, explicitly not on backtest return**:

- **Signal frequency** — 5,952 signals reaching sizing over ten years
  (joint-highest), 233 closed trades at Rung 0 (second only to
  `thematic_momentum`'s 269, which is gross-profitable but fee-killed, §4).
  Frequency is what exercises the ops loop inside a two-month window.
- **Smallest departure from the committed book** — at 0.2308 it already holds
  the largest weight, so concentrating on it is the least violent overlay
  available.
- **Already the drill sleeve** — `DRILL_BASE_SLEEVE = "momentum"`
  (`scripts/run_paper.py`), so the isolation-drill machinery already exercises
  exactly this sleeve.
- **Carries the bear tickers** — `SH`/`PSQ` are in its eligible universe
  (`shared/universe.py:40,71`), so the sleeve is not structurally long-only.

**Not** selected for posting the highest Arm A return (+75.48%). That figure is
survivorship-biased (§1) and selecting on it would add a further trial on top of
the eight-candidate → six-sleeve search of 2026-05-26. Every ground above is
independent of realised return.

### 9.4 What this decision deliberately does not change

- **`position_size_pct` stays 0.12 and `top_n` stays 5.** Consequence: maximum
  deployment is 5 × 12% = **60% of the 3,700**, leaving ~40% in cash. This is
  accepted, not overlooked. Raising it means moving `position_size_pct` *and*
  the momentum `RiskEngine`'s `position_entry_limit_pct=12.0` together — a
  strategy-parameter change, which is a new trial in the registry and belongs at
  an epoch boundary, not inside a capital decision. The idle cash also absorbs
  SGD→USD translation and whole-share slop.
- **No sleeve is judged or retired here.** `quality_value` returns +9.71% in the
  $100k control; its Rung-0 failure is a budget fact, not an edge fact.

### 9.5 Risk-posture consequences — IPS amendment required

Concentrating is an allocation change; dropping the hedge is a **risk-posture
change**, and it carries a second-order effect §7 did not name:

1. **No standing crash hedge.** `tail_risk_hedge` is suspended, so the book
   holds no dedicated defensive position.
2. **The crash entry freeze becomes a total trading freeze.** Every sleeve
   *except* `tail_risk_hedge` is wrapped in `make_crash_freeze_signals_fn`
   (`scripts/run_paper.py:264-271`; wrapper at
   `scripts/run_backtest.py:1796-1818`), which suppresses **all** buy signals in
   a crash regime and passes exits through. With `momentum` as the only sleeve,
   a crash regime means the book opens **nothing at all** — including the
   `SH`/`PSQ` rotation that would otherwise be its own hedge. Exits still fire
   via the 10% trailing stop. **Accepted for Rung 0:** at smoke-test size,
   "crash ⇒ stop opening risk" is a conservative posture and one fewer moving
   part. Exempting momentum's bear tickers from the freeze is a **follow-up
   candidate, not part of this decision** — it changes safety logic and needs
   its own story.
3. **Single-factor concentration.** Accepted; the Gate-3 bound (max drawdown
   ≤12% on USD NAV) governs unchanged, on ~USD 3.7k of exposure.
4. **If `momentum` is demoted, the book goes to cash.** Per the kill-criteria
   rule that a demotion does not auto-redistribute
   ([sleeve-kill-criteria.md](sleeve-kill-criteria.md)), the vacated weight sits
   in cash until the operator rebalances at a review. With one sleeve that means
   a 100%-cash book awaiting a review — an intended outcome, not an edge case.

### 9.6 Conditions attached

1. **Paper runs the same shape before live.** `scripts/run_paper.py:104-111`
   hardcodes the six-way allocations and has no whole-share parity with the live
   rung. A six-sleeve fractional paper book does not predict a one-sleeve
   whole-share live book. This is the implementation half of the decision and
   lands via KAN-33.
2. **One capital-specific divergence baseline** regenerated per D16
   (whole-share, commission floor, Rung-0 capital, `momentum` only) with its own
   monitor pins — cheaper than option (a), which would have required a second
   baseline describing a book that is 100% cash in two of six sleeves.
3. **The PIT re-run (§8 step 1) does not block this decision.** It was made on
   fill feasibility and commission arithmetic, both functions of a position
   budget and a share price, and therefore independent of index membership. The
   PIT re-run remains **required before any Rung-0 return figure or divergence
   threshold is quoted.**
4. **Re-admission is an epoch boundary.** Suspended sleeves return at a higher
   rung by amendment, and the Rung-0 20-session divergence-OK window does **not**
   transfer to the multi-sleeve book it becomes.

### 9.7 Evidence limits — read before citing any Rung-0 result

Five concurrent positions over roughly two months produces a handful of closed
trades. That is enough to validate fills, reconciliation, stop behaviour,
slippage, and the daily ops loop — what the IPS asks Rung 0 to prove. It is
nowhere near enough to say anything about `momentum`'s edge. **A clean Rung 0 is
evidence the machine works, never evidence the strategy works.**

### 9.8 Standing condition — `momentum`'s edge verdict of record

Direction D10 requires the edge-validation framework's incumbent evaluation to
complete **before Rung 0 arms**, and that evaluation has authority over this
decision: a sleeve without a passing verdict of record cannot be the sleeve
Rung 0 runs.

As of this decision the verdicts of record do not exist — the table in
[incumbent-edge-evaluation.md](incumbent-edge-evaluation.md) is empty pending
the real run. Its **rehearsal**, explicitly marked not-evidence and not-citable
(inflated by survivorship and same-bar fills), nonetheless puts `momentum` at
**DSR 0.868 against the 0.95 threshold — FAIL**, with `sector_rotation` the
cleanest pass at 0.993. The rehearsal's failure mode for `momentum` is
deflation against a search of eight, not out-of-sample collapse: its holdout
Sharpe is **+1.16** over 44 sessions.

**Therefore this decision is conditional.** If `momentum` fails its verdict of
record:

- **D8 reopens.** It is not amended silently and Rung 0 does not arm on a failed
  sleeve.
- **The named fallback is `sector_rotation`** — the rehearsal's cleanest DSR, and
  at 100% of Rung 0 it sizes to **$740 per position, 95.7% of the universe
  fillable (132/138), ~27 bps round-trip drag**, all better than `momentum`'s.
  The cost is operational: 741 sized signals over ten years against
  `momentum`'s 5,952, so a two-month window exercises the ops loop roughly an
  order of magnitude less. That trade-off is re-decided at the time, on the
  verdicts of record, not pre-committed here.
- **If neither passes, Rung 0 does not arm at all** — a decision the ladder
  already provides for, and a better outcome than arming on a sleeve the
  framework rejected.

---

## Reproducing this memo

Everything in §2–§6 comes from the three arms above. The weekly refresh
(`deploy/launchd/run_backtest_refresh.sh`) is unaffected: `--whole-shares`
defaults off, so every existing invocation stays fractional and byte-identical.

That last claim was verified rather than assumed. Arm C was run twice on the
same cached bars — once on this branch, once on `origin/develop` (84f8c7d) —
and every trade, portfolio value, metric and aggregate matched exactly across
all six sleeves.
