# Per-Epoch Drill Runbook (KAN-32)

Two drills per epoch, run in one window against the `__drill__` sleeve:

| Drill | `DrillType` | Proves |
|---|---|---|
| 1 — restart / halt | `restart_halt` | The halt latch is enforced across a restart: a buy is durably rejected, a risk-reducing exit still completes, and an order already live at the broker is found and cancelled. |
| 2 — synthetic stop-loss | `synthetic_stop` | A stop breach reaches IB and takes a **real paper fill**, with no dead-letter. |

They exist because "the halt gate is implemented" and "the halt gate has been
observed working" are different claims, and only the second one is evidence.

**Everything here is operator work.** Agents do not run any of it: the procedure
mutates the paper database, restarts containers, and places real orders on
DUN551088. The repo's destructive-actions policy (CLAUDE.md) applies to every
`psql` and `docker` line below.

**Read before starting:**
[`drill-evidence-isolation.md`](drill-evidence-isolation.md) (the exclusion
contract), [`container-deploy.md`](container-deploy.md) (build / recreate /
image-hash proof — this runbook rebuilds the execution image twice and defers to
that procedure for how).

---

## Three facts that shape the whole procedure

These were verified against the code, and two of them contradict the way the
story was originally specced. They are the reason the steps look the way they do.

### 1. A kill flattens the six graded sleeves. Use an out-of-band halt instead.

The story says to activate the halt "via the API kill endpoint". **Do not.**
`POST /api/v1/kill` publishes a `KillMessage`, and the risk service's
`process_kill` calls `_liquidate_all`, which reloads *every* authoritative open
position and flattens it (`services/risk_management/runner.py:1039`,
`:1059`). That writes `OrderIntent`, `Trade` and `Position` rows for all six
sleeves — exactly what AC4 forbids.

The halt latch can be set **out of band**, without liquidating. The risk
service's `sync_from_store` adopts an unowned halt row on its periodic cadence
and logs *"Adopted out-of-band halt on re-sync"*
(`services/risk_management/kill_switch.py:151-166`) — it does not liquidate on
that path. Execution reads the same latch on every approved order and every loop
iteration. So a plain `INSERT` into `system_halt` halts the system exactly as a
kill does, minus the flatten.

Clearing still goes through the real human-clear path: `DELETE /api/v1/kill`.

### 2. The software stop-loss scan cannot see a `__drill__` position.

The story routes Drill 2 through `RiskEngine.check_stop_loss`. That path is
structurally blind to the drill tag: the risk service's in-memory book comes from
`load_open_positions`, which filters `~Position.portfolio.startswith("_")`
(`shared/position_loader.py:32`), and `_refresh_portfolio_from_db` rebuilds the
book — and `_current_prices` with it — from that same call on every cycle
(`:1953`). A fill can insert the ticker transiently, but the next refresh removes
it again. Setting `highest_price_since_entry` and waiting for the scan therefore
observes nothing.

`BrokerStopManager._open_positions` (`services/execution/broker_stops.py:1282`)
filters on status, quantity and account only — **not** portfolio. A `__drill__`
position gets a real GTC stop like any other, which is both correct (a real
position needs real protection) and the mechanism Drill 2 drives.

`load_liquidation_targets` (`shared/liquidation.py:74`) also keeps drill rows, so
a kill can still flatten one.

Drill 2 therefore exercises the **broker-native stop** (KAN-19/20), which is the
primary stop-loss protection under design decision D16 anyway. This is also the
observation KAN-18's spike deliberately deferred to this drill
([`broker-stop-prototype.md`](broker-stop-prototype.md), Q1 trigger half).

### 3. Two timers restart at zero, which is what makes the drill watchable.

`_last_halt_sweep_at` and `_last_stop_verification_at` both start at `None`
(`services/execution/runner.py:189`, `:206`), so the first loop iteration after a
restart runs the sweep immediately instead of waiting out its interval (30s for
the halt sweep, `risk.passive_scan_interval_minutes` = 30 **minutes** for stop
verification). **Restarting the execution container is how you make a background
scan happen now.** Every "restart execution" below is there for that reason, not
as a reset.

---

## Preconditions

| # | Check | Why |
|---|---|---|
| 1 | Run in **bash**, not zsh | `deploy/launchd/secrets.sh` is bash-only, and its exports must survive into the `docker compose` calls |
| 2 | `docker compose ps` — every service Up and healthy | The drill recreates containers in place; it does not bring up a cold stack |
| 3 | An epoch exists and is RUNNING | `record_epoch.py drill --epoch` ties the outcome to it |
| 4 | US regular trading hours for Phase B | A stop only arms during RTH at `broker_stops_outside_rth: false` |
| 5 | `ib.account_id` is set in `config/default.yaml` | The stop backfill walks every account's positions without it |

