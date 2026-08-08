"""PIN de login de chofer (reemplaza el OTP por SMS).

Nulo a propósito: no hay forma de generar un PIN "de verdad" para los
choferes que ya existen sin que quede un valor adivinable o sin que nadie
se entere de cuál es — un operador se lo asigna a cada uno desde
POST /drivers/{id}/pin en cuanto migre. Mientras tanto, sin PIN, ese
chofer simplemente no puede entrar (mismo tratamiento que una unidad sin
device_key).

Revision ID: 0010
Revises: 0009
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("drivers", sa.Column("pin_hash", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("drivers", "pin_hash")
