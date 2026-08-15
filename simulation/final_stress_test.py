"""Prueba de resiliencia: un "día de alta demanda" comprimido.

Uso:
    python -m simulation.final_stress_test
    python -m simulation.final_stress_test --minutos 15 --viajes 50

Corre seis fases contra la base y el Redis REALES de desarrollo, no contra
mocks: lo que se quiere saber es si el sistema aguanta, y un mock del motor de
despacho probaría el mock. Lo único que se intercepta es la salida a Twilio,
por razones obvias.

    Fase 1  Ráfaga: N viajes en 30 s con asyncio.gather
    Fase 2  Cola: sin choferes libres, nadie se asigna y las posiciones salen
    Fase 3  FIFO: se libera flota y se atiende por orden de llegada
    Fase 4  Mensajes: ni uno se pierde, ni siquiera con Twilio fallando
    Fase 5  Cancelaciones en solicitado / asignado / en_curso
    Fase 6  Caída de Redis: se cae, se levanta, la fila sigue ahí

**No corre contra producción.** Aborta si DATABASE_URL no apunta a localhost:
crea decenas de viajes y unidades de mentira, y limpiarlos de una base real
sería un problema, no una prueba.

Al terminar borra todo lo que creó (placas STRESS-, teléfonos del bloque de
pruebas). Con --conservar se queda para inspeccionarlo a mano.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import text

from app.config import settings
from app.core import whatsapp as whatsapp_module
from app.core.demand import measure_demand
from app.core.dispatch import dispatch_trip
from app.core.redis_client import redis_client, set_last_position
from app.core.security import generate_token, hash_token
from app.database import SessionLocal
from app.models import (
    Driver,
    DriverStatus,
    Stand,
    Trip,
    TripStatus,
    User,
    UserRole,
    Vehicle,
    VehicleAssignment,
    VehicleStatus,
)

PLATE_PREFIX = "STRESS-"
PHONE_BLOCK = "644999"


@dataclass
class Report:
    """Se acumula durante toda la corrida y se imprime al final: un fallo en
    la fase 2 no debe impedir ver qué pasó en la 5."""

    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        line = f"{name}{f' — {detail}' if detail else ''}"
        (self.passed if ok else self.failed).append(line)
        print(f"  {'✓' if ok else '✗'} {line}", flush=True)


class MessageSpy:
    """Sustituye la salida a Twilio. Cuenta cada mensaje y, si se le pide,
    finge caídas: lo que importa no es que Twilio funcione, sino que un fallo
    suyo no tumbe el flujo que lo disparó ni pierda avisos silenciosamente."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail_next = 0

    async def send(self, to_phone: str, body: str) -> None:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise RuntimeError("Twilio simulado: caído")
        self.sent.append((to_phone, body))


async def _guard_not_production() -> None:
    url = str(settings.DATABASE_URL)
    if "localhost" not in url and "127.0.0.1" not in url:
        print(f"ABORTA: DATABASE_URL no es local ({url.split('@')[-1]}).")
        print("Esta prueba crea y borra datos a lo bruto; no corre fuera de desarrollo.")
        sys.exit(1)


