# Social Sentiment Research Pipeline — Design

**Date:** 2026-08-02
**Status:** Approved design, pre-implementation
**Owner:** Huiliang

## Thesis and purpose

Traditional media cycles are getting slower and have a smaller impact on stock
pricing than before; fast, noisy social channels (Reddit, StockTwits, Discord)
may now carry the earlier, larger price-relevant signal. This project builds a
**research pipeline** to test that thesis empirically — it does **not** wire
anything into the trading path. Only a signal that passes pre-committed
statistical gates graduates (via a separate future design) into the sleeve
pipeline.

Key scoping decisions (made 2026-08-02):

| Decision | Choice |
|---|---|
| End use | Research first — collect, score, and validate before any trading integration |
| Sources | Reddit, StockTwits, Discord (social focus) + a **free news baseline** (Finnhub) as the slow-channel comparison series. Instagram dropped (no legitimate read API). Alpha Vantage (paid, quality) deferred until a gate passes. |
| Universe | The existing ~140-ticker watchlist the sleeves trade |
| Scoring | Provider scores where given + local model (VADER) for free text; raw text always stored so the archive can be re-scored later |
| Validation | Pre-committed IC + event study + social-vs-news lead/lag, evaluated once per schedule — no peeking-and-tweaking |
| Isolation | Zero changes to `services/*`, `run_paper.py`, `run_backtest.py`, or anything the live-ops test depends on |

## Why a standalone pipeline (approach decision)

Three approaches were considered:

- **A. Standalone collector + Postgres (chosen):** new isolated package,
  launchd-scheduled one-shot jobs, evaluation script modeled on
  `divergence_monitor.py`. Touches nothing live.
- **B. Microservice events path:** implement the stubbed
  `EventsSourceProtocol`, fix the broken events→signals deserialization, use
  `EventRecord`. Rejected for phase 1: the microservices don't trade today,
  the events hop needs repair before any data flows, and it means modifying
  running services mid-live-ops-test.
- **C. News-only historical study:** rejected as the whole of phase 1 because
  it cannot test the social thesis and every month without social collectors
  is unrecoverable history lost.

The social clock is the scarce resource — Discord/StockTwits history cannot be
bought later — so collection starts as early as possible.

## Architecture

New top-level `sentiment/` package (peer of `backtest/` — not under
`services/` because it is not a stream microservice), plus thin entry points
in `scripts/`:

```
sentiment/
  sources/
    base.py          # SentimentSourceProtocol + RawMessage dataclass
    finnhub_news.py  # free-tier news baseline (provider-scored where available)
    reddit.py        # PRAW; configurable subreddits
    stocktwits.py    # per-symbol public stream; native bullish/bearish tags
    discord.py       # bot REST history fetch by cursor (no live gateway)
  scoring.py         # local scoring, pluggable; VADER first
  tickers.py         # $TICKER / cashtag extraction from free text
  aggregate.py       # raw messages -> sentiment_daily, NYSE-close cutoff
  evaluation.py      # IC, event-study, lead/lag math
scripts/
  collect_sentiment.py   # one-shot poll across enabled sources (launchd)
  sentiment_eval.py      # runs the pre-committed gates; divergence_monitor style
tests/sentiment/
```

All collectors implement one protocol:

```python
class SentimentSourceProtocol(Protocol):
    name: str
    def fetch(self, tickers: list[str], since: datetime) -> list[RawMessage]: ...
```

Everything runs as **one-shot scheduled jobs** — no daemons. Discord reads
channel history via the bot REST API with a stored cursor rather than holding
a live gateway connection.

## Data model

One Alembic migration adding three tables (SQLAlchemy models in
`shared/models/sentiment.py`):

- **`sentiment_messages`** — the raw archive, append-only, never mutated:
  `id`, `source`, `source_id`, `ticker`, `author`, `text`, `url`,
  `posted_at`, `collected_at`, `provider_score` (nullable float),
  `local_score` (nullable float), `score_model` (nullable str), `meta` JSON.
  Unique constraint on `(source, source_id, ticker)` makes re-polls
  idempotent. `posted_at`/`collected_at` deliberately mirrors the repo's
  `effective_at`/`ingested_at` point-in-time convention — this is what makes
  any later backtest honest.
