"""Bind del teléfono del chofer: emite un device_token (una vez).

Distinto de Vehicle.device_key (GPS de la unidad). Solo con cuenta ACTIVE
y PIN ya puesto. El token en claro sale aquí y no se vuelve a mostrar.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_roles
from app.core.security import generate_token, hash_token
from app.database import get_db
from app.models import Driver, DriverDevice, DriverDeviceStatus, User, UserRole
from app.schemas.driver_application import DriverDeviceBindOut, DriverDeviceBindRequest

router = APIRouter(prefix="/driver-devices", tags=["dispositivos de chofer"])

driver_only = require_roles(UserRole.DRIVER)


@router.post("/bind", response_model=DriverDeviceBindOut)
async def bind_driver_device(
    payload: DriverDeviceBindRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(driver_only),
):
    result = await db.execute(select(Driver).where(Driver.user_id == user.id))
    driver = result.scalar_one_or_none()
    if driver is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chofer no encontrado")
    if driver.must_set_pin or driver.pin_hash is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Primero tienes que crear tu PIN",
        )

    now = datetime.now(UTC)
    open_devices = await db.execute(
        select(DriverDevice).where(
            DriverDevice.driver_id == driver.id,
            DriverDevice.status == DriverDeviceStatus.ACTIVE,
        )
    )
    for device in open_devices.scalars().all():
        device.status = DriverDeviceStatus.REVOKED
        device.revoked_at = now
    await db.flush()

    raw_token = generate_token()
    db.add(
        DriverDevice(
            driver_id=driver.id,
            device_token_hash=hash_token(raw_token),
            device_id_hash=hash_token(payload.device_id),
            status=DriverDeviceStatus.ACTIVE,
        )
    )
    if payload.push_token:
        driver.push_token = payload.push_token
    await db.flush()

    return DriverDeviceBindOut(device_token=raw_token, folio_ctm=driver.folio_ctm)
