"""Endpoints de sitios.

POST/PATCH construyen el polígono en la propia base con PostGIS
(ST_Buffer + ST_GeomFromGeoJSON) en vez de esperar que el cliente calcule
la holgura o el centroide — así el operador solo traza el contorno real
del sitio. El resto de la sección 9 de la spec (fila, reordenar, broadcast
stand:queue) llega después; esto es lo mínimo para empezar a trazar los 6
sitios reales sobre los placeholders sembrados en el paso 3.
"""

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.deps import require_roles
from app.core.stands import get_stand_queue, remove_from_queue, reorder_queue
from app.database import get_db
from app.models import Stand, StandQueue, StandQueueStatus, User, UserRole
from app.schemas.stand import (
    QueuePositionOut,
    QueueReorderRequest,
    StandCreate,
    StandDetail,
    StandOut,
    StandUpdate,
)

router = APIRouter(prefix="/stands", tags=["stands"])

staff_only = require_roles(UserRole.OPERATOR, UserRole.ADMIN)
# Trazar/mover un sitio es configuración de flotilla, no operación diaria
# — mismo nivel que dar de alta una unidad (ver app.api.vehicles).
admin_only = require_roles(UserRole.ADMIN)

_DETAIL_SELECT = """
    SELECT id, name, active, is_placeholder, still_seconds, max_speed_kmh, polygon_buffer_meters,
           ST_AsGeoJSON(polygon::geometry) AS polygon_geojson,
           ST_Y(center::geometry) AS center_lat, ST_X(center::geometry) AS center_lng
    FROM stands WHERE id = :id
"""


async def _get_detail_row(db: AsyncSession, stand_id: uuid.UUID):
    row = (await db.execute(text(_DETAIL_SELECT), {"id": stand_id})).mappings().first()
    if row is None:
        return None
    return {**row, "polygon_geojson": json.loads(row["polygon_geojson"])}


async def _get_detail_or_404(db: AsyncSession, stand_id: uuid.UUID) -> dict:
    row = await _get_detail_row(db, stand_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sitio no encontrado")
    return row


async def _find_overlap(
    db: AsyncSession, polygon_geojson: dict, buffer_m: int, *, exclude_id: uuid.UUID | None = None
):
    """ST_Intersects contra el resto de sitios activos — protección contra
    un polígono mal trazado que se encima con otro (mencionado como
    pendiente en el docstring de app.models.stand desde el paso 3)."""
    query = """
        SELECT id, name FROM stands
        WHERE active = true
          AND ST_Intersects(
                polygon,
                ST_Buffer(ST_SetSRID(ST_GeomFromGeoJSON(:geojson), 4326)::geography, :buffer_m)
              )
    """
    params: dict = {"geojson": json.dumps(polygon_geojson), "buffer_m": buffer_m}
    if exclude_id is not None:
        query += " AND id != :exclude_id"
        params["exclude_id"] = exclude_id
    query += " LIMIT 1"
    return (await db.execute(text(query), params)).mappings().first()


@router.get("", response_model=list[StandOut])
async def list_stands(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(staff_only),
):
    result = await db.execute(select(Stand).order_by(Stand.name))
    return list(result.scalars().all())


@router.get("/{stand_id}", response_model=StandDetail)
async def get_stand(
    stand_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(staff_only),
):
    return await _get_detail_or_404(db, stand_id)


@router.post("", response_model=StandDetail, status_code=status.HTTP_201_CREATED)
async def create_stand(
    payload: StandCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(admin_only),
):
    buffer_m = payload.buffer_meters if payload.buffer_meters is not None else settings.STAND_POLYGON_BUFFER_METERS
    still_seconds = (
        payload.still_seconds if payload.still_seconds is not None else settings.STAND_STILL_SECONDS_DEFAULT
    )
    max_speed_kmh = payload.max_speed_kmh if payload.max_speed_kmh is not None else settings.STAND_MAX_SPEED_KMH

    overlap = await _find_overlap(db, payload.polygon_geojson, buffer_m)
    if overlap is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"El polígono se encima con el sitio «{overlap['name']}»"
        )

    new_id = uuid.uuid4()
    await db.execute(
        text(
            """
            INSERT INTO stands
                (id, name, polygon, center, still_seconds, max_speed_kmh, polygon_buffer_meters, active, is_placeholder)
            VALUES (
                :id, :name,
                ST_Buffer(ST_SetSRID(ST_GeomFromGeoJSON(:geojson), 4326)::geography, :buffer_m),
                ST_SetSRID(ST_Centroid(ST_GeomFromGeoJSON(:geojson)), 4326)::geography,
                :still_seconds, :max_speed_kmh, :buffer_m, :active, false
            )
            """
        ),
        {
            "id": new_id,
            "name": payload.name,
            "geojson": json.dumps(payload.polygon_geojson),
            "buffer_m": buffer_m,
            "still_seconds": still_seconds,
            "max_speed_kmh": max_speed_kmh,
            "active": payload.active,
        },
    )
    await db.commit()
    return await _get_detail_or_404(db, new_id)


