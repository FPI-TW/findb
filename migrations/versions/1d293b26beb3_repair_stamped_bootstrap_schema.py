"""repair stamped bootstrap schema

Revision ID: 1d293b26beb3
Revises: 9e246ab981f7
Create Date: 2026-04-23 09:27:43.182002
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1d293b26beb3"
down_revision: Union[str, Sequence[str], None] = "9e246ab981f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Some local DBs were stamped to head without running bootstrap DDL.
    # Rebuild missing tables/indexes idempotently from ORM metadata.
    bind = op.get_bind()
    op.execute("CREATE SCHEMA IF NOT EXISTS raw")

    from app.models import canonical as _canonical  # noqa: F401
    from app.models import correction as _correction  # noqa: F401
    from app.models import raw as _raw  # noqa: F401
    from app.models import registry as _registry  # noqa: F401
    from app.models.base import Base

    Base.metadata.create_all(bind=bind, checkfirst=True)


def downgrade() -> None:
    # Irreversible repair migration; avoid dropping reconstructed tables.
    return None
