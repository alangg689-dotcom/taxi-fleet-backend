"""Siembra una flotilla de prueba: 100 choferes, 100 unidades y sus turnos.

Uso:
    python -m scripts.seed_drivers_units            # siembra
    python -m scripts.seed_drivers_units --limpiar  # borra el lote sembrado

Todo el lote es reconocible y borrable de un golpe: los teléfonos caen en el
bloque 644900XXXX, las placas empiezan con SIM- y los numerales van de R-1 a
R-100. Nada de esto pisa datos reales.

Los teléfonos van SIN +52: la flotilla es mexicana y el país se da por hecho,
así que la operadora captura los 10 dígitos y ya.

De paso deja utilizable el sitio placeholder que quedó sin trazo original en
la migración 0012 (era angosto y erosionarlo lo colapsaba a vacío). Se le pone
un cuadro sintético en su misma ubicación y se le marca el nombre como
provisional: es para poder ver el despacho funcionando, NO es la geocerca real
de esa calle. Hay que retrazarlo antes de producción.

Escribe directo contra la base con el ORM, igual que scripts.seed_admin — no
pasa por la API, así que no necesita un token de operador.
"""

from __future__ import annotations

import asyncio
import json
import math
import sys

from sqlalchemy import select, text

from app.core.security import generate_pin, hash_token
from app.database import SessionLocal
from app.models import (
    Driver,
    DriverStatus,
    Stand,
    User,
    UserRole,
    Vehicle,
    VehicleAssignment,
    VehicleStatus,
)

FLEET_SIZE = 100

# Marcas del lote. Cambiar cualquiera de estas rompe --limpiar sobre lo ya
# sembrado, así que si se tocan hay que borrar antes.
PHONE_BLOCK = "644900"
PLATE_PREFIX = "SIM-"
LICENSE_PREFIX = "SIM-LIC-"

# Las unidades se reparten solo entre los sitios reales: los otros tres se
# llaman "PLACEHOLDER — no usar" y meterles flotilla sería sembrar confusión.
REAL_STAND_NAMES = ("Sitio Alianza", "Sitio Ocotillo", "Sitio Vista Dorada")

PROVISIONAL_STAND_ID = "853c33df-7dd3-4de7-be49-84cbcc03dd58"
PROVISIONAL_STAND_NAME = "PLACEHOLDER - Sitio 1 (Provisional)"
PROVISIONAL_HALF_SIDE_M = 30
PROVISIONAL_BUFFER_M = 15

FIRST_NAMES = (
    "Miguel", "José", "Luis", "Juan", "Carlos", "Jorge", "Ramón", "Ernesto",
    "Alfredo", "Sergio", "Héctor", "Rubén", "Javier", "Óscar", "Martín",
    "Guadalupe", "Rosario", "Alma", "Norma", "Leticia",
)
LAST_NAMES = (
    "Valenzuela", "Bojórquez", "Duarte", "Coronado", "Yocupicio", "Islas",
    "Ruiz", "Moroyoqui", "Anaya", "Buitimea", "Salazar", "Gastélum",
    "Peñúñuri", "Robles", "Cázares", "Quintero", "Ibarra", "Leyva",
    "Rendón", "Zazueta",
)


def driver_name(index: int) -> str:
    """Nombre estable para el mismo índice: resembrar dos veces no cambia
    quién es R-42, que es lo que uno acaba usando de referencia al probar."""
    first = FIRST_NAMES[index % len(FIRST_NAMES)]
    paternal = LAST_NAMES[(index * 7) % len(LAST_NAMES)]
    maternal = LAST_NAMES[(index * 13 + 5) % len(LAST_NAMES)]
    return f"{first} {paternal} {maternal}"


def square_geojson(lat: float, lng: float, half_side_m: float) -> dict:
    """Cuadrado centrado en (lat, lng), en GeoJSON — coordenadas [lng, lat],
    como manda el estándar y como lo espera PostGIS."""
    dlat = half_side_m / 111_320
    dlng = half_side_m / (111_320 * math.cos(math.radians(lat)))
    return {
        "type": "Polygon",
        "coordinates": [[
            [lng - dlng, lat - dlat],
            [lng + dlng, lat - dlat],
            [lng + dlng, lat + dlat],
            [lng - dlng, lat + dlat],
            [lng - dlng, lat - dlat],
        ]],
    }


async def fix_provisional_stand(db) -> str | None:
    """Le pone trazo al sitio que quedó sin él. Conserva su ubicación actual
    —ya está a unos 200 m de Vista Dorada, dentro del grupo— y solo le cambia
    la forma por un cuadro limpio de 60 m de lado.

    Guarda `outline` además de `polygon` para que el editor de vértices y el
    campo de holgura del dashboard funcionen sobre él (ver migración 0012)."""
    row = (
        await db.execute(
            text(
                """
                SELECT ST_Y(center::geometry) AS lat, ST_X(center::geometry) AS lng
                FROM stands WHERE id = :id
                """
            ),
            {"id": PROVISIONAL_STAND_ID},
        )
    ).mappings().first()
    if row is None:
        return None

    outline = square_geojson(row["lat"], row["lng"], PROVISIONAL_HALF_SIDE_M)
    await db.execute(
        text(
            """
            UPDATE stands SET
                name = :name,
                outline = ST_SetSRID(ST_GeomFromGeoJSON(:geojson), 4326)::geography,
                polygon = ST_Buffer(
                    ST_SetSRID(ST_GeomFromGeoJSON(:geojson), 4326)::geography, :buffer_distance
                ),
                center = ST_SetSRID(ST_Centroid(ST_GeomFromGeoJSON(:geojson)), 4326)::geography,
                polygon_buffer_meters = :buffer_m,
                is_placeholder = true
            WHERE id = :id
            """
        ),
        {
            "id": PROVISIONAL_STAND_ID,
            "name": PROVISIONAL_STAND_NAME,
            "geojson": json.dumps(outline),
            # Dos parámetros para el mismo número: la columna es integer y la
            # distancia de ST_Buffer es double precision, y asyncpg no deduce
            # un solo tipo para un parámetro usado en los dos lugares.
            "buffer_distance": float(PROVISIONAL_BUFFER_M),
            "buffer_m": PROVISIONAL_BUFFER_M,
        },
    )
    # is_placeholder=true a propósito: el dashboard lo pinta punteado y con la
    # píldora "provisional", que es exactamente lo que es.
    return PROVISIONAL_STAND_NAME


