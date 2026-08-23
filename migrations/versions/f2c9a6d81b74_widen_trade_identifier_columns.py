"""widen trade identifier columns

Widening only. No data is read, moved, or dropped.

``trades.recommendation_id`` was ``varchar(50)`` while the *same* identifier is
``varchar(255)`` on ``order_intents`` (``shared/models/order_ledger.py:52``) and
on ``execution_fills`` (``:110``). The id format
``sleeve-{date}-{account}-{mode}-{sleeve}-{ticker}-{side}`` outgrew 50 characters
when account identity was added, so every projection into ``trades`` raised
``StringDataRightTruncation``. Measured 2026-08-21: 122 of 131 ``order_intents``
rows already carry an id longer than 50, and the longest is 60.

``trades.exit_reason`` becomes ``Text`` rather than a wider ``varchar``. It is
populated from ``OrderIntent.reason`` (``services/portfolio_accounting/projector.py:126``),
which is itself ``Text`` and therefore unbounded. Reason strings already in the
codebase exceed 50 characters, and any varchar bound chosen here would be the
same guess that failed for ``recommendation_id``. Matching the source type is
the only bound that cannot be outgrown.

The three sibling ``String(50)`` columns audited alongside these are left alone
deliberately, each with 2x headroom or better against the longest value its
format can produce: ``trades.portfolio`` (17, ``thematic_momentum``),
``positions.sector`` (22, ``Consumer Discretionary``), ``positions.account_id``
(9, an IB account id). See
``tests/migrations/test_widen_trade_identifier_columns_migration.py``.

``op.batch_alter_table`` is required, not stylistic: sqlite cannot ``ALTER
COLUMN`` and the migration tests run on sqlite. On Postgres batch mode emits a
plain ``ALTER TABLE ... ALTER COLUMN ... TYPE``, so production takes the cheap
path -- a widening, which Postgres performs without a table rewrite.

Revision ID: f2c9a6d81b74
Revises: a5f3c81d0e72
Create Date: 2026-08-22

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f2c9a6d81b74"
down_revision: Union[str, Sequence[str], None] = "a5f3c81d0e72"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("trades") as batch_op:
        batch_op.alter_column(
            "recommendation_id",
            existing_type=sa.String(50),
            type_=sa.String(255),
            existing_nullable=True,
        )
        batch_op.alter_column(
            "exit_reason",
            existing_type=sa.String(50),
            type_=sa.Text(),
            existing_nullable=True,
        )


def downgrade() -> None:
    """Narrow the columns again.

    This is a development convenience, NOT a production escape hatch. The moment
    the fix works, ``trades`` holds ids longer than 50 characters, and Postgres
    refuses to narrow a column whose data would not fit. Rolling back in
    production therefore requires deciding what to do with those rows first,
    which is a data decision and deliberately not automated here.
    """
    with op.batch_alter_table("trades") as batch_op:
        batch_op.alter_column(
            "exit_reason",
            existing_type=sa.Text(),
            type_=sa.String(50),
            existing_nullable=True,
        )
        batch_op.alter_column(
            "recommendation_id",
            existing_type=sa.String(255),
            type_=sa.String(50),
            existing_nullable=True,
        )
