r"""Endpoints del ciclo de vida de un viaje.

El despachador ya conoce la unidad y el chofer al crear el viaje (no hay
"marketplace" de choferes disponibles, es una operadora asignando llamadas),
así que vehicle_id/driver_id se capturan desde el alta. El estado avanza así:

    SOLICITADO --accept--> ASIGNADO --start--> EN_CURSO --complete--> COMPLETADO
                    \_______________________________________/
                                      \--cancel--> CANCELADO

`accept`/`start`/`complete` los dispara normalmente la app del chofer; `cancel`
puede venir de cualquiera de los dos lados.
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from geoalchemy2 import Geometry
from sqlalchemy import cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.location import _point
from app.core.deps import require_roles
from app.database import get_db
from app.models import Driver, Trip, TripStatus, User, UserRole, Vehicle
from app.schemas.trip import TripCreate, TripOut

router = APIRouter(prefix="/trips", tags=["viajes"])

staff_only = require_roles(UserRole.OPERATOR, UserRole.ADMIN)
driver_or_staff = require_roles(UserRole.DRIVER, UserRole.OPERATOR, UserRole.ADMIN)

_ACTIVE_STATUSES = (TripStatus.SOLICITADO, TripStatus.ASIGNADO, TripStatus.EN_CURSO)


def _trip_columns():
    """Columnas del viaje con origin/destination ya convertidos a lat/lng.

    Igual que en location.py: la geometría nunca se lee como atributo del
    objeto ORM, se proyecta con ST_X/ST_Y en la propia consulta.
    """
    return (
        Trip.id,
        Trip.vehicle_id,
        Trip.driver_id,
        func.ST_Y(cast(Trip.origin, Geometry)).label("origin_lat"),
        func.ST_X(cast(Trip.origin, Geometry)).label("origin_lng"),
        Trip.origin_address,
        func.ST_Y(cast(Trip.destination, Geometry)).label("destination_lat"),
        func.ST_X(cast(Trip.destination, Geometry)).label("destination_lng"),
        Trip.destination_address,
        Trip.status,
        Trip.requested_at,
        Trip.started_at,
        Trip.completed_at,
    )


async def _get_trip_out(db: AsyncSession, trip_id: uuid.UUID) -> TripOut:
    result = await db.execute(select(*_trip_columns()).where(Trip.id == trip_id))
    return TripOut(**result.mappings().one())


async def _authorize_trip(trip: Trip, user: User, db: AsyncSession) -> None:
    """Un chofer solo puede tocar sus propios viajes; staff puede con todos."""
    if user.role != UserRole.DRIVER:
        return
    result = await db.execute(select(Driver).where(Driver.user_id == user.id))
    driver = result.scalar_one_or_none()
    if driver is None or trip.driver_id != driver.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No tienes acceso a este viaje")


async def _get_trip_or_404(db: AsyncSession, trip_id: uuid.UUID) -> Trip:
    trip = await db.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Viaje no encontrado")
    return trip


def _apply_transition(trip: Trip, expected: TripStatus, new: TripStatus) -> None:
    if trip.status != expected:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"El viaje está en '{trip.status.value}', se esperaba '{expected.value}'",
        )
    trip.status = new


@router.post("", response_model=TripOut, status_code=status.HTTP_201_CREATED)
async def create_trip(
    payload: TripCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(staff_only),
):
    if await db.get(Vehicle, payload.vehicle_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unidad no encontrada")
    if await db.get(Driver, payload.driver_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chofer no encontrado")

    active = await db.execute(
        select(Trip.id).where(
            Trip.vehicle_id == payload.vehicle_id, Trip.status.in_(_ACTIVE_STATUSES)
        )
    )
    if active.scalar_one_or_none() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "La unidad ya tiene un viaje activo")

    destination = (
        _point(payload.destination_lat, payload.destination_lng)
        if payload.destination_lat is not None
        else None
    )
    trip = Trip(
        vehicle_id=payload.vehicle_id,
        driver_id=payload.driver_id,
        origin=_point(payload.origin_lat, payload.origin_lng),
        origin_address=payload.origin_address,
        destination=destination,
        destination_address=payload.destination_address,
    )
    db.add(trip)
    await db.flush()
    return await _get_trip_out(db, trip.id)


@router.get("", response_model=list[TripOut])
async def list_trips(
    trip_status: TripStatus | None = Query(None, alias="status"),
    vehicle_id: uuid.UUID | None = None,
    driver_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(driver_or_staff),
):
    """Staff ve toda la flota y puede filtrar por cualquier driver_id/vehicle_id.

    Un chofer solo ve sus propios viajes: es como se resuelve "mis viajes" sin
    un endpoint aparte (que además chocaría en el orden de rutas con
    `/trips/{trip_id}`, igual que pasa con `/vehicles/nearby`). `driver_id` se
    ignora si lo manda un chofer — se fuerza al suyo, así `GET /trips` sin
    argumentos ya le sirve a la app.
    """
    query = select(*_trip_columns()).order_by(Trip.requested_at.desc())

    if user.role == UserRole.DRIVER:
        own_driver = await db.execute(select(Driver).where(Driver.user_id == user.id))
        driver = own_driver.scalar_one_or_none()
        if driver is None:
            return []
        query = query.where(Trip.driver_id == driver.id)
    elif driver_id is not None:
        query = query.where(Trip.driver_id == driver_id)

    if trip_status is not None:
        query = query.where(Trip.status == trip_status)
    if vehicle_id is not None:
        query = query.where(Trip.vehicle_id == vehicle_id)

    result = await db.execute(query)
    return [TripOut(**row) for row in result.mappings().all()]


@router.get("/{trip_id}", response_model=TripOut)
async def get_trip(
    trip_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(driver_or_staff),
):
    trip = await _get_trip_or_404(db, trip_id)
    await _authorize_trip(trip, user, db)
    return await _get_trip_out(db, trip_id)


@router.post("/{trip_id}/accept", response_model=TripOut)
async def accept_trip(
    trip_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(driver_or_staff),
):
    """El chofer confirma que toma el viaje asignado."""
    trip = await _get_trip_or_404(db, trip_id)
    await _authorize_trip(trip, user, db)
    _apply_transition(trip, TripStatus.SOLICITADO, TripStatus.ASIGNADO)
    return await _get_trip_out(db, trip_id)


@router.post("/{trip_id}/start", response_model=TripOut)
async def start_trip(
    trip_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(driver_or_staff),
):
    """El chofer recogió al pasajero y el viaje arranca."""
    trip = await _get_trip_or_404(db, trip_id)
    await _authorize_trip(trip, user, db)
    _apply_transition(trip, TripStatus.ASIGNADO, TripStatus.EN_CURSO)
    trip.started_at = datetime.now(UTC)
    return await _get_trip_out(db, trip_id)


@router.post("/{trip_id}/complete", response_model=TripOut)
async def complete_trip(
    trip_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(driver_or_staff),
):
    trip = await _get_trip_or_404(db, trip_id)
    await _authorize_trip(trip, user, db)
    _apply_transition(trip, TripStatus.EN_CURSO, TripStatus.COMPLETADO)
    trip.completed_at = datetime.now(UTC)
    return await _get_trip_out(db, trip_id)


@router.post("/{trip_id}/cancel", response_model=TripOut)
async def cancel_trip(
    trip_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(driver_or_staff),
):
    trip = await _get_trip_or_404(db, trip_id)
    await _authorize_trip(trip, user, db)
    if trip.status not in _ACTIVE_STATUSES:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"El viaje ya está '{trip.status.value}'"
        )
    trip.status = TripStatus.CANCELADO
    return await _get_trip_out(db, trip_id)
