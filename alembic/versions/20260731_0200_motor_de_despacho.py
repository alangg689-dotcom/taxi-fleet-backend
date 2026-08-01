"""Motor de despacho automático: viajes sin chofer asignado de antemano.

Hasta ahora un viaje siempre nacía con vehicle_id/driver_id ya elegidos por
un operador. Para poder crear un viaje "solicitado" (ej. desde un futuro bot
de WhatsApp) sin saber todavía quién lo va a tomar, ambas columnas pasan a
ser nullable, y se agregan offered_driver_id/offered_vehicle_id/
offer_expires_at para trackear a quién se le está ofreciendo en este momento
mientras el motor de despacho recorre candidatos cercanos.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("trips", "vehicle_id", nullable=True)
    op.alter_column("trips", "driver_id", nullable=True)

    op.add_column(
        "trips",
        sa.Column(
            "offered_driver_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("drivers.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "trips",
        sa.Column(
            "offered_vehicle_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vehicles.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "trips",
        sa.Column("offer_expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("trips", "offer_expires_at")
    op.drop_column("trips", "offered_vehicle_id")
    op.drop_column("trips", "offered_driver_id")
    op.alter_column("trips", "driver_id", nullable=False)
    op.alter_column("trips", "vehicle_id", nullable=False)
