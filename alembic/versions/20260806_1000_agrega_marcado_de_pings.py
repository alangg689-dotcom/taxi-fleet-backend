"""Agrega queue_eligible/flag_reason a location_pings: la capa de validación
(spec-sitios-y-fila-v2.md, sección 6) guarda todo ping para el historial de
rutas, pero marca los que no deben mover la máquina de estados de la fila de
sitios (precisión pobre, salto imposible, posible GPS falso, o llegó
demasiado tarde). No afecta el despacho ni el mapa en vivo — esa lectura
sigue igual, esto es exclusivo de la fila que todavía no existe.

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "location_pings",
        sa.Column("queue_eligible", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "location_pings",
        sa.Column("flag_reason", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("location_pings", "flag_reason")
    op.drop_column("location_pings", "queue_eligible")
