"""merge system_halt (T1) and model content_hash (T9) heads

Revision ID: 82623f87013d
Revises: a4e1b9c07d3f, e7a1c4d92f3b
Create Date: 2026-08-09 17:48:13.920716

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '82623f87013d'
down_revision: Union[str, Sequence[str], None] = ('a4e1b9c07d3f', 'e7a1c4d92f3b')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
