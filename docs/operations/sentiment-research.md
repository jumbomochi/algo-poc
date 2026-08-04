# Sentiment Research Pipeline — Runbook

Spec: `docs/superpowers/specs/2026-08-02-social-sentiment-research-design.md`
This pipeline is research-only. It never touches the trading path.

## One-time setup

1. API keys (all free tiers):
   - Finnhub: register at finnhub.io -> `FINNHUB_API_KEY`
   - Reddit: create a "script" app at reddit.com/prefs/apps ->
     `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`
   - Discord: create a bot, invite it to permitted servers, list channel ids
     in `config/default.yaml` under `sentiment.discord.channel_ids`, set
     `sentiment.discord.enabled: true` -> `DISCORD_BOT_TOKEN`
2. Apply the migration (additive): `alembic upgrade head`
3. Copy `ops/launchd/com.algopoc.sentiment-collect.plist` to
   `~/Library/LaunchAgents/`, fill in the env vars, then:
   `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.algopoc.sentiment-collect.plist`

## Daily operation

- The job runs hourly; each cycle collects all enabled sources and rebuilds
  the last 5 sessions of `sentiment_daily`.
- Logs: `output/logs/sentiment-collect.log` (structured JSON).
- A failing source logs `source_fetch_failed` and leaves its cursor
  unadvanced — the gap stays visible and the evaluation masks it.
- Health check: `SELECT source, max(collected_at) FROM sentiment_messages
  GROUP BY source;` (psql against localhost:55432 for local compose override) — every enabled source should be < 2h old during
  US market hours.

## Evaluation (the pre-committed gates)

- **Social gate date: 2026-11 (3 months after collection start).** Run once:
  `python scripts/sentiment_eval.py --bars <bars cache> --json-out output/sentiment_eval_$(date +%Y%m%d).json`
- Gates (fixed in the spec before data was seen — do not tune):
  mean IC >= 0.03 with t >= 2 at any of horizons 1/3/5, OR event-study mean
  abnormal return >= 30 bps with p < 0.05 over n >= 30 events.
- Verdicts: PASS -> write the graduation design (separate spec);
  FAIL -> drop the source, or extend one quarter (at most once);
  INSUFFICIENT_DATA -> fix collection, extend the window.
- Exit code 2 = collection gaps exceeded 10% of the window for a source;
  investigate before trusting any verdict.
