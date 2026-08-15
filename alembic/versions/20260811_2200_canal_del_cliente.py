"""Generaliza la identidad del cliente en trips: además del teléfono de
WhatsApp, ahora hay que saber POR DÓNDE pidió el viaje y a qué destinatario
contestarle en ese canal. Telegram no identifica a nadie por teléfono sino por
un chat_id numérico que el bot recibe en cada mensaje.

`customer_channel` se guarda como texto, no como enum nativo de Postgres, a
diferencia del resto de los enums del proyecto: agregar un canal (Messenger,
web, app del cliente) debe ser desplegar código, no un ALTER TYPE con su
migración. La lista viva está en app.models.enums.CustomerChannel.

Los viajes que ya existen con customer_phone son todos de WhatsApp — es el
único canal que hubo hasta ahora — así que se rellenan con ese valor para que
el barrido y los avisos de estado los sigan encontrando.

Revision ID: 0013
Revises: 0012
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("trips", sa.Column("customer_channel", sa.String(length=16), nullable=True))
    # Telegram entrega el chat_id como entero de 64 bits y puede ser negativo
    # (grupos). Se guarda como texto para no atarse a ese formato: el siguiente
    # canal identificará al cliente de otra forma.
    op.add_column("trips", sa.Column("customer_chat_id", sa.String(length=64), nullable=True))

    op.execute(
        "UPDATE trips SET customer_channel = 'whatsapp' WHERE customer_phone IS NOT NULL"
    )

    # El barrido de viajes atorados y el aviso de llegada buscan viajes activos
    # con cliente identificado; sin esto es un seq scan sobre toda la tabla de
    # viajes en cada pasada (cada 30 s) y en cada lote de pings.
    op.create_index(
        "ix_trips_customer_channel",
        "trips",
        ["customer_channel"],
        postgresql_where=sa.text("customer_channel IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_trips_customer_channel", table_name="trips")
    op.drop_column("trips", "customer_chat_id")
    op.drop_column("trips", "customer_channel")