```bash
source deploy/launchd/secrets.sh
docker compose ps
docker compose exec -T postgres psql -U algo -d algo_poc -c "\
SELECT label, rung, started_at FROM gate_epochs ORDER BY id DESC LIMIT 3;"
```

`record_epoch.py` has no read-only `show` subcommand — its four are `start`,
`event`, `drill` and `evaluate`, and `evaluate` writes a transition — so the
epoch is confirmed by reading `gate_epochs` directly.

### Record the AC4 baseline before anything moves

AC4 is a **row-level** assertion — equity is deliberately not compared, because
markets move during a drill and the drill's own commissions debit shared broker
cash. Capture the counts now; you re-run the identical query at the end.

```bash
docker compose exec -T postgres psql -U algo -d algo_poc -c "\
SELECT 'order_intents' AS t, portfolio, count(*) FROM order_intents GROUP BY portfolio \
UNION ALL SELECT 'positions', portfolio, count(*) FROM positions GROUP BY portfolio \
UNION ALL SELECT 'trades', portfolio, count(*) FROM trades GROUP BY portfolio \
ORDER BY 1, 2;"
```

Save the output. Only the `__drill__` rows may change.

### Turn the broker-stop flag on

`execution.broker_stops_enabled` ships **off** (`config/default.yaml:99`) and
there is no `ALGO_*` override for it — `config/` is baked into the image, so this
is a file edit plus a rebuild.

KAN-33 step 2 says to enable the flag "at the boundary, not before". The drill
*is* the rehearsal for that boundary, so it is enabled here and, if both drills
pass, left on into the v2 start. If you do turn it back off afterwards, read the
warning at `config/default.yaml:95` first: **turning the flag off does not cancel
stops already resting at IB.** They must be cancelled at the broker or the
account keeps orphan protective orders no code knows about.

```bash
# Edit config/default.yaml: execution.broker_stops_enabled: true
docker compose build execution
docker compose up -d --force-recreate --no-deps execution
docker compose logs --tail=30 execution
```

Follow [`container-deploy.md`](container-deploy.md) Step 4 to prove the running
image actually changed — `--build` alone has left containers on the old image
before.

---

## Phase A — Drill 1 (restart / halt)

Three assertions. A3 is taken first because it needs an order resting at the
broker *before* the halt exists.

### A3 — the sweep finds and cancels an order live during a halt

> **The true race cannot be produced on demand.** KAN-12's pre-submit check
> narrows the window between "halt lands" and "`placeOrder` returns" but cannot
> close it, and nothing can schedule an order into that window. What is verified
> here is the sweep's *capability* against a real live order: it discovers the
> order by `orderRef`, maps it to its intent, and cancels it. **Record that the
> true race was not reproduced.**

Run the tagged entry **pre-open** so the limit buy rests at IB instead of filling
immediately. `run_paper.py` has no market-calendar gate — it runs whenever
invoked.

```bash
python scripts/run_paper.py --portfolio-tag __drill__ --portfolio-tag-capital 500
```

The tag is validated in the inverted direction on purpose: only a name for which
`is_excluded_portfolio` is True is accepted, so a typo like `--portfolio-tag
momentum` is refused with exit 2 before the database is opened.

Confirm the buy is live at the broker and note its `orderRef` (it is the
`recommendation_id`):

```bash
docker compose logs --since=10m execution | grep -E "Processing approved order|submit"
docker compose exec -T postgres psql -U algo -d algo_poc -c "\
SELECT recommendation_id, symbol, action, status, ib_order_id FROM order_intents \
WHERE portfolio = '__drill__' ORDER BY id DESC LIMIT 5;"
```

Expect `status = SUBMITTED` with an `ib_order_id` (the enum is uppercase). Now set the halt out of band:

```bash
docker compose exec -T postgres psql -U algo -d algo_poc -c "\
INSERT INTO system_halt (mode, active, source, reason, triggered_by, activated_at) \
VALUES ('paper', true, 'drill', 'KAN-32 epoch drill', 'operator', now());"
```

`source` is a free 32-char string; `drill` keeps the audit trail honest about why
the system halted. Restart execution to force an immediate sweep:

```bash
docker compose restart execution
docker compose logs --since=5m execution | grep -i "halt sweep"
```

**Pass:** a `Halt sweep cancelling an order live during a halt` line naming the
drill's `order_id` / `order_ref` / `ticker`, followed by a
`halt_sweep_order_cancelled` alert. The intent's `ib_order_id` is now populated
even if it was not before — that is `_record_raced_submission` closing the
visibility hole the sweep exists for.

