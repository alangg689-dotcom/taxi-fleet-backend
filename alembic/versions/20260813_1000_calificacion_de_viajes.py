"""Calificación del viaje por el cliente (1 a 5).

La captura el bot al terminar el viaje — el cliente contesta con un número.
Nula para todos los viajes donde el cliente no contestó (la mayoría, en la
práctica: calificar es opcional y no se insiste) y para los viajes de
operador, que no tienen a quién preguntarle.

El CHECK vive en la base y no solo en el bot a propósito: la calificación
entra por texto libre de WhatsApp, y el día que otro canal la escriba mal,
mejor un error ruidoso que un 47 guardado como calificación.

Revision ID: 0014
Revises: 0013
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("trips", sa.Column("rating", sa.SmallInteger(), nullable=True))
    op.create_check_constraint(
        "ck_trips_rating_1_5", "trips", "rating IS NULL OR (rating >= 1 AND rating <= 5)"
    )


def downgrade() -> None:
    op.drop_constraint("ck_trips_rating_1_5", "trips")
    op.drop_column("trips", "rating")
