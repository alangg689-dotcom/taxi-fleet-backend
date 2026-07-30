"""Esquema inicial: identidad, flotilla, telemetría y viajes.

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- Extensiones ---------------------------------------------------------
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")

    # --- Identidad -----------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(255), unique=True),
        sa.Column("phone", sa.String(20), unique=True),
        sa.Column("password_hash", sa.String(255)),
        sa.Column(
            "role",
            sa.Enum("driver", "operator", "admin", name="user_role"),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_users_email", "users", ["email"])
    op.create_index("ix_users_phone", "users", ["phone"])

    op.create_table(
        "drivers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            unique=True,
            nullable=False,
        ),
        sa.Column("full_name", sa.String(150), nullable=False),
        sa.Column("license_number", sa.String(50), unique=True, nullable=False),
        sa.Column(
            "status",
            sa.Enum("activo", "inactivo", name="driver_status"),
            nullable=False,
            server_default="activo",
        ),
    )

    op.create_table(
        "operators",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            unique=True,
            nullable=False,
        ),
        sa.Column("full_name", sa.String(150), nullable=False),
        sa.Column(
            "permission_level",
            sa.Enum("admin", "despachador", name="permission_level"),
            nullable=False,
            server_default="despachador",
        ),
    )

    op.create_table(
        "refresh_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(255), unique=True, nullable=False),
        sa.Column("device_info", sa.String(255)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_index("ix_refresh_tokens_token_hash", "refresh_tokens", ["token_hash"])

    # --- Flotilla ------------------------------------------------------------
    op.create_table(
        "vehicles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("plate", sa.String(15), unique=True, nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("year", sa.Integer),
        sa.Column(
            "status",
            sa.Enum(
                "disponible",
                "ocupado",
                "offline",
                "mantenimiento",
                name="vehicle_status",
            ),
            nullable=False,
            server_default="offline",
        ),
        sa.Column("device_key_hash", sa.String(255)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_vehicles_plate", "vehicles", ["plate"])
    op.create_index("ix_vehicles_device_key_hash", "vehicles", ["device_key_hash"])

    op.create_table(
        "vehicle_assignments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "vehicle_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vehicles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "driver_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("drivers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_assignment_vehicle_active", "vehicle_assignments", ["vehicle_id", "ended_at"]
    )
    # Una unidad no puede tener dos turnos abiertos al mismo tiempo.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_one_open_shift_per_vehicle
        ON vehicle_assignments (vehicle_id)
        WHERE ended_at IS NULL
        """
    )

    # --- Telemetría ----------------------------------------------------------
    op.create_table(
        "location_pings",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "vehicle_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vehicles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "location",
            geoalchemy2.Geography(geometry_type="POINT", srid=4326),
            nullable=False,
        ),
        sa.Column("speed", sa.Float),
        sa.Column("heading", sa.Float),
        sa.Column("accuracy", sa.Float),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # TimescaleDB exige que la columna de particionado forme parte de
        # cualquier índice único, incluida la llave primaria.
        sa.PrimaryKeyConstraint("id", "timestamp"),
        sa.UniqueConstraint("vehicle_id", "timestamp", name="uq_ping_vehicle_time"),
    )
    op.create_index("ix_ping_vehicle_time", "location_pings", ["vehicle_id", "timestamp"])

    # Convierte la tabla en hypertable: TimescaleDB la particiona por fecha de
    # forma transparente, en fragmentos de 7 días.
    op.execute(
        """
        SELECT create_hypertable(
            'location_pings', 'timestamp',
            chunk_time_interval => INTERVAL '7 days',
            migrate_data => TRUE
        )
        """
    )

    # Comprime los fragmentos con más de 30 días (ahorro típico de 90%+).
    op.execute(
        """
        ALTER TABLE location_pings SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'vehicle_id',
            timescaledb.compress_orderby = 'timestamp DESC'
        )
        """
    )
    op.execute(
        "SELECT add_compression_policy('location_pings', INTERVAL '30 days')"
    )

    # Retención: descarta el detalle crudo con más de un año.
    # Ajustar según los requisitos legales/fiscales de la operación.
    op.execute("SELECT add_retention_policy('location_pings', INTERVAL '365 days')")

    # --- Viajes --------------------------------------------------------------
    op.create_table(
        "trips",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "vehicle_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vehicles.id"),
            nullable=False,
        ),
        sa.Column(
            "driver_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("drivers.id"),
            nullable=False,
        ),
        sa.Column(
            "origin",
            geoalchemy2.Geography(geometry_type="POINT", srid=4326),
            nullable=False,
        ),
        sa.Column(
            "destination", geoalchemy2.Geography(geometry_type="POINT", srid=4326)
        ),
        sa.Column("origin_address", sa.String(255)),
        sa.Column("destination_address", sa.String(255)),
        sa.Column(
            "status",
            sa.Enum(
                "solicitado",
                "asignado",
                "en_curso",
                "completado",
                "cancelado",
                name="trip_status",
            ),
            nullable=False,
            server_default="solicitado",
        ),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_trips_vehicle_id", "trips", ["vehicle_id"])
    op.create_index("ix_trips_driver_id", "trips", ["driver_id"])
    op.create_index("ix_trips_status", "trips", ["status"])


def downgrade() -> None:
    op.drop_table("trips")
    op.drop_table("location_pings")
    op.drop_table("vehicle_assignments")
    op.drop_table("vehicles")
    op.drop_table("refresh_tokens")
    op.drop_table("operators")
    op.drop_table("drivers")
    op.drop_table("users")

    for enum_name in (
        "trip_status",
        "vehicle_status",
        "permission_level",
        "driver_status",
        "user_role",
    ):
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")