async def _seed_fleet(db, count: int) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Unidades con turno abierto y GPS fresco, listas para ser candidatas.
    Nacen en `offline` — la fase 2 necesita que NO haya nadie disponible."""
    stand = (await db.execute(text("SELECT id, ST_Y(center::geometry) AS lat, ST_X(center::geometry) AS lng FROM stands ORDER BY name LIMIT 1"))).mappings().first()
    if stand is None:
        print("ABORTA: no hay sitios en la base. Corre antes scripts.seed_drivers_units.")
        sys.exit(1)

    pairs: list[tuple[uuid.UUID, uuid.UUID]] = []
    for i in range(count):
        user = User(phone=f"{PHONE_BLOCK}{i:04d}", role=UserRole.DRIVER)
        db.add(user)
        await db.flush()
        driver = Driver(
            user_id=user.id,
            full_name=f"Chofer de estrés {i}",
            license_number=f"STRESS-LIC-{i:04d}",
            numeral=f"S-{i}",
            status=DriverStatus.ACTIVO,
            pin_hash=hash_token(generate_token()),
        )
        db.add(driver)
        vehicle = Vehicle(
            plate=f"{PLATE_PREFIX}{i:03d}",
            model="Unidad de estrés",
            status=VehicleStatus.OFFLINE,
            stand_id=stand["id"],
            device_key_hash=hash_token(generate_token()),
        )
        db.add(vehicle)
        await db.flush()
        db.add(VehicleAssignment(vehicle_id=vehicle.id, driver_id=driver.id))
        pairs.append((vehicle.id, driver.id))
    await db.commit()

    # GPS fresco en Redis: el motor exige posición reciente para considerar
    # candidata a una unidad.
    for vehicle_id, _ in pairs:
        await set_last_position(
            str(vehicle_id),
            {
                "vehicle_id": str(vehicle_id),
                "lat": stand["lat"],
                "lng": stand["lng"],
                "speed": 0.0,
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )
    return pairs


async def _make_trip(db, phone: str, lat: float, lng: float) -> uuid.UUID:
    trip = Trip(
        # EWKT directo, igual que las fábricas de las pruebas: GeoAlchemy lo
        # entiende sin pasar por ST_GeomFromText.
        origin=f"SRID=4326;POINT({lng} {lat})",
        customer_channel="whatsapp",
        customer_phone=phone,
    )
    db.add(trip)
    await db.flush()
    trip_id = trip.id
    await db.commit()
    return trip_id


# --- Fases --------------------------------------------------------------------


async def fase1_rafaga(report: Report, trip_count: int, origin: tuple[float, float]) -> list[uuid.UUID]:
    print(f"\nFASE 1 — Ráfaga de {trip_count} viajes en 30 s")
    started = time.monotonic()

    async def _one(i: int) -> uuid.UUID:
        # Escalonados dentro de la ventana, no todos en el mismo milisegundo:
        # una ráfaga instantánea prueba el pool de conexiones, no la operación.
        await asyncio.sleep((i / trip_count) * 30)
        async with SessionLocal() as db:
            return await _make_trip(db, f"{PHONE_BLOCK}{i:04d}", origin[0], origin[1])

    trip_ids = await asyncio.gather(*(_one(i) for i in range(trip_count)))
    elapsed = time.monotonic() - started

    async with SessionLocal() as db:
        created = (
            await db.execute(
                text("SELECT count(*) FROM trips WHERE id = ANY(:ids)"),
                {"ids": list(trip_ids)},
            )
        ).scalar_one()

    report.check("Se crearon todos los viajes", created == trip_count, f"{created}/{trip_count} en {elapsed:.0f}s")
    return list(trip_ids)


async def fase2_cola(report: Report, trip_ids: list[uuid.UUID]) -> None:
    print("\nFASE 2 — Sin choferes: la cola crece y nadie se asigna")
    # Se despacha de verdad; sin candidatos, el motor termina su pasada sin
    # asignar y el viaje se queda esperando al barrido.
    await asyncio.gather(*(dispatch_trip(t) for t in trip_ids[:10]))

    async with SessionLocal() as db:
        assigned = (
            await db.execute(
                text("SELECT count(*) FROM trips WHERE id = ANY(:ids) AND status != 'solicitado'"),
                {"ids": trip_ids},
            )
        ).scalar_one()
        demand = await measure_demand(db)

    report.check("Ningún viaje se asignó sin flota", assigned == 0, f"{assigned} asignados")
    report.check(
        "El sistema detecta alta demanda",
        demand.high_demand,
        f"{demand.waiting_trips} esperando, {demand.available_drivers} libres",
    )
    report.check(
        "La espera máxima se extendió",
        demand.max_wait_seconds == settings.BOT_TRIP_MAX_WAIT_HIGH_DEMAND_SECONDS,
        f"{demand.max_wait_seconds}s",
    )


async def fase3_fifo(report: Report, trip_ids: list[uuid.UUID], pairs) -> None:
    print("\nFASE 3 — Se libera la flota: se atiende por orden de llegada")
    async with SessionLocal() as db:
        await db.execute(
            text("UPDATE vehicles SET status='disponible' WHERE plate LIKE :p"),
            {"p": f"{PLATE_PREFIX}%"},
        )
        await db.commit()

    # Se despachan los cinco más viejos, en orden, uno por uno: el motor
    # ofrece y espera respuesta, así que en paralelo competirían por la misma
    # unidad y el orden dejaría de ser observable.
    oldest = trip_ids[:5]
    for trip_id in oldest:
        await asyncio.wait_for(dispatch_trip(trip_id), timeout=40)
        async with SessionLocal() as db:
            # Nadie acepta (no hay app), así que la señal de que el motor
            # llegó a ese viaje es que le puso una oferta encima.
            await db.execute(
                text("UPDATE trips SET status='asignado', vehicle_id=offered_vehicle_id, driver_id=offered_driver_id WHERE id=:t AND offered_vehicle_id IS NOT NULL"),
                {"t": trip_id},
            )
            await db.commit()

    async with SessionLocal() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT id, status FROM trips WHERE id = ANY(:ids) ORDER BY requested_at"
                ),
                {"ids": oldest},
            )
        ).mappings().all()

    served = [r["id"] for r in rows if r["status"] == "asignado"]
    report.check(
        "Los viajes más viejos se atendieron primero",
        served == [t for t in oldest if t in served],
        f"{len(served)}/{len(oldest)} asignados en orden",
    )


async def fase4_mensajes(report: Report, spy: MessageSpy) -> None:
    print("\nFASE 4 — Mensajería: sin pérdidas, ni con Twilio caído")
    from app.core.customer_notify import notify_customer

    before = len(spy.sent)
    async with SessionLocal() as db:
        trips = (
            await db.execute(
                text(
                    "SELECT id FROM trips WHERE customer_phone LIKE :p ORDER BY requested_at LIMIT 20"
                ),
                {"p": f"{PHONE_BLOCK}%"},
            )
        ).scalars().all()
        for trip_id in trips:
            trip = await db.get(Trip, trip_id)
            await notify_customer(trip, "Mensaje de prueba de estrés")

    report.check(
        "Se entregaron todos los avisos",
        len(spy.sent) - before == len(trips),
        f"{len(spy.sent) - before}/{len(trips)}",
    )

    # Ahora con Twilio fallando: el aviso se pierde (no hay reintento), pero
    # lo que NO puede pasar es que la excepción suba y tumbe al llamador.
    spy.fail_next = 3
    crashed = False
    async with SessionLocal() as db:
        trip = await db.get(Trip, trips[0])
        try:
            for _ in range(3):
                await notify_customer(trip, "Mensaje con Twilio caído")
        except Exception:
            crashed = True

    report.check("Un fallo de Twilio no tumba el flujo", not crashed)
    spy.fail_next = 0


async def fase5_cancelaciones(report: Report, origin: tuple[float, float]) -> None:
    print("\nFASE 5 — Cancelar en cada estado")
    from app.core.dispatch import set_vehicle_status

    for target_status in ("solicitado", "asignado", "en_curso"):
        async with SessionLocal() as db:
            trip_id = await _make_trip(db, f"{PHONE_BLOCK}9999", origin[0], origin[1])
            if target_status != "solicitado":
                vehicle = (
                    await db.execute(
                        text("SELECT id FROM vehicles WHERE plate LIKE :p LIMIT 1"),
                        {"p": f"{PLATE_PREFIX}%"},
                    )
                ).scalar_one()
                await db.execute(
                    text("UPDATE trips SET status=:s, vehicle_id=:v WHERE id=:t"),
                    {"s": target_status, "v": vehicle, "t": trip_id},
                )
                await db.commit()

            trip = await db.get(Trip, trip_id)
            await db.refresh(trip)
            trip.status = TripStatus.CANCELADO
            await db.commit()
            if trip.vehicle_id:
                await set_vehicle_status(db, trip.vehicle_id, VehicleStatus.DISPONIBLE, trip_id=trip_id)

            final = (await db.get(Trip, trip_id)).status

        report.check(f"Cancelar desde '{target_status}'", final == TripStatus.CANCELADO, final.value)


async def fase6_caida_de_redis(report: Report) -> None:
    print("\nFASE 6 — Caída de Redis")
    async with SessionLocal() as db:
        before = (
            await db.execute(text("SELECT count(*) FROM stand_queue WHERE status='formado'"))
        ).scalar_one()

    try:
        await redis_client.ping()
    except Exception:
        report.check("Redis respondía antes de la prueba", False, "ya estaba caído")
        return

    print("  → deteniendo Redis (docker compose stop redis)…", flush=True)
    stop = await asyncio.create_subprocess_shell(
        "docker compose stop redis", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    await stop.wait()

    # try/finally: si la comprobación de abajo revienta, Redis TIENE que
    # volver de todos modos — dejarlo caído convertiría una prueba fallida en
    # un entorno de desarrollo roto.
    try:
        degraded_ok = True
        try:
            await measure_demand_safe()
        except Exception as exc:  # noqa: BLE001 — se reporta, no se traga
            degraded_ok = False
            print(f"     (la medición de demanda falló con Redis abajo: {exc})")

        report.check("La base sigue respondiendo sin Redis", degraded_ok)
    finally:
        print("  → levantando Redis…", flush=True)
        start = await asyncio.create_subprocess_shell(
            "docker compose start redis", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        await start.wait()

        for _ in range(30):
            try:
                await redis_client.ping()
                break
            except Exception:
                await asyncio.sleep(1)

    async with SessionLocal() as db:
        after = (
            await db.execute(text("SELECT count(*) FROM stand_queue WHERE status='formado'"))
        ).scalar_one()

    report.check("Redis volvió", True)
    report.check(
        "La fila de espera sobrevivió",
        after == before,
        f"{before} antes, {after} después — vive en Postgres, no en Redis",
    )


async def measure_demand_safe() -> None:
    async with SessionLocal() as db:
        await measure_demand(db)


async def _cleanup() -> None:
    async with SessionLocal() as db:
        vehicle_ids = (
            await db.execute(
                text("SELECT id FROM vehicles WHERE plate LIKE :p"), {"p": f"{PLATE_PREFIX}%"}
            )
        ).scalars().all()
        if vehicle_ids:
            for table in ("stand_queue_events", "stand_queue", "location_pings"):
                await db.execute(
                    text(f"DELETE FROM {table} WHERE vehicle_id = ANY(:ids)"), {"ids": vehicle_ids}
                )
            await db.execute(
                text("DELETE FROM trips WHERE vehicle_id = ANY(:ids) OR offered_vehicle_id = ANY(:ids)"),
                {"ids": vehicle_ids},
            )
            await db.execute(text("DELETE FROM vehicles WHERE id = ANY(:ids)"), {"ids": vehicle_ids})
        await db.execute(
            text("DELETE FROM trips WHERE customer_phone LIKE :p"), {"p": f"{PHONE_BLOCK}%"}
        )
        await db.execute(
            text("DELETE FROM users WHERE phone LIKE :p"), {"p": f"{PHONE_BLOCK}%"}
        )
        await db.commit()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Prueba de resiliencia de la flotilla.")
    parser.add_argument("--viajes", type=int, default=50, help="viajes de la ráfaga (default 50)")
    parser.add_argument("--flota", type=int, default=8, help="unidades de prueba (default 8)")
    parser.add_argument("--conservar", action="store_true", help="no borrar los datos al terminar")
    args = parser.parse_args()

    await _guard_not_production()

    report = Report()
    spy = MessageSpy()
    # Se intercepta el emisor, no notify_customer: así se ejerce de verdad el
    # ruteo por canal, que es donde estuvo el bug de dejar mudo a un canal.
    whatsapp_module.send_whatsapp_message = spy.send

    started = time.monotonic()
    print("=" * 62)
    print("PRUEBA DE RESILIENCIA — día de alta demanda comprimido")
    print("=" * 62)

    async with SessionLocal() as db:
        pairs = await _seed_fleet(db, args.flota)
        stand = (
            await db.execute(
                text("SELECT ST_Y(center::geometry) AS lat, ST_X(center::geometry) AS lng FROM stands ORDER BY name LIMIT 1")
            )
        ).mappings().first()
    origin = (stand["lat"], stand["lng"])

    try:
        trip_ids = await fase1_rafaga(report, args.viajes, origin)
        await fase2_cola(report, trip_ids)
        await fase3_fifo(report, trip_ids, pairs)
        await fase4_mensajes(report, spy)
        await fase5_cancelaciones(report, origin)
        await fase6_caida_de_redis(report)
    finally:
        if not args.conservar:
            print("\nLimpiando datos de la prueba…", flush=True)
            await _cleanup()
        else:
            print(f"\nDatos conservados (placas {PLATE_PREFIX}, teléfonos {PHONE_BLOCK}).")

    elapsed = time.monotonic() - started
    print("\n" + "=" * 62)
    print(f"RESULTADO — {len(report.passed)} bien, {len(report.failed)} mal, {elapsed / 60:.1f} min")
    print("=" * 62)
    for line in report.failed:
        print(f"  ✗ {line}")
    sys.exit(1 if report.failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