- **`sentiment_daily`** — per `(ticker, date, source)`: `message_count`,
  `mean_score`, `weighted_score`, `score_std`, `unique_authors`,
  `sentiment_zscore`, `volume_zscore` (both vs that ticker's trailing 60-day
  baseline). Volume z-score baselines are session-based with zero-fill for
  quiet sessions (a ticker's silence still counts as a real zero); score
  z-score baselines remain message-days only, since sentiment on a day with
  no mentions is undefined rather than zero (decision 2026-08-04, pre-data).
  Rebuilt from raw by `aggregate.py`; the day boundary is NYSE
  close via `shared/market_calendar.py`. Rebuildable at any time from
  `sentiment_messages`.
- **`sentiment_cursors`** — last-fetched position per source/channel, so
  collection resumes without gaps or duplicates.

## Collectors — source-specific notes

- **Finnhub news (baseline):** free tier (~60 calls/min) covers the 140
  tickers daily. Purpose is the slow-channel comparison series, not quality
  news analytics. The provider sits behind the protocol; Alpha Vantage
  (paid) slots in later without redesign if a gate passes and deeper history
  or better scoring is wanted.
- **Reddit:** PRAW with official API credentials. Subreddits configurable;
  initial set: wallstreetbets, stocks, investing. Tickers extracted by
  cashtag and word-boundary regex; one row per (message, ticker) pair.
  Generous API history → daily polling suffices.
- **StockTwits:** per-symbol public stream returns only the **last ~30
  messages per symbol**, so it needs roughly hourly polling during US market
  hours or data is lost. ~200 req/hr unauthenticated fits 140 tickers with
  staggering. Native bullish/bearish tags map to provider_score +1/−1.
- **Discord:** requires a user-supplied list of servers/channels where
  membership exists **and bot access is permitted** (open item below). Bot
  token + channel IDs in config; incremental history fetch by cursor. No
  pre-collection history exists — this source's clock starts when the bot
  lands.

Per-source failure isolation: a source that errors logs and is skipped that
cycle; other sources proceed. A failed cycle leaves the cursor unadvanced, so
gaps are visible in the data (see evaluation masking below).

## Scoring

- Provider scores stored as delivered (Finnhub sentiment where present;
  StockTwits bullish/bearish → +1/−1).
- Local scoring for free text (Reddit, Discord): **VADER at collect time** —
  instant, tiny dependency, good enough to detect whether signal exists.
- `score_model` records provenance per row, so a heavier model (e.g. FinBERT)
  can batch re-score the archive later without ambiguity. Raw text is always
  kept; no scoring decision is irreversible.

## Evaluation and pre-committed gates

`scripts/sentiment_eval.py`, modeled on `divergence_monitor.py` (console
table + JSON output, non-zero exit on data-collection gaps). Uses the same
bars as the backtest (`--bars-from-json` cache) — no new price source.

Three tests:

1. **Information coefficient:** Spearman rank correlation of day-t sentiment
   vs t+1 / t+3 / t+5 forward returns, per source and combined, with monthly
   breakdown and t-statistics.
2. **Event study:** spike defined as `sentiment_zscore > 2` **and**
   `volume_zscore > 2`; measure mean abnormal return (vs universe mean) over
   the next 1–5 days.
3. **Lead/lag (the thesis test):** cross-correlation of social sentiment vs
   the news baseline series per ticker — does social lead news, and does
   price react at the social spike or the news print?

Days inside a known collection gap for a source are masked from that source's
evaluation rather than treated as "no sentiment".

**Gates (pre-committed, evaluated once per schedule — no peeking-and-tweaking):**

- **Evaluation date:** after **3 months** of live social collection
  (evaluation window = full collection period, minus masked gaps).
- **Pass, per source:** mean IC ≥ 0.03 with t-stat ≥ 2 at any horizon, **or**
  event-study mean abnormal return ≥ 30 bps with p < 0.05 over n ≥ 30 events.
- **Fail:** the source is dropped, or collection is extended by one quarter —
  at most once.
- **On pass:** graduation to the trading path is a **separate design**
  (expected shape: `fetch_sentiment.py` cache + point-in-time lookup, the
  `fetch_earnings.py` pattern, into `run_paper.py`/`run_backtest.py`).
  Nothing in this project touches trading.

The lead/lag test has no gate; it is descriptive evidence for/against the
thesis and informs the graduation design.

## Operations

- **launchd jobs** (same pattern as the daily paper run and divergence
  monitor): hourly collection during US market hours (StockTwits constraint;
  other sources ride along harmlessly), one daily aggregation + gap-check
  run after the collection window.
- **Secrets** via env vars in the launchd plist — `FINNHUB_API_KEY`,
  `REDDIT_CLIENT_ID`/`REDDIT_CLIENT_SECRET`, `DISCORD_BOT_TOKEN`. Nothing
  committed.
- **Config**: a new `sentiment:` section in `config/default.yaml` — per-source
  enable flags, subreddit list, Discord channel IDs, poll cadence. Read by
  the scripts via the existing `shared/config.py` loader.
- Structured logging via `shared/logging.py`; Prometheus textfile metrics
  optional, not in phase 1.

## Testing

`tests/sentiment/` with pytest (`asyncio_mode = "auto"` per repo convention):

- Recorded API payload fixtures per source; no live network in tests.
- Ticker extraction: cashtags, word-boundary hits, false-positive traps
  ($A, "AI", "IT", common words that are also symbols).
- Dedup / idempotent re-poll against the unique constraint.
- Aggregation: point-in-time cutoffs at NYSE close, z-score baselines.
- Evaluation math validated on synthetic data with known correlations
  (planted IC, planted spike effects) — this also substitutes for the
  news-backfill harness shakeout that a paid provider would have offered.
- Cursor/gap behavior: failed cycle leaves cursor unadvanced; gap days masked
  in evaluation.

## Milestones

1. **M1 — Skeleton + news baseline:** package, migration, config,
   Finnhub collector, VADER scoring, aggregation, launchd job. Data flowing
   end-to-end for the watchlist.
2. **M2 — Social collectors:** Reddit + StockTwits (hourly), then Discord
   once servers/permissions are confirmed. Social clock starts here — this is
   the schedule-critical milestone.
3. **M3 — Evaluation harness:** `sentiment_eval.py` with IC, event study,
   lead/lag; validated on synthetic data; gap masking.
4. **M4 — Gate evaluation:** run after 3 months of social collection; outcome
   decides graduation design or source drops.

## Open items (user input needed before/during M2)

- **Discord server/channel list** where membership exists and bot access is
  permitted, plus bot creation on those servers.
- **Reddit API credentials** (script-type app on the Reddit account).
- **Finnhub API key** (free registration).

## Out of scope

- Any change to `services/*`, `run_paper.py`, `run_backtest.py`, risk, or
  execution.
- The graduation/integration design (separate spec if a gate passes).
- Alpha Vantage or other paid news providers (deferred until a gate passes).
- Instagram (no legitimate read API for third-party content).
- Intraday trading off sentiment (system trades daily bars; raw timestamps
  are kept so intraday research remains possible later).