**Also record, as a second observation:** any resting SELL is left alone
(`Halt sweep leaving a risk-reducing sell alone`). A halt must not orphan
protection, and the sweep checks direction before identity.

If instead you see `halt_sweep_unknown_order`, the buy reached IB with an
`orderRef` that matches no intent. That is a real finding, not a drill failure —
capture it and stop.

### A1 — a buy is durably rejected while halted, and never submitted

With the halt still active, publish another drill buy:

```bash
python scripts/run_paper.py --portfolio-tag __drill__ --portfolio-tag-capital 500
docker compose logs --since=5m execution | grep -i "Rejecting buy: system is halted"
docker compose exec -T postgres psql -U algo -d algo_poc -c "\
SELECT recommendation_id, symbol, action, status, reason, ib_order_id FROM order_intents \
WHERE portfolio = '__drill__' ORDER BY id DESC LIMIT 3;"
```

**Pass:** `status = SUBMISSION_FAILED`, `reason = halted`, `ib_order_id` NULL,
and a `halted_order_rejected` alert at `high`. The message is acked, not
retained, and never dead-lettered — a buy decided before an incident must not
execute after the clear, against a market and a book that have both moved.

Confirm nothing reached the broker:

```bash
docker compose logs --since=5m execution | grep -c "Processing approved order"
```

### A2 — a risk-reducing exit still completes during the halt

> **The kill-path liquidation SELL specifically is not exercised, and cannot be.**
> The only in-system producer of one is `_liquidate_all`, which flattens all six
> sleeves (fact 1 above). Running it to satisfy this assertion would violate AC4.
> What is observed instead is the same property on a drill-scoped exit: an
> exposure-reducing order completes while the system is halted. **Record that the
> kill-path sell was not reproduced.**

The observation is produced by Phase B, which is run **with the halt still
active**: the broker stop is placed, re-levelled, triggered and filled while
`system_halt.active` is true, and the halt sweep leaves it alone throughout. Do
not clear the halt yet — go to Phase B.

---

## Phase B — Drill 2 (synthetic stop-loss)

### Open the drill position

Phase A left the entry cancelled and the system halted, and a buy is rejected
while halted (that is A1). So the order here is: **clear, buy, re-halt.**

Clear the halt and wait for the risk service to resume — it adopts and drops the
latch on its periodic re-sync, not instantly:

```bash
ADMIN_KEY="$(printf '%s' "$API_KEYS" | tr ',' '\n' | awk -F: '$2=="admin"{print $1; exit}')"
curl -X DELETE -H "X-API-Key: $ADMIN_KEY" http://127.0.0.1:8000/api/v1/kill
docker compose logs --since=5m risk-management | grep -i "Halt cleared out-of-band"  # re-run until it appears
```

Open the position during RTH so the entry fills. Re-running the tagged sleeve is
safe: an existing tag row is left alone, so the drill's cash is not topped back
up.

```bash
python scripts/run_paper.py --portfolio-tag __drill__ --portfolio-tag-capital 500
```

Once the fill has projected into `positions`, re-set the halt — the rest of
Phase B runs under it, which is what produces A2:

```bash
docker compose exec -T postgres psql -U algo -d algo_poc -c "\
INSERT INTO system_halt (mode, active, source, reason, triggered_by, activated_at) \
VALUES ('paper', true, 'drill', 'KAN-32 epoch drill', 'operator', now());"
```

Confirm the position and its stop:

```bash
docker compose exec -T postgres psql -U algo -d algo_poc -c "\
SELECT ticker, quantity, avg_entry_price, current_price, highest_price_since_entry, con_id \
FROM positions WHERE portfolio = '__drill__' AND status = 'open';"
docker compose logs --since=10m execution | grep -i "stop"
```

The verifier places a GTC stop at `highest_price_since_entry × (1 − 15%)`
(`risk.stop_loss_trailing_pct`, `config/default.yaml:70`).

### Force the breach

The stop level tracks `highest_price_since_entry` and, by KAN-20 AC5, **never
moves down**. Raising the high above the mark therefore makes the verifier retire
the resting stop and re-place it *above* the market, where IB triggers it on
arrival.

The multiplier has to clear `1 / (1 − 0.15) ≈ 1.176`. Use **1.25**, which puts
the stop about 6% above the mark — enough to fire, not so far that the order
looks absurd in the audit trail.

