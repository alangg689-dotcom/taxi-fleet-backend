"""Simulador de pings GPS: mueve las unidades sembradas por el mapa.

Uso:
    python -m scripts.seed_gps_pings
    python -m scripts.seed_gps_pings --unidades 25 --intervalo 3

Manda pings por HTTP al endpoint real (`POST /location/ping`) y no directo a
la base a propósito: así el recorrido completo se ejerce de verdad —
throttle, validación del ping, máquina de la fila del sitio, publicación en
Redis y de ahí al WebSocket del dashboard. Escribir en la tabla a mano
pintaría puntos en el mapa sin probar nada de eso.

## Sobre la device_key

El endpoint se autentica con `X-Device-Key`, que es una credencial de verdad:
el backend guarda solo su hash. Las unidades sembradas por
`scripts.seed_drivers_units` nacieron sin clave (nunca se emparejó un teléfono
con ellas), así que este script se las genera al arrancar y conserva el valor
en claro solo en memoria, mientras corre.

Por eso toca **únicamente las unidades con placa `SIM-`**. Regenerarle la
clave a una unidad real dejaría muerto al teléfono que la tenga guardada, que
es justo lo que le pasaría a TEL-001 si no filtráramos.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import random
from datetime import UTC, datetime

import httpx
from sqlalchemy import text

from app.core.security import generate_token, hash_token
from app.database import SessionLocal

DEFAULT_API = "http://localhost:8000/api/v1"
SEEDED_PLATE_PREFIX = "SIM-"

# Radio en el que se mueve cada unidad alrededor de su sitio. Más allá de esto
# se le da vuelta hacia el centro: sin eso, un paseo aleatorio largo termina
# desparramando la flotilla por todo Sonora.
ROAM_RADIUS_M = 500.0

# Metros por grado de latitud. Para longitud hay que corregir por el coseno de
# la latitud, que en Empalme (~28°) achica el grado casi un 12%.
M_PER_DEG_LAT = 111_320.0


class Unit:
    """Una unidad simulada y su estado de marcha entre ping y ping."""

    def __init__(self, vehicle_id: str, plate: str, numeral: str | None,
                 device_key: str, home_lat: float, home_lng: float,
                 rng: random.Random) -> None:
        self.vehicle_id = vehicle_id
        self.plate = plate
        self.numeral = numeral
        self.device_key = device_key
        self.home_lat = home_lat
        self.home_lng = home_lng
        self.rng = rng
        # Arranca desparramada alrededor del sitio, no todas en el centro.
        offset = rng.uniform(0, ROAM_RADIUS_M * 0.6)
        bearing = rng.uniform(0, 360)
        self.lat, self.lng = _move(home_lat, home_lng, bearing, offset)
        self.heading = rng.uniform(0, 360)
        self.speed_kmh = rng.choice([0.0, 0.0, 15.0, 25.0, 35.0])

    def step(self, seconds: float) -> None:
        """Avanza la unidad. La velocidad y el rumbo cambian poco a poco: un
        salto brusco lo marcaría `ping_validation` como ping no creíble."""
        if self.rng.random() < 0.25:
            self.speed_kmh = max(0.0, min(60.0, self.speed_kmh + self.rng.uniform(-12, 12)))
        self.heading = (self.heading + self.rng.uniform(-25, 25)) % 360

        # Si se alejó demasiado del sitio, se le apunta de regreso.
        if _distance_m(self.lat, self.lng, self.home_lat, self.home_lng) > ROAM_RADIUS_M:
            self.heading = _bearing(self.lat, self.lng, self.home_lat, self.home_lng)

        meters = self.speed_kmh * 1000 / 3600 * seconds
        self.lat, self.lng = _move(self.lat, self.lng, self.heading, meters)


def _move(lat: float, lng: float, bearing_deg: float, meters: float) -> tuple[float, float]:
    rad = math.radians(bearing_deg)
    dlat = meters * math.cos(rad) / M_PER_DEG_LAT
    dlng = meters * math.sin(rad) / (M_PER_DEG_LAT * math.cos(math.radians(lat)))
    return lat + dlat, lng + dlng


def _distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    dlat = (lat2 - lat1) * M_PER_DEG_LAT
    dlng = (lng2 - lng1) * M_PER_DEG_LAT * math.cos(math.radians(lat1))
    return math.hypot(dlat, dlng)


def _bearing(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    dlat = (lat2 - lat1) * M_PER_DEG_LAT
    dlng = (lng2 - lng1) * M_PER_DEG_LAT * math.cos(math.radians(lat1))
    return math.degrees(math.atan2(dlng, dlat)) % 360


async def load_units(count: int, rng: random.Random) -> list[Unit]:
    """Elige unidades sembradas con turno abierto y les asegura device_key.

    El turno abierto no es capricho: sin él la unidad no tiene chofer, y el
    mapa la rotularía con la placa en vez del numeral — que es justo lo que se
    quiere ver moverse.
    """
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                text(
                    """
                    SELECT v.id::text AS id, v.plate,
                           v.device_key_hash IS NOT NULL AS has_key,
                           d.numeral,
                           ST_Y(s.center::geometry) AS lat,
                           ST_X(s.center::geometry) AS lng
                    FROM vehicles v
                    JOIN stands s ON s.id = v.stand_id
                    JOIN vehicle_assignments a
                      ON a.vehicle_id = v.id AND a.ended_at IS NULL
                    JOIN drivers d ON d.id = a.driver_id
                    WHERE v.plate LIKE :prefix
                    ORDER BY v.plate
                    LIMIT :count
                    """
                ),
                {"prefix": f"{SEEDED_PLATE_PREFIX}%", "count": count},
            )
        ).mappings().all()

        if not rows:
            print(f"No hay unidades con placa {SEEDED_PLATE_PREFIX}… y turno abierto.")
            print("Corre antes: python -m scripts.seed_drivers_units")
            raise SystemExit(1)

        units: list[Unit] = []
        for row in rows:
            # Se regenera siempre: el valor en claro de una corrida anterior se
            # perdió al terminar el proceso, y solo se guarda el hash.
            key = generate_token()
            await db.execute(
                text("UPDATE vehicles SET device_key_hash = :h WHERE id = :id"),
                {"h": hash_token(key), "id": row["id"]},
            )
            units.append(
                Unit(row["id"], row["plate"], row["numeral"], key,
                     row["lat"], row["lng"], rng)
            )
        await db.commit()
        return units


async def send_ping(client: httpx.AsyncClient, api: str, unit: Unit) -> str | None:
    """Devuelve None si el ping se aceptó, o un texto con el motivo del fallo."""
    body = {
        "pings": [
            {
                "lat": round(unit.lat, 6),
                "lng": round(unit.lng, 6),
                # Obligatorio, y el servidor rechaza un desfase de más de
                # CLOCK_DRIFT_MAX_SECONDS (60 s). Va en UTC con zona explícita.
                "timestamp": datetime.now(UTC).isoformat(),
                "speed": round(unit.speed_kmh, 1),
                "heading": round(unit.heading, 1),
                # Por debajo de GPS_MAX_ACCURACY_METERS (50), si no el ping no
                # mueve la fila del sitio.
                "accuracy": 5.0,
            }
        ]
    }
    try:
        response = await client.post(
            f"{api}/location/ping",
            json=body,
            headers={"X-Device-Key": unit.device_key},
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        return f"sin conexión: {exc}"
    if response.status_code >= 400:
        return f"HTTP {response.status_code}: {response.text[:180]}"
    return None


async def main() -> None:
    parser = argparse.ArgumentParser(description="Simula pings GPS de la flotilla sembrada.")
    parser.add_argument("--unidades", type=int, default=10, help="cuántas unidades mover (default 10)")
    parser.add_argument("--intervalo", type=float, default=5.0, help="segundos entre pings (default 5)")
    parser.add_argument("--api", default=DEFAULT_API, help=f"base de la API (default {DEFAULT_API})")
    parser.add_argument("--semilla", type=int, default=None, help="semilla del aleatorio, para repetir un recorrido")
    args = parser.parse_args()

    rng = random.Random(args.semilla)
    units = await load_units(args.unidades, rng)

    # flush en todos los prints: sin terminal (redirigido a un archivo, o
    # lanzado desde otra herramienta) Python almacena la salida y el progreso
    # no aparece hasta que el proceso termina — que en un bucle infinito es
    # nunca.
    print(f"{len(units)} unidades listas, mandando a {args.api} cada {args.intervalo}s.", flush=True)
    print("Numerales: " + ", ".join(u.numeral or u.plate for u in units), flush=True)
    print("Ctrl+C para parar.\n", flush=True)

    tick = 0
    async with httpx.AsyncClient() as client:
        while True:
            tick += 1
            for unit in units:
                unit.step(args.intervalo)

            results = await asyncio.gather(
                *(send_ping(client, args.api, unit) for unit in units)
            )
            failures = [(u, r) for u, r in zip(units, results) if r is not None]

            moving = sum(1 for u in units if u.speed_kmh > 1)
            print(
                f"[{tick:>4}] {len(units) - len(failures)}/{len(units)} aceptados · "
                f"{moving} en movimiento",
                flush=True,
            )
            # Solo el primer fallo de cada tanda: si el backend está caído, cien
            # líneas idénticas no dicen más que una.
            if failures:
                unit, reason = failures[0]
                print(f"        fallo en {unit.numeral or unit.plate}: {reason}", flush=True)

            await asyncio.sleep(args.intervalo)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nDetenido.")
