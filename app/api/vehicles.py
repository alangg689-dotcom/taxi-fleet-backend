"""Endpoints de vehículos y del historial de asignaciones (turnos)."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_roles
from app.core.security import generate_token, hash_token
from app.database import get_db
from app.models import Driver, User, UserRole, Vehicle, VehicleAssignment
from app.schemas.vehicle import (
    AssignmentCreate,
    AssignmentOut,
    VehicleCreate,
    VehicleCreated,
    VehicleOut,
    VehicleUpdate,
)

router = APIRouter(prefix="/vehicles", tags=["vehicles"])

staff_only = require_roles(UserRole.OPERATOR, UserRole.ADMIN)
admin_only = require_roles(UserRole.ADMIN)


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

    device_key = generate_token()
    vehicle = Vehicle(
        plate=payload.plate,
        model=payload.model,
        year=payload.year,
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

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(vehicle, field, value)
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
