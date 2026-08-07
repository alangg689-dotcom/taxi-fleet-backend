"""Sitios y fila de espera (spec-sitios-y-fila-v2.md, sección 4).

Crea `stands`, `stand_queue`, `stand_queue_events`, el enum
`stand_queue_status`, y `vehicles.stand_id`.

Son 6 sitios fijos por decisión de negocio — esta migración los siembra
como PLACEHOLDER (nombre + un cuadro de ~200m alrededor de un punto de
referencia, sin polígono real trazado todavía) y reparte las unidades
existentes entre ellos de forma round-robin, porque `vehicles.stand_id` va
NOT NULL y no hay manera de dejarlo vacío. Un operador debe:
  1. Trazar los 6 polígonos reales vía el dashboard en cuanto exista esa
     pantalla (paso 7 de la spec) — PATCH /stands/{id} los reemplaza sin
     tocar la fila.
  2. Reasignar cada unidad a su sitio correcto (el round-robin de aquí es
     solo para satisfacer la restricción, no una asignación real).

Revision ID: 0008
Revises: 0007
"""

import uuid
from collections.abc import Sequence

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (nombre, lat, lng) del centro de referencia — Empalme/Guaymas, Sonora,
# donde se ha probado el resto del proyecto. Puros placeholders.
_PLACEHOLDER_STANDS = [
    ("Sitio 1 (pendiente de trazar)", 27.9461, -110.9282),
    ("Sitio 2 (pendiente de trazar)", 27.9500, -110.9250),
    ("Sitio 3 (pendiente de trazar)", 27.9420, -110.9320),
    ("Sitio 4 (pendiente de trazar)", 27.9550, -110.9200),
    ("Sitio 5 (pendiente de trazar)", 27.9380, -110.9350),
    ("Sitio 6 (pendiente de trazar)", 27.9480, -110.9400),
]


def upgrade() -> None:
    op.create_table(
        "stands",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column(
            "polygon", geoalchemy2.Geography(geometry_type="POLYGON", srid=4326), nullable=False
        ),
        sa.Column(
            "center", geoalchemy2.Geography(geometry_type="POINT", srid=4326), nullable=False
        ),
        sa.Column("still_seconds", sa.Integer(), nullable=False, server_default="45"),
        sa.Column("max_speed_kmh", sa.Float(), nullable=False, server_default="5.0"),
        sa.Column("polygon_buffer_meters", sa.Integer(), nullable=False, server_default="15"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    # No se llama .create() aparte: create_table ya lo hace al toparse con
    # esta columna (llamarlo dos veces revienta con "type already exists").
    stand_queue_status = postgresql.ENUM(
        "formado", "asignado", "salio", name="stand_queue_status"
    )

    op.create_table(
        "stand_queue",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "stand_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("stands.id", ondelete="CASCADE"),
            nullable=False,
        ),
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
            "entered_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("status", stand_queue_status, nullable=False, server_default="formado"),
        sa.Column("position_held", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("left_at", sa.DateTime(timezone=True)),
        sa.Column("left_reason", sa.String(length=30)),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_stand_queue_stand_id", "stand_queue", ["stand_id"])
    op.create_index("ix_stand_queue_vehicle_id", "stand_queue", ["vehicle_id"])
    op.create_index("ix_stand_queue_driver_id", "stand_queue", ["driver_id"])
    op.create_index("ix_queue_stand_status", "stand_queue", ["stand_id", "status"])
    # Parcial: una unidad no puede estar en dos filas a la vez, pero el
    # historial (asignado/salio) sí puede repetirse por unidad.
    op.create_index(
        "uq_queue_vehicle_active",
        "stand_queue",
        ["vehicle_id"],
        unique=True,
        postgresql_where=sa.text("status = 'formado'"),
    )

    op.create_table(
        "stand_queue_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "vehicle_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vehicles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "driver_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("drivers.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "stand_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("stands.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event", sa.String(length=30), nullable=False),
        sa.Column("detail", postgresql.JSONB()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_stand_queue_events_vehicle_id", "stand_queue_events", ["vehicle_id"])
    op.create_index("ix_stand_queue_events_stand_id", "stand_queue_events", ["stand_id"])
    op.create_index("ix_stand_queue_events_created_at", "stand_queue_events", ["created_at"])

    # --- vehicles.stand_id: nullable primero, sembrar+backfill, luego NOT NULL ---
    op.add_column(
        "vehicles",
        sa.Column(
            "stand_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("stands.id"), nullable=True
        ),
    )
    op.create_index("ix_vehicles_stand_id", "vehicles", ["stand_id"])

    conn = op.get_bind()
    stand_ids: list[uuid.UUID] = []
    for name, lat, lng in _PLACEHOLDER_STANDS:
        d = 0.001  # ~110m de lado — holgado, es placeholder
        polygon_wkt = (
            f"SRID=4326;POLYGON(({lng - d} {lat - d}, {lng + d} {lat - d}, "
            f"{lng + d} {lat + d}, {lng - d} {lat + d}, {lng - d} {lat - d}))"
        )
        point_wkt = f"SRID=4326;POINT({lng} {lat})"
        stand_id = uuid.uuid4()
        conn.execute(
            sa.text(
                """
                INSERT INTO stands (id, name, polygon, center, created_at)
                VALUES (:id, :name, ST_GeogFromText(:polygon), ST_GeogFromText(:center), now())
                """
            ),
            {"id": stand_id, "name": name, "polygon": polygon_wkt, "center": point_wkt},
        )
        stand_ids.append(stand_id)

    vehicle_rows = conn.execute(sa.text("SELECT id FROM vehicles ORDER BY created_at")).fetchall()
    for i, (vehicle_id,) in enumerate(vehicle_rows):
        conn.execute(
            sa.text("UPDATE vehicles SET stand_id = :stand_id WHERE id = :vehicle_id"),
            {"stand_id": stand_ids[i % len(stand_ids)], "vehicle_id": vehicle_id},
        )

    op.alter_column("vehicles", "stand_id", nullable=False)


def downgrade() -> None:
    op.drop_index("ix_vehicles_stand_id", table_name="vehicles")
    op.drop_column("vehicles", "stand_id")
    op.drop_table("stand_queue_events")
    op.drop_index("uq_queue_vehicle_active", table_name="stand_queue")
    op.drop_table("stand_queue")
    postgresql.ENUM(name="stand_queue_status").drop(op.get_bind())
    op.drop_table("stands")
