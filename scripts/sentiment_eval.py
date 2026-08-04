#!/usr/bin/env python3
"""Sentiment research evaluation — the pre-committed gates.

Per enabled source: information coefficient (Spearman, horizons 1/3/5),
spike event study, gap report, and the social-vs-news lead/lag comparison.
Gates are constants below, fixed in the design doc BEFORE data was seen
(docs/superpowers/specs/2026-08-02-social-sentiment-research-design.md).
Do not tune them to the data.

Usage:
    python scripts/sentiment_eval.py --bars output/bars_cache.json
    python scripts/sentiment_eval.py --bars ... --json-out output/sentiment_eval.json

Exit codes: 0 = evaluated; 2 = a source's collection-gap fraction > 10%.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone

import pandas as pd
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from sentiment.evaluation import (
    EventStudyResult,
    ICResult,
    event_study,
    information_coefficient,
    lead_lag,
)
from shared.config import load_config
from shared.models import SentimentDaily

# --- Pre-committed gates (spec 2026-08-02). Do not tune. ---
GATE_MIN_IC = 0.03
GATE_MIN_TSTAT = 2.0
GATE_MIN_ABNORMAL = 0.003  # 30 bps
GATE_MAX_P = 0.05
GATE_MIN_EVENTS = 30
GATE_MIN_SESSIONS = 40  # below this, verdict is INSUFFICIENT_DATA
GAP_FRACTION_LIMIT = 0.10

SOCIAL_SOURCES = ["reddit", "stocktwits", "discord"]
NEWS_SOURCE = "finnhub_news"


@dataclass
class SourceVerdict:
    source: str
    ic_results: list[ICResult]
    event_results: list[EventStudyResult]
    gap_fraction: float
    verdict: str


def load_daily(session: Session, source: str, score_column: str = "weighted_score") -> pd.DataFrame:
    rows = session.execute(
        select(SentimentDaily).where(SentimentDaily.source == source)
    ).scalars().all()
    records = [
        {
            "ticker": r.ticker,
            "session_date": r.session_date,
            "score": getattr(r, score_column),
            "sentiment_zscore": r.sentiment_zscore,
            "volume_zscore": r.volume_zscore,
        }
        for r in rows
    ]
    df = pd.DataFrame(records, columns=["ticker", "session_date", "score", "sentiment_zscore", "volume_zscore"])
    return df.dropna(subset=["score"])


def load_bars_json(path: str) -> dict[str, list[dict]]:
    with open(path) as f:
        cached = json.load(f)
    return {
        ticker: [{"date": date.fromisoformat(b["date"]), "close": b["close"]} for b in bars]
        for ticker, bars in (cached.get("bars") or {}).items()
    }


def gap_report(daily: pd.DataFrame, sessions: list[date]) -> tuple[int, float]:
    """Sessions in the eval window with zero rows — collection outages."""
    if not sessions:
        return 0, 0.0
    covered = set(daily["session_date"]) if len(daily) else set()
    gaps = [s for s in sessions if s not in covered]
    return len(gaps), len(gaps) / len(sessions)


def judge(
    ic_results: list[ICResult],
    event_results: list[EventStudyResult],
    n_sessions_with_data: int,
) -> str:
    if n_sessions_with_data < GATE_MIN_SESSIONS:
        return "INSUFFICIENT_DATA"
    ic_pass = any(
        r.mean_ic >= GATE_MIN_IC and r.t_stat >= GATE_MIN_TSTAT for r in ic_results
    )
    event_pass = any(
        r.n_events >= GATE_MIN_EVENTS
        and r.mean_abnormal_return >= GATE_MIN_ABNORMAL
        and r.p_value < GATE_MAX_P
        for r in event_results
    )
    return "PASS" if (ic_pass or event_pass) else "FAIL"


def evaluate_source(session: Session, source: str, bars: dict[str, list[dict]]) -> SourceVerdict:
    daily = load_daily(session, source)
    all_sessions = sorted({d for bars_ in bars.values() for d in (b["date"] for b in bars_)})
    if len(daily):
        window = [
            s for s in all_sessions
            if daily["session_date"].min() <= s <= daily["session_date"].max()
        ]
    else:
        window = []
    _, gap_fraction = gap_report(daily, window)
    if len(daily) == 0:
        return SourceVerdict(source, [], [], gap_fraction, "INSUFFICIENT_DATA")
    ic_results = information_coefficient(daily, bars)
    event_results = event_study(daily, bars)
    n_sessions = daily["session_date"].nunique()
    return SourceVerdict(
        source, ic_results, event_results, gap_fraction, judge(ic_results, event_results, n_sessions)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars", required=True, help="backtest bars cache JSON")
    parser.add_argument("--config", default="config/default.yaml")
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args()

    config = load_config(args.config)
    engine = create_engine(config.database.url)
    bars = load_bars_json(args.bars)

    verdicts: list[SourceVerdict] = []
    lead_lag_rows: list[dict] = []
    with Session(engine) as session:
        for source in SOCIAL_SOURCES + [NEWS_SOURCE]:
            verdicts.append(evaluate_source(session, source, bars))
        news_daily = load_daily(session, NEWS_SOURCE)
        for source in SOCIAL_SOURCES:
            social_daily = load_daily(session, source)
            if len(social_daily) and len(news_daily):
                for lc in lead_lag(social_daily, news_daily):
                    lead_lag_rows.append(
                        {"source": source, "lag_days": lc.lag_days, "correlation": lc.correlation}
                    )

    print(f"{'source':<14} {'verdict':<18} {'gap%':>6}  IC(h=1/3/5, t-stat)  events")
    for v in verdicts:
        ics = "  ".join(f"{r.mean_ic:+.3f}(t={r.t_stat:.1f})" for r in v.ic_results) or "-"
        events = "  ".join(
            f"h{r.horizon}:n={r.n_events},ar={r.mean_abnormal_return:+.4f},p={r.p_value:.3f}"
            for r in v.event_results
        ) or "-"
        print(f"{v.source:<14} {v.verdict:<18} {v.gap_fraction:>5.1%}  {ics}  {events}")
    if lead_lag_rows:
        print("\nlead/lag (positive lag = social leads news):")
        for row in lead_lag_rows:
            print(f"  {row['source']:<12} lag={row['lag_days']:+d}  corr={row['correlation']:+.3f}")

    if args.json_out:
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "gates": {
                "min_ic": GATE_MIN_IC,
                "min_tstat": GATE_MIN_TSTAT,
                "min_abnormal": GATE_MIN_ABNORMAL,
                "max_p": GATE_MAX_P,
                "min_events": GATE_MIN_EVENTS,
            },
            "verdicts": [asdict(v) for v in verdicts],
            "lead_lag": lead_lag_rows,
        }
        with open(args.json_out, "w") as f:
            json.dump(payload, f, indent=2, default=str)

    if any(v.gap_fraction > GAP_FRACTION_LIMIT for v in verdicts if v.verdict != "INSUFFICIENT_DATA"):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
