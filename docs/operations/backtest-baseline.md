# Backtest Baseline

**Purpose:** how to produce a headline backtest whose numbers are worth acting
on, and which the divergence monitor will accept as a like-for-like baseline
for live trading.

Background: the 2026-08-06 implementation review (Theme 4) found the previous
headline backtest optimistic by construction — a survivorship-biased universe,
fills that were unachievable in live trading, fundamentals read before they
were filed, and an ML filter that could be applied to its own training window.
Those are fixed in code; what is left is regenerating the baseline.

---

## The execution model

Every backtest now runs the same execution model, and declares it in the saved
results so downstream tools can check it:

| Assumption | Value | Why |
|---|---|---|
| Fills | `next_open` | A decision taken on `close[t]` queues an order for the next session. Entries fill at `open[t+1]`, or at the limit if only the intraday low reaches it; unfilled limits expire as day orders. Exits fill at `open[t+1]`. |
| Slippage | 10 bps base, ×2.0 for inverse ETFs, ×2.5 for thematic ETFs | One number for a mega-cap and for `PRNT` was never credible. |
| Commission | `max($1/order, $0.005/share)` | IB's actual schedule. At this account size the **floor** is what binds, not the per-share rate. |
| Universe | point-in-time membership, when snapshots are supplied | Otherwise the run is survivorship-biased and says so, loudly. |
| Universe coverage | ≥ 95% of membership-days priceable | A point-in-time member whose bars cannot be pulled is silently skipped, and the skipped names are disproportionately the delistings. |
| Fundamentals | available 45 days after period-end (or the row's `filing_date`) | A quarter ending 31 March is not public on 31 March. |
| Share sizing | fractional, unless `--whole-shares` | Live truncates to whole shares and refuses the order at zero; the backtest does not, so the two disagree by default. See below. |

`config.fill_model`, `config.commission_minimum`,
`config.slippage_bps_by_ticker`, `config.point_in_time_universe` and
`config.coverage` are written into every results JSON.
`scripts/divergence_monitor.py` reads them and reports `NO_DATA` rather than a
misleading OK/BREACH if the baseline is not like-for-like. All four of
next-open fills, a per-order commission floor, a point-in-time universe **and**
an `OK` coverage state are required — a re-run that fixed the fills and the
costs but kept the static ticker list is still inflated by winner
pre-selection, so it does not pass. Anything the config does not declare is
read as the unsafe value; absence is never a pass.

When the baseline fails that check the monitor exits **3** (not 0), and
`deploy/launchd/run_divergence.sh` logs it as `divergence monitor BLIND`. A
blind monitor is an outage, not a clean run.

### Whole-share sizing (`--whole-shares`)

Off by default, so every existing invocation — the weekly refresh included —
is unchanged. With it on, entry quantities truncate toward zero at the sizing
sites **and** after `RiskEngine.check_entry` caps an order, mirroring
`ib_executor._effective_quantity`. An entry whose budget cannot buy one whole
share opens no position and is recorded per sleeve:

```json
"skipped_signals": {"count": 7395, "signals": [
  {"ticker": "AAPL", "date": "2019-04-01", "fractional_quantity": 0.2131, "price": 160.2}
]},
"entry_signals_sized": 7395
```

`entry_signals_sized` is the denominator — how many entries the sleeve tried to
open at all — so a skip count can be read as a rate. `config.whole_shares`
records which mode produced the artifact, and `open_positions` lists lots still
held at the last session, which `trades` (closed round-trips only) omits.

This matters most at small capital, where a sleeve's per-position budget can be
smaller than one share: see
[`rung0-economics.md`](rung0-economics.md), where `quality_value` fills 0 of
7,395 signals at USD 3,700.

---

## Point-in-time universe file

`--universe-snapshots` takes a JSON file of sparse membership snapshots. Each
snapshot lists the index constituents **effective from that date** until the
next snapshot:

```json
{
  "source": "<where this came from>",
  "generated_at": "2026-08-07",
  "snapshots": {
    "2015-01-02": ["AAPL", "MSFT", "..."],
    "2015-04-01": ["AAPL", "MSFT", "..."]
  }
}
```

A bare `{"2015-01-02": [...]}` mapping (no envelope) is also accepted.

Rules the loader enforces:

- Dates **before the first snapshot have no members** — nothing is tradable.
  Back-filling the earliest snapshot backwards would silently reintroduce the
  survivorship bias, so the run goes quiet instead (obvious, not optimistic).
- Names that appear in an early snapshot and not a later one are **removed from
  the universe on the later snapshot's date**. An open position in such a name
  is liquidated at the next open with `exit_reason: universe_removal` — that is
  how index removals and delistings get paid for.
- Sector, thematic, inverse and defensive ETFs are exempt (`ALWAYS_TRADABLE` in
  `scripts/run_backtest.py`): they are not index constituents, so membership
  must not gate them.

**Delistings.** A held ticker whose bars simply stop is treated as a delisting:
after `DELISTING_STALE_SESSIONS` (5) consecutive sessions with no print, the
position is written off at its last observed close, charged the usual exit
slippage and commission, and recorded as a closed trade with
`exit_reason: "delisted"` (or the reason already queued, e.g.
`universe_removal`). This matters for more than NAV: leaving the position open
would keep it out of win rate, expectancy and `total_trades`, which is
survivorship bias moved out of the universe and into the trade statistics. The
write-off marks at the last close, which is neutral — optimistic for a
bankruptcy, pessimistic for a cash acquisition at a premium.

**Sourcing the data.** The membership history **is** in this repo, at
`data/universe/sp500_membership.json` (KAN-23). It is generated, not
hand-maintained:

```bash
python scripts/ops/build_membership_snapshot.py --start 2015-01-01
```

That reads the revision history of the Wikipedia article *List of S&P 500
companies* through the MediaWiki API — one revision per quarter — and writes
both the snapshot file and `shared/historical_sectors.py` from the same
revisions. Every snapshot records the exact `revid` and revision timestamp it
came from in the file's `revisions` block, so the file is reproducible and can
be audited line-by-line against Wikipedia. Consecutive quarters with identical
membership are collapsed (a snapshot is effective *until the next one*); in
practice membership has changed every single quarter of this window, so the
current file has one snapshot per quarter and the collapse never fires.

Four honest limits on this source. All are fine for a divergence baseline and
**none** of them are fine for attribution or an index-replication claim:

- Wikipedia lags real index changes by days, so an entry/exit date is accurate
  to roughly a week rather than to the session.
- Quarterly cadence bounds how long a departed name stays tradable (or a new
  member stays invisible) at one quarter.
- **Ticker renames read as a removal plus an addition.** A constituents table
  has no way to say "this is the same company under a new symbol", so the
  calendar sees the old symbol leave and a new one join — and the backtest
  liquidates the position at the next open with `exit_reason: universe_removal`
  and books a fabricated round-trip. `TICKER_ALIASES` in the generator
  canonicalises the renames that intersect `CONTRACT_CONID_OVERRIDES`
  (`MRSH`→`MMC`, `FISV`→`FI`), because those two would otherwise be handed to
  IB under the one spelling that has no conId pin — the names the override
  exists to rescue would be exactly the ones it missed. Other renames in the
  window are left as-is; each costs one phantom round-trip in a single name.
- **Recycled tickers can resolve to the wrong company.** `make_stock_contract`
  builds `Stock(ticker, "SMART", "USD")`, so a symbol later reassigned to a
  different issuer (`GAS`, `MON`, `RAI`, `ETFC`, `WYN`, `Q` all appear in this
  window) resolves at IB to whoever holds it *today*. That returns bars, so
  `coverage.excluded_tickers` — which only catches names with *zero* bars —
  never fires, and a foreign price series is merged into the baseline silently.
  Pinning conIds in the snapshot envelope would close this; it is not done yet.
  When reviewing a regenerated baseline, sanity-check that each delisted name's
  last bar date is near its membership exit rather than near today.

If you later buy a vendor history, point `--output` at the same path and keep
the envelope shape; nothing downstream needs to change.

Two things must accompany the file for the equity sleeves to work properly on
delisted names:

1. **Bars** for every ticker in `snapshots` (the run fetches
   `MembershipCalendar.all_tickers()`, which includes the delisted ones). This
   is the operator step — see *Regenerating the headline baseline* below.
2. **Sector labels** for those names. `SECTOR_MAP` (in `shared/universe.py` —
   `scripts/fetch_fundamentals.py` only re-exports it) covers the present-day
   top 100 only, so historical members used to fall into the `Unknown` bucket
   and be grouped together by the sector-concentration limit — the freeze
   documented in the 2026-08-07 incident. `shared/historical_sectors.py` now
   supplies a real GICS sector for the other ~690 names, and `lookup_sector`
   consults it last, so the curated map still wins for anything currently
   traded. `tests/shared/test_universe.py` fails if a regenerated snapshot ever
   introduces a name with no sector.

   **This changes live risk behaviour, not just the backtest.**
   `lookup_sector` feeds the risk service, the position loader and the
   portfolio projector. Any *currently held* name outside the curated top-100
   moves from the `Unknown` pseudo-bucket into a real sector, which changes
   which entries `sector_concentration_pct` rejects — that is the intended fix,
   but it lands the moment the containers are rebuilt, so watch the first
   session's rejections after deploying.

   **Known divergence:** the curated `SECTOR_MAP` labels `TGT` *Consumer
   Discretionary*; S&P reclassified Target to *Consumer Staples* in 2023 and
   the index history agrees. The generator reports such conflicts and refuses to
   auto-apply them — a curated label decides how a **currently held** name is
   bucketed by the live risk engine, so changing one is a trading-behaviour
   change that belongs in its own reviewed commit.

**Coverage floor: ≥ 95% of membership-days must be priceable.** Point-in-time
membership only removes survivorship bias if the historical members can
actually be priced — a name whose bars fail to pull is skipped in silence, and
the names that fail are disproportionately the delistings. Every run with
`--universe-snapshots` therefore measures coverage in **membership-days** (the
sum over sessions of how many constituents the index had that day, so a name
that left in 2019 weighs less than one present throughout) and writes it to
`config.coverage`:

```json
"coverage": {
  "total_membership_days": 1258000,
  "excluded_membership_days": 21400,
  "excluded_pct": 1.7,
  "excluded_tickers": {"DELISTED_CO": 3020, "...": 0},
  "floor_pct": 5.0,
  "state": "OK"
}
```

`state` is `BLOCKED` above the floor, and the run prints the ten worst
exclusions so you know which bars to chase. A `BLOCKED` baseline — or one with
no `coverage` block at all, which is how every pre-KAN-22 artifact reads — is
not like-for-like, so the monitor exits 3 rather than scoring against it. The
floor is inclusive: exactly 5.0% excluded still passes. See
`backtest/membership.py`.

**Sleeve universes are scoped, and each equity sleeve ranks point-in-time.**
`bars_by_ticker` is the union of every sleeve's instruments, so each ranking
sleeve is given an explicit `eligible_tickers`: momentum gets the historical
equity members plus the inverse ETFs, quality_value gets the historical equity
members, and the ETF sleeves keep their fixed lists. Both equity sleeves also
take the membership calendar and drop non-members from each date's ranking —
otherwise top-N slots are filled by names the runner will refuse to buy and the
sleeve silently trades nothing. If you add a ranking sleeve over equities, give
it the same two arguments.

---

## The pinned baseline of record

The divergence monitor does **not** score against whichever
`output/backtest_multi_*.json` sorts last. It scores against the artifact named
by `divergence.baseline_pin` in `config/default.yaml` (KAN-51).

Recency selection is not a pin. Under it the baseline of record was a filesystem
accident: `run_divergence.sh` passed no `--backtest` at all, so every 04:45 run
took `find_latest_backtest_json()`, and the weekly refresh replaced the artifact
the gate evidence was being measured against every Tuesday — with nothing
recording that it had changed, and no way afterwards to say which baseline had
judged a given session. D16 requires the Rung-0 baseline to have "its own monitor
pins"; this is the mechanism half of that.

### Where the pin lives, and what enforces it

```yaml
# config/default.yaml
divergence:
  baseline_pin: output/backtest_multi_20260819_183451.json
```

- **Resolution.** `scripts/ops/baseline_pin.py` turns that value into an
  absolute path (relative paths resolve against the caller's working directory,
  and both wrappers `cd` to the deployed checkout first). `ALGO_BASELINE_PIN`
  overrides it for one run — useful for scoring an ad-hoc comparison against a
  different artifact, and used by the wrapper tests. Never export it in a login
  shell: the nightly job would then judge against whatever it points at.
- **Invocation.** `deploy/launchd/run_divergence.sh` passes
  `--backtest <pin> --pinned`. `--pinned` is what swaps the tolerant ad-hoc
  semantics for pinned ones.
- **Ad-hoc runs are unchanged.** Without `--pinned`, `--backtest` is optional and
  recency remains the documented default. That is deliberate: making the strict
  behaviour global would break every local run against an older artifact, which
  is not what the pin buys.

### The two ways a pinned run refuses to judge

Both exit **3** — the same outage code as a non-comparable baseline, because it
is the same outage: no drift detection is running. Both print a stable token that
`scripts/ops/divergence_alert.py` reads back so the Telegram names the actual
reason instead of the generic "not comparable" text, and **neither writes a
report or an evidence-store row** — a session that was never scored must not
land in the store as a `NO_DATA` observation, because the epoch clock reads
`NO_DATA` as "the monitor ran and could not judge".

| Token | When | What to do |
|---|---|---|
| `BASELINE_PIN_MISSING` | The pinned path is absent, unreadable, or no pin is configured at all | Fix `divergence.baseline_pin`, or produce the artifact it names |
| `BASELINE_SHAPE_MISMATCH` | The pinned baseline's sleeve set differs from the live book's | Re-pin to a baseline of the live book's shape (see below) |

There is deliberately **no fallback to recency** on `BASELINE_PIN_MISSING`. A
job that ran, reported success and meant nothing is the 2026-08-13 failure
pattern — the 04:15 and 04:45 jobs both aborted silently for two days and
2026-08-13 is a permanent hole in the gate evidence. The wrapper does not decide
either: it hands the empty pin to the monitor and lets the monitor be the single
authority on whether the run can mean anything.

**Why the shape check exists.** `is_like_for_like` checks the baseline's
*execution* assumptions — fills, the commission floor, the point-in-time
universe, the coverage state — and has no opinion on its *shape*. A six-sleeve,
27×-capital artifact scored against a one-sleeve Rung-0 book passes every one of
those requirements and still is not a comparison: the aggregate sums only dates
present in *every* portfolio, and a per-sleeve verdict that exists on one side
only is silently skipped. The check compares the baseline's `portfolios` keys
against the live `portfolio_config` rows, dropping synthetic sleeves on both
sides (`is_excluded_portfolio` — see
[drill-evidence-isolation.md](drill-evidence-isolation.md)), and it runs on the
**full** live set before `--portfolio` narrows anything, so a scoped ad-hoc run
cannot report a clean sleeve out of a book the pin does not describe.

### Which baseline judged a session

Every `divergence_daily` row records the pinned artifact's basename in
`baseline_id`, and `(sleeve, session_date, baseline_id)` is the uniqueness key —
so verdicts scored against different book shapes sit in different rows rather
than overwriting each other, and the evidence the gate reads can be filtered to
one baseline.

### Re-pinning at a rung change

Re-pin deliberately; it is a change to what the evidence means.

1. Produce the new baseline (*Regenerating the headline baseline* above) and
   check `config.coverage.state` is `OK`.
2. Confirm its `portfolios` keys match the live book's sleeves — otherwise the
   first pinned run exits 3 with `BASELINE_SHAPE_MISMATCH`.
3. Set `divergence.baseline_pin` to the new path and land it through a PR.
4. A config-only change needs **no** redeploy: `~/ibc/run_divergence.sh` resolves
   the pin by running `$ALGO_DIR/scripts/ops/baseline_pin.py`, which reads the
   repo checkout's `config/default.yaml`, so the next 04:45 run picks it up.
   (One exception — **the pin mechanism itself does not exist in production until
   the wrappers are redeployed.** `local.algo-divergence.plist` runs the deployed
   copy of `run_divergence.sh`, so until `deploy/launchd/deploy.sh` has run after
   KAN-51 landed, the 04:45 job still passes no `--backtest` and still takes the
   recency path. The wrapper's drift check only writes a WARNING line into the
   log.)
5. Run the monitor by hand once and confirm the log line `baseline pin: <path>`
   and a non-3 exit before trusting the next night's verdict.

### The weekly refresh cannot delete the pin

`run_backtest_refresh.sh` prunes `output/backtest_multi_*.json` older than 90
days. Once pinned, the baseline of record is the one artifact in `output/` that
*stops* being rewritten — so it is guaranteed to age past that cutoff and be
deleted by the job whose purpose is to keep the monitor working. The prune now
excludes it (`find ... ! -samefile "$PIN"`, an inode comparison so the spelling
of the path cannot matter), and **skips itself entirely** when the pin cannot be
resolved: disk is cheap and the artifact is not reproducible, since its coverage
numbers depend on which bars IB served that week.

### Staleness now measures "has not been re-pinned"

A consequence worth stating rather than discovering. The KAN-56 staleness check
(exit **4** past `--max-baseline-age-days`, default 14) was written when the
monitor followed recency, so it meant *the weekly refresh has stopped producing
artifacts*. Under a pin the refresh no longer moves what the monitor reads, so
the same check now means *this pin has not been re-pinned in 14 days* — which for
a baseline deliberately fixed for the duration of an epoch is expected, not a
fault. Two things keep that from becoming nightly noise today: exit 3 outranks
exit 4, and the current pin is `coverage.state: BLOCKED` (see
[KAN-59](https://huiliang.atlassian.net/browse/KAN-59)), so the monitor exits 3
regardless. Revisit the threshold when the Rung-0 baseline is pinned for real
(P2-24); the refresh's own dead-man switch, not this check, is what covers "the
refresh died".

---

## Regenerating the headline baseline

Run these yourself; they need IB Gateway and write into `output/`.

```bash
# 1. Fetch bars for every historical member + the ETF sleeves.
#    Requires IB Gateway on 127.0.0.1:7497.
python scripts/run_backtest.py \
    --years 10 \
    --universe-snapshots data/universe/sp500_membership.json \
    --output-dir output

# 2. Refresh fundamentals / earnings caches if the universe changed.
python scripts/fetch_fundamentals.py
python scripts/fetch_earnings.py

# 3. Iterate without touching IB again, reusing the bars from step 1.
python scripts/run_backtest.py \
    --bars-from-json output/backtest_multi_<TIMESTAMP>.json \
    --universe-snapshots data/universe/sp500_membership.json \
    --output-dir output
```

The run prints a `SURVIVORSHIP BIASED` banner if `--universe-snapshots` is
omitted. Treat any headline number produced without it as indicative only.

### The weekly refresh passes the snapshot too

`deploy/launchd/run_backtest_refresh.sh` (Tuesdays 05:00 SGT) reruns the same
backtest so the baseline stays current. It passes `--universe-snapshots`, and
**aborts with exit 2 and a Telegram alert if the snapshot file is missing**
rather than running without it. Producing nothing is strictly better than
producing a survivorship-biased artifact. The pin has narrowed that guard's blast
radius without removing it: such an artifact no longer becomes the baseline of
record by being newest, but it still sits in `output/` looking like a candidate
at the next re-pin, and an operator who picks it reverts the monitor to exit 3 —
undoing the rebaseline, just later.

Exit codes: `1` = IB Gateway unreachable, `2` = membership snapshot missing,
`124` = exceeded the deadline and was killed, otherwise the backtest's own code.

**The job got roughly six times bigger.** `resolve_backtest_universe` returns
the 140-ticker sleeve union without a membership calendar and ~830 names with
one, and `run_backtest.py` issues one historical-data request per ticker-year —
so `--years 10` goes from ~1,400 requests to ~8,300. Against IB's pacing limits
that is hours, not minutes. The wrapper therefore bounds the run at
`ALGO_REFRESH_TIMEOUT_SECONDS` (default 6h) and kills it past that, alerting on
exit 124, so a runaway 05:00 SGT job cannot still be contending for the gateway
when the next day's 04:15 paper run starts. (There is no clientId collision —
the backtest uses 10, `run_paper` 58/59 — but they share one pacing budget.)
Record the measured wall-clock of the first full PIT run here and re-tune the
deadline if 6h turns out to be tight.

**This does not take effect until you redeploy.** `local.algo-backtest-refresh.plist`
runs `~/ibc/run_backtest_refresh.sh`, a deployed copy — merging the repo change
does nothing to Tuesday's job until `deploy/launchd/deploy.sh` runs. Until then
the job still omits `--universe-snapshots` and still has no snapshot guard; the
wrapper's drift check only writes a WARNING line into the log.

### Names IB cannot price

The point-in-time universe is only as good as the bars behind it, and the names
that fail to pull are disproportionately the delistings — exactly the ones that
removed the bias. `scripts/run_backtest.py` prints a `failed:` list at the end
of the fetch, and the artifact's `config.coverage.excluded_tickers` records the
membership-day cost of each one.

After a regeneration, record the exclusions here, per name, with their
membership-day cost — a bare percentage hides whether the 3% missing is one
long-lived name or forty brief ones:

| Ticker | Membership days lost | Why IB cannot price it |
|---|---|---|
| _(none recorded yet — fill in from the first PIT regeneration)_ | | |

Total excluded must stay at or below the 5.0% floor or the artifact reads
`coverage.state: BLOCKED` and the monitor exits 3.

### Comparing old to new

Expect the rebaselined numbers to be **worse** than the pre-2026-08-06 figures,
and the gap itself is the finding. The four effects, in rough order of size:

- next-open fills remove the same-bar entry edge and make every stop-out pay
  the gap, which is where the reported ~11.6% max drawdown was partly artifact;
- the point-in-time universe removes the winner pre-selection;
- the commission floor dominates small orders;
- the ddof=1 Sharpe is a few percent lower than the population form.

---

## Applying an ML signal filter

`--ml-filter` refuses to run a model over its own training window.
`scripts/train_signal_model.py` writes a `<model>.meta.json` sidecar recording
the training window and embargo, and the backtest requires `--start-date` to be
on or after `out_of_sample_from`:

```bash
python scripts/train_signal_model.py --results output/backtest_multi_<TS>.json
# -> data/models/signal_quality_model.txt
# -> data/models/signal_quality_model.meta.json  (out_of_sample_from: YYYY-MM-DD)

python scripts/run_backtest.py \
    --bars-from-json output/backtest_multi_<TS>.json \
    --universe-snapshots data/universe/sp500_membership.json \
    --ml-filter data/models/signal_quality_model.txt \
    --start-date <out_of_sample_from>
```

Without a sidecar, or with an overlapping window, the run exits with an error
naming the earliest date it would accept. The walk-forward evaluation inside
the trainer is purged by holding period and embargoed by 5 days, and reports
`purged` / `embargoed` counts per fold.

---

## After regenerating

1. Check `config.coverage.state` in the new artifact. If it is `BLOCKED`, the
   baseline is not usable — chase the bars for the tickers in
   `excluded_tickers` (highest membership-day counts first) and re-run.
2. Point the divergence monitor at the new file by re-pinning it — see *The
   pinned baseline of record* above; the monitor does **not** pick it up by being
   newest. Confirm the header no longer says `[NOT LIKE-FOR-LIKE]`.
3. Re-check the IPS's stated expectations against the new numbers — if the
   strategy's justification moved, the IPS has to move with it.
