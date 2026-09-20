"""Autorregistro de choferes: solicitudes, dispositivos y folio CTM.

`driver_applications` es la cola de alta (sin foto de tarjetón, sin OTP).
Al aprobar se crea User+Driver con pin_hash vacío y must_set_pin=true:
el PIN lo inventa el chofer, no el operador.

`folio_ctm` es el ID operativo de la unidad (visible; varios choferes
pueden compartir el mismo). Convive con `drivers.numeral` (R18, radio),
que es otra cosa. Nunca se usa como password ni como device_key GPS.

`driver_devices` guarda el hash del device_token del teléfono — distinto
de `vehicles.device_key_hash` (telemetría de la unidad).

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
    op.add_column(
        "drivers",
        sa.Column("must_set_pin", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("drivers", sa.Column("folio_ctm", sa.String(length=20), nullable=True))
    op.create_index("ix_drivers_folio_ctm", "drivers", ["folio_ctm"])

    driver_unit_role = postgresql.ENUM(
        "OWNER", "SHIFT_DRIVER", name="driver_unit_role", create_type=True
    )
    driver_unit_role.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "drivers",
        sa.Column(
            "unit_role",
            postgresql.ENUM(
                "OWNER", "SHIFT_DRIVER", name="driver_unit_role", create_type=False
            ),
            nullable=True,
        ),
    )

    op.add_column("vehicles", sa.Column("folio_ctm", sa.String(length=20), nullable=True))
    op.create_index(
        "ix_vehicles_folio_ctm",
        "vehicles",
        ["folio_ctm"],
        unique=True,
        postgresql_where=sa.text("folio_ctm IS NOT NULL"),
    )

    driver_account_status = postgresql.ENUM(
        "PENDING_APPROVAL",
        "ACTIVE",
        "REJECTED",
        "SUSPENDED",
        name="driver_account_status",
        create_type=True,
    )
    driver_account_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "driver_applications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("full_name", sa.String(length=150), nullable=False),
        sa.Column("phone", sa.String(length=20), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("folio_ctm", sa.String(length=20), nullable=False),
        sa.Column("license_plate", sa.String(length=15), nullable=False),
        sa.Column(
            "unit_role",
            postgresql.ENUM(
                "OWNER", "SHIFT_DRIVER", name="driver_unit_role", create_type=False
            ),
            nullable=False,
        ),
        sa.Column("photo_profile_url", sa.String(length=512), nullable=True),
        sa.Column("photo_license_url", sa.String(length=512), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(
                "PENDING_APPROVAL",
                "ACTIVE",
                "REJECTED",
                "SUSPENDED",
                name="driver_account_status",
                create_type=False,
            ),
            nullable=False,
            server_default="PENDING_APPROVAL",
        ),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column(
            "reviewed_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "driver_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("drivers.id"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_driver_applications_status", "driver_applications", ["status"])
    op.create_index("ix_driver_applications_phone", "driver_applications", ["phone"])
    op.create_index(
        "ix_driver_applications_folio_ctm", "driver_applications", ["folio_ctm"]
    )
    op.create_index(
        "ix_driver_applications_created_at", "driver_applications", ["created_at"]
    )

    driver_device_status = postgresql.ENUM(
        "ACTIVE", "REVOKED", name="driver_device_status", create_type=True
    )
    driver_device_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "driver_devices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "driver_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("drivers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("device_token_hash", sa.String(length=255), nullable=False),
        sa.Column("device_id_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "ACTIVE", "REVOKED", name="driver_device_status", create_type=False
            ),
            nullable=False,
            server_default="ACTIVE",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("device_token_hash", name="uq_driver_devices_token_hash"),
    )
    op.create_index("ix_driver_devices_driver_id", "driver_devices", ["driver_id"])
    op.create_index(
        "ix_driver_devices_device_token_hash", "driver_devices", ["device_token_hash"]
    )
    op.create_index(
        "ix_driver_devices_device_id_hash", "driver_devices", ["device_id_hash"]
    )
    op.create_index(
        "uq_driver_devices_active_driver",
        "driver_devices",
        ["driver_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )


def downgrade() -> None:
    op.drop_index("uq_driver_devices_active_driver", table_name="driver_devices")
    op.drop_index("ix_driver_devices_device_id_hash", table_name="driver_devices")
    op.drop_index("ix_driver_devices_device_token_hash", table_name="driver_devices")
    op.drop_index("ix_driver_devices_driver_id", table_name="driver_devices")
    op.drop_table("driver_devices")
    postgresql.ENUM(name="driver_device_status").drop(op.get_bind(), checkfirst=True)

    op.drop_index("ix_driver_applications_created_at", table_name="driver_applications")
    op.drop_index("ix_driver_applications_folio_ctm", table_name="driver_applications")
    op.drop_index("ix_driver_applications_phone", table_name="driver_applications")
    op.drop_index("ix_driver_applications_status", table_name="driver_applications")
    op.drop_table("driver_applications")
    postgresql.ENUM(name="driver_account_status").drop(op.get_bind(), checkfirst=True)

    op.drop_index("ix_vehicles_folio_ctm", table_name="vehicles")
    op.drop_column("vehicles", "folio_ctm")

    op.drop_column("drivers", "unit_role")
    postgresql.ENUM(name="driver_unit_role").drop(op.get_bind(), checkfirst=True)
    op.drop_index("ix_drivers_folio_ctm", table_name="drivers")
    op.drop_column("drivers", "folio_ctm")
    op.drop_column("drivers", "must_set_pin")
