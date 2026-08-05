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

import asyncio
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from geoalchemy2 import Geometry
from sqlalchemy import cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.location import _point
from app.core.deps import require_roles
from app.core.dispatch import dispatch_trip
from app.core.whatsapp import send_whatsapp_message
from app.database import get_db
from app.models import Driver, Trip, TripStatus, User, UserRole, Vehicle, VehicleAssignment, VehicleStatus
from app.schemas.trip import (
    TripComplete,
    TripCreate,
    TripDispatchCreate,
    TripOut,
    TripStreetHailCreate,
)

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
        Trip.fare,
        Trip.offered_driver_id,
        Trip.offered_vehicle_id,
        Trip.offer_expires_at,
    )


async def _get_trip_out(db: AsyncSession, trip_id: uuid.UUID) -> TripOut:
    result = await db.execute(select(*_trip_columns()).where(Trip.id == trip_id))
    return TripOut(**result.mappings().one())


async def _authorize_trip(trip: Trip, user: User, db: AsyncSession) -> None:
    """Un chofer solo puede tocar sus propios viajes o uno que el motor de
    despacho le esté ofreciendo todavía (driver_id sigue vacío hasta que lo
    acepta); staff puede con todos."""
    if user.role != UserRole.DRIVER:
        return
    result = await db.execute(select(Driver).where(Driver.user_id == user.id))
    driver = result.scalar_one_or_none()
    is_own_trip = driver is not None and trip.driver_id == driver.id
    is_offered_to_driver = driver is not None and trip.offered_driver_id == driver.id
    if not is_own_trip and not is_offered_to_driver:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No tienes acceso a este viaje")


async def _get_trip_or_404(db: AsyncSession, trip_id: uuid.UUID) -> Trip:
    trip = await db.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Viaje no encontrado")
    return trip


async def _get_own_driver_or_403(db: AsyncSession, user: User) -> Driver:
    if user.role != UserRole.DRIVER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Esta acción es solo para choferes")
    result = await db.execute(select(Driver).where(Driver.user_id == user.id))
    driver = result.scalar_one_or_none()
    if driver is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Esta acción es solo para choferes")
    return driver


async def _set_vehicle_status(db: AsyncSession, vehicle_id: uuid.UUID | None, status_: VehicleStatus) -> None:
    """No toca offline/mantenimiento — esos son decisión de un operador, no
    algo que el ciclo de vida de un viaje deba pisar."""
    if vehicle_id is None:
        return
    vehicle = await db.get(Vehicle, vehicle_id)
    if vehicle is not None and vehicle.status in (VehicleStatus.DISPONIBLE, VehicleStatus.OCUPADO):
        vehicle.status = status_


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


@router.post("/dispatch", response_model=TripOut, status_code=status.HTTP_201_CREATED)
async def dispatch_new_trip(
    payload: TripDispatchCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(staff_only),
):
    """Crea un viaje SIN elegir unidad/chofer: el motor de despacho busca al
    chofer disponible más cercano y se lo ofrece, en cascada, hasta que
    alguien acepte. `staff_only` por ahora — es el mismo camino que más
    adelante llamará un bot de WhatsApp, pero internamente (sin pasar por
    este endpoint HTTP con auth de operador).

    La respuesta llega de inmediato con el viaje en 'solicitado' y sin
    driver_id todavía; el resultado del despacho se ve consultando
    GET /trips/{id} más tarde (pasa a 'asignado' si alguien acepta).
    """
    destination = (
        _point(payload.destination_lat, payload.destination_lng)
        if payload.destination_lat is not None
        else None
    )
    trip = Trip(
        origin=_point(payload.origin_lat, payload.origin_lng),
        origin_address=payload.origin_address,
        destination=destination,
        destination_address=payload.destination_address,
    )
    db.add(trip)
    await db.flush()
    trip_id = trip.id
    await db.commit()

    asyncio.create_task(dispatch_trip(trip_id))
    return await _get_trip_out(db, trip_id)


@router.post("/street-hail", response_model=TripOut, status_code=status.HTTP_201_CREATED)
async def start_street_hail(
    payload: TripStreetHailCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(driver_or_staff),
):
    """Corte de calle: el chofer toma un pasaje directo sin operador ni motor
    de despacho de por medio. Nace ya 'en_curso' — no hay a quién ofrecérselo,
    el chofer ya tiene al pasajero enfrente. Se cierra con el mismo
    `POST /trips/{id}/complete` de cualquier otro viaje (importe opcional),
    así sí cuenta en el resumen de ingresos del chofer."""
    driver = await _get_own_driver_or_403(db, user)

    assignment = await db.execute(
        select(VehicleAssignment).where(
            VehicleAssignment.driver_id == driver.id,
            VehicleAssignment.ended_at.is_(None),
        )
    )
    current = assignment.scalar_one_or_none()
    if current is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No tienes un turno abierto")

    vehicle = await db.get(Vehicle, current.vehicle_id)
    if vehicle is None or vehicle.status != VehicleStatus.DISPONIBLE:
        raise HTTPException(status.HTTP_409_CONFLICT, "Tu unidad no está disponible ahorita")

    trip = Trip(
        driver_id=driver.id,
        vehicle_id=vehicle.id,
        origin=_point(payload.origin_lat, payload.origin_lng),
        origin_address=payload.origin_address,
        status=TripStatus.EN_CURSO,
        started_at=datetime.now(UTC),
    )
    db.add(trip)
    await db.flush()
    await _set_vehicle_status(db, vehicle.id, VehicleStatus.OCUPADO)
    return await _get_trip_out(db, trip.id)


