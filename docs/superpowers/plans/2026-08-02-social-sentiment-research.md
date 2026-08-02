# Social Sentiment Research Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the standalone social-sentiment research pipeline specified in `docs/superpowers/specs/2026-08-02-social-sentiment-research-design.md` — collectors (Finnhub news baseline, Reddit, StockTwits, Discord), Postgres archive, VADER scoring, daily aggregation, and an IC / event-study / lead-lag evaluation harness with pre-committed gates.

**Architecture:** New top-level `sentiment/` package (peer of `backtest/`) with one-shot collectors behind a common protocol, three new DB tables (`sentiment_messages` raw archive, `sentiment_daily` aggregates, `sentiment_cursors`), and two entry-point scripts (`scripts/collect_sentiment.py` for launchd, `scripts/sentiment_eval.py` for the gates). Nothing under `services/`, `scripts/run_paper.py`, or `scripts/run_backtest.py` is touched.

**Tech Stack:** Python 3.12, SQLAlchemy 2 + Alembic, httpx, PRAW (Reddit), vaderSentiment, pandas/numpy/scipy, pytest.

## Global Constraints

- **Never modify** `services/*`, `scripts/run_paper.py`, `scripts/run_backtest.py`, risk, or execution code — this pipeline is research-only (spec: "Out of scope").
- All new modules start with `from __future__ import annotations`.
- All datetimes stored/compared are **timezone-aware UTC**; session dates are NYSE sessions via `shared/market_calendar.MarketCalendar`.
- Tests are offline: recorded fixture payloads, fake clients, in-memory SQLite (`create_engine("sqlite:///:memory:")` + `Base.metadata.create_all`). No live network in tests.
- Secrets come **only** from env vars: `FINNHUB_API_KEY`, `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT`, `DISCORD_BOT_TOKEN`. Never committed, never in YAML.
- The universe is the active-sleeve watchlist: `shared.universe.get_union_universe(shared.universe.ACTIVE_SLEEVES)`.
- Pre-committed gates (do not tune): mean IC ≥ 0.03 with t-stat ≥ 2 at any of horizons 1/3/5, OR event-study mean abnormal return ≥ 30 bps (0.003) with p < 0.05 over n ≥ 30 events. Spike = sentiment z > 2 AND volume z > 2 vs trailing 60-day baseline.
- The Alembic migration is **additive only** (three new tables). No destructive SQL anywhere; `sentiment_daily` is upserted, never deleted.
- Run the full test suite with plain `pytest`; run task-specific tests with the commands given per step.

---

### Task 1: SQLAlchemy models + Alembic migration

**Files:**
- Create: `shared/models/sentiment.py`
- Modify: `shared/models/__init__.py` (register 3 models)
- Modify: `pyproject.toml` (add `"sentiment"` to hatch wheel packages — the package itself arrives in Task 3, registering now avoids a second pyproject edit)
- Create: `migrations/versions/<generated>_add_sentiment_research_tables.py`
- Test: `tests/sentiment/__init__.py`, `tests/sentiment/test_models.py`

**Interfaces:**
- Consumes: `shared.models.base.Base`
- Produces: ORM classes `SentimentMessage`, `SentimentDaily`, `SentimentCursor` importable from `shared.models`. Column names exactly as written below (later tasks depend on `meta`, `posted_at`, `collected_at`, `provider_score`, `local_score`, `score_model`, `session_date`, `sentiment_zscore`, `volume_zscore`, `key`, `position`).

- [ ] **Step 1: Write the failing test**

Create `tests/sentiment/__init__.py` (empty) and `tests/sentiment/test_models.py`:

```python
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shared.models import Base, SentimentDaily, SentimentMessage, SentimentCursor

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session


def make_message(source_id: str = "m1", ticker: str = "AAPL") -> SentimentMessage:
    return SentimentMessage(
        source="stocktwits",
        source_id=source_id,
        ticker=ticker,
        author="trader_joe",
        text="$AAPL to the moon",
        url="https://stocktwits.com/x/1",
        posted_at=NOW,
        collected_at=NOW,
        provider_score=1.0,
        local_score=0.6,
        score_model="vader",
        meta={"likes": 3},
    )


def test_sentiment_message_roundtrip(session):
    session.add(make_message())
    session.commit()
    row = session.query(SentimentMessage).one()
    assert row.ticker == "AAPL"
    assert row.meta == {"likes": 3}
    assert row.provider_score == 1.0


def test_sentiment_message_unique_constraint(session):
    session.add(make_message())
    session.commit()
    session.add(make_message())  # same (source, source_id, ticker)
    with pytest.raises(IntegrityError):
        session.commit()


def test_same_message_different_ticker_allowed(session):
    session.add(make_message(ticker="AAPL"))
    session.add(make_message(ticker="MSFT"))
    session.commit()
    assert session.query(SentimentMessage).count() == 2


def test_sentiment_daily_unique_constraint(session):
    def make_daily():
        return SentimentDaily(
            ticker="AAPL",
            session_date=date(2026, 8, 3),
            source="reddit",
            message_count=5,
            mean_score=0.2,
            weighted_score=0.3,
            score_std=0.1,
            unique_authors=4,
            sentiment_zscore=None,
            volume_zscore=None,
            computed_at=NOW,
        )

    session.add(make_daily())
    session.commit()
    session.add(make_daily())
    with pytest.raises(IntegrityError):
        session.commit()


def test_cursor_roundtrip(session):
    session.add(SentimentCursor(key="discord:123", position=NOW.isoformat(), updated_at=NOW))
    session.commit()
    row = session.get(SentimentCursor, "discord:123")
    assert row.position == NOW.isoformat()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/sentiment/test_models.py -v`
Expected: FAIL with `ImportError: cannot import name 'SentimentDaily' from 'shared.models'`

- [ ] **Step 3: Write the models**

Create `shared/models/sentiment.py`:

```python
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base


class SentimentMessage(Base):
    """Raw sentiment archive — append-only, never mutated.

    posted_at/collected_at mirror the repo's effective_at/ingested_at
    point-in-time convention so later backtests stay honest.
    """

    __tablename__ = "sentiment_messages"
    __table_args__ = (
        UniqueConstraint("source", "source_id", "ticker", name="uq_sentiment_message"),
        Index("ix_sentiment_messages_ticker_posted", "ticker", "posted_at"),
        Index("ix_sentiment_messages_source_posted", "source", "posted_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    source_id: Mapped[str] = mapped_column(String(120), nullable=False)
    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    author: Mapped[str | None] = mapped_column(String(100), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    provider_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    local_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    score_model: Mapped[str | None] = mapped_column(String(50), nullable=True)
    meta: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class SentimentDaily(Base):
    """Per (ticker, NYSE session, source) aggregate. Rebuildable from raw."""

    __tablename__ = "sentiment_daily"
    __table_args__ = (
        UniqueConstraint("ticker", "session_date", "source", name="uq_sentiment_daily"),
        Index("ix_sentiment_daily_source_date", "source", "session_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False)
    mean_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    weighted_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    score_std: Mapped[float | None] = mapped_column(Float, nullable=True)
    unique_authors: Mapped[int] = mapped_column(Integer, nullable=False)
    sentiment_zscore: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_zscore: Mapped[float | None] = mapped_column(Float, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SentimentCursor(Base):
    """Last-fetched position per source (or per source:channel for Discord).

    position is an ISO-8601 UTC datetime string; a failed cycle leaves it
    unadvanced so collection gaps stay visible.
    """

    __tablename__ = "sentiment_cursors"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    position: Mapped[str] = mapped_column(String(50), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

In `shared/models/__init__.py` add the import (alphabetical with the others) and the three names to `__all__`:

```python
from shared.models.sentiment import SentimentCursor, SentimentDaily, SentimentMessage
```

In `pyproject.toml`, change the hatch packages line to:

```toml
[tool.hatch.build.targets.wheel]
packages = ["shared", "services", "backtest", "sentiment"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/sentiment/test_models.py -v`
Expected: 5 PASS

- [ ] **Step 5: Write the migration**

Get the current head first:

Run: `alembic heads`
Expected: a single revision id — call it `<HEAD>` below.

Create `migrations/versions/a1b2c3d4e5f6_add_sentiment_research_tables.py` (generate the filename's revision id with `python -c "import uuid; print(uuid.uuid4().hex[:12])"` and use it consistently):

```python
"""add sentiment research tables

Additive only: three new tables for the social-sentiment research pipeline
(docs/superpowers/specs/2026-08-02-social-sentiment-research-design.md).
No existing table is touched.

Revision ID: <GENERATED>
Revises: <HEAD>
Create Date: 2026-08-02

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "<GENERATED>"
down_revision: Union[str, Sequence[str], None] = "<HEAD>"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sentiment_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("source_id", sa.String(120), nullable=False),
        sa.Column("ticker", sa.String(10), nullable=False),
        sa.Column("author", sa.String(100), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("url", sa.String(500), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_score", sa.Float(), nullable=True),
        sa.Column("local_score", sa.Float(), nullable=True),
        sa.Column("score_model", sa.String(50), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=False),
        sa.UniqueConstraint("source", "source_id", "ticker", name="uq_sentiment_message"),
    )
    op.create_index(
        "ix_sentiment_messages_ticker_posted",
        "sentiment_messages",
        ["ticker", "posted_at"],
    )
    op.create_index(
        "ix_sentiment_messages_source_posted",
        "sentiment_messages",
        ["source", "posted_at"],
    )
    op.create_table(
        "sentiment_daily",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ticker", sa.String(10), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("message_count", sa.Integer(), nullable=False),
        sa.Column("mean_score", sa.Float(), nullable=True),
        sa.Column("weighted_score", sa.Float(), nullable=True),
        sa.Column("score_std", sa.Float(), nullable=True),
        sa.Column("unique_authors", sa.Integer(), nullable=False),
        sa.Column("sentiment_zscore", sa.Float(), nullable=True),
        sa.Column("volume_zscore", sa.Float(), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("ticker", "session_date", "source", name="uq_sentiment_daily"),
    )
    op.create_index(
        "ix_sentiment_daily_source_date",
        "sentiment_daily",
        ["source", "session_date"],
    )
    op.create_table(
        "sentiment_cursors",
        sa.Column("key", sa.String(120), primary_key=True),
        sa.Column("position", sa.String(50), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("sentiment_cursors")
    op.drop_table("sentiment_daily")
    op.drop_table("sentiment_messages")
```

- [ ] **Step 6: Apply the migration (additive only) and verify**

Run: `alembic upgrade head`
Expected: `Running upgrade <HEAD> -> <GENERATED>, add sentiment research tables`

Run: `alembic heads`
Expected: the new revision id, single head.

- [ ] **Step 7: Run the full suite and commit**

Run: `pytest tests/sentiment/ tests/shared/ -q`
Expected: all pass.

```bash
git add shared/models/sentiment.py shared/models/__init__.py pyproject.toml migrations/versions/ tests/sentiment/
git commit -m "feat: add sentiment research tables (messages, daily, cursors)"
```

---

### Task 2: Config section

**Files:**
- Modify: `shared/config.py` (add config models + wire into `AppConfig`)
- Modify: `config/default.yaml` (add `sentiment:` section)
- Test: `tests/sentiment/test_config.py`

**Interfaces:**
- Produces: `AppConfig.sentiment: SentimentConfig` with fields `finnhub_news.enabled`, `stocktwits.enabled`, `reddit.enabled/.subreddits/.posts_per_subreddit`, `discord.enabled/.channel_ids`, `zscore_baseline_days`, `zscore_min_baseline_days`. Scripts in Tasks 12–13 read exactly these names.

- [ ] **Step 1: Write the failing test**

Create `tests/sentiment/test_config.py`:

```python
from __future__ import annotations

from shared.config import AppConfig, load_config


def test_sentiment_defaults():
    config = AppConfig()
    assert config.sentiment.finnhub_news.enabled is False
    assert config.sentiment.reddit.subreddits == ["wallstreetbets", "stocks", "investing"]
    assert config.sentiment.reddit.posts_per_subreddit == 100
    assert config.sentiment.discord.channel_ids == []
    assert config.sentiment.zscore_baseline_days == 60
    assert config.sentiment.zscore_min_baseline_days == 20


def test_default_yaml_enables_phase1_sources():
    config = load_config("config/default.yaml")
    assert config.sentiment.finnhub_news.enabled is True
    assert config.sentiment.stocktwits.enabled is True
    assert config.sentiment.reddit.enabled is True
    # Discord waits for a confirmed server/channel list (spec open item)
    assert config.sentiment.discord.enabled is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/sentiment/test_config.py -v`
Expected: FAIL with `AttributeError: 'AppConfig' object has no attribute 'sentiment'`

- [ ] **Step 3: Implement**

In `shared/config.py`, after `SignalsConfig`, add:

```python
class SentimentSourceConfig(BaseModel):
    enabled: bool = False


class RedditSentimentConfig(SentimentSourceConfig):
    subreddits: list[str] = Field(
        default_factory=lambda: ["wallstreetbets", "stocks", "investing"]
    )
    posts_per_subreddit: int = 100


class DiscordSentimentConfig(SentimentSourceConfig):
    # Channel ids only — the bot token comes from DISCORD_BOT_TOKEN env.
    channel_ids: list[str] = Field(default_factory=list)


class SentimentConfig(BaseModel):
    finnhub_news: SentimentSourceConfig = Field(default_factory=SentimentSourceConfig)
    stocktwits: SentimentSourceConfig = Field(default_factory=SentimentSourceConfig)
    reddit: RedditSentimentConfig = Field(default_factory=RedditSentimentConfig)
    discord: DiscordSentimentConfig = Field(default_factory=DiscordSentimentConfig)
    zscore_baseline_days: int = 60
    zscore_min_baseline_days: int = 20
```

In `AppConfig`, after the `signals` field, add:

```python
    sentiment: SentimentConfig = Field(default_factory=SentimentConfig)
```

In `config/default.yaml`, after the `signals:` section, add:

```yaml
sentiment:
  finnhub_news:
    enabled: true
  stocktwits:
    enabled: true
  reddit:
    enabled: true
    subreddits: [wallstreetbets, stocks, investing]
    posts_per_subreddit: 100
  discord:
    enabled: false   # flip on once servers/channels are confirmed
    channel_ids: []
  zscore_baseline_days: 60
  zscore_min_baseline_days: 20
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/sentiment/test_config.py tests/shared/ -q`
Expected: all pass (existing config tests must not break).

- [ ] **Step 5: Commit**

```bash
git add shared/config.py config/default.yaml tests/sentiment/test_config.py
git commit -m "feat: add sentiment config section"
```

---

### Task 3: Package skeleton — RawMessage, source protocol, ticker extraction

**Files:**
- Create: `sentiment/__init__.py` (empty), `sentiment/sources/__init__.py` (empty)
- Create: `sentiment/sources/base.py`
- Create: `sentiment/tickers.py`
- Test: `tests/sentiment/test_tickers.py`

**Interfaces:**
- Produces:
  - `RawMessage(source, source_id, ticker, text, posted_at, author=None, url=None, provider_score=None, meta={})` — frozen dataclass, `posted_at` tz-aware UTC.
  - `SentimentSourceProtocol` with attribute `name: str` and method `fetch(tickers: list[str], since: datetime) -> list[RawMessage]`.
  - `extract_tickers(text: str, universe: set[str]) -> set[str]`.

- [ ] **Step 1: Write the failing test**

Create `tests/sentiment/test_tickers.py`:

```python
from __future__ import annotations

from sentiment.tickers import extract_tickers

UNIVERSE = {"AAPL", "TSLA", "NVDA", "IT", "NOW", "ALL", "V", "MA", "KO", "LOW", "ICE", "TAN", "HACK", "BRK B"}


def test_cashtag_hits():
    assert extract_tickers("$TSLA and $aapl look strong", UNIVERSE) == {"TSLA", "AAPL"}


def test_cashtag_outside_universe_ignored():
    assert extract_tickers("$GME squeeze!", UNIVERSE) == set()


def test_bare_symbol_in_universe():
    assert extract_tickers("NVDA earnings tomorrow", UNIVERSE) == {"NVDA"}


def test_bare_lowercase_not_matched():
    assert extract_tickers("nvda earnings tomorrow", UNIVERSE) == set()


def test_ambiguous_words_need_cashtag():
    # IT/NOW/ALL/LOW/ICE/TAN/HACK are real tickers but also common words:
    # bare mentions are ignored, cashtags still count.
    assert extract_tickers("IT is ALL over NOW, buy LOW", UNIVERSE) == set()
    assert extract_tickers("$NOW crushed earnings", UNIVERSE) == {"NOW"}


def test_single_letter_needs_cashtag():
    assert extract_tickers("V for victory", UNIVERSE) == set()
    assert extract_tickers("long $V and $MA", UNIVERSE) == {"V", "MA"}


def test_no_false_positive_on_substrings():
    assert extract_tickers("SNOWFLAKE is not NOW", UNIVERSE) == set()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/sentiment/test_tickers.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sentiment'`

- [ ] **Step 3: Implement**

Create `sentiment/sources/base.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class RawMessage:
    """One (message, ticker) pair from any source. posted_at is tz-aware UTC."""

    source: str
    source_id: str
    ticker: str
    text: str
    posted_at: datetime
    author: str | None = None
    url: str | None = None
    provider_score: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class SentimentSourceProtocol(Protocol):
    name: str

    def fetch(self, tickers: list[str], since: datetime) -> list[RawMessage]: ...
```

Create `sentiment/tickers.py`:

```python
from __future__ import annotations

import re

# Tickers that are also common English words/acronyms: a bare uppercase
# mention is almost always the word, so they only count as cashtags.
# Single-letter tickers (V, ...) are handled by the len >= 2 rule below.
AMBIGUOUS_TICKERS = {
    "AI", "ALL", "AN", "AT", "BE", "CAN", "DO", "GO", "IT", "LOW",
    "NOW", "ON", "ONE", "OR", "SO", "TAN", "UPS", "ICE", "HACK",
    "HERO", "LIT", "GE", "KO", "MA", "MO", "PM", "SH",
}

_CASHTAG_RE = re.compile(r"\$([A-Za-z]{1,5})\b")
_BARE_RE = re.compile(r"\b([A-Z]{2,5})\b")


def extract_tickers(text: str, universe: set[str]) -> set[str]:
    """Extract watchlist tickers from free text.

    Cashtags ($TSLA, case-insensitive) match any universe ticker. Bare
    uppercase words match only unambiguous universe tickers of length >= 2.
    """
    found: set[str] = set()
    for match in _CASHTAG_RE.finditer(text):
        symbol = match.group(1).upper()
        if symbol in universe:
            found.add(symbol)
    for match in _BARE_RE.finditer(text):
        symbol = match.group(1)
        if symbol in universe and symbol not in AMBIGUOUS_TICKERS:
            found.add(symbol)
    return found
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/sentiment/test_tickers.py -v`
Expected: 7 PASS

- [ ] **Step 5: Commit**

```bash
git add sentiment/ tests/sentiment/test_tickers.py
git commit -m "feat: sentiment package skeleton — RawMessage protocol and ticker extraction"
```

---

### Task 4: VADER scoring

**Files:**
- Modify: `pyproject.toml` (add dependencies `vaderSentiment`, `praw`, `scipy` — praw/scipy are consumed by Tasks 8 and 11 but one dependency install beats three)
- Create: `sentiment/scoring.py`
- Test: `tests/sentiment/test_scoring.py`

**Interfaces:**
- Produces: `VaderScorer` with attribute `model_name: str = "vader"` and method `score(text: str) -> float` (compound score in [-1, 1]).

- [ ] **Step 1: Add dependencies and install**

In `pyproject.toml` `dependencies`, add:

```toml
    "vaderSentiment>=3.3,<4.0",
    "praw>=7.7,<8.0",
    "scipy>=1.11,<2.0",
```

Run: `pip install -e ".[dev]"`
Expected: installs cleanly.

- [ ] **Step 2: Write the failing test**

Create `tests/sentiment/test_scoring.py`:

```python
from __future__ import annotations

from sentiment.scoring import VaderScorer


def test_positive_text_scores_positive():
    scorer = VaderScorer()
    assert scorer.score("Amazing earnings, this stock is a huge winner!") > 0.3


def test_negative_text_scores_negative():
    scorer = VaderScorer()
    assert scorer.score("Terrible guidance, total disaster, selling everything") < -0.3


def test_neutral_text_scores_near_zero():
    scorer = VaderScorer()
    assert abs(scorer.score("The company reported quarterly results")) < 0.3


def test_model_name():
    assert VaderScorer().model_name == "vader"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/sentiment/test_scoring.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sentiment.scoring'`

- [ ] **Step 4: Implement**

Create `sentiment/scoring.py`:

```python
from __future__ import annotations

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer


class VaderScorer:
    """Local sentiment scoring. score() returns VADER's compound in [-1, 1].

    model_name is persisted per row (score_model column) so a heavier model
    can re-score the archive later without provenance ambiguity.
    """

    model_name = "vader"

    def __init__(self) -> None:
        self._analyzer = SentimentIntensityAnalyzer()

    def score(self, text: str) -> float:
        return self._analyzer.polarity_scores(text)["compound"]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/sentiment/test_scoring.py -v`
Expected: 4 PASS

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml sentiment/scoring.py tests/sentiment/test_scoring.py
git commit -m "feat: VADER local sentiment scoring"
```

---

### Task 5: Store — idempotent inserts + cursors

**Files:**
- Create: `sentiment/store.py`
- Test: `tests/sentiment/test_store.py`

**Interfaces:**
- Consumes: `RawMessage` (Task 3), `VaderScorer`-shaped scorer (Task 4: `.score(text)`, `.model_name`), ORM models (Task 1).
- Produces:
  - `store_messages(session, messages: list[RawMessage], scorer) -> int` — inserts new rows (local-scores every message), skips existing `(source, source_id, ticker)`, commits, returns inserted count.
  - `get_cursor(session, key: str, default: datetime) -> datetime`
  - `set_cursor(session, key: str, position: datetime) -> None` (commits)

- [ ] **Step 1: Write the failing test**

Create `tests/sentiment/test_store.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from sentiment.sources.base import RawMessage
from sentiment.store import get_cursor, set_cursor, store_messages
from shared.models import Base, SentimentMessage

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


class FakeScorer:
    model_name = "fake"

    def score(self, text: str) -> float:
        return 0.5


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session


def make_raw(source_id: str = "m1", provider_score: float | None = None) -> RawMessage:
    return RawMessage(
        source="stocktwits",
        source_id=source_id,
        ticker="AAPL",
        text="great quarter",
        posted_at=NOW,
        author="joe",
        provider_score=provider_score,
        meta={"likes": 2},
    )


def test_store_inserts_and_scores(session):
    n = store_messages(session, [make_raw(provider_score=1.0)], FakeScorer())
    assert n == 1
    row = session.query(SentimentMessage).one()
    assert row.provider_score == 1.0
    assert row.local_score == 0.5
    assert row.score_model == "fake"
    assert row.collected_at is not None


def test_store_is_idempotent(session):
    store_messages(session, [make_raw()], FakeScorer())
    n = store_messages(session, [make_raw(), make_raw("m2")], FakeScorer())
    assert n == 1
    assert session.query(SentimentMessage).count() == 2


def test_cursor_default_then_roundtrip(session):
    default = datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert get_cursor(session, "reddit", default) == default
    set_cursor(session, "reddit", NOW)
    assert get_cursor(session, "reddit", default) == NOW
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/sentiment/test_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sentiment.store'`

- [ ] **Step 3: Implement**

Create `sentiment/store.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from sentiment.sources.base import RawMessage
from shared.models import SentimentCursor, SentimentMessage


def store_messages(session: Session, messages: list[RawMessage], scorer) -> int:
    """Insert new messages, skipping any existing (source, source_id, ticker).

    Every message gets a local score (cheap) alongside any provider score.
    """
    inserted = 0
    now = datetime.now(timezone.utc)
    for msg in messages:
        exists = session.execute(
            select(SentimentMessage.id).where(
                SentimentMessage.source == msg.source,
                SentimentMessage.source_id == msg.source_id,
                SentimentMessage.ticker == msg.ticker,
            )
        ).first()
        if exists:
            continue
        session.add(
            SentimentMessage(
                source=msg.source,
                source_id=msg.source_id,
                ticker=msg.ticker,
                author=msg.author,
                text=msg.text,
                url=msg.url,
                posted_at=msg.posted_at,
                collected_at=now,
                provider_score=msg.provider_score,
                local_score=scorer.score(msg.text),
                score_model=scorer.model_name,
                meta=dict(msg.meta),
            )
        )
        inserted += 1
    session.commit()
    return inserted


def get_cursor(session: Session, key: str, default: datetime) -> datetime:
    row = session.get(SentimentCursor, key)
    if row is None:
        return default
    return datetime.fromisoformat(row.position)


def set_cursor(session: Session, key: str, position: datetime) -> None:
    row = session.get(SentimentCursor, key)
    now = datetime.now(timezone.utc)
    if row is None:
        session.add(SentimentCursor(key=key, position=position.isoformat(), updated_at=now))
    else:
        row.position = position.isoformat()
        row.updated_at = now
    session.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/sentiment/test_store.py -v`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add sentiment/store.py tests/sentiment/test_store.py
git commit -m "feat: idempotent sentiment store with per-source cursors"
```

---

### Task 6: Finnhub news collector (the slow-channel baseline)

**Files:**
- Create: `sentiment/sources/finnhub_news.py`
- Test: `tests/sentiment/test_finnhub_news.py`

**Interfaces:**
- Consumes: `RawMessage` (Task 3).
- Produces: `FinnhubNewsSource(api_key: str, http_client=None)` with `name = "finnhub_news"`, implementing `fetch(tickers, since)`. Free-tier company-news has **no sentiment field** → `provider_score=None`; VADER (via the store) scores headline+summary.

- [ ] **Step 1: Write the failing test**

Create `tests/sentiment/test_finnhub_news.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone

from sentiment.sources.finnhub_news import FinnhubNewsSource

SINCE = datetime(2026, 8, 1, tzinfo=timezone.utc)

PAYLOAD = [
    {
        "category": "company",
        "datetime": 1785657600,  # 2026-08-02 08:00:00 UTC
        "headline": "Apple beats on earnings",
        "id": 999001,
        "related": "AAPL",
        "source": "Reuters",
        "summary": "Strong iPhone quarter.",
        "url": "https://example.com/apple",
    },
    {
        "category": "company",
        "datetime": 1785024000,  # 2026-07-26 — before `since`, must be dropped
        "headline": "Old news",
        "id": 999000,
        "related": "AAPL",
        "source": "Reuters",
        "summary": "",
        "url": "https://example.com/old",
    },
]


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    def __init__(self, payload):
        self._payload = payload
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, params))
        return FakeResponse(self._payload)


def test_fetch_maps_payload_and_filters_since():
    client = FakeClient(PAYLOAD)
    source = FinnhubNewsSource(api_key="k", http_client=client)
    msgs = source.fetch(["AAPL"], since=SINCE)
    assert len(msgs) == 1
    msg = msgs[0]
    assert msg.source == "finnhub_news"
    assert msg.source_id == "999001"
    assert msg.ticker == "AAPL"
    assert "Apple beats on earnings" in msg.text
    assert "Strong iPhone quarter." in msg.text
    assert msg.provider_score is None
    assert msg.posted_at == datetime(2026, 8, 2, 8, 0, tzinfo=timezone.utc)


def test_symbol_mapping_for_class_shares():
    client = FakeClient([])
    source = FinnhubNewsSource(api_key="k", http_client=client)
    source.fetch(["BRK B"], since=SINCE)
    _, params = client.calls[0]
    assert params["symbol"] == "BRK.B"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/sentiment/test_finnhub_news.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

Create `sentiment/sources/finnhub_news.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from sentiment.sources.base import RawMessage


class FinnhubNewsSource:
    """Company news via Finnhub /company-news. Free tier: ~60 calls/min.

    The free response carries no sentiment score, so provider_score is None
    and the store's local scorer rates headline+summary.
    """

    name = "finnhub_news"
    BASE_URL = "https://finnhub.io/api/v1/company-news"

    def __init__(self, api_key: str, http_client=None) -> None:
        self._api_key = api_key
        self._client = http_client or httpx.Client(timeout=30)

    def fetch(self, tickers: list[str], since: datetime) -> list[RawMessage]:
        out: list[RawMessage] = []
        today = datetime.now(timezone.utc).date()
        for ticker in tickers:
            symbol = ticker.replace(" ", ".")  # "BRK B" -> "BRK.B"
            resp = self._client.get(
                self.BASE_URL,
                params={
                    "symbol": symbol,
                    "from": since.date().isoformat(),
                    "to": today.isoformat(),
                    "token": self._api_key,
                },
            )
            resp.raise_for_status()
            for item in resp.json():
                posted = datetime.fromtimestamp(item["datetime"], tz=timezone.utc)
                if posted < since:
                    continue
                text = item["headline"]
                if item.get("summary"):
                    text = f"{text}. {item['summary']}"
                out.append(
                    RawMessage(
                        source=self.name,
                        source_id=str(item["id"]),
                        ticker=ticker,
                        text=text,
                        posted_at=posted,
                        url=item.get("url"),
                        provider_score=None,
                        meta={"news_source": item.get("source")},
                    )
                )
        return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/sentiment/test_finnhub_news.py -v`
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add sentiment/sources/finnhub_news.py tests/sentiment/test_finnhub_news.py
git commit -m "feat: Finnhub company-news collector (slow-channel baseline)"
```

---

### Task 7: StockTwits collector

**Files:**
- Create: `sentiment/sources/stocktwits.py`
- Test: `tests/sentiment/test_stocktwits.py`

**Interfaces:**
- Consumes: `RawMessage` (Task 3).
- Produces: `StockTwitsSource(http_client=None)` with `name = "stocktwits"`, implementing `fetch(tickers, since)`. Native Bullish/Bearish tags map to `provider_score` ±1.0; untagged → None. `meta["likes"]` feeds the weighted aggregate. 404 symbols are skipped silently (ETFs missing from StockTwits must not kill the cycle).

- [ ] **Step 1: Write the failing test**

Create `tests/sentiment/test_stocktwits.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone

from sentiment.sources.stocktwits import StockTwitsSource

SINCE = datetime(2026, 8, 1, tzinfo=timezone.utc)

PAYLOAD = {
    "messages": [
        {
            "id": 555001,
            "body": "$AAPL breaking out",
            "created_at": "2026-08-02T14:30:00Z",
            "user": {"username": "bulls_r_us"},
            "entities": {"sentiment": {"basic": "Bullish"}},
            "likes": {"total": 7},
        },
        {
            "id": 555002,
            "body": "$AAPL no opinion",
            "created_at": "2026-08-02T15:00:00Z",
            "user": {"username": "neutral_nick"},
            "entities": {"sentiment": None},
        },
        {
            "id": 555000,
            "body": "old message",
            "created_at": "2026-07-20T10:00:00Z",
            "user": {"username": "old_timer"},
            "entities": {"sentiment": {"basic": "Bearish"}},
        },
    ]
}


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    def __init__(self, responses):
        # responses: dict url-substring -> FakeResponse
        self._responses = responses
        self.calls = []

    def get(self, url, params=None):
        self.calls.append(url)
        for fragment, response in self._responses.items():
            if fragment in url:
                return response
        return FakeResponse({}, status_code=404)


def test_fetch_maps_sentiment_and_filters_since():
    client = FakeClient({"AAPL.json": FakeResponse(PAYLOAD)})
    source = StockTwitsSource(http_client=client)
    msgs = source.fetch(["AAPL"], since=SINCE)
    assert len(msgs) == 2
    bullish = next(m for m in msgs if m.source_id == "555001")
    assert bullish.provider_score == 1.0
    assert bullish.author == "bulls_r_us"
    assert bullish.meta["likes"] == 7
    untagged = next(m for m in msgs if m.source_id == "555002")
    assert untagged.provider_score is None


def test_unknown_symbol_404_is_skipped():
    client = FakeClient({"AAPL.json": FakeResponse(PAYLOAD)})
    source = StockTwitsSource(http_client=client)
    msgs = source.fetch(["ZZZZ", "AAPL"], since=SINCE)
    assert {m.ticker for m in msgs} == {"AAPL"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/sentiment/test_stocktwits.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

Create `sentiment/sources/stocktwits.py`:

```python
from __future__ import annotations

from datetime import datetime

import httpx

from sentiment.sources.base import RawMessage

_SENTIMENT_MAP = {"Bullish": 1.0, "Bearish": -1.0}


class StockTwitsSource:
    """Per-symbol public stream. Returns only the last ~30 messages per
    symbol, so this source needs roughly hourly polling during US market
    hours (launchd job in Task 14). ~200 req/hr unauthenticated.
    """

    name = "stocktwits"
    BASE_URL = "https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"

    def __init__(self, http_client=None) -> None:
        self._client = http_client or httpx.Client(timeout=30)

    def fetch(self, tickers: list[str], since: datetime) -> list[RawMessage]:
        out: list[RawMessage] = []
        for ticker in tickers:
            symbol = ticker.replace(" ", ".")
            resp = self._client.get(self.BASE_URL.format(symbol=symbol))
            if resp.status_code == 404:  # symbol not on StockTwits
                continue
            resp.raise_for_status()
            for item in resp.json().get("messages", []):
                posted = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
                if posted <= since:
                    continue
                sentiment = (item.get("entities") or {}).get("sentiment") or {}
                likes = (item.get("likes") or {}).get("total", 0)
                out.append(
                    RawMessage(
                        source=self.name,
                        source_id=str(item["id"]),
                        ticker=ticker,
                        text=item["body"],
                        posted_at=posted,
                        author=(item.get("user") or {}).get("username"),
                        url=f"https://stocktwits.com/message/{item['id']}",
                        provider_score=_SENTIMENT_MAP.get(sentiment.get("basic")),
                        meta={"likes": likes},
                    )
                )
        return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/sentiment/test_stocktwits.py -v`
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add sentiment/sources/stocktwits.py tests/sentiment/test_stocktwits.py
git commit -m "feat: StockTwits collector with native bullish/bearish tags"
```

---

### Task 8: Reddit collector

**Files:**
- Create: `sentiment/sources/reddit.py`
- Test: `tests/sentiment/test_reddit.py`

**Interfaces:**
- Consumes: `RawMessage`, `extract_tickers` (Task 3).
- Produces: `RedditSource(reddit, subreddits: list[str], posts_per_subreddit: int = 100)` with `name = "reddit"`, implementing `fetch(tickers, since)`. `reddit` is a PRAW `Reddit` instance (duck-typed for tests: `.subreddit(name).new(limit=n)` yielding objects with `.id .title .selftext .author .permalink .score .created_utc`). One `RawMessage` per (post, extracted ticker).

- [ ] **Step 1: Write the failing test**

Create `tests/sentiment/test_reddit.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from sentiment.sources.reddit import RedditSource

SINCE = datetime(2026, 8, 1, tzinfo=timezone.utc)


def make_post(post_id, title, selftext, created_utc, score=10):
    return SimpleNamespace(
        id=post_id,
        title=title,
        selftext=selftext,
        author="deep_value",
        permalink=f"/r/stocks/comments/{post_id}/x/",
        score=score,
        created_utc=created_utc,
    )


class FakeSubreddit:
    def __init__(self, posts):
        self._posts = posts

    def new(self, limit=100):
        return iter(self._posts[:limit])


class FakeReddit:
    def __init__(self, posts_by_sub):
        self._posts_by_sub = posts_by_sub

    def subreddit(self, name):
        return FakeSubreddit(self._posts_by_sub[name])


def test_fetch_extracts_tickers_and_filters_since():
    posts = [
        make_post("p1", "$TSLA and NVDA both printing", "", 1785657600),  # 2026-08-02
        make_post("p2", "old $TSLA post", "", 1785024000),  # 2026-07-26 — dropped
        make_post("p3", "no tickers here", "just vibes", 1785657600),
    ]
    reddit = FakeReddit({"stocks": posts})
    source = RedditSource(reddit, subreddits=["stocks"])
    msgs = source.fetch(["TSLA", "NVDA"], since=SINCE)
    assert {(m.source_id, m.ticker) for m in msgs} == {("p1", "TSLA"), ("p1", "NVDA")}
    msg = msgs[0]
    assert msg.source == "reddit"
    assert msg.meta["subreddit"] == "stocks"
    assert msg.meta["likes"] == 10
    assert msg.url.startswith("https://reddit.com/")
    assert msg.provider_score is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/sentiment/test_reddit.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

Create `sentiment/sources/reddit.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone

from sentiment.sources.base import RawMessage
from sentiment.tickers import extract_tickers


class RedditSource:
    """New posts from configured subreddits via a PRAW Reddit instance.

    Reddit keeps deep listing history, so daily polling is enough; the
    posts_per_subreddit limit bounds each cycle.
    """

    name = "reddit"

    def __init__(self, reddit, subreddits: list[str], posts_per_subreddit: int = 100) -> None:
        self._reddit = reddit
        self._subreddits = subreddits
        self._limit = posts_per_subreddit

    def fetch(self, tickers: list[str], since: datetime) -> list[RawMessage]:
        universe = set(tickers)
        out: list[RawMessage] = []
        for sub_name in self._subreddits:
            for post in self._reddit.subreddit(sub_name).new(limit=self._limit):
                posted = datetime.fromtimestamp(post.created_utc, tz=timezone.utc)
                if posted <= since:
                    continue
                text = post.title
                if post.selftext:
                    text = f"{post.title}\n{post.selftext}"
                for ticker in extract_tickers(text, universe):
                    out.append(
                        RawMessage(
                            source=self.name,
                            source_id=post.id,
                            ticker=ticker,
                            text=text,
                            posted_at=posted,
                            author=str(post.author) if post.author else None,
                            url=f"https://reddit.com{post.permalink}",
                            provider_score=None,
                            meta={"subreddit": sub_name, "likes": post.score},
                        )
                    )
        return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/sentiment/test_reddit.py -v`
Expected: 1 PASS

- [ ] **Step 5: Commit**

```bash
git add sentiment/sources/reddit.py tests/sentiment/test_reddit.py
git commit -m "feat: Reddit collector over configured subreddits"
```

---

### Task 9: Discord collector

**Files:**
- Create: `sentiment/sources/discord.py`
- Test: `tests/sentiment/test_discord.py`

**Interfaces:**
- Consumes: `RawMessage`, `extract_tickers` (Task 3).
- Produces: `DiscordSource(bot_token: str, channel_ids: list[str], http_client=None)` with `name = "discord"`, implementing `fetch(tickers, since)`. Also `snowflake_for(dt: datetime) -> str` (module function). `source_id` is `"{channel_id}:{message_id}"` for global uniqueness. Paginates with `after=` snowflakes until a short page.

- [ ] **Step 1: Write the failing test**

Create `tests/sentiment/test_discord.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone

from sentiment.sources.discord import DiscordSource, snowflake_for

SINCE = datetime(2026, 8, 1, tzinfo=timezone.utc)

PAGE = [
    {
        "id": "1400000000000000001",
        "content": "loading up on $NVDA calls",
        "timestamp": "2026-08-02T13:00:00+00:00",
        "author": {"username": "gamma_gang"},
    },
    {
        "id": "1400000000000000002",
        "content": "nothing ticker related",
        "timestamp": "2026-08-02T13:05:00+00:00",
        "author": {"username": "lurker"},
    },
]


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


class FakeClient:
    def __init__(self, pages):
        self._pages = list(pages)
        self.calls = []

    def get(self, url, params=None, headers=None):
        self.calls.append((url, params, headers))
        return FakeResponse(self._pages.pop(0) if self._pages else [])


def test_snowflake_roundtrip_ordering():
    early = snowflake_for(datetime(2026, 8, 1, tzinfo=timezone.utc))
    late = snowflake_for(datetime(2026, 8, 2, tzinfo=timezone.utc))
    assert int(late) > int(early)


def test_fetch_extracts_tickers_and_authenticates():
    client = FakeClient([PAGE])
    source = DiscordSource("tok123", channel_ids=["999"], http_client=client)
    msgs = source.fetch(["NVDA"], since=SINCE)
    assert len(msgs) == 1
    msg = msgs[0]
    assert msg.source_id == "999:1400000000000000001"
    assert msg.ticker == "NVDA"
    assert msg.author == "gamma_gang"
    _, params, headers = client.calls[0]
    assert headers["Authorization"] == "Bot tok123"
    assert params["after"] == snowflake_for(SINCE)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/sentiment/test_discord.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

Create `sentiment/sources/discord.py`:

```python
from __future__ import annotations

from datetime import datetime

import httpx

from sentiment.sources.base import RawMessage
from sentiment.tickers import extract_tickers

_DISCORD_EPOCH_MS = 1_420_070_400_000  # 2015-01-01T00:00:00Z
_PAGE_SIZE = 100


def snowflake_for(dt: datetime) -> str:
    """Discord snowflake whose timestamp component equals dt."""
    return str((int(dt.timestamp() * 1000) - _DISCORD_EPOCH_MS) << 22)


class DiscordSource:
    """Incremental channel-history reads via the bot REST API — one-shot,
    no live gateway connection. Only usable on channels where the bot is
    a permitted member (spec open item: user-supplied channel list).
    """

    name = "discord"
    BASE_URL = "https://discord.com/api/v10"

    def __init__(self, bot_token: str, channel_ids: list[str], http_client=None) -> None:
        self._headers = {"Authorization": f"Bot {bot_token}"}
        self._channel_ids = channel_ids
        self._client = http_client or httpx.Client(timeout=30)

    def fetch(self, tickers: list[str], since: datetime) -> list[RawMessage]:
        universe = set(tickers)
        out: list[RawMessage] = []
        for channel_id in self._channel_ids:
            after = snowflake_for(since)
            while True:
                resp = self._client.get(
                    f"{self.BASE_URL}/channels/{channel_id}/messages",
                    params={"after": after, "limit": _PAGE_SIZE},
                    headers=self._headers,
                )
                resp.raise_for_status()
                batch = sorted(resp.json(), key=lambda m: int(m["id"]))
                if not batch:
                    break
                for item in batch:
                    posted = datetime.fromisoformat(item["timestamp"])
                    for ticker in extract_tickers(item.get("content", ""), universe):
                        out.append(
                            RawMessage(
                                source=self.name,
                                source_id=f"{channel_id}:{item['id']}",
                                ticker=ticker,
                                text=item["content"],
                                posted_at=posted,
                                author=(item.get("author") or {}).get("username"),
                                provider_score=None,
                                meta={"channel_id": channel_id},
                            )
                        )
                after = batch[-1]["id"]
                if len(batch) < _PAGE_SIZE:
                    break
        return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/sentiment/test_discord.py -v`
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add sentiment/sources/discord.py tests/sentiment/test_discord.py
git commit -m "feat: Discord channel-history collector (REST, cursor-driven)"
```

---

### Task 10: Daily aggregation

**Files:**
- Create: `sentiment/aggregate.py`
- Test: `tests/sentiment/test_aggregate.py`

**Interfaces:**
- Consumes: ORM models (Task 1), `shared.market_calendar.MarketCalendar`.
- Produces:
  - `session_date_for(posted_at: datetime, cal: MarketCalendar) -> date` — the NYSE session a message informs: the session whose close is the **next close at or after** `posted_at` (a Friday-evening post maps to Monday; an after-close Tuesday post maps to Wednesday). This is the look-ahead guard.
  - `best_score(provider_score, local_score) -> float | None` — provider preferred.
  - `rebuild_daily(session, cal, start: date, end: date, baseline_days: int = 60, min_baseline_days: int = 20) -> int` — upserts `SentimentDaily` rows for sessions in `[start, end]` (never deletes), then fills `sentiment_zscore`/`volume_zscore` vs each row's trailing baseline; returns upserted row count. Weighted score weights each message by `1 + meta.get("likes", 0)`.

- [ ] **Step 1: Write the failing test**

Create `tests/sentiment/test_aggregate.py`:

```python
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from sentiment.aggregate import best_score, rebuild_daily, session_date_for
from shared.market_calendar import MarketCalendar
from shared.models import Base, SentimentDaily, SentimentMessage

CAL = MarketCalendar()


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session


def add_message(session, ticker, posted_at, provider=None, local=None, likes=0, author="a", source_id=None):
    session.add(
        SentimentMessage(
            source="stocktwits",
            source_id=source_id or f"{ticker}-{posted_at.isoformat()}-{author}",
            ticker=ticker,
            author=author,
            text="x",
            posted_at=posted_at,
            collected_at=posted_at,
            provider_score=provider,
            local_score=local,
            score_model="vader",
            meta={"likes": likes},
        )
    )
    session.commit()


def test_session_date_intraday():
    # Tuesday 2026-08-04 14:00 UTC (10:00 ET, market open) -> same session
    dt = datetime(2026, 8, 4, 14, 0, tzinfo=timezone.utc)
    assert session_date_for(dt, CAL) == date(2026, 8, 4)


def test_session_date_after_close_rolls_forward():
    # Tuesday 2026-08-04 21:00 UTC (17:00 ET, after close) -> Wednesday
    dt = datetime(2026, 8, 4, 21, 0, tzinfo=timezone.utc)
    assert session_date_for(dt, CAL) == date(2026, 8, 5)


def test_session_date_weekend_rolls_to_monday():
    # Saturday 2026-08-01 -> Monday 2026-08-03
    dt = datetime(2026, 8, 1, 15, 0, tzinfo=timezone.utc)
    assert session_date_for(dt, CAL) == date(2026, 8, 3)


def test_best_score_prefers_provider():
    assert best_score(1.0, 0.2) == 1.0
    assert best_score(None, 0.2) == 0.2
    assert best_score(None, None) is None


def test_rebuild_daily_aggregates(session):
    morning = datetime(2026, 8, 4, 14, 0, tzinfo=timezone.utc)
    add_message(session, "AAPL", morning, provider=1.0, likes=4, author="a")
    add_message(session, "AAPL", morning, provider=None, local=-1.0, likes=0, author="b")
    n = rebuild_daily(session, CAL, date(2026, 8, 4), date(2026, 8, 4))
    assert n == 1
    row = session.query(SentimentDaily).one()
    assert row.ticker == "AAPL"
    assert row.session_date == date(2026, 8, 4)
    assert row.message_count == 2
    assert row.unique_authors == 2
    assert row.mean_score == pytest.approx(0.0)  # (1.0 + -1.0) / 2
    # weights: 1+4=5 for +1.0, 1+0=1 for -1.0 -> (5 - 1) / 6
    assert row.weighted_score == pytest.approx(4 / 6)
    assert row.sentiment_zscore is None  # not enough baseline days


def test_rebuild_daily_is_upsert_not_duplicate(session):
    morning = datetime(2026, 8, 4, 14, 0, tzinfo=timezone.utc)
    add_message(session, "AAPL", morning, provider=1.0)
    rebuild_daily(session, CAL, date(2026, 8, 4), date(2026, 8, 4))
    rebuild_daily(session, CAL, date(2026, 8, 4), date(2026, 8, 4))
    assert session.query(SentimentDaily).count() == 1


def test_zscore_with_planted_baseline(session):
    # ~22 quiet trading days with mild variance (constant baselines have
    # std == 0, which by design yields z-score None), then a loud +1.0 day x5
    sessions = [d for d in (date(2026, 6, 1 + i) for i in range(0, 30))
                if CAL.is_trading_day(d)]
    for i, d in enumerate(sessions):
        dt = datetime(d.year, d.month, d.day, 15, 0, tzinfo=timezone.utc)
        quiet_score = 0.1 if i % 2 == 0 else -0.1
        add_message(session, "AAPL", dt, provider=quiet_score, author=f"u{i}", source_id=f"q{i}")
        if i % 2 == 0:  # vary volume too: alternate 1 vs 2 messages/day
            add_message(session, "AAPL", dt, provider=quiet_score, author=f"u{i}b", source_id=f"q{i}b")
    loud_day = date(2026, 7, 6)  # Monday
    assert CAL.is_trading_day(loud_day)
    for i in range(5):
        dt = datetime(2026, 7, 6, 15, 0, tzinfo=timezone.utc)
        add_message(session, "AAPL", dt, provider=1.0, author=f"loud{i}", source_id=f"l{i}")
    assert len(sessions) >= 20  # enough baseline rows for min_baseline_days
    rebuild_daily(session, CAL, sessions[0], loud_day, baseline_days=60, min_baseline_days=20)
    loud = (
        session.query(SentimentDaily)
        .filter(SentimentDaily.session_date == loud_day)
        .one()
    )
    assert loud.volume_zscore is not None and loud.volume_zscore > 2
    assert loud.sentiment_zscore is not None and loud.sentiment_zscore > 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/sentiment/test_aggregate.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

Create `sentiment/aggregate.py`:

```python
from __future__ import annotations

import statistics
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.market_calendar import MarketCalendar
from shared.models import SentimentDaily, SentimentMessage

ET = ZoneInfo("America/New_York")


def session_date_for(posted_at: datetime, cal: MarketCalendar) -> date:
    """The NYSE session this message can inform: the session whose close is
    the next close at or after posted_at. After-close and weekend messages
    roll forward — the look-ahead guard for later backtests."""
    probe = posted_at
    close = cal.get_next_market_close(probe)
    while close < posted_at:
        probe = probe + timedelta(days=1)
        close = cal.get_next_market_close(probe)
    return close.astimezone(ET).date()


def best_score(provider_score: float | None, local_score: float | None) -> float | None:
    return provider_score if provider_score is not None else local_score


def _zscore(value: float, baseline: list[float], min_n: int) -> float | None:
    if len(baseline) < min_n:
        return None
    mean = statistics.fmean(baseline)
    std = statistics.pstdev(baseline)
    if std == 0:
        return None
    return (value - mean) / std


def rebuild_daily(
    session: Session,
    cal: MarketCalendar,
    start: date,
    end: date,
    baseline_days: int = 60,
    min_baseline_days: int = 20,
) -> int:
    """Upsert SentimentDaily for sessions in [start, end]; never deletes.

    Messages are pulled from a padded posted_at window (weekends/holidays
    roll forward, so pad the left edge) and bucketed by session_date_for.
    """
    window_start = datetime(start.year, start.month, start.day, tzinfo=timezone.utc) - timedelta(days=5)
    window_end = datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc)
    rows = session.execute(
        select(SentimentMessage).where(
            SentimentMessage.posted_at >= window_start,
            SentimentMessage.posted_at <= window_end,
        )
    ).scalars().all()

    groups: dict[tuple[str, date, str], list[SentimentMessage]] = {}
    for msg in rows:
        session_day = session_date_for(msg.posted_at, cal)
        if not (start <= session_day <= end):
            continue
        groups.setdefault((msg.ticker, session_day, msg.source), []).append(msg)

    now = datetime.now(timezone.utc)
    upserted = 0
    for (ticker, session_day, source), msgs in sorted(groups.items(), key=lambda kv: kv[0][1]):
        scores = [s for s in (best_score(m.provider_score, m.local_score) for m in msgs) if s is not None]
        weights = [
            1 + (m.meta or {}).get("likes", 0)
            for m in msgs
            if best_score(m.provider_score, m.local_score) is not None
        ]
        mean = statistics.fmean(scores) if scores else None
        weighted = (
            sum(s * w for s, w in zip(scores, weights)) / sum(weights) if scores else None
        )
        std = statistics.pstdev(scores) if len(scores) > 1 else (0.0 if scores else None)

        existing = session.execute(
            select(SentimentDaily).where(
                SentimentDaily.ticker == ticker,
                SentimentDaily.session_date == session_day,
                SentimentDaily.source == source,
            )
        ).scalar_one_or_none()
        row = existing or SentimentDaily(ticker=ticker, session_date=session_day, source=source)
        row.message_count = len(msgs)
        row.mean_score = mean
        row.weighted_score = weighted
        row.score_std = std
        row.unique_authors = len({m.author for m in msgs if m.author})
        row.computed_at = now
        if existing is None:
            session.add(row)
        session.flush()

        baseline_rows = session.execute(
            select(SentimentDaily)
            .where(
                SentimentDaily.ticker == ticker,
                SentimentDaily.source == source,
                SentimentDaily.session_date < session_day,
            )
            .order_by(SentimentDaily.session_date.desc())
            .limit(baseline_days)
        ).scalars().all()
        score_baseline = [r.mean_score for r in baseline_rows if r.mean_score is not None]
        volume_baseline = [float(r.message_count) for r in baseline_rows]
        row.sentiment_zscore = (
            _zscore(mean, score_baseline, min_baseline_days) if mean is not None else None
        )
        row.volume_zscore = _zscore(float(len(msgs)), volume_baseline, min_baseline_days)
        upserted += 1

    session.commit()
    return upserted
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/sentiment/test_aggregate.py -v`
Expected: 7 PASS. (If `test_zscore_with_planted_baseline` trips on calendar specifics, fix the planted dates, not the aggregation logic.)

- [ ] **Step 5: Commit**

```bash
git add sentiment/aggregate.py tests/sentiment/test_aggregate.py
git commit -m "feat: daily sentiment aggregation with session mapping and z-scores"
```

---

### Task 11: Evaluation math — IC, event study, lead/lag

**Files:**
- Create: `sentiment/evaluation.py`
- Test: `tests/sentiment/test_evaluation.py`

**Interfaces:**
- Consumes: nothing from the DB — pure functions over in-memory data (the script in Task 13 does the loading).
- Produces (all take `daily`: a `pandas.DataFrame` with columns `ticker`, `session_date` (datetime.date), `score` (float), and for the event study also `sentiment_zscore`, `volume_zscore`; and `bars_by_ticker: dict[str, list[dict]]` where each bar has `date: datetime.date` and `close: float`, sorted ascending):
  - `forward_returns(bars_by_ticker, horizon: int) -> dict[str, dict[date, float]]`
  - `information_coefficient(daily, bars_by_ticker, horizons=(1, 3, 5), min_tickers=10) -> list[ICResult]` where `ICResult(horizon, mean_ic, t_stat, n_days)`
  - `event_study(daily, bars_by_ticker, horizons=(1, 3, 5), z_threshold=2.0) -> list[EventStudyResult]` where `EventStudyResult(horizon, n_events, mean_abnormal_return, p_value)`; abnormal return is **directional**: `sign(score) * (ticker fwd return − universe mean fwd return)`
  - `lead_lag(social_daily, news_daily, max_lag=5) -> list[LagCorrelation]` where `LagCorrelation(lag_days, correlation)`; positive lag = social leads news

- [ ] **Step 1: Write the failing test**

Create `tests/sentiment/test_evaluation.py`:

```python
from __future__ import annotations

import random
from datetime import date, timedelta

import pandas as pd
import pytest

from sentiment.evaluation import (
    event_study,
    forward_returns,
    information_coefficient,
    lead_lag,
)


def make_bars(closes: list[float], start: date = date(2026, 1, 5)) -> list[dict]:
    out, d = [], start
    for close in closes:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append({"date": d, "close": close})
        d += timedelta(days=1)
    return out


def test_forward_returns():
    bars = {"AAPL": make_bars([100.0, 110.0, 121.0])}
    fwd = forward_returns(bars, horizon=1)
    first_day = bars["AAPL"][0]["date"]
    assert fwd["AAPL"][first_day] == pytest.approx(0.10)


def _planted_universe(n_days=120, n_tickers=30, signal=0.03, seed=7):
    """Tickers whose next-day return follows today's score -> IC > 0."""
    rng = random.Random(seed)
    tickers = [f"T{i:02d}" for i in range(n_tickers)]
    start = date(2026, 1, 5)
    days = []
    d = start
    while len(days) < n_days + 6:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    rows, bars = [], {}
    scores = {t: [rng.uniform(-1, 1) for _ in range(n_days)] for t in tickers}
    for t in tickers:
        closes = [100.0]
        for i in range(n_days + 5):
            drift = signal * scores[t][i - 1] if 0 < i <= n_days else 0.0
            closes.append(closes[-1] * (1 + drift + rng.gauss(0, 0.005)))
        bars[t] = [{"date": days[i], "close": closes[i]} for i in range(len(days))]
        for i in range(n_days):
            rows.append({"ticker": t, "session_date": days[i], "score": scores[t][i]})
    return pd.DataFrame(rows), bars


def test_ic_detects_planted_signal():
    daily, bars = _planted_universe()
    results = information_coefficient(daily, bars, horizons=(1,))
    assert results[0].horizon == 1
    assert results[0].mean_ic > 0.1
    assert results[0].t_stat > 2
    assert results[0].n_days > 50


def test_ic_near_zero_on_noise():
    daily, bars = _planted_universe(signal=0.0, seed=11)
    results = information_coefficient(daily, bars, horizons=(1,))
    assert abs(results[0].mean_ic) < 0.05


def test_event_study_detects_planted_spikes():
    daily, bars = _planted_universe(signal=0.0, seed=3)
    daily["sentiment_zscore"] = 0.0
    daily["volume_zscore"] = 0.0
    # plant 40 positive-spike events with a +2% next-day pop
    spikes = daily.sample(n=40, random_state=1).index
    daily.loc[spikes, ["sentiment_zscore", "volume_zscore"]] = 3.0
    daily.loc[spikes, "score"] = 1.0
    for idx in spikes:
        row = daily.loc[idx]
        ticker_bars = bars[row["ticker"]]
        dates = [b["date"] for b in ticker_bars]
        i = dates.index(row["session_date"])
        bump = 1.02
        for bar in ticker_bars[i + 1:]:
            bar["close"] *= bump
    results = event_study(daily, bars, horizons=(1,))
    assert results[0].n_events == 40
    assert results[0].mean_abnormal_return > 0.005
    assert results[0].p_value < 0.05


def test_lead_lag_detects_social_leading_news():
    rng = random.Random(5)
    start = date(2026, 1, 5)
    days = []
    d = start
    while len(days) < 100:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    base = [rng.uniform(-1, 1) for _ in range(102)]
    social = pd.DataFrame(
        {"ticker": "AAPL", "session_date": days, "score": base[2:102]}
    )
    news = pd.DataFrame(  # news repeats social with a 2-day delay
        {"ticker": "AAPL", "session_date": days, "score": base[0:100]}
    )
    results = lead_lag(social, news, max_lag=5)
    best = max(results, key=lambda r: r.correlation)
    assert best.lag_days == 2
    assert best.correlation > 0.9
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/sentiment/test_evaluation.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

Create `sentiment/evaluation.py`:

```python
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

import pandas as pd
from scipy import stats


@dataclass(frozen=True)
class ICResult:
    horizon: int
    mean_ic: float
    t_stat: float
    n_days: int


@dataclass(frozen=True)
class EventStudyResult:
    horizon: int
    n_events: int
    mean_abnormal_return: float
    p_value: float


@dataclass(frozen=True)
class LagCorrelation:
    lag_days: int
    correlation: float


def forward_returns(
    bars_by_ticker: dict[str, list[dict]], horizon: int
) -> dict[str, dict[date, float]]:
    out: dict[str, dict[date, float]] = {}
    for ticker, bars in bars_by_ticker.items():
        by_day: dict[date, float] = {}
        for i in range(len(bars) - horizon):
            entry, exit_ = bars[i]["close"], bars[i + horizon]["close"]
            if entry:
                by_day[bars[i]["date"]] = exit_ / entry - 1
        out[ticker] = by_day
    return out


def information_coefficient(
    daily: pd.DataFrame,
    bars_by_ticker: dict[str, list[dict]],
    horizons: tuple[int, ...] = (1, 3, 5),
    min_tickers: int = 10,
) -> list[ICResult]:
    """Daily cross-sectional Spearman IC of score vs forward return."""
    results = []
    for horizon in horizons:
        fwd = forward_returns(bars_by_ticker, horizon)
        daily_ics = []
        for session_date, group in daily.groupby("session_date"):
            pairs = [
                (row["score"], fwd.get(row["ticker"], {}).get(session_date))
                for _, row in group.iterrows()
            ]
            pairs = [(s, r) for s, r in pairs if r is not None]
            if len(pairs) < min_tickers:
                continue
            ic, _ = stats.spearmanr([s for s, _ in pairs], [r for _, r in pairs])
            if not math.isnan(ic):
                daily_ics.append(ic)
        if not daily_ics:
            results.append(ICResult(horizon, 0.0, 0.0, 0))
            continue
        series = pd.Series(daily_ics)
        n = len(series)
        std = series.std(ddof=1)
        t_stat = float(series.mean() / (std / math.sqrt(n))) if std > 0 else 0.0
        results.append(ICResult(horizon, float(series.mean()), t_stat, n))
    return results


def event_study(
    daily: pd.DataFrame,
    bars_by_ticker: dict[str, list[dict]],
    horizons: tuple[int, ...] = (1, 3, 5),
    z_threshold: float = 2.0,
) -> list[EventStudyResult]:
    """Directional abnormal return after joint sentiment+volume spikes."""
    events = daily[
        (daily["sentiment_zscore"].abs() > z_threshold)
        & (daily["volume_zscore"] > z_threshold)
    ]
    results = []
    for horizon in horizons:
        fwd = forward_returns(bars_by_ticker, horizon)
        abnormal: list[float] = []
        for _, event in events.iterrows():
            session_date = event["session_date"]
            ticker_fwd = fwd.get(event["ticker"], {}).get(session_date)
            if ticker_fwd is None:
                continue
            universe_fwds = [
                by_day[session_date]
                for by_day in fwd.values()
                if session_date in by_day
            ]
            universe_mean = sum(universe_fwds) / len(universe_fwds)
            direction = 1.0 if event["score"] >= 0 else -1.0
            abnormal.append(direction * (ticker_fwd - universe_mean))
        if len(abnormal) < 2:
            results.append(EventStudyResult(horizon, len(abnormal), 0.0, 1.0))
            continue
        t_result = stats.ttest_1samp(abnormal, 0.0)
        results.append(
            EventStudyResult(
                horizon,
                len(abnormal),
                float(pd.Series(abnormal).mean()),
                float(t_result.pvalue),
            )
        )
    return results


def lead_lag(
    social_daily: pd.DataFrame, news_daily: pd.DataFrame, max_lag: int = 5
) -> list[LagCorrelation]:
    """Cross-correlation of universe-mean daily scores.

    Positive lag k: social score on day t vs news score on day t+k —
    high correlation at k > 0 means social leads news by k sessions.
    """
    social = social_daily.groupby("session_date")["score"].mean().sort_index()
    news = news_daily.groupby("session_date")["score"].mean().sort_index()
    results = []
    for lag in range(-max_lag, max_lag + 1):
        shifted_news = news.shift(-lag)
        aligned = pd.concat([social, shifted_news], axis=1, keys=["social", "news"]).dropna()
        if len(aligned) < 20:
            results.append(LagCorrelation(lag, 0.0))
            continue
        corr = aligned["social"].corr(aligned["news"])
        results.append(LagCorrelation(lag, float(corr) if not math.isnan(corr) else 0.0))
    return results
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/sentiment/test_evaluation.py -v`
Expected: 5 PASS. (These are statistical tests on seeded synthetic data — if one fails, check the synthetic generator's planted effect size before touching the math.)

- [ ] **Step 5: Commit**

```bash
git add sentiment/evaluation.py tests/sentiment/test_evaluation.py
git commit -m "feat: IC, event-study, and lead/lag evaluation math"
```

---

### Task 12: `scripts/collect_sentiment.py`

**Files:**
- Create: `scripts/collect_sentiment.py`
- Test: `tests/sentiment/test_collect_script.py`

**Interfaces:**
- Consumes: `AppConfig.sentiment` (Task 2), `store_messages`/`get_cursor`/`set_cursor` (Task 5), all four sources (Tasks 6–9), `rebuild_daily` (Task 10), `shared.universe.get_union_universe(ACTIVE_SLEEVES)`.
- Produces:
  - `build_sources(config: AppConfig, env: dict[str, str]) -> list` — instantiates enabled sources whose credentials are present; logs and skips a source whose env creds are missing (except StockTwits, which needs none).
  - `run_collection(session, sources: list, tickers: list[str], now: datetime) -> dict[str, int]` — per-source: read cursor (default `now - 3 days`), fetch, store, advance cursor to max `posted_at` seen (unchanged when nothing fetched); one source's exception is logged and skipped, others proceed.
  - CLI: `python scripts/collect_sentiment.py [--config config/default.yaml] [--aggregate-days 5]`

- [ ] **Step 1: Write the failing test**

Create `tests/sentiment/test_collect_script.py`:

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from scripts.collect_sentiment import build_sources, run_collection
from sentiment.sources.base import RawMessage
from sentiment.store import get_cursor
from shared.config import AppConfig
from shared.models import Base, SentimentMessage

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session


class GoodSource:
    name = "good"

    def fetch(self, tickers, since):
        return [
            RawMessage(
                source="good",
                source_id="1",
                ticker="AAPL",
                text="nice",
                posted_at=NOW - timedelta(hours=1),
            )
        ]


class BrokenSource:
    name = "broken"

    def fetch(self, tickers, since):
        raise RuntimeError("API down")


def test_run_collection_stores_and_advances_cursor(session):
    counts = run_collection(session, [GoodSource()], ["AAPL"], now=NOW)
    assert counts == {"good": 1}
    assert session.query(SentimentMessage).count() == 1
    assert get_cursor(session, "good", NOW) == NOW - timedelta(hours=1)


def test_failed_source_is_isolated_and_cursor_unmoved(session):
    default = NOW - timedelta(days=3)
    counts = run_collection(session, [BrokenSource(), GoodSource()], ["AAPL"], now=NOW)
    assert counts == {"broken": 0, "good": 1}
    assert get_cursor(session, "broken", default) == default


def test_build_sources_skips_missing_credentials():
    config = AppConfig()
    config.sentiment.finnhub_news.enabled = True
    config.sentiment.stocktwits.enabled = True
    config.sentiment.reddit.enabled = True
    config.sentiment.discord.enabled = False
    # No FINNHUB_API_KEY / reddit creds in env -> only stocktwits builds
    sources = build_sources(config, env={})
    assert [s.name for s in sources] == ["stocktwits"]


def test_build_sources_with_credentials():
    config = AppConfig()
    config.sentiment.finnhub_news.enabled = True
    config.sentiment.discord.enabled = True
    config.sentiment.discord.channel_ids = ["123"]
    env = {"FINNHUB_API_KEY": "k", "DISCORD_BOT_TOKEN": "t"}
    names = [s.name for s in build_sources(config, env=env)]
    assert "finnhub_news" in names
    assert "discord" in names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/sentiment/test_collect_script.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.collect_sentiment'`

- [ ] **Step 3: Implement**

Create `scripts/collect_sentiment.py`:

```python
#!/usr/bin/env python3
"""One-shot sentiment collection cycle (launchd-scheduled hourly).

For each enabled source with available credentials: read the cursor,
fetch messages since it, score + store them (idempotent), and advance the
cursor to the newest posted_at seen. A failing source is logged and
skipped; its cursor stays put so the gap remains visible. Afterwards,
rebuild the last N sessions of sentiment_daily.

Usage:
    python scripts/collect_sentiment.py
    python scripts/collect_sentiment.py --config config/default.yaml --aggregate-days 5
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta, timezone

import structlog
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from sentiment.aggregate import rebuild_daily
from sentiment.scoring import VaderScorer
from sentiment.sources.discord import DiscordSource
from sentiment.sources.finnhub_news import FinnhubNewsSource
from sentiment.sources.reddit import RedditSource
from sentiment.sources.stocktwits import StockTwitsSource
from sentiment.store import get_cursor, set_cursor, store_messages
from shared.config import AppConfig, load_config
from shared.market_calendar import MarketCalendar
from shared.universe import ACTIVE_SLEEVES, get_union_universe

logger = structlog.get_logger("collect_sentiment")

DEFAULT_LOOKBACK = timedelta(days=3)


def build_sources(config: AppConfig, env: dict[str, str]) -> list:
    sources: list = []
    cfg = config.sentiment
    if cfg.finnhub_news.enabled:
        if env.get("FINNHUB_API_KEY"):
            sources.append(FinnhubNewsSource(api_key=env["FINNHUB_API_KEY"]))
        else:
            logger.warning("source_skipped_missing_creds", source="finnhub_news")
    if cfg.stocktwits.enabled:
        sources.append(StockTwitsSource())
    if cfg.reddit.enabled:
        if env.get("REDDIT_CLIENT_ID") and env.get("REDDIT_CLIENT_SECRET"):
            import praw

            reddit = praw.Reddit(
                client_id=env["REDDIT_CLIENT_ID"],
                client_secret=env["REDDIT_CLIENT_SECRET"],
                user_agent=env.get("REDDIT_USER_AGENT", "algo-poc-sentiment/0.1"),
            )
            sources.append(
                RedditSource(
                    reddit,
                    subreddits=cfg.reddit.subreddits,
                    posts_per_subreddit=cfg.reddit.posts_per_subreddit,
                )
            )
        else:
            logger.warning("source_skipped_missing_creds", source="reddit")
    if cfg.discord.enabled:
        if env.get("DISCORD_BOT_TOKEN") and cfg.discord.channel_ids:
            sources.append(
                DiscordSource(env["DISCORD_BOT_TOKEN"], channel_ids=cfg.discord.channel_ids)
            )
        else:
            logger.warning("source_skipped_missing_creds", source="discord")
    return sources


def run_collection(
    session: Session, sources: list, tickers: list[str], now: datetime
) -> dict[str, int]:
    scorer = VaderScorer()
    counts: dict[str, int] = {}
    for source in sources:
        since = get_cursor(session, source.name, default=now - DEFAULT_LOOKBACK)
        try:
            messages = source.fetch(tickers, since)
        except Exception:
            logger.exception("source_fetch_failed", source=source.name)
            counts[source.name] = 0
            continue
        inserted = store_messages(session, messages, scorer)
        counts[source.name] = inserted
        if messages:
            newest = max(m.posted_at for m in messages)
            set_cursor(session, source.name, max(newest, since))
        logger.info(
            "source_collected",
            source=source.name,
            fetched=len(messages),
            inserted=inserted,
        )
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/default.yaml")
    parser.add_argument("--aggregate-days", type=int, default=5)
    args = parser.parse_args()

    config = load_config(args.config)
    engine = create_engine(config.database.url)
    tickers = get_union_universe(ACTIVE_SLEEVES)
    now = datetime.now(timezone.utc)
    cal = MarketCalendar()

    with Session(engine) as session:
        sources = build_sources(config, env=dict(os.environ))
        if not sources:
            logger.error("no_sources_enabled")
            return 1
        counts = run_collection(session, sources, tickers, now)
        end = date.today()
        start = end - timedelta(days=args.aggregate_days + 4)
        upserted = rebuild_daily(
            session,
            cal,
            start,
            end,
            baseline_days=config.sentiment.zscore_baseline_days,
            min_baseline_days=config.sentiment.zscore_min_baseline_days,
        )
    logger.info("collection_cycle_done", counts=counts, daily_rows=upserted)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/sentiment/test_collect_script.py -v`
Expected: 4 PASS

- [ ] **Step 5: Smoke-run against the local stack (no creds needed — StockTwits only)**

Run: `python scripts/collect_sentiment.py --aggregate-days 2`
Expected: exits 0; log lines show `source_collected source=stocktwits` with a non-zero `fetched`, plus `source_skipped_missing_creds` warnings for sources without env keys. (Requires the local docker Postgres up and the Task 1 migration applied. This is the one intentionally-online step; skip it if offline and note that in the commit message.)

- [ ] **Step 6: Commit**

```bash
git add scripts/collect_sentiment.py tests/sentiment/test_collect_script.py
git commit -m "feat: one-shot sentiment collection script with per-source isolation"
```

---

### Task 13: `scripts/sentiment_eval.py` — the pre-committed gates

**Files:**
- Create: `scripts/sentiment_eval.py`
- Test: `tests/sentiment/test_eval_script.py`

**Interfaces:**
- Consumes: `SentimentDaily` (Task 1), evaluation functions (Task 11), the backtest bars cache format `{"bars": {ticker: [{"date": "YYYY-MM-DD", "close": ...}, ...]}}` (same file `run_backtest.py --bars-from-json` reads).
- Produces:
  - `load_daily(session, source: str, score_column: str = "weighted_score") -> pd.DataFrame` — columns `ticker, session_date, score, sentiment_zscore, volume_zscore`, rows with NULL score dropped.
  - `load_bars_json(path: str) -> dict[str, list[dict]]` — parses dates, keeps `date`/`close`.
  - `gap_report(daily: pd.DataFrame, sessions: list[date]) -> tuple[int, float]` — (number of eval-window sessions with zero rows for this source, fraction of window).
  - `SourceVerdict(source, ic_results, event_results, gap_fraction, verdict)` dataclass; `verdict` in `{"PASS", "FAIL", "INSUFFICIENT_DATA"}`.
  - `judge(ic_results, event_results, n_sessions_with_data) -> str` implementing the gates verbatim (constants below).
  - CLI: `python scripts/sentiment_eval.py --bars data/cache/bars.json [--config config/default.yaml] [--json-out output/sentiment_eval.json]`. Exit 0 normally, exit 2 if any enabled source's gap fraction > 0.10.

- [ ] **Step 1: Write the failing test**

Create `tests/sentiment/test_eval_script.py`:

```python
from __future__ import annotations

from datetime import date

from scripts.sentiment_eval import (
    GATE_MAX_P,
    GATE_MIN_ABNORMAL,
    GATE_MIN_EVENTS,
    GATE_MIN_IC,
    GATE_MIN_SESSIONS,
    GATE_MIN_TSTAT,
    gap_report,
    judge,
)
from sentiment.evaluation import EventStudyResult, ICResult

import pandas as pd


def test_gate_constants_are_the_spec_values():
    assert GATE_MIN_IC == 0.03
    assert GATE_MIN_TSTAT == 2.0
    assert GATE_MIN_ABNORMAL == 0.003
    assert GATE_MAX_P == 0.05
    assert GATE_MIN_EVENTS == 30


def test_judge_pass_via_ic():
    ic = [ICResult(horizon=1, mean_ic=0.05, t_stat=2.5, n_days=60)]
    events = [EventStudyResult(horizon=1, n_events=3, mean_abnormal_return=0.0, p_value=0.9)]
    assert judge(ic, events, n_sessions_with_data=60) == "PASS"


def test_judge_pass_via_event_study():
    ic = [ICResult(horizon=1, mean_ic=0.0, t_stat=0.1, n_days=60)]
    events = [EventStudyResult(horizon=3, n_events=35, mean_abnormal_return=0.004, p_value=0.01)]
    assert judge(ic, events, n_sessions_with_data=60) == "PASS"


def test_judge_fail():
    ic = [ICResult(horizon=1, mean_ic=0.01, t_stat=0.5, n_days=60)]
    events = [EventStudyResult(horizon=1, n_events=35, mean_abnormal_return=0.001, p_value=0.4)]
    assert judge(ic, events, n_sessions_with_data=60) == "FAIL"


def test_judge_insufficient_data():
    ic = [ICResult(horizon=1, mean_ic=0.10, t_stat=3.0, n_days=5)]
    events = []
    assert judge(ic, events, n_sessions_with_data=5) == "INSUFFICIENT_DATA"
    assert GATE_MIN_SESSIONS > 5


def test_gap_report():
    daily = pd.DataFrame(
        {
            "ticker": ["AAPL", "AAPL"],
            "session_date": [date(2026, 8, 3), date(2026, 8, 5)],
            "score": [0.1, 0.2],
        }
    )
    sessions = [date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5), date(2026, 8, 6)]
    n_gaps, fraction = gap_report(daily, sessions)
    assert n_gaps == 2
    assert fraction == 0.5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/sentiment/test_eval_script.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

Create `scripts/sentiment_eval.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/sentiment/test_eval_script.py -v`
Expected: 6 PASS

- [ ] **Step 5: Run the whole sentiment suite**

Run: `pytest tests/sentiment/ -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/sentiment_eval.py tests/sentiment/test_eval_script.py
git commit -m "feat: sentiment evaluation script with pre-committed gates"
```

---

### Task 14: launchd job + operational runbook

**Files:**
- Create: `ops/launchd/com.algopoc.sentiment-collect.plist` (template — `ops/launchd/` is a new directory; existing launchd jobs live outside the repo, this one is versioned as the reference copy)
- Create: `docs/operations/sentiment-research.md`

**Interfaces:**
- Consumes: `scripts/collect_sentiment.py` CLI (Task 12), `scripts/sentiment_eval.py` CLI (Task 13).
- Produces: documentation only — no code contracts.

- [ ] **Step 1: Write the plist template**

Create `ops/launchd/com.algopoc.sentiment-collect.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.algopoc.sentiment-collect</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/env</string>
        <string>python3</string>
        <string>scripts/collect_sentiment.py</string>
        <string>--aggregate-days</string>
        <string>5</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/huiliang/GitHub/algo-poc</string>
    <key>StartInterval</key>
    <integer>3600</integer>
    <key>EnvironmentVariables</key>
    <dict>
        <!-- Fill in before bootstrapping; never commit real values. -->
        <key>FINNHUB_API_KEY</key>
        <string>REPLACE_ME</string>
        <key>REDDIT_CLIENT_ID</key>
        <string>REPLACE_ME</string>
        <key>REDDIT_CLIENT_SECRET</key>
        <string>REPLACE_ME</string>
        <key>REDDIT_USER_AGENT</key>
        <string>algo-poc-sentiment/0.1</string>
        <key>DISCORD_BOT_TOKEN</key>
        <string>REPLACE_ME</string>
    </dict>
    <key>StandardOutPath</key>
    <string>/Users/huiliang/GitHub/algo-poc/output/logs/sentiment-collect.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/huiliang/GitHub/algo-poc/output/logs/sentiment-collect.err.log</string>
</dict>
</plist>
```

(Hourly around the clock: StockTwits' ~30-message window makes market-hours coverage critical; off-hours cycles are cheap and news/social flow 24/7. The dedup constraint makes extra cycles harmless.)

- [ ] **Step 2: Write the runbook**

Create `docs/operations/sentiment-research.md`:

```markdown
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
  GROUP BY source;` — every enabled source should be < 2h old during
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
```

- [ ] **Step 3: Verify the plist parses and the suite passes**

Run: `plutil -lint ops/launchd/com.algopoc.sentiment-collect.plist && pytest tests/sentiment/ -q`
Expected: `OK` + all tests pass.

- [ ] **Step 4: Commit**

```bash
git add ops/launchd/com.algopoc.sentiment-collect.plist docs/operations/sentiment-research.md
git commit -m "feat: sentiment collection launchd template and runbook"
```

---

## Final verification (after all tasks)

- [ ] Run: `pytest -q` — the **full** suite, not just `tests/sentiment/` (config and models changes touch shared code).
- [ ] Run: `pip install -e ".[dev]"` — confirms pyproject is consistent.
- [ ] Confirm `git log --oneline` shows one commit per task.
- [ ] Confirm nothing under `services/`, `scripts/run_paper.py`, or `scripts/run_backtest.py` changed: `git diff f281ce5..HEAD --stat -- services scripts/run_paper.py scripts/run_backtest.py` is empty (adjust the base ref to the commit before Task 1).