```bash
docker compose exec -T postgres psql -U algo -d algo_poc -c "\
UPDATE positions SET highest_price_since_entry = current_price * 1.25 \
WHERE portfolio = '__drill__' AND status = 'open' \
RETURNING ticker, current_price, highest_price_since_entry, \
          round((highest_price_since_entry * 0.85)::numeric, 2) AS stop_level;"
```

`update_peak_prices` (`scripts/paper_state.py`) only ratchets this column upward,
so the injected value will not be undone by a later run. If the position somehow
survives the drill, reset the column by hand — see Unwind.

Restart execution to run the verification now rather than in up to 30 minutes:

```bash
docker compose restart execution
docker compose logs --since=5m execution | grep -iE "stop verification|Position already covered|Cannot size"
```

**Pass, in order:**

1. `Broker stop verification adjusted resting protection` with the old stop in
   `cancelled_order_ids` and a new id in `placed_order_ids`.
2. The new stop triggers at IB and fills.
3. A `FillMessage` is emitted, the fill projects, and the `stop-…` intent reaches
   `FILLED` — this is the whole point of running it here rather than in the KAN-18
   spike, where a stop firing from an untracked client produced no `FillMessage`
   and desynced the book.

```bash
docker compose exec -T postgres psql -U algo -d algo_poc -c "\
SELECT recommendation_id, symbol, action, status, filled_quantity, limit_price \
FROM order_intents WHERE portfolio = '__drill__' AND recommendation_id LIKE 'stop-%' \
ORDER BY id DESC LIMIT 5;"
docker compose exec -T postgres psql -U algo -d algo_poc -c "\
SELECT symbol, side, quantity, price, execution_id FROM execution_fills \
 WHERE portfolio = '__drill__' \
ORDER BY id DESC LIMIT 5;"
```

**And the no-dead-letter check (AC2):**

```bash
docker compose exec -T redis redis-cli -a "$REDIS_PASSWORD" --no-auth-warning \
  XLEN stream:approved_orders:dlq
docker compose exec -T redis redis-cli -a "$REDIS_PASSWORD" --no-auth-warning \
  KEYS '*dlq*'
```

`XLEN` must be unchanged from before the drill (the queue has historically not
existed at all — see [`dlq-audit-2026-08.md`](dlq-audit-2026-08.md)).

**If IB rejects the stop rather than triggering it** — an error 110 or a "stop
price above market" refusal — that is a real finding about IB's behaviour, which
is the one thing the KAN-18 spike could not observe. Record the exact error,
lower the multiplier toward 1.18, and retry once. Do not paper over it.

### Clear the halt

```bash
ADMIN_KEY="$(printf '%s' "$API_KEYS" | tr ',' '\n' | awk -F: '$2=="admin"{print $1; exit}')"
curl -X DELETE -H "X-API-Key: $ADMIN_KEY" http://127.0.0.1:8000/api/v1/kill
docker compose logs --since=5m risk-management | grep -i "Halt cleared out-of-band"
```

---

## Unwind

**Never use `scripts/ops/flatten_paper_account.py` to unwind a drill.** It takes
no symbol or portfolio filter — `--apply` closes **every** position on
DUN551088, the six graded sleeves included. It exists for the Path A
re-baseline, and reaching for it here would destroy the record the drill was
designed not to touch.

A passing Drill 2 unwinds itself: the stop sells the whole drill position. Verify:

```bash
docker compose exec -T postgres psql -U algo -d algo_poc -c "\
SELECT ticker, quantity, status FROM positions WHERE portfolio = '__drill__';"
```

Expect no `open` rows. If a residual remains (a partial fill, or a stop IB
refused), there is **no per-position unwind tool in the repo**. In order:

1. Re-run the verifier — `docker compose restart execution` — and let it re-place
   a stop sized to the remaining shares. This is the only route that keeps the
   ledger and the broker in agreement.
2. If that fails, close it by hand in IB Gateway. A manual sell is placed by a
   client the execution service does not track, so **no `FillMessage` is emitted
   and the book desyncs** (`quantity_mismatch`, severity `major`, entries
   disabled until an operator-gated repair). Follow it immediately with
   `reconcile_paper.py --report` and apply the generated repair plan.
3. Reset the injected high if any drill row is still open:
   `UPDATE positions SET highest_price_since_entry = current_price WHERE portfolio = '__drill__' AND status = 'open';`

### Reconcile — required before recording any outcome

A drill that leaves an open position corrupts the next day's book.

```bash
python scripts/reconcile_paper.py --report
```

**Pass:** status `ok`. Anything else means the drill is not finished.

### Close any critical the drill raised

`alert_records` has no portfolio column, so the exclusion contract cannot reach
go-live gate 5 — an unresolved critical would block it for 14 days.

