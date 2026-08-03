"""Agrega push_token a drivers: el token de notificaciones push de Expo del
teléfono del chofer — la red de seguridad para cuando /ws/driver no tiene un
socket vivo (app en segundo plano o cerrada).

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("drivers", sa.Column("push_token", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("drivers", "push_token")
