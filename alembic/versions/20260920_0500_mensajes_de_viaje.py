"""Hilo de mensajes cliente↔chofer, scoped al viaje.

El pasajero de WhatsApp puede escribirle al chofer asignado (sobre todo
mientras va en camino al punto de recogida) y el chofer responde desde
la app. El hilo ES el viaje: no hay tabla de threads. Solo se escribe
mientras el viaje está asignado o en_curso — eso lo impone el código
(app.core.trip_chat), no un trigger: un trigger no distinguiría un
mensaje tardío legítimo de uno que no debió entrar.

`sender` es texto con CHECK, no un enum nativo: igual que customer_channel,
agregar un emisor (operadora, sistema) es desplegar código.

Revision ID: 0015
Revises: 0014
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trip_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "trip_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("trips.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sender", sa.String(length=16), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_trip_messages_sender",
        "trip_messages",
        "sender IN ('customer', 'driver')",
    )
    op.create_check_constraint(
        "ck_trip_messages_body_not_empty",
        "trip_messages",
        "length(btrim(body)) > 0",
    )
    op.create_index(
        "ix_trip_messages_trip_id_created_at",
        "trip_messages",
        ["trip_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_trip_messages_trip_id_created_at", table_name="trip_messages")
    op.drop_constraint("ck_trip_messages_body_not_empty", "trip_messages")
    op.drop_constraint("ck_trip_messages_sender", "trip_messages")
    op.drop_table("trip_messages")