@router.patch("/{stand_id}", response_model=StandDetail)
async def update_stand(
    stand_id: uuid.UUID,
    payload: StandUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(admin_only),
):
    """Parcial: solo toca lo que venga en el payload. Mandar
    polygon_geojson reemplaza el polígono y apaga is_placeholder sin tocar
    la fila existente — las unidades ya formadas se quedan formadas, la
    máquina de estados usa el polígono nuevo desde el siguiente ping."""
    current = await _get_detail_or_404(db, stand_id)
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return current

    set_clauses: list[str] = []
    params: dict = {"id": stand_id}

    if "polygon_geojson" in updates:
        buffer_m = updates.get("buffer_meters", current["polygon_buffer_meters"])
        overlap = await _find_overlap(db, updates["polygon_geojson"], buffer_m, exclude_id=stand_id)
        if overlap is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"El polígono se encima con el sitio «{overlap['name']}»"
            )
        set_clauses += [
            "polygon = ST_Buffer(ST_SetSRID(ST_GeomFromGeoJSON(:geojson), 4326)::geography, :buffer_m)",
            "center = ST_SetSRID(ST_Centroid(ST_GeomFromGeoJSON(:geojson)), 4326)::geography",
            "polygon_buffer_meters = :buffer_m",
            "is_placeholder = false",
        ]
        params["geojson"] = json.dumps(updates["polygon_geojson"])
        params["buffer_m"] = buffer_m
    elif "buffer_meters" in updates:
        # Sin polygon_geojson no hay forma de volver a aplicar el buffer
        # (no se guarda el trazo original sin holgura) — solo actualiza el
        # número; ver docstring de StandUpdate.
        set_clauses.append("polygon_buffer_meters = :buffer_m")
        params["buffer_m"] = updates["buffer_meters"]

    for field in ("name", "still_seconds", "max_speed_kmh", "active"):
        if field in updates:
            set_clauses.append(f"{field} = :{field}")
            params[field] = updates[field]

    if set_clauses:
        await db.execute(text(f"UPDATE stands SET {', '.join(set_clauses)} WHERE id = :id"), params)
        await db.commit()

    return await _get_detail_or_404(db, stand_id)


# --- Fila del sitio -------------------------------------------------------------


@router.get("/{stand_id}/queue", response_model=list[QueuePositionOut])
async def get_queue(
    stand_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(staff_only),
):
    if await db.get(Stand, stand_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sitio no encontrado")
    return await get_stand_queue(db, stand_id)


@router.post("/{stand_id}/queue/reorder", response_model=list[QueuePositionOut])
async def reorder_stand_queue(
    stand_id: uuid.UUID,
    payload: QueueReorderRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(staff_only),
):
    """Override manual del orden (evento operator_override) — para
    disputas en la fila. `vehicle_ids` debe traer exactamente las unidades
    formadas hoy en este sitio; si no calza, 422 antes de tocar nada."""
    if await db.get(Stand, stand_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sitio no encontrado")
    try:
        return await reorder_queue(db, stand_id, payload.vehicle_ids)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.delete("/{stand_id}/queue/{vehicle_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_vehicle_from_queue(
    stand_id: uuid.UUID,
    vehicle_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(staff_only),
):
    """Salida manual (evento operator_override) — el chofer se fue sin
    avisar, o un ajuste que no puede esperar al ciclo automático."""
    result = await db.execute(
        select(StandQueue).where(
            StandQueue.stand_id == stand_id,
            StandQueue.vehicle_id == vehicle_id,
            StandQueue.status == StandQueueStatus.FORMADO,
        )
    )
    entry = result.scalar_one_or_none()
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Esa unidad no está formada en este sitio")
    await remove_from_queue(db, entry)