@router.get("", response_model=list[TripOut])
async def list_trips(
    response: Response,
    trip_status: TripStatus | None = Query(None, alias="status"),
    vehicle_id: uuid.UUID | None = None,
    driver_id: uuid.UUID | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(driver_or_staff),
):
    """Staff ve toda la flota y puede filtrar por cualquier driver_id/vehicle_id.

    Un chofer solo ve sus propios viajes: es como se resuelve "mis viajes" sin
    un endpoint aparte (que además chocaría en el orden de rutas con
    `/trips/{trip_id}`, igual que pasa con `/vehicles/nearby`). `driver_id` se
    ignora si lo manda un chofer — se fuerza al suyo, así `GET /trips` sin
    argumentos ya le sirve a la app.

    El total que coincide con los filtros (antes de limit/offset) va en el
    header `X-Total-Count`, igual que en /vehicles y /drivers.
    """
    conditions = []

    if user.role == UserRole.DRIVER:
        own_driver = await db.execute(select(Driver).where(Driver.user_id == user.id))
        driver = own_driver.scalar_one_or_none()
        if driver is None:
            response.headers["X-Total-Count"] = "0"
            return []
        conditions.append(Trip.driver_id == driver.id)
    elif driver_id is not None:
        conditions.append(Trip.driver_id == driver_id)

    if trip_status is not None:
        conditions.append(Trip.status == trip_status)
    if vehicle_id is not None:
        conditions.append(Trip.vehicle_id == vehicle_id)

    total = await db.scalar(select(func.count()).select_from(Trip).where(*conditions))
    response.headers["X-Total-Count"] = str(total)

    query = (
        select(*_trip_columns())
        .where(*conditions)
        # `requested_at` solo (server_default=func.now()) no basta como orden
        # de paginación: dentro de una misma transacción, now() en Postgres
        # devuelve el mismo valor para todos los INSERT — cualquier viaje
        # creado junto (o en pruebas, dentro del mismo SAVEPOINT) empata, y
        # sin desempate la paginación entre páginas deja de ser estable.
        .order_by(Trip.requested_at.desc(), Trip.id.desc())
        .limit(limit)
        .offset(offset)
    )
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
    """El chofer confirma que toma el viaje.

    Dos caminos posibles: si el viaje ya trae driver_id (alta manual de un
    operador), es la confirmación de siempre. Si no lo trae (lo creó el
    motor de despacho), solo puede aceptar el chofer al que se le está
    ofreciendo *ahora mismo* (offered_driver_id) — es quien recibió el
    trip_offer por su WebSocket.
    """
    trip = await _get_trip_or_404(db, trip_id)

    if trip.driver_id is not None:
        await _authorize_trip(trip, user, db)
        _apply_transition(trip, TripStatus.SOLICITADO, TripStatus.ASIGNADO)
        await _set_vehicle_status(db, trip.vehicle_id, VehicleStatus.OCUPADO)
        return await _get_trip_out(db, trip_id)

    driver = await _get_own_driver_or_403(db, user)
    if trip.offered_driver_id != driver.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No tienes una oferta activa para este viaje")
    if trip.offer_expires_at is not None and trip.offer_expires_at < datetime.now(UTC):
        raise HTTPException(status.HTTP_409_CONFLICT, "La oferta de este viaje ya expiró")

    trip.driver_id = trip.offered_driver_id
    trip.vehicle_id = trip.offered_vehicle_id
    trip.status = TripStatus.ASIGNADO
    trip.offered_driver_id = None
    trip.offered_vehicle_id = None
    trip.offer_expires_at = None
    await _set_vehicle_status(db, trip.vehicle_id, VehicleStatus.OCUPADO)

    if trip.customer_phone:
        vehicle = await db.get(Vehicle, trip.vehicle_id)
        plate = vehicle.plate if vehicle is not None else "sin placa"
        await send_whatsapp_message(
            trip.customer_phone,
            f"¡Un taxi va en camino! Unidad {plate}. Te avisamos cuando llegue.",
        )

    return await _get_trip_out(db, trip_id)


@router.post("/{trip_id}/reject", response_model=TripOut)
async def reject_trip(
    trip_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(driver_or_staff),
):
    """Solo tiene sentido en el flujo de despacho automático: el chofer al
    que se le ofreció el viaje declina, y el motor de despacho (que está
    esperando este cambio) pasa al siguiente candidato sin agotar el
    timeout completo."""
    trip = await _get_trip_or_404(db, trip_id)
    driver = await _get_own_driver_or_403(db, user)

    if trip.offered_driver_id != driver.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No tienes una oferta activa para este viaje")

    trip.offered_driver_id = None
    trip.offered_vehicle_id = None
    trip.offer_expires_at = None
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
    payload: TripComplete = TripComplete(),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(driver_or_staff),
):
    """`fare` es opcional: lo que cobró el chofer, a mano — para que pueda
    llevar su propio registro de ingresos en la app. No es un cobro real ni
    se calcula solo."""
    trip = await _get_trip_or_404(db, trip_id)
    await _authorize_trip(trip, user, db)
    _apply_transition(trip, TripStatus.EN_CURSO, TripStatus.COMPLETADO)
    trip.completed_at = datetime.now(UTC)
    trip.fare = payload.fare
    await _set_vehicle_status(db, trip.vehicle_id, VehicleStatus.DISPONIBLE)
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
    await _set_vehicle_status(db, trip.vehicle_id, VehicleStatus.DISPONIBLE)
    return await _get_trip_out(db, trip_id)
