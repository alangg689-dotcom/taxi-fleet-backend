"""Endpoints de choferes: alta y gestión del perfil.

Separado a propósito de auth.py (login por PIN) y de vehicles.py
(asignaciones de turno): aquí solo vive el ciclo de vida del perfil del
chofer, no cómo entra al sistema ni a qué unidad está asignado ahora mismo
— salvo el PIN mismo, que se asigna aquí (al dar de alta o al
regenerarlo) porque es el operador quien lo entrega, no algo que el
chofer elija en la app.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_roles
from app.core.security import generate_pin, hash_token
from app.database import get_db
from app.models import Driver, User, UserRole
from app.schemas.driver import DriverCreate, DriverCreated, DriverOut, DriverUpdate, PushTokenUpdate

router = APIRouter(prefix="/drivers", tags=["choferes"])

staff_only = require_roles(UserRole.OPERATOR, UserRole.ADMIN)
admin_only = require_roles(UserRole.ADMIN)
driver_only = require_roles(UserRole.DRIVER)


def _driver_query():
    """Trae el teléfono desde users: vive ahí, no en drivers, porque el
    login (teléfono + PIN) es un atributo de la cuenta, no del perfil
    operativo. pin_hash nunca se incluye aquí — no hay razón para que
    salga en ninguna respuesta salvo el PIN en claro, una sola vez, al
    darlo de alta o regenerarlo."""
    return select(
        Driver.id,
        Driver.user_id,
        User.phone,
        Driver.full_name,
        Driver.license_number,
        Driver.status,
        User.is_active,
    ).join(User, User.id == Driver.user_id)


async def _get_driver_or_404(db: AsyncSession, driver_id: uuid.UUID) -> Driver:
    driver = await db.get(Driver, driver_id)
    if driver is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chofer no encontrado")
    return driver


@router.post("", response_model=DriverCreated, status_code=status.HTTP_201_CREATED)
async def create_driver(
    payload: DriverCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(admin_only),
):
    """Da de alta un chofer: crea su cuenta (rol DRIVER, sin password — entra
    por teléfono + PIN) y su perfil operativo en un solo paso.

    El PIN se genera aquí mismo y se devuelve en claro únicamente en esta
    respuesta — hay que capturarlo para dárselo al chofer, igual que la
    clave de dispositivo al dar de alta una unidad."""
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

    pin = generate_pin()
    driver = Driver(
        user_id=user.id,
        full_name=payload.full_name,
        license_number=payload.license_number,
        pin_hash=hash_token(pin),
    )
    db.add(driver)
    await db.flush()

    result = await db.execute(_driver_query().where(Driver.id == driver.id))
    return DriverCreated(**result.mappings().one(), pin=pin)


@router.post("/{driver_id}/pin", response_model=DriverCreated)
async def regenerate_pin(
    driver_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(staff_only),
):
    """Genera un PIN nuevo para un chofer que ya existe — el anterior deja
    de servir de inmediato. Pensado para cuando lo olvida o para dárselo
    por primera vez a un chofer migrado desde el login por OTP (pin_hash
    nace en NULL para esos, no pueden entrar hasta que se les asigne uno)."""
    driver = await _get_driver_or_404(db, driver_id)

    pin = generate_pin()
    driver.pin_hash = hash_token(pin)

    result = await db.execute(_driver_query().where(Driver.id == driver_id))
    return DriverCreated(**result.mappings().one(), pin=pin)


@router.post("/me/push-token", status_code=status.HTTP_204_NO_CONTENT)
async def register_push_token(
    payload: PushTokenUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(driver_only),
):
    """El chofer registra el token de push de Expo de su teléfono en cuanto
    la app tiene permiso y uno vigente — es la red de seguridad que usa
    app.core.dispatch para avisarle una oferta de viaje aunque /ws/driver no
    tenga un socket vivo en ese momento (app en segundo plano o cerrada)."""
    result = await db.execute(select(Driver).where(Driver.user_id == user.id))
    driver = result.scalar_one_or_none()
    if driver is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chofer no encontrado")
    driver.push_token = payload.push_token
    await db.commit()


@router.get("", response_model=list[DriverOut])
async def list_drivers(
    response: Response,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(staff_only),
):
    """El total (antes de limit/offset) va en el header `X-Total-Count`, igual
    que en /vehicles: el cuerpo se queda como lista plana."""
    total = await db.scalar(select(func.count()).select_from(Driver))
    response.headers["X-Total-Count"] = str(total)

    result = await db.execute(
        _driver_query().order_by(Driver.full_name).limit(limit).offset(offset)
    )
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
    driver = await _get_driver_or_404(db, driver_id)

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(driver, field, value)

    result = await db.execute(_driver_query().where(Driver.id == driver_id))
    return DriverOut(**result.mappings().one())


@router.post("/{driver_id}/deactivate", response_model=DriverOut)
async def deactivate_driver(
    driver_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(admin_only),
):
    """Revoca el acceso por completo (User.is_active=False): a diferencia de
    `status` (activo/inactivo operativo — ej. de vacaciones, sigue pudiendo
    entrar), esto le corta el login ya, para cuando deja la flotilla. Efectivo
    de inmediato: get_current_user revisa is_active en cada request, no solo
    al emitir el token."""
    driver = await _get_driver_or_404(db, driver_id)
    user = await db.get(User, driver.user_id)
    user.is_active = False

    result = await db.execute(_driver_query().where(Driver.id == driver_id))
    return DriverOut(**result.mappings().one())


@router.post("/{driver_id}/reactivate", response_model=DriverOut)
async def reactivate_driver(
    driver_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(admin_only),
):
    driver = await _get_driver_or_404(db, driver_id)
    user = await db.get(User, driver.user_id)
    user.is_active = True

    result = await db.execute(_driver_query().where(Driver.id == driver_id))
    return DriverOut(**result.mappings().one())
