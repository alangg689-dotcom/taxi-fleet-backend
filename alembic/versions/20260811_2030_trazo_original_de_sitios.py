"""Guarda el trazo original del sitio, sin holgura, junto al poligono.

Hasta ahora solo se guardaba `polygon`, que es el resultado de ST_Buffer
sobre lo que trazo el operador. Eso costaba dos cosas:

1. ST_Buffer redondea cada esquina en 8 segmentos por cuadrante, asi que un
   cuadrado de 4 esquinas se guarda con ~36 vertices y un trazo de 10 con
   ~90. Cuando el dashboard abria ese poligono para "ajustar vertices",
   leaflet-draw ponia dos manijas por vertice (la del vertice y la del punto
   medio) y quedaban cientos de manijas encimadas: imposible de usar.
2. No habia forma de cambiar la holgura de un sitio ya creado sin volver a
   trazarlo, porque el trazo sin holgura no existia en ningun lado (estaba
   dicho tal cual en el docstring de StandUpdate).

Con `outline` las dos se resuelven: el operador edita SU trazo (pocos
vertices) y el servidor recalcula el poligono con ST_Buffer al guardar.

Backfill: para los sitios que ya existen se aproxima el trazo original
erosionando el poligono guardado (ST_Buffer con distancia negativa) y
simplificandolo, porque erosionar deja las esquinas redondeadas y volveriamos
al problema 1. Es una APROXIMACION, no el trazo real, que se perdio. No toca
`polygon`: ninguna geocerca se mueve por esta migracion. La aproximacion solo
se materializa el dia que un operador edite ese sitio a proposito, viendolo
en el mapa. Los sitios placeholder de la 0008 se quedan en NULL — no tienen
trazo real que aproximar, hay que trazarlos.

Revision ID: 0012
Revises: 0011
"""

from collections.abc import Sequence

import geoalchemy2
import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tolerancia de simplificacion, en grados, proporcional a la holgura: para
# borrar el arco redondeado de una esquina hay que superar su sagita, que es
# r*(1-cos45) ~= 0.29*r. Con 0.6*r se pasa de sobra sin deformar tramos
# rectos. 111320 m por grado de latitud.
_SIMPLIFY_TOLERANCE = "(polygon_buffer_meters * 0.6 / 111320.0)"

_ERODED = "ST_Buffer(polygon, -polygon_buffer_meters)::geometry"


def upgrade() -> None:
    op.add_column(
        "stands",
        sa.Column(
            "outline",
            geoalchemy2.Geography(geometry_type="POLYGON", srid=4326, spatial_index=False),
            nullable=True,
        ),
    )
    # Sin indice espacial a proposito: outline nunca se consulta por
    # ST_Intersects ni por cercania, solo se lee y escribe por id del sitio.
    op.execute(
        f"""
        UPDATE stands
        SET outline = ST_SimplifyPreserveTopology({_ERODED}, {_SIMPLIFY_TOLERANCE})::geography
        WHERE is_placeholder = false
          AND polygon_buffer_meters > 0
          AND NOT ST_IsEmpty({_ERODED})
          AND GeometryType({_ERODED}) = 'POLYGON'
        """
    )


def downgrade() -> None:
    op.drop_column("stands", "outline")
