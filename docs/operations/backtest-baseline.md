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
rather than running without it. That guard is the point: the monitor
auto-selects the *newest* `output/backtest_multi_*.json`, so one refresh
without the flag would write a survivorship-biased artifact that supersedes the
rebaselined one and revert the monitor to exit 3 — undoing the rebaseline
within a week, silently. Producing nothing is strictly better.

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

**Measured 2026-08-19 (KAN-52), the first PIT regeneration:**
`output/backtest_multi_20260819_183451.json`, 826 tickers requested,
**`coverage.state: BLOCKED`** — 142,856 of 1,265,893 membership-days excluded
(**11.28%** against the 5.0% floor) across **164 names**.

The dominant failure mode is not a stale symbol and not a missing
`primaryExchange`. IB keeps a contract record for a delisted name but attaches
it to the pseudo-exchange `VALUE`, and its historical service refuses that
exchange outright:

```
Error 200: No security definition has been found for the request,
           contract: Stock(symbol='AVB', exchange='SMART', currency='USD')
Error 162: No data of type EODChart is available for the exchange 'VALUE'
           and the security type 'Stock'
```

Both were reproduced by hand against the paper gateway on 2026-08-19. Neither
`SMART` routing, nor an explicit `conId`, nor `exchange='VALUE'` recovers a
single bar — the conId form qualifies the contract and *then* returns 162. So
**there is no contract-construction fix**: IB cannot supply delisted-name
history on this account, and a coverage-`OK` PIT baseline is unreachable from
IB alone. Closing the gap needs a survivorship-free vendor for the excluded
names, which is not yet ticketed.

| Failure mode | Names | Membership-days | Share of exclusion |
|---|---:|---:|---:|
| Error 200 — no active listing; resolves only on `VALUE` | 132 | 127,819 | 89.5% |
| Error 162 — resolves, HMDS returns no data | 24 | 14,809 | 10.4% |
| Partial window — priced for part of its membership only | 8 | 228 | 0.2% |

Per name, with their membership-day cost — a bare percentage hides whether the
3% missing is one long-lived name or forty brief ones:

| Ticker | Membership days lost | Why IB cannot price it |
|---|---:|---|
| `AVB` | 2,511 | no active listing — resolves only on `VALUE` (err 200) |
| `EQR` | 2,511 | no active listing — resolves only on `VALUE` (err 200) |
| `EA` | 2,509 | no active listing — resolves only on `VALUE` (err 200) |
| `BK` | 2,477 | no active listing — resolves only on `VALUE` (err 200) |
| `HOLX` | 2,477 | no active listing — resolves only on `VALUE` (err 200) |
| `IPG` | 2,354 | no active listing — resolves only on `VALUE` (err 200) |
| `K` | 2,354 | no active listing — resolves only on `VALUE` (err 200) |
| `HES` | 2,290 | no active listing — resolves only on `VALUE` (err 200) |
| `JNPR` | 2,290 | no active listing — resolves only on `VALUE` (err 200) |
| `WBA` | 2,290 | no active listing — resolves only on `VALUE` (err 200) |
| `DFS` | 2,226 | no active listing — resolves only on `VALUE` (err 200) |
| `MRO` | 2,104 | no active listing — resolves only on `VALUE` (err 200) |
| `ANSS` | 2,073 | no active listing — resolves only on `VALUE` (err 200) |
| `WRK` | 2,040 | no active listing — resolves only on `VALUE` (err 200) |
| `CMA` | 1,976 | no active listing — resolves only on `VALUE` (err 200) |
| `PXD` | 1,976 | no active listing — resolves only on `VALUE` (err 200) |
| `ATVI` | 1,852 | no active listing — resolves only on `VALUE` (err 200) |
| `SEE` | 1,852 | no active listing — resolves only on `VALUE` (err 200) |
| `ABC` | 1,789 | no active listing — resolves only on `VALUE` (err 200) |
| `COO` | 1,756 | resolves, HMDS returns no data (err 162) |
| `PKI` | 1,726 | no active listing — resolves only on `VALUE` (err 200) |
| `FBHS` | 1,602 | no active listing — resolves only on `VALUE` (err 200) |
| `NLSN` | 1,602 | no active listing — resolves only on `VALUE` (err 200) |
| `DISH` | 1,572 | no active listing — resolves only on `VALUE` (err 200) |
| `RE` | 1,572 | no active listing — resolves only on `VALUE` (err 200) |
| `CTXS` | 1,539 | no active listing — resolves only on `VALUE` (err 200) |
| `ANTM` | 1,475 | no active listing — resolves only on `VALUE` (err 200) |
| `BLL` | 1,475 | no active listing — resolves only on `VALUE` (err 200) |
| `CERN` | 1,475 | no active listing — resolves only on `VALUE` (err 200) |
| `DISCA` | 1,475 | no active listing — resolves only on `VALUE` (err 200) |
| `DISCK` | 1,475 | no active listing — resolves only on `VALUE` (err 200) |
| `FB` | 1,475 | resolves, HMDS returns no data (err 162) |
| `PBCT` | 1,475 | no active listing — resolves only on `VALUE` (err 200) |
| `FLT` | 1,445 | no active listing — resolves only on `VALUE` (err 200) |
| `GPS` | 1,413 | no active listing — resolves only on `VALUE` (err 200) |
| `WLTW` | 1,413 | no active listing — resolves only on `VALUE` (err 200) |
| `XLNX` | 1,413 | no active listing — resolves only on `VALUE` (err 200) |
| `COG` | 1,351 | no active listing — resolves only on `VALUE` (err 200) |
| `HBI` | 1,351 | no active listing — resolves only on `VALUE` (err 200) |
| `KSU` | 1,351 | no active listing — resolves only on `VALUE` (err 200) |
| `DRE` | 1,322 | no active listing — resolves only on `VALUE` (err 200) |
| `ALXN` | 1,287 | no active listing — resolves only on `VALUE` (err 200) |
| `LB` | 1,287 | resolves, HMDS returns no data (err 162) |
| `SIVB` | 1,260 | no active listing — resolves only on `VALUE` (err 200) |
| `FLIR` | 1,223 | no active listing — resolves only on `VALUE` (err 200) |
| `VAR` | 1,223 | no active listing — resolves only on `VALUE` (err 200) |
| `INFO` | 1,196 | resolves, HMDS returns no data (err 162) |
| `CXO` | 1,160 | no active listing — resolves only on `VALUE` (err 200) |
| `TIF` | 1,160 | no active listing — resolves only on `VALUE` (err 200) |
| `ABMD` | 1,134 | no active listing — resolves only on `VALUE` (err 200) |
| `TWTR` | 1,134 | no active listing — resolves only on `VALUE` (err 200) |
| `CTRA` | 1,126 | no active listing — resolves only on `VALUE` (err 200) |
| `ETFC` | 1,099 | no active listing — resolves only on `VALUE` (err 200) |
| `MYL` | 1,099 | no active listing — resolves only on `VALUE` (err 200) |
| `NBL` | 1,099 | no active listing — resolves only on `VALUE` (err 200) |
| `ODFL` | 1,093 | resolves, HMDS returns no data (err 162) |
| `FRC` | 1,071 | no active listing — resolves only on `VALUE` (err 200) |
| `PEAK` | 1,067 | no active listing — resolves only on `VALUE` (err 200) |
| `ADS` | 1,035 | no active listing — resolves only on `VALUE` (err 200) |
| `CTL` | 1,035 | no active listing — resolves only on `VALUE` (err 200) |
| `JWN` | 1,035 | no active listing — resolves only on `VALUE` (err 200) |
| `CTLT` | 1,005 | no active listing — resolves only on `VALUE` (err 200) |
| `AGN` | 971 | no active listing — resolves only on `VALUE` (err 200) |
| `RTN` | 971 | no active listing — resolves only on `VALUE` (err 200) |
| `UTX` | 971 | no active listing — resolves only on `VALUE` (err 200) |
| `XEC` | 908 | no active listing — resolves only on `VALUE` (err 200) |
| `ARNC` | 879 | no active listing — resolves only on `VALUE` (err 200) |
| `CBS` | 846 | no active listing — resolves only on `VALUE` (err 200) |
| `CELG` | 846 | no active listing — resolves only on `VALUE` (err 200) |
| `HCP` | 846 | no active listing — resolves only on `VALUE` (err 200) |
| `JEC` | 846 | no active listing — resolves only on `VALUE` (err 200) |
| `STI` | 846 | resolves, HMDS returns no data (err 162) |
| `SYMC` | 846 | no active listing — resolves only on `VALUE` (err 200) |
| `VIAB` | 846 | no active listing — resolves only on `VALUE` (err 200) |
| `APC` | 782 | resolves, HMDS returns no data (err 162) |
| `FL` | 782 | no active listing — resolves only on `VALUE` (err 200) |
| `RHT` | 782 | no active listing — resolves only on `VALUE` (err 200) |
| `TMK` | 782 | no active listing — resolves only on `VALUE` (err 200) |
| `TSS` | 782 | no active listing — resolves only on `VALUE` (err 200) |
| `NLOK` | 756 | no active listing — resolves only on `VALUE` (err 200) |
| `HFC` | 755 | no active listing — resolves only on `VALUE` (err 200) |
| `HRS` | 718 | no active listing — resolves only on `VALUE` (err 200) |
| `LLL` | 718 | no active listing — resolves only on `VALUE` (err 200) |
| `MXIM` | 693 | no active listing — resolves only on `VALUE` (err 200) |
| `ESRX` | 655 | no active listing — resolves only on `VALUE` (err 200) |
| `KORS` | 655 | no active listing — resolves only on `VALUE` (err 200) |
| `NFX` | 655 | resolves, HMDS returns no data (err 162) |
| `SCG` | 655 | no active listing — resolves only on `VALUE` (err 200) |
| `FOX` | 642 | resolves, HMDS returns no data (err 162) |
| `FOXA` | 642 | resolves, HMDS returns no data (err 162) |
| `CDAY` | 626 | no active listing — resolves only on `VALUE` (err 200) |
| `AET` | 594 | no active listing — resolves only on `VALUE` (err 200) |
| `CA` | 594 | no active listing — resolves only on `VALUE` (err 200) |
| `COL` | 594 | no active listing — resolves only on `VALUE` (err 200) |
| `PX` | 594 | no active listing — resolves only on `VALUE` (err 200) |
| `SRCL` | 594 | no active listing — resolves only on `VALUE` (err 200) |
| `NWL` | 580 | resolves, HMDS returns no data (err 162) |
| `VIAC` | 567 | no active listing — resolves only on `VALUE` (err 200) |
| `BHGE` | 566 | no active listing — resolves only on `VALUE` (err 200) |
| `DPS` | 531 | no active listing — resolves only on `VALUE` (err 200) |
| `GGP` | 531 | no active listing — resolves only on `VALUE` (err 200) |
| `XL` | 531 | no active listing — resolves only on `VALUE` (err 200) |
| `UAL` | 516 | resolves, HMDS returns no data (err 162) |
| `DAY` | 502 | no active listing — resolves only on `VALUE` (err 200) |
| `EVHC` | 502 | no active listing — resolves only on `VALUE` (err 200) |
| `CSRA` | 468 | resolves, HMDS returns no data (err 162) |
| `LUK` | 468 | no active listing — resolves only on `VALUE` (err 200) |
| `MON` | 468 | no active listing — resolves only on `VALUE` (err 200) |
| `TWX` | 468 | no active listing — resolves only on `VALUE` (err 200) |
| `WYN` | 468 | no active listing — resolves only on `VALUE` (err 200) |
| `DWDP` | 438 | no active listing — resolves only on `VALUE` (err 200) |
| `BCR` | 404 | no active listing — resolves only on `VALUE` (err 200) |
| `CBG` | 404 | no active listing — resolves only on `VALUE` (err 200) |
| `CHK` | 404 | no active listing — resolves only on `VALUE` (err 200) |
| `HCN` | 404 | no active listing — resolves only on `VALUE` (err 200) |
| `PCLN` | 404 | resolves, HMDS returns no data (err 162) |
| `PDCO` | 404 | no active listing — resolves only on `VALUE` (err 200) |
| `SNI` | 404 | no active listing — resolves only on `VALUE` (err 200) |
| `PARA` | 388 | resolves, HMDS returns no data (err 162) |
| `WCG` | 377 | no active listing — resolves only on `VALUE` (err 200) |
| `COH` | 343 | no active listing — resolves only on `VALUE` (err 200) |
| `DLPH` | 343 | no active listing — resolves only on `VALUE` (err 200) |
| `LVLT` | 343 | no active listing — resolves only on `VALUE` (err 200) |
| `XEL` | 343 | resolves, HMDS returns no data (err 162) |
| `PEP` | 336 | resolves, HMDS returns no data (err 162) |
| `PFG` | 334 | resolves, HMDS returns no data (err 162) |
| `BBBY` | 280 | no active listing — resolves only on `VALUE` (err 200) |
| `BHI` | 280 | no active listing — resolves only on `VALUE` (err 200) |
| `DOW` | 280 | resolves, HMDS returns no data (err 162) |
| `MNK` | 280 | no active listing — resolves only on `VALUE` (err 200) |
| `RAI` | 280 | no active listing — resolves only on `VALUE` (err 200) |
| `SPLS` | 280 | resolves, HMDS returns no data (err 162) |
| `TSO` | 280 | no active listing — resolves only on `VALUE` (err 200) |
| `WFM` | 280 | no active listing — resolves only on `VALUE` (err 200) |
| `DD` | 260 | resolves, HMDS returns no data (err 162) |
| `ANDV` | 251 | no active listing — resolves only on `VALUE` (err 200) |
| `DNB` | 217 | no active listing — resolves only on `VALUE` (err 200) |
| `MJN` | 217 | no active listing — resolves only on `VALUE` (err 200) |
| `SWN` | 217 | no active listing — resolves only on `VALUE` (err 200) |
| `TGNA` | 217 | no active listing — resolves only on `VALUE` (err 200) |
| `YHOO` | 217 | no active listing — resolves only on `VALUE` (err 200) |
| `IR` | 182 | partial window — 2329 bars (err none) |
| `ENDP` | 154 | no active listing — resolves only on `VALUE` (err 200) |
| `FTR` | 154 | no active listing — resolves only on `VALUE` (err 200) |
| `HAR` | 154 | no active listing — resolves only on `VALUE` (err 200) |
| `LLTC` | 154 | no active listing — resolves only on `VALUE` (err 200) |
| `SE` | 154 | resolves, HMDS returns no data (err 162) |
| `STJ` | 154 | no active listing — resolves only on `VALUE` (err 200) |
| `LM` | 92 | no active listing — resolves only on `VALUE` (err 200) |
| `Q` | 63 | resolves, HMDS returns no data (err 162) |
| `UA C` | 63 | no active listing — resolves only on `VALUE` (err 200) |
| `SATS` | 62 | no active listing — resolves only on `VALUE` (err 200) |
| `AA` | 40 | partial window — 2471 bars (err none) |
| `CPGX` | 29 | no active listing — resolves only on `VALUE` (err 200) |
| `DO` | 29 | no active listing — resolves only on `VALUE` (err 200) |
| `EMC` | 29 | resolves, HMDS returns no data (err 162) |
| `HOT` | 29 | no active listing — resolves only on `VALUE` (err 200) |
| `TYC` | 29 | no active listing — resolves only on `VALUE` (err 200) |
| `AVGO` | 1 | partial window — 2510 bars (err none) |
| `EXPD` | 1 | partial window — 2510 bars (err none) |
| `FI` | 1 | partial window — 2510 bars (err none) |
| `FITB` | 1 | partial window — 2510 bars (err none) |
| `LUMN` | 1 | partial window — 2508 bars (err none) |
| `SLG` | 1 | partial window — 2510 bars (err none) |

Total excluded must stay at or below the 5.0% floor or the artifact reads
`coverage.state: BLOCKED` and the monitor exits 3. **This regeneration is over
the floor, so it is not a baseline of record** — see
[rung0-economics.md](rung0-economics.md) §1 for what may and may not be quoted
from it.

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
2. Point the divergence monitor at the new file (it picks the latest
   `output/backtest_multi_*.json` automatically) and confirm the header no
   longer says `[NOT LIKE-FOR-LIKE]`.
3. Re-check the IPS's stated expectations against the new numbers — if the
   strategy's justification moved, the IPS has to move with it.
