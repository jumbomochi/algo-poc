"""--reset must be impossible to run non-interactively and must back up first.

Regression: on 2026-07-10 an agent session ran `echo yes | run_paper.py --reset`,
piping past the interactive confirmation and wiping the paper book without
authorization. The guard refuses when stdin is not a TTY, and a JSON backup of
all four state tables is written before any row is deleted.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from scripts.paper_state import PaperTradingState
from shared.models.base import Base
from shared.models.equity_snapshot import EquitySnapshot
from shared.models.portfolio import Position, Trade
from shared.models.research import ResearchCandidate

REPO_ROOT = Path(__file__).resolve().parents[2]


def make_session(db_url: str):
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def seed_state(session) -> None:
    PaperTradingState.create_new(portfolio_capitals={"sleeve_a": 10_000.0},
                                 session=session)
    session.add(Position(portfolio="sleeve_a", ticker="AAPL", quantity=2.0,
                         avg_entry_price=100.0, current_price=101.0,
                         peak_price=101.0, highest_price_since_entry=101.0,
                         opened_at=datetime(2026, 7, 7, tzinfo=timezone.utc)))
    session.add(Trade(portfolio="sleeve_a", ticker="AAPL", side="buy",
                      quantity=2.0, price=100.0, entry_price=100.0,
                      entry_date=date(2026, 7, 7), pnl=0.0,
                      executed_at=datetime(2026, 7, 7, tzinfo=timezone.utc)))
    session.add(EquitySnapshot(portfolio="sleeve_a", date=date(2026, 7, 7),
                               equity=10_000.0, cash=9_800.0, market_value=200.0,
                               created_at=datetime(2026, 7, 7, tzinfo=timezone.utc)))
    session.commit()


class TestDumpAndReset:
    def test_dump_writes_all_four_tables(self, tmp_path):
        from scripts.run_paper import dump_paper_state

        session = make_session("sqlite:///:memory:")
        seed_state(session)
        out = dump_paper_state(session, tmp_path / "backup.json")

        payload = json.loads(out.read_text())
        assert set(payload) == {"portfolio_config", "positions", "trades",
                                "equity_snapshots"}
        assert len(payload["positions"]) == 1
        assert payload["positions"][0]["ticker"] == "AAPL"
        assert len(payload["equity_snapshots"]) == 1
        assert len(payload["trades"]) == 1
        assert len(payload["portfolio_config"]) == 1
        session.close()

    def test_reset_wipes_all_four_tables(self):
        from scripts.run_paper import reset_paper_state

        session = make_session("sqlite:///:memory:")
        seed_state(session)
        reset_paper_state(session)

        assert session.query(Position).count() == 0
        assert session.query(Trade).count() == 0
        assert session.query(EquitySnapshot).count() == 0
        with pytest.raises(ValueError):
            PaperTradingState.load(session)
        session.close()

    def test_reset_preserves_research_audit_history(self):
        from scripts.run_paper import reset_paper_state

        session = make_session("sqlite:///:memory:")
        seed_state(session)
        session.add(
            ResearchCandidate(
                candidate_key="a" * 64,
                portfolio="momentum",
                ticker="AAPL",
                as_of=date(2026, 7, 7),
                action="buy",
                raw_signal={"action": "buy"},
                factor_values={"price_momentum_126d@1.0.0": 0.25},
                provenance={
                    "data_cutoff": "2026-07-07",
                    "universe_snapshot_id": "sha256:" + "1" * 64,
                    "code_revision": "sha256:" + "2" * 64,
                    "input_artifact_checksum": "sha256:" + "3" * 64,
                },
                risk_approved=False,
                risk_reason="position cap",
            )
        )
        session.commit()

        reset_paper_state(session)

        candidate = session.query(ResearchCandidate).one()
        assert candidate.candidate_key == "a" * 64
        assert session.query(Position).count() == 0
        assert session.query(Trade).count() == 0
        assert session.query(EquitySnapshot).count() == 0
        with pytest.raises(ValueError):
            PaperTradingState.load(session)
        session.close()


class TestCliGuard:
    def test_piped_confirmation_is_refused(self, tmp_path):
        """`echo yes | run_paper.py --reset` must refuse and delete nothing."""
        db_path = tmp_path / "paper.db"
        db_url = f"sqlite:///{db_path}"
        session = make_session(db_url)
        seed_state(session)
        session.close()

        result = subprocess.run(
            [sys.executable, "scripts/run_paper.py", "--reset",
             "--db-url", db_url],
            input="yes\n", capture_output=True, text=True, cwd=REPO_ROOT,
            timeout=120,
        )

        assert result.returncode == 2, result.stdout + result.stderr
        assert "Refusing --reset" in result.stdout + result.stderr

        session = make_session(db_url)
        assert session.query(Position).count() == 1
        assert session.query(EquitySnapshot).count() == 1
        session.close()

    def test_interactive_reset_backs_up_before_wipe(self, tmp_path, monkeypatch):
        """A genuine TTY 'yes' still works, and a backup lands in output/."""
        import scripts.run_paper as run_paper

        db_path = tmp_path / "paper.db"
        db_url = f"sqlite:///{db_path}"
        session = make_session(db_url)
        seed_state(session)
        session.close()

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sys, "argv",
                            ["run_paper.py", "--reset", "--db-url", db_url])
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _: "yes")

        run_paper.main()

        backups = list((tmp_path / "output").glob("paper_state_pre_reset_*.json"))
        assert len(backups) == 1
        payload = json.loads(backups[0].read_text())
        assert payload["positions"][0]["ticker"] == "AAPL"

        session = make_session(db_url)
        assert session.query(Position).count() == 0
        assert session.query(EquitySnapshot).count() == 0
        session.close()
