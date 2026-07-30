"""Agregado continuo: posición promedio por unidad cada 5 minutos.

Para reportería sobre rangos largos (una semana, un mes) sin tener que leer
millones de pings crudos de location_pings en cada consulta. TimescaleDB
mantiene esta vista materializada al día con una política de refresco propia,
independiente del ciclo de vida de la app.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VIEW = "vehicle_position_5min"


def upgrade() -> None:
    # Promedio de lat/lng (no ST_Collect/ST_Centroid): avg() sobre un float es
    # una agregación estándar con soporte garantizado en continuous aggregates;
    # los agregados espaciales de PostGIS no todos lo tienen. Para el radio de
    # un bucket de 5 minutos el promedio simple de coordenadas es una
    # aproximación más que suficiente.
    op.execute(
        f"""
        CREATE MATERIALIZED VIEW {_VIEW}
        WITH (timescaledb.continuous) AS
        SELECT
            vehicle_id,
            time_bucket('5 minutes', timestamp) AS bucket,
            avg(ST_Y(location::geometry)) AS avg_lat,
            avg(ST_X(location::geometry)) AS avg_lng,
            avg(speed) AS avg_speed,
            max(speed) AS max_speed,
            count(*) AS ping_count
        FROM location_pings
        GROUP BY vehicle_id, bucket
        WITH NO DATA
        """
    )

    op.execute(
        f"""
        SELECT add_continuous_aggregate_policy('{_VIEW}',
            start_offset => INTERVAL '1 day',
            end_offset => INTERVAL '5 minutes',
            schedule_interval => INTERVAL '5 minutes')
        """
    )

    # refresh_continuous_aggregate es un PROCEDURE: Timescale exige que corra
    # fuera de un bloque transaccional explícito (internamente hace su propio
    # manejo de transacciones por tramos). autocommit_block saca este statement
    # de la transacción que Alembic abre para el resto de la migración.
    # NULL, NULL materializa todo el histórico ya existente de una sola vez;
    # en una tabla enorme convendría acotar el rango en vez de todo de un tirón.
    with op.get_context().autocommit_block():
        op.execute(f"CALL refresh_continuous_aggregate('{_VIEW}', NULL, NULL)")


def downgrade() -> None:
    op.execute(f"SELECT remove_continuous_aggregate_policy('{_VIEW}', if_exists => TRUE)")
    op.execute(f"DROP MATERIALIZED VIEW IF EXISTS {_VIEW}")
