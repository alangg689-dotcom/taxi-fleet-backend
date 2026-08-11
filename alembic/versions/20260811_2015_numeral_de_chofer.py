"""Numeral del chofer (R18, R20…) — el identificador con el que la
operadora lo llama por radio.

Nulo a proposito, igual que pin_hash en la 0010: los choferes que ya
existen no tienen numeral y no hay forma de inventarles uno correcto desde
aqui. Un operador se lo asigna con PATCH /drivers/{id}. El dashboard los
distingue porque numeral llega en null.

Unico: dos choferes con el mismo numeral harian ambiguo el mapa de flota,
que es justo donde se usa. El indice es parcial (WHERE numeral IS NOT
NULL) para que los que todavia no tienen no choquen entre si — en
Postgres varios NULL no colisionan en un UNIQUE normal, pero el indice
parcial lo deja explicito y no indexa filas que no aportan.

Revision ID: 0011
Revises: 0010
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("drivers", sa.Column("numeral", sa.String(length=10), nullable=True))
    op.create_index(
        "ix_drivers_numeral",
        "drivers",
        ["numeral"],
        unique=True,
        postgresql_where=sa.text("numeral IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_drivers_numeral", table_name="drivers")
    op.drop_column("drivers", "numeral")
