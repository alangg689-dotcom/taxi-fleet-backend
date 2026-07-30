"""Endpoints de choferes: alta y gestión del perfil.

Separado a propósito de auth.py (login por OTP) y de vehicles.py
(asignaciones de turno): aquí solo vive el ciclo de vida del perfil del
chofer, no cómo entra al sistema ni a qué unidad está asignado ahora mismo.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_roles
from app.database import get_db
from app.models import Driver, User, UserRole
from app.schemas.driver import DriverCreate, DriverOut, DriverUpdate

router = APIRouter(prefix="/drivers", tags=["choferes"])

staff_only = require_roles(UserRole.OPERATOR, UserRole.ADMIN)
admin_only = require_roles(UserRole.ADMIN)


def _driver_query():
    """Trae el teléfono desde users: vive ahí, no en drivers, porque el login
    por OTP es un atributo de la cuenta, no del perfil operativo."""
    return select(
        Driver.id,
        Driver.user_id,
        User.phone,
        Driver.full_name,
        Driver.license_number,
        Driver.status,
    ).join(User, User.id == Driver.user_id)


@router.post("", response_model=DriverOut, status_code=status.HTTP_201_CREATED)
async def create_driver(
    payload: DriverCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(admin_only),
):
    """Da de alta un chofer: crea su cuenta (rol DRIVER, sin password — entra
    por teléfono + OTP) y su perfil operativo en un solo paso."""
    phone_taken = await db.execute(select(User).where(User.phone == payload.phone))
    if phone_taken.scalar_one_or_none() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Ya existe una cuenta con ese teléfono")

    license_taken = await db.execute(
        select(Driver).where(Driver.license_number == payload.license_number)
    )
    if license_taken.scalar_one_or_none() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Ya existe un chofer con esa licencia")

    user = User(phone=payload.phone, role=UserRole.DRIVER)
    db.add(user)
    await db.flush()

    driver = Driver(
        user_id=user.id,
        full_name=payload.full_name,
        license_number=payload.license_number,
    )
    db.add(driver)
    await db.flush()

    result = await db.execute(_driver_query().where(Driver.id == driver.id))
    return DriverOut(**result.mappings().one())


@router.get("", response_model=list[DriverOut])
async def list_drivers(
    db: AsyncSession = Depends(get_db), _: User = Depends(staff_only)
):
    result = await db.execute(_driver_query().order_by(Driver.full_name))
    return [DriverOut(**row) for row in result.mappings().all()]


@router.get("/{driver_id}", response_model=DriverOut)
async def get_driver(
    driver_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(staff_only),
):
    result = await db.execute(_driver_query().where(Driver.id == driver_id))
    row = result.mappings().one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chofer no encontrado")
    return DriverOut(**row)


@router.patch("/{driver_id}", response_model=DriverOut)
async def update_driver(
    driver_id: uuid.UUID,
    payload: DriverUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(staff_only),
):
    driver = await db.get(Driver, driver_id)
    if driver is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chofer no encontrado")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(driver, field, value)

    result = await db.execute(_driver_query().where(Driver.id == driver_id))
    return DriverOut(**result.mappings().one())
