"""Endpoints de vehículos y del historial de asignaciones (turnos)."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_roles
from app.core.redis_client import publish_vehicle_status_update
from app.core.security import generate_token, hash_token
from app.core.stands import get_vehicle_queue_position
from app.database import get_db
from app.models import Driver, Stand, User, UserRole, Vehicle, VehicleAssignment
from app.schemas.stand import QueuePositionOut
from app.schemas.vehicle import (
    AssignmentCreate,
    AssignmentOut,
    VehicleCreate,
    VehicleCreated,
    VehicleOut,
    VehicleStatusUpdate,
    VehicleUpdate,
)

router = APIRouter(prefix="/vehicles", tags=["vehicles"])

staff_only = require_roles(UserRole.OPERATOR, UserRole.ADMIN)
admin_only = require_roles(UserRole.ADMIN)
driver_or_staff = require_roles(UserRole.DRIVER, UserRole.OPERATOR, UserRole.ADMIN)


@router.get("", response_model=list[VehicleOut])
async def list_vehicles(
    response: Response,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(staff_only),
):
    """El total (antes de aplicar limit/offset) va en el header `X-Total-Count`:
    así el cuerpo se queda como una lista plana, sin romper a quien ya
    consume este endpoint sin paginar."""
    total = await db.scalar(select(func.count()).select_from(Vehicle))
    response.headers["X-Total-Count"] = str(total)

    result = await db.execute(
        select(Vehicle).order_by(Vehicle.plate).limit(limit).offset(offset)
    )
    return list(result.scalars().all())


@router.get("/{vehicle_id}", response_model=VehicleOut)
async def get_vehicle(
    vehicle_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(staff_only),
):
    vehicle = await db.get(Vehicle, vehicle_id)
    if vehicle is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unidad no encontrada")
    return vehicle


@router.get("/{vehicle_id}/queue-position", response_model=QueuePositionOut | None)
async def get_queue_position(
    vehicle_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(driver_or_staff),
):
    """None (200, no 404) si la unidad no está formada ahora mismo — no
    estar en fila es el estado normal de casi cualquier unidad casi todo
    el tiempo, no una condición de error. Un chofer solo puede consultar
    la unidad de su propio turno actual, igual que en POST .../status."""
    vehicle = await db.get(Vehicle, vehicle_id)
    if vehicle is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unidad no encontrada")

    if user.role == UserRole.DRIVER:
        own_driver = await db.execute(select(Driver).where(Driver.user_id == user.id))
        driver = own_driver.scalar_one_or_none()
        assignment = await db.execute(
            select(VehicleAssignment).where(
                VehicleAssignment.vehicle_id == vehicle_id,
                VehicleAssignment.ended_at.is_(None),
            )
        )
        current = assignment.scalar_one_or_none()
        if driver is None or current is None or current.driver_id != driver.id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "No tienes el turno de esta unidad")

    return await get_vehicle_queue_position(db, vehicle_id)


@router.post("", response_model=VehicleCreated, status_code=status.HTTP_201_CREATED)
async def create_vehicle(
    payload: VehicleCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(admin_only),
):
    """Da de alta una unidad y genera su clave de dispositivo.

    La clave se devuelve en claro únicamente en esta respuesta; en la base solo
    queda el hash. Hay que capturarla en la app del chofer en ese momento.
    """
    exists = await db.execute(select(Vehicle).where(Vehicle.plate == payload.plate))
    if exists.scalar_one_or_none() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Ya existe una unidad con esa placa")

    if await db.get(Stand, payload.stand_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sitio no encontrado")

    device_key = generate_token()
    vehicle = Vehicle(
        plate=payload.plate,
        model=payload.model,
        year=payload.year,
        stand_id=payload.stand_id,
        device_key_hash=hash_token(device_key),
    )
    db.add(vehicle)
    await db.flush()

    return VehicleCreated(
        id=vehicle.id,
        plate=vehicle.plate,
        model=vehicle.model,
        year=vehicle.year,
        status=vehicle.status,
        stand_id=vehicle.stand_id,
        device_key=device_key,
    )


@router.patch("/{vehicle_id}", response_model=VehicleOut)
async def update_vehicle(
    vehicle_id: uuid.UUID,
    payload: VehicleUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(staff_only),
):
    vehicle = await db.get(Vehicle, vehicle_id)
    if vehicle is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unidad no encontrada")

    updates = payload.model_dump(exclude_unset=True)
    if "stand_id" in updates and await db.get(Stand, updates["stand_id"]) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sitio no encontrado")

    for field, value in updates.items():
        setattr(vehicle, field, value)
    return vehicle


@router.post("/{vehicle_id}/device-key", response_model=VehicleCreated)
async def regenerate_device_key(
    vehicle_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(staff_only),
):
    """Genera una clave de dispositivo nueva para una unidad que ya existe —
    la anterior deja de servir de inmediato. Pensado para cuando cambia el
    teléfono montado en la unidad; a diferencia de POST /vehicles/{id}/status,
    esto es solo para operador/admin, el chofer no puede hacerlo por su cuenta."""
    vehicle = await db.get(Vehicle, vehicle_id)
    if vehicle is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unidad no encontrada")

    device_key = generate_token()
    vehicle.device_key_hash = hash_token(device_key)

    return VehicleCreated(
        id=vehicle.id,
        plate=vehicle.plate,
        model=vehicle.model,
        year=vehicle.year,
        status=vehicle.status,
        stand_id=vehicle.stand_id,
        device_key=device_key,
    )


@router.post("/{vehicle_id}/status", response_model=VehicleOut)
async def set_vehicle_status(
    vehicle_id: uuid.UUID,
    payload: VehicleStatusUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(driver_or_staff),
):
    """A diferencia de PATCH /vehicles/{id} (solo staff, cualquier campo),
    esto lo puede mandar el propio chofer para marcarse disponible/ocupado
    él solo — pensado para el corte de calle: un pasajero que para el taxi
    directo, sin pasar por operador ni por el motor de despacho. Un chofer
    solo puede tocar la unidad que tiene asignada en su turno actual."""
    vehicle = await db.get(Vehicle, vehicle_id)
    if vehicle is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unidad no encontrada")

    if user.role == UserRole.DRIVER:
        own_driver = await db.execute(select(Driver).where(Driver.user_id == user.id))
        driver = own_driver.scalar_one_or_none()
        assignment = await db.execute(
            select(VehicleAssignment).where(
                VehicleAssignment.vehicle_id == vehicle_id,
                VehicleAssignment.ended_at.is_(None),
            )
        )
        current = assignment.scalar_one_or_none()
        if driver is None or current is None or current.driver_id != driver.id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "No tienes el turno de esta unidad")

    vehicle.status = payload.status
    await db.commit()

    # Este endpoint es siempre un cambio manual (switch del chofer o ajuste
    # de operador) — nunca por un viaje, así que on_trip es False de ley;
    # accept/complete/cancel llevan su propio aviso (ver _set_vehicle_status
    # en app.api.trips).
    await publish_vehicle_status_update(str(vehicle.id), vehicle.status.value, on_trip=False)
    return vehicle


# --- Asignaciones de turno ----------------------------------------------------

@router.post(
    "/{vehicle_id}/assignments",
    response_model=AssignmentOut,
    status_code=status.HTTP_201_CREATED,
)
async def open_assignment(
    vehicle_id: uuid.UUID,
    payload: AssignmentCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(staff_only),
):
    """Abre un turno. Cierra automáticamente el turno anterior de esa unidad,
    de modo que nunca haya dos choferes activos en el mismo vehículo."""
    vehicle = await db.get(Vehicle, vehicle_id)
    if vehicle is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unidad no encontrada")

    driver = await db.get(Driver, payload.driver_id)
    if driver is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chofer no encontrado")

    now = datetime.now(UTC)
    open_shifts = await db.execute(
        select(VehicleAssignment).where(
            VehicleAssignment.vehicle_id == vehicle_id,
            VehicleAssignment.ended_at.is_(None),
        )
    )
    for shift in open_shifts.scalars().all():
        shift.ended_at = now

    assignment = VehicleAssignment(
        vehicle_id=vehicle_id, driver_id=payload.driver_id, started_at=now
    )
    db.add(assignment)
    await db.flush()
    return assignment


@router.get("/{vehicle_id}/assignments", response_model=list[AssignmentOut])
async def list_assignments(
    vehicle_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(staff_only),
):
    """Historial completo de turnos de la unidad, para auditoría y reportería."""
    result = await db.execute(
        select(VehicleAssignment)
        .where(VehicleAssignment.vehicle_id == vehicle_id)
        .order_by(VehicleAssignment.started_at.desc())
    )
    return list(result.scalars().all())


@router.post("/{vehicle_id}/assignments/close", response_model=AssignmentOut)
async def close_assignment(
    vehicle_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(staff_only),
):
    """Cierra el turno activo sin abrir uno nuevo (fin de jornada)."""
    result = await db.execute(
        select(VehicleAssignment).where(
            VehicleAssignment.vehicle_id == vehicle_id,
            VehicleAssignment.ended_at.is_(None),
        )
    )
    assignment = result.scalar_one_or_none()
    if assignment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No hay turno activo")

    assignment.ended_at = datetime.now(UTC)
    return assignment
