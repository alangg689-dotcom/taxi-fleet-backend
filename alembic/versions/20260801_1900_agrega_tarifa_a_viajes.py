"""Agrega el campo fare a trips: lo que cobró el chofer, capturado a mano al
completar el viaje. Sirve para que el chofer lleve su propio registro de
ingresos en la app — no hay cálculo automático de tarifa en este proyecto.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("trips", sa.Column("fare", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("trips", "fare")