async def seed(db) -> None:
    stands = (
        await db.execute(select(Stand).where(Stand.name.in_(REAL_STAND_NAMES)))
    ).scalars().all()
    missing = set(REAL_STAND_NAMES) - {s.name for s in stands}
    if missing:
        print(f"Faltan sitios en la base: {', '.join(sorted(missing))}")
        print("Nombres esperados:", ", ".join(REAL_STAND_NAMES))
        sys.exit(1)
    stands = sorted(stands, key=lambda s: REAL_STAND_NAMES.index(s.name))

    already = (
        await db.execute(select(User.id).where(User.phone.like(f"{PHONE_BLOCK}%")).limit(1))
    ).scalar_one_or_none()
    if already is not None:
        print(f"Ya hay choferes sembrados (teléfonos {PHONE_BLOCK}XXXX).")
        print("Corre 'python -m scripts.seed_drivers_units --limpiar' antes de volver a sembrar.")
        sys.exit(1)

    provisional = await fix_provisional_stand(db)

    per_stand = {s.name: 0 for s in stands}
    for i in range(1, FLEET_SIZE + 1):
        stand = stands[(i - 1) % len(stands)]
        per_stand[stand.name] += 1

        user = User(phone=f"{PHONE_BLOCK}{i:04d}", role=UserRole.DRIVER)
        db.add(user)
        await db.flush()

        # Con PIN para que el listado no muestre 100 choferes "sin PIN": el
        # valor en claro se descarta aquí a propósito. Para entrar de verdad
        # con alguno, regenéraselo desde el dashboard, que es el flujo normal.
        driver = Driver(
            user_id=user.id,
            full_name=driver_name(i),
            license_number=f"{LICENSE_PREFIX}{i:04d}",
            numeral=f"R-{i}",
            status=DriverStatus.ACTIVO,
            pin_hash=hash_token(generate_pin()),
        )
        db.add(driver)
        await db.flush()

        # device_key_hash queda nulo: los simuladores hablan directo con la
        # base, no por HTTP, así que no necesitan clave. Para emparejar un
        # teléfono real se genera desde el dashboard.
        vehicle = Vehicle(
            plate=f"{PLATE_PREFIX}{i:03d}",
            model="Nissan Tsuru",
            year=2015 + (i % 8),
            status=VehicleStatus.DISPONIBLE,
            stand_id=stand.id,
        )
        db.add(vehicle)
        await db.flush()

        # El turno abierto es lo que vuelve candidata a la unidad para el
        # motor de despacho, y de donde sale el numeral que rotula el mapa.
        db.add(VehicleAssignment(vehicle_id=vehicle.id, driver_id=driver.id))

    await db.commit()

    print(f"Sembrados {FLEET_SIZE} choferes (R-1 a R-{FLEET_SIZE}) con unidad y turno abierto.")
    for name, count in per_stand.items():
        print(f"  {name}: {count} unidades")
    if provisional is not None:
        print(f"Sitio con trazo sintético: {provisional} (cuadro de 60 m — retrazar antes de producción)")
    else:
        print(f"Aviso: no se encontró el sitio {PROVISIONAL_STAND_ID}, se dejó como estaba.")


async def clean(db) -> None:
    vehicle_ids = (
        await db.execute(select(Vehicle.id).where(Vehicle.plate.like(f"{PLATE_PREFIX}%")))
    ).scalars().all()
    user_ids = (
        await db.execute(select(User.id).where(User.phone.like(f"{PHONE_BLOCK}%")))
    ).scalars().all()

    if not vehicle_ids and not user_ids:
        print("No hay nada sembrado que borrar.")
        return

    # En orden de dependencia. vehicle_assignments cae solo por ON DELETE
    # CASCADE, pero el resto apunta a vehicles sin cascada.
    if vehicle_ids:
        for table in ("stand_queue_events", "stand_queue", "location_pings"):
            await db.execute(
                text(f"DELETE FROM {table} WHERE vehicle_id = ANY(:ids)"), {"ids": vehicle_ids}
            )
        await db.execute(
            text(
                "DELETE FROM trips WHERE vehicle_id = ANY(:ids) OR offered_vehicle_id = ANY(:ids)"
            ),
            {"ids": vehicle_ids},
        )
        await db.execute(
            text("DELETE FROM vehicles WHERE id = ANY(:ids)"), {"ids": vehicle_ids}
        )
    # Borrar el user arrastra al driver por ON DELETE CASCADE.
    if user_ids:
        await db.execute(text("DELETE FROM users WHERE id = ANY(:ids)"), {"ids": user_ids})

    await db.commit()
    print(f"Borradas {len(vehicle_ids)} unidades y {len(user_ids)} choferes del lote de prueba.")
    print("El sitio provisional se queda con su trazo sintético; retrazarlo cuando toque.")


async def main() -> None:
    async with SessionLocal() as db:
        if "--limpiar" in sys.argv[1:]:
            await clean(db)
        else:
            await seed(db)


if __name__ == "__main__":
    asyncio.run(main())
