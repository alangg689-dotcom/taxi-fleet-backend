"""Helpers para sembrar datos directamente en la sesión de una prueba.

Van directo al ORM en vez de pasar por /auth o /vehicles: es el "arrange" de
cada prueba, no lo que se está probando, y hacerlo por HTTP solo agregaría
ruido y tiempo de ejecución.
"""

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.location import _point
from app.core.security import create_access_token, generate_token, hash_password, hash_token
from app.models import (
    Driver,
    DriverStatus,
    LocationPing,
    Operator,
    PermissionLevel,
    Stand,
    User,
    UserRole,
    Vehicle,
    VehicleAssignment,
    VehicleStatus,
)

DEFAULT_PASSWORD = "Password123"


async def make_staff_user(
    db: AsyncSession, *, role: UserRole = UserRole.OPERATOR, email: str | None = None
) -> tuple[User, str]:
    """Crea un operador/admin y devuelve (user, access_token)."""
    email = email or f"{role.value}-{uuid.uuid4().hex[:8]}@flotilla.mx"
    user = User(email=email, password_hash=hash_password(DEFAULT_PASSWORD), role=role)
    db.add(user)
    await db.flush()

    db.add(
        Operator(
            user_id=user.id,
            full_name="Operador de prueba",
            permission_level=(
                PermissionLevel.ADMIN if role == UserRole.ADMIN else PermissionLevel.DESPACHADOR
            ),
        )
    )
    await db.flush()

    token = create_access_token(str(user.id), role.value)
    return user, token


async def make_driver(
    db: AsyncSession, *, phone: str | None = None, license_number: str | None = None
) -> tuple[Driver, str]:
    """Crea un chofer (User + Driver) y devuelve (driver, access_token)."""
    phone = phone or f"+5255{uuid.uuid4().int % 10**8:08d}"
    license_number = license_number or f"LIC-{uuid.uuid4().hex[:10]}"

    user = User(phone=phone, role=UserRole.DRIVER)
    db.add(user)
    await db.flush()

    driver = Driver(
        user_id=user.id,
        full_name="Chofer de prueba",
        license_number=license_number,
        status=DriverStatus.ACTIVO,
    )
    db.add(driver)
    await db.flush()

    token = create_access_token(str(user.id), UserRole.DRIVER.value)
    return driver, token


async def make_stand(
    db: AsyncSession,
    *,
    name: str | None = None,
    center: tuple[float, float] = (19.4326, -99.1332),
) -> Stand:
    """Sitio de prueba: un cuadro de ~100 m alrededor de `center` (lat, lng)
    — suficiente para probar pertenencia geoespacial sin trazar un polígono
    real. `center` default es el Zócalo, igual que el resto de las pruebas."""
    lat, lng = center
    d = 0.0005  # ~55m en esta latitud
    polygon_wkt = (
        f"SRID=4326;POLYGON(("
        f"{lng - d} {lat - d}, {lng + d} {lat - d}, "
        f"{lng + d} {lat + d}, {lng - d} {lat + d}, "
        f"{lng - d} {lat - d}))"
    )
    stand = Stand(
        name=name or f"Sitio de prueba {uuid.uuid4().hex[:6]}",
        polygon=polygon_wkt,
        center=_point(lat, lng),
    )
    db.add(stand)
    await db.flush()
    return stand


async def make_vehicle(
    db: AsyncSession,
    *,
    plate: str | None = None,
    with_device_key: bool = False,
    status: VehicleStatus = VehicleStatus.OFFLINE,
    stand_id: uuid.UUID | None = None,
) -> Vehicle | tuple[Vehicle, str]:
    """Con with_device_key=True devuelve (vehicle, device_key crudo): igual que
    el alta real, el hash es lo único que queda en la base. `status` default
    offline, igual que el alta real — el motor de despacho solo considera
    'disponible' (ver test_dispatch.py).

    `stand_id` es NOT NULL en el modelo real (son 6 sitios fijos, toda
    unidad pertenece a uno) — si no se pasa uno, esta fábrica crea un sitio
    de prueba desechable para no obligar a cada prueba existente a que le
    importe la fila."""
    plate = plate or f"TST-{uuid.uuid4().hex[:6].upper()}"
    device_key = generate_token() if with_device_key else None
    if stand_id is None:
        stand_id = (await make_stand(db)).id
    vehicle = Vehicle(
        plate=plate,
        model="Vehículo de prueba",
        status=status,
        device_key_hash=hash_token(device_key) if device_key else None,
        stand_id=stand_id,
    )
    db.add(vehicle)
    await db.flush()
    return (vehicle, device_key) if with_device_key else vehicle


async def make_location_ping(
    db: AsyncSession,
    *,
    vehicle_id: uuid.UUID,
    timestamp: datetime,
    lat: float = 19.4326,
    lng: float = -99.1332,
) -> LocationPing:
    ping = LocationPing(
        id=uuid.uuid4(),
        vehicle_id=vehicle_id,
        location=_point(lat, lng),
        speed=40.0,
        heading=90.0,
        accuracy=5.0,
        timestamp=timestamp,
    )
    db.add(ping)
    await db.flush()
    return ping


async def make_open_assignment(
    db: AsyncSession, *, vehicle_id: uuid.UUID, driver_id: uuid.UUID
) -> VehicleAssignment:
    """Turno abierto (ended_at=NULL): así el motor de despacho sabe qué
    chofer está manejando esa unidad ahora mismo."""
    assignment = VehicleAssignment(vehicle_id=vehicle_id, driver_id=driver_id)
    db.add(assignment)
    await db.flush()
    return assignment


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