**Using an out-of-band halt instead of a kill mostly avoids this.** The two
criticals a `restart_halt` drill would otherwise raise —
`kill_switch_activated` (`services/risk_management/runner.py:1092`) and
`kill_switch_liquidation` (`services/execution/runner.py:1568`) — are published
on the kill-message path, which this procedure never takes. The drill's own
alerts, `halted_order_rejected` and `halt_sweep_order_cancelled`, are `high`,
and gate 5 does not count those.

One critical *can* still appear: `halt_sweep_cancel_failed`, when the sweep
finds the order but IB refuses the cancel. That is a genuine incident, not drill
noise — investigate before resolving it.

```bash
docker compose exec -T postgres psql -U algo -d algo_poc -c "\
SELECT id, event_type, priority, raised_at, resolved_at FROM alert_records \
WHERE priority = 'critical' ORDER BY id DESC LIMIT 10;"
python -m scripts.ops.resolve_alert --list
python -m scripts.ops.resolve_alert --id 41 --resolved-by huiliang
```

(Substitute the real id and your own name. Resolution is deliberately a named
human act, and the tool refuses to overwrite an earlier one, so the trail shows
who called it first.)

---

## Verify the evidence isolation held (AC5)

Exercised live, not assumed.

**Row-level (AC4):** re-run the baseline count query from Preconditions and diff
it. Only `__drill__` counts may have moved. Equity is deliberately not compared.

**Gate metrics:** the drill fills must not move a single gate number.

```bash
python -m scripts.ops.go_live_gate --json
```

Compare against the run from before the drill. Gates 1, 3 and 4 read
`equity_snapshots`, `order_intents` and `execution_fills` and carry the exclusion
predicate through the fills→intents join; gate 5 cannot be portfolio-scoped,
which is why the alert above is resolved by hand.

**Divergence input:**

```bash
python scripts/divergence_monitor.py --no-output
```

Expect an explicit skip line naming `__drill__` with its reason, and no
`__drill__` verdict row:

```bash
docker compose exec -T postgres psql -U algo -d algo_poc -c "\
SELECT sleeve, count(*) FROM divergence_daily GROUP BY sleeve;"
```

---

## Record the outcomes (AC3)

One `DrillOutcome` row per drill, tied to the epoch. `--detail` is where the
honesty lives: the two things that were **not** reproduced belong in it.

```bash
python -m scripts.ops.record_epoch drill --epoch v2 --type restart_halt --passed \
  --detail "A1 rejected+unsubmitted; A3 sweep cancelled by orderRef; A2 observed as a drill-scoped exit during the halt. NOT REPRODUCED: the true check-to-submit race; the kill-path liquidation SELL."

python -m scripts.ops.record_epoch drill --epoch v2 --type synthetic_stop --passed \
  --detail "Breach forced via highest_price_since_entry x1.25; verifier re-levelled and re-placed; IB triggered; real paper fill projected; stream:approved_orders:dlq unchanged."
```

Use `--failed` when an assertion did not hold. A failed drill is evidence too —
it is the reason the drill exists — and the epoch engine counts drills as a
shortfall criterion, not a fail-the-epoch one.

---

## What these drills do not prove

Carry this list forward; it is part of the outcome, not a caveat on it.

1. **The check-to-submit race.** Verified as capability, not reproduced (A3).
2. **The kill-path liquidation SELL.** Its only producer flattens the graded book
   (A2).
3. **The software stop-loss scan.** Structurally blind to `__drill__`, so this
   drill says nothing about it. It remains the fallback path with
   `broker_stops_enabled` off, and it is exercised only by a real breach on a
   graded sleeve.
4. **Overnight gap protection.** `broker_stops_outside_rth: false` leaves the stop
   dormant outside RTH; the drill runs inside RTH and observes nothing about gaps.
5. **Gate 5 isolation.** `alert_records` has no portfolio column. The gap is
   sidestepped rather than closed here — the out-of-band halt raises no critical
   — so it remains open for any drill or incident that does go through a kill.

---

## Related

- [`drill-evidence-isolation.md`](drill-evidence-isolation.md) — the exclusion contract (KAN-24)
- [`broker-stop-prototype.md`](broker-stop-prototype.md) — the KAN-18 spike, whose Q1 trigger half this runbook closes
- [`container-deploy.md`](container-deploy.md) — build / recreate / image-hash proof (KAN-17)
- [`dlq-audit-2026-08.md`](dlq-audit-2026-08.md) — `stream:approved_orders:dlq` history (KAN-21)
- KAN-25 — the `DrillOutcome` row and `record_epoch.py drill`
- KAN-33 — epoch v2 start, which requires both drills passed
