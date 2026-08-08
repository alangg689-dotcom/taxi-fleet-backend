"""Bandera is_placeholder en stands + nombres inequívocos.

La migración 0008 sembró 6 sitios placeholder para poder poner
vehicles.stand_id NOT NULL. Sin una bandera clara, en tres semanas nadie
sabe cuáles ya se trazaron de verdad y cuáles siguen siendo el cuadro de
referencia. Esta migración:
  1. Agrega stands.is_placeholder (default false).
  2. Marca is_placeholder=true en los 6 sembrados por su nombre original
     ("Sitio N (pendiente de trazar)").
  3. Les pone un nombre que no se puede confundir con uno real.

Revision ID: 0009
Revises: 0008
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_NAMES = [f"Sitio {n} (pendiente de trazar)" for n in range(1, 7)]


def upgrade() -> None:
    op.add_column(
        "stands",
        sa.Column("is_placeholder", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    conn = op.get_bind()
    for old_name in _OLD_NAMES:
        new_name = old_name.replace(
            "Sitio", "PLACEHOLDER — no usar — Sitio", 1
        ).replace(" (pendiente de trazar)", "")
        conn.execute(
            sa.text(
                "UPDATE stands SET is_placeholder = true, name = :new_name WHERE name = :old_name"
            ),
            {"new_name": new_name, "old_name": old_name},
        )


def downgrade() -> None:
    conn = op.get_bind()
    for old_name in _OLD_NAMES:
        new_name = old_name.replace(
            "Sitio", "PLACEHOLDER — no usar — Sitio", 1
        ).replace(" (pendiente de trazar)", "")
        conn.execute(
            sa.text("UPDATE stands SET name = :old_name WHERE name = :new_name"),
            {"new_name": new_name, "old_name": old_name},
        )
    op.drop_column("stands", "is_placeholder")
