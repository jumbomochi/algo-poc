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
| Fundamentals | available 45 days after period-end (or the row's `filing_date`) | A quarter ending 31 March is not public on 31 March. |

`config.fill_model`, `config.commission_minimum`,
`config.slippage_bps_by_ticker` and `config.point_in_time_universe` are written
into every results JSON. `scripts/divergence_monitor.py` reads them and reports
`NO_DATA` rather than a misleading OK/BREACH if the baseline is not
like-for-like. All three of next-open fills, a per-order commission floor **and**
a point-in-time universe are required — a re-run that fixed the fills and the
costs but kept the static ticker list is still inflated by winner
pre-selection, so it does not pass. Anything the config does not declare is
read as the unsafe value; absence is never a pass.

When the baseline fails that check the monitor exits **3** (not 0), and
`deploy/launchd/run_divergence.sh` logs it as `divergence monitor BLIND`. A
blind monitor is an outage, not a clean run.

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

**Sourcing the data.** The membership history is not in this repo — it has to
come from a data vendor or a reconstructed index-change list. Whatever the
source, record it in the file's `source` field. Two things must accompany it
for the equity sleeves to work properly on delisted names:

1. **Bars** for every ticker in `snapshots` (the run fetches
   `MembershipCalendar.all_tickers()`, which includes the delisted ones).
2. **Fundamentals and sector labels** for those names — `SECTOR_MAP` in
   `scripts/fetch_fundamentals.py` only covers the present-day top 100, so
   delisted names currently fall into the `Unknown` sector bucket and are
   grouped together by the sector-concentration limit. Extend `SECTOR_MAP`
   when the snapshot file lands.

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

1. Point the divergence monitor at the new file (it picks the latest
   `output/backtest_multi_*.json` automatically) and confirm the header no
   longer says `[NOT LIKE-FOR-LIKE]`.
2. Re-check the IPS's stated expectations against the new numbers — if the
   strategy's justification moved, the IPS has to move with it.
