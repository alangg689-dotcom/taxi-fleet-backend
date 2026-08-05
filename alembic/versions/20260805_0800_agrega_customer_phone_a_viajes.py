"""Agrega customer_phone a trips: el teléfono de WhatsApp del cliente que
originó el viaje a través del bot — a dónde se le contesta con el estado del
viaje. Nulo en viajes creados por operador/dashboard (no hay cliente
identificado).

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("trips", sa.Column("customer_phone", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("trips", "customer_phone")
