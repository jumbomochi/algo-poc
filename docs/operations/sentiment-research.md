# Sentiment Research Pipeline — Runbook

Spec: `docs/superpowers/specs/2026-08-02-social-sentiment-research-design.md`
This pipeline is research-only. It never touches the trading path.

## One-time setup

1. API keys (free tiers). Store each in 1Password, then mirror it into the
   login keychain, where the job reads it (the same model as every other
   launchd job; the plist carries no secrets):
   `deploy/launchd/secrets.sh --import --only FINNHUB_API_KEY`
   - Finnhub: register at finnhub.io -> `FINNHUB_API_KEY`. This is the one
     source that can collect today.
   - Reddit: create a "script" app at reddit.com/prefs/apps ->
     `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`.
   - Discord: not wired to the keychain. It stays off until there are
     servers and channels with bot permission.
   - StockTwits is **off** (`config/default.yaml`). Its public API sits behind
     a Cloudflare challenge and returned 403 to any non-browser client from
     this host on 2026-10-18.
   A source whose key is absent is skipped and named in the log; the job
   exits 1 only when no source has a key.
2. The migration is already part of `alembic upgrade head`.
3. Release as usual (pull the deploy clone, then `deploy/launchd/deploy.sh`,
   which copies `run_sentiment_collect.sh` to `~/ibc` and
   `local.algo-sentiment-collect.plist` to `~/Library/LaunchAgents`), then
   load the job yourself:
   `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.algo-sentiment-collect.plist`
   Until it is loaded, `deploy.sh` and the daily pipeline report list it as
   NOT LOADED.

### First-run verification

After bootstrapping, wait for the first hourly cycle (or run
`~/ibc/run_sentiment_collect.sh` once by hand) and confirm in
`~/ibc/logs/sentiment-collect.log`:

- the wrapper's line `keys present: FINNHUB_API_KEY ...` names the keys you
  imported;
- a `collection_cycle_done` line follows, with a non-zero `finnhub_news`
  count during US market days;
- no `source_fetch_failed` for an enabled source.

## Recovery after an outage

Raw messages survive gaps (a stopped launchd job just means a longer `since`
window on the next run), but the hourly job only rebuilds the last 5
sessions of `sentiment_daily` (`--aggregate-days 5`), so a longer outage
leaves a hole in the aggregate table that the routine cycle will never
backfill on its own. After downtime, rerun the aggregation with a window
wide enough to cover the hole:
`python scripts/collect_sentiment.py --aggregate-days <N>` where `<N>` is
at least the number of days of the outage plus a few days of padding.

## Daily operation

- The job runs hourly; each cycle collects all enabled sources and rebuilds
  the last 5 sessions of `sentiment_daily`.
- Logs: `~/ibc/logs/sentiment-collect.log` (structured JSON).
- A failing source logs `source_fetch_failed` and leaves its cursor
  unadvanced — the gap stays visible and the evaluation masks it.
- Health check: `SELECT source, max(collected_at) FROM sentiment_messages
  GROUP BY source;` (psql against localhost:55432 for local compose override) — every enabled source should be < 2h old during
  US market hours.

## Evaluation (the pre-committed gates)

- **Gate date: 3 months after collection actually starts** (the first
  `collection_cycle_done` with stored messages). The spec's 2026-11 assumed an
  August start that never happened: the job was not installed until 2026-10.
  Run once:
  `python scripts/sentiment_eval.py --bars <bars cache> --json-out output/sentiment_eval_$(date +%Y%m%d).json`
- Gates (fixed in the spec before data was seen — do not tune):
  mean IC >= 0.03 with t >= 2 at any of horizons 1/3/5, OR event-study mean
  abnormal return >= 30 bps with p < 0.05 over n >= 30 events.
- Verdicts: PASS -> write the graduation design (separate spec);
  FAIL -> drop the source, or extend one quarter (at most once);
  INSUFFICIENT_DATA -> fix collection, extend the window.
- Exit code 2 = collection gaps exceeded 10% of the window for a source;
  investigate before trusting any verdict.
