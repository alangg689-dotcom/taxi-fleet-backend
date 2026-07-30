"""Endpoints de telemetría GPS.

Este es el camino caliente del sistema: con 100 unidades reportando cada 5-10
segundos son ~10-20 escrituras por segundo en régimen normal, más las ráfagas
de los buffers offline.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from geoalchemy2 import Geometry
from sqlalchemy import cast, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_vehicle_by_device_key, require_roles
from app.core.redis_client import (
    get_all_last_positions,
    get_last_position,
    publish_location_update,
    set_last_position,
)
from app.database import get_db
from app.models import LocationPing, User, UserRole, Vehicle
from app.schemas.location import (
    BatchAccepted,
    LocationBatchIn,
    LocationOut,
    LocationPingIn,
    NearbyVehicle,
    PositionSummary,
)

router = APIRouter(tags=["telemetría"])
staff_only = require_roles(UserRole.OPERATOR, UserRole.ADMIN)


def _point(lat: float, lng: float) -> str:
    """WKT en el orden que espera PostGIS: longitud primero, luego latitud."""
    return f"SRID=4326;POINT({lng} {lat})"


async def _persist_pings(
    db: AsyncSession, vehicle_id: uuid.UUID, pings: list[LocationPingIn]
) -> int:
    """Inserta el lote en una sola sentencia y descarta duplicados."""
    rows = [
        {
            "id": uuid.uuid4(),
            "vehicle_id": vehicle_id,
            "location": _point(p.lat, p.lng),
            "speed": p.speed,
            "heading": p.heading,
            "accuracy": p.accuracy,
            "timestamp": p.timestamp,
        }
        for p in pings
    ]
    stmt = pg_insert(LocationPing).values(rows).on_conflict_do_nothing(
        constraint="uq_ping_vehicle_time"
    )
    result = await db.execute(stmt)
    return result.rowcount or 0


async def _broadcast_latest(vehicle_id: uuid.UUID, ping: LocationPingIn) -> None:
    """Actualiza el cache y publica el evento para todos los dashboards."""
    payload = {
        "vehicle_id": str(vehicle_id),
        "lat": ping.lat,
        "lng": ping.lng,
        "speed": ping.speed,
        "heading": ping.heading,
        "accuracy": ping.accuracy,
        "timestamp": ping.timestamp.isoformat(),
    }
    await set_last_position(str(vehicle_id), payload)
    await publish_location_update(payload)


@router.post(
    "/location/ping",
    response_model=BatchAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def ingest_pings(
    payload: LocationBatchIn,
    vehicle: Vehicle = Depends(get_vehicle_by_device_key),
    db: AsyncSession = Depends(get_db),
):
    """Recibe uno o varios pings de una unidad.

    Un solo endpoint cubre los dos casos: en operación normal la app manda un
    ping por petición; al recuperar señal tras un túnel manda el buffer
    acumulado. Del lote solo se difunde el ping más reciente por hora de
    captura, porque el mapa únicamente necesita la posición actual — el resto
    ya quedó guardado para el historial de rutas.
    """
    ordered = sorted(payload.pings, key=lambda p: p.timestamp)
    accepted = await _persist_pings(db, vehicle.id, ordered)
    await _broadcast_latest(vehicle.id, ordered[-1])

    return BatchAccepted(
        accepted=accepted, duplicates=len(ordered) - accepted
    )


@router.get("/vehicles/{vehicle_id}/location", response_model=LocationOut)
async def last_location(
    vehicle_id: uuid.UUID, _: User = Depends(staff_only)
):
    """Última posición conocida, leída del cache en memoria."""
    cached = await get_last_position(str(vehicle_id))
    if cached is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Sin posición reciente para esta unidad"
        )
    return LocationOut(**cached)


@router.get("/vehicles/locations", response_model=list[LocationOut])
async def all_locations(_: User = Depends(staff_only)):
    """Snapshot de toda la flota: lo que el dashboard pinta al abrir el mapa."""
    return [LocationOut(**p) for p in await get_all_last_positions()]


@router.get("/vehicles/nearby", response_model=list[NearbyVehicle])
async def nearby_vehicles(
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    radius: int = Query(3000, ge=100, le=50000, description="Radio en metros"),
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(staff_only),
):
    """Unidades dentro de un radio, ordenadas por cercanía.

    Usa ST_DWithin sobre el índice GiST de la columna geography: por eso la
    posición se guarda como GEOGRAPHY(Point, 4326) y no como dos decimales.
    Con lat/lng sueltos habría que calcular Haversine fila por fila, sin índice.

    DISTINCT ON toma el ping más reciente de cada unidad; se acota a la última
    hora para no recorrer todo el historial.
    """
    query = text(
        """
        SELECT DISTINCT ON (p.vehicle_id)
               p.vehicle_id,
               v.plate,
               ST_Y(p.location::geometry) AS lat,
               ST_X(p.location::geometry) AS lng,
               ST_Distance(p.location, :origin::geography) AS distance_m,
               p.timestamp
        FROM location_pings p
        JOIN vehicles v ON v.id = p.vehicle_id
        WHERE p.timestamp > now() - interval '1 hour'
          AND ST_DWithin(p.location, :origin::geography, :radius)
        ORDER BY p.vehicle_id, p.timestamp DESC
        """
    )
    result = await db.execute(
        query, {"origin": _point(lat, lng), "radius": radius}
    )
    rows = sorted(result.mappings().all(), key=lambda r: r["distance_m"])[:limit]
    return [NearbyVehicle(**row) for row in rows]


@router.get("/vehicles/{vehicle_id}/history", response_model=list[LocationOut])
async def location_history(
    vehicle_id: uuid.UUID,
    since: datetime = Query(..., description="Inicio del rango (ISO 8601)"),
    until: datetime = Query(..., description="Fin del rango (ISO 8601)"),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(staff_only),
):
    """Recorrido de una unidad en un rango de fechas."""
    result = await db.execute(
        select(
            LocationPing.vehicle_id,
            func.ST_Y(cast(LocationPing.location, Geometry)).label("lat"),
            func.ST_X(cast(LocationPing.location, Geometry)).label("lng"),
            LocationPing.speed,
            LocationPing.heading,
            LocationPing.accuracy,
            LocationPing.timestamp,
        )
        .where(
            LocationPing.vehicle_id == vehicle_id,
            LocationPing.timestamp.between(since, until),
        )
        .order_by(LocationPing.timestamp)
    )
    return [LocationOut(**row) for row in result.mappings().all()]


@router.get(
    "/vehicles/{vehicle_id}/history/summary", response_model=list[PositionSummary]
)
async def location_history_summary(
    vehicle_id: uuid.UUID,
    since: datetime = Query(..., description="Inicio del rango (ISO 8601)"),
    until: datetime = Query(..., description="Fin del rango (ISO 8601)"),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(staff_only),
):
    """Posición promedio cada 5 minutos, para reportería sobre rangos largos.

    Lee del agregado continuo `vehicle_position_5min` en vez de
    location_pings: en un rango de semanas o meses, promediar millones de
    pings crudos en cada consulta sería impracticable. TimescaleDB mantiene la
    vista al día con su propia política de refresco (hasta ~5 min de rezago).
    """
    result = await db.execute(
        text(
            """
            SELECT vehicle_id, bucket, avg_lat, avg_lng, avg_speed, max_speed, ping_count
            FROM vehicle_position_5min
            WHERE vehicle_id = :vehicle_id AND bucket BETWEEN :since AND :until
            ORDER BY bucket
            """
        ),
        {"vehicle_id": vehicle_id, "since": since, "until": until},
    )
    return [PositionSummary(**row) for row in result.mappings().all()]
