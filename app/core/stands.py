"""Motor de estados de la fila de sitios (spec-sitios-y-fila-v2.md, sección 7).

Dos disparadores, tal como pide la spec:
  1. Por evento — evaluate_ping_for_queue(), llamada desde app.api.location
     con cada ping ya validado (ping_validation.validate_ping,
     queue_eligible=True) y que pasó la precondición (disponible + turno
     abierto). Corre en una tarea de fondo separada del camino caliente de
     ingest_pings (10-20 escrituras/seg).
  2. Barrido periódico — sweep_stand_queues(), cada
     STAND_SWEEP_INTERVAL_SECONDS (ver main.py) para lo que NO depende de
     que llegue un ping: expirar cronómetros de detención y detectar
     pérdida de señal (nadie manda un ping avisando que dejó de mandar
     pings).

Sub-estados (FUERA/DENTRO/CANDIDATO) son derivados, no se persisten — solo
FORMADO vive en `stand_queue`. El cronómetro de CANDIDATO y la racha de
lecturas fuera del polígono (para confirmar salida) se guardan en Redis,
mismo patrón que la racha de posible GPS falso en app.core.ping_validation.
"""

import json
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.redis_client import get_last_position, redis_client
from app.database import SessionLocal
from app.models import (
    Stand,
    StandQueue,
    StandQueueEvent,
    StandQueueStatus,
    Vehicle,
    VehicleAssignment,
    VehicleStatus,
)
from app.schemas.location import LocationPingIn

logger = logging.getLogger(__name__)


# --- Estado transitorio en Redis (sub-estados derivados) --------------------


def _candidate_key(vehicle_id: str) -> str:
    return f"vehicle:{vehicle_id}:stand_candidate"


def _exit_streak_key(vehicle_id: str) -> str:
    return f"vehicle:{vehicle_id}:stand_exit_streak"


def _signal_warned_key(vehicle_id: str) -> str:
    return f"vehicle:{vehicle_id}:stand_signal_warned"


async def _get_candidate_since(vehicle_id: str, stand_id: str) -> datetime | None:
    raw = await redis_client.get(_candidate_key(vehicle_id))
    if not raw:
        return None
    data = json.loads(raw)
    if data.get("stand_id") != stand_id:
        return None
    return datetime.fromisoformat(data["since"])


async def _start_candidate_timer(vehicle_id: str, stand_id: str) -> None:
    await redis_client.set(
        _candidate_key(vehicle_id),
        json.dumps({"stand_id": stand_id, "since": datetime.now(UTC).isoformat()}),
        ex=3600,
    )


async def _clear_candidate_timer(vehicle_id: str) -> None:
    await redis_client.delete(_candidate_key(vehicle_id))


async def _bump_exit_streak(vehicle_id: str, was_fast: bool) -> tuple[int, bool]:
    raw = await redis_client.get(_exit_streak_key(vehicle_id))
    data = json.loads(raw) if raw else {"count": 0, "any_fast": False}
    data["count"] += 1
    data["any_fast"] = data["any_fast"] or was_fast
    await redis_client.set(_exit_streak_key(vehicle_id), json.dumps(data), ex=300)
    return data["count"], data["any_fast"]


async def _reset_exit_streak(vehicle_id: str) -> None:
    await redis_client.delete(_exit_streak_key(vehicle_id))


# --- Helpers de datos ---------------------------------------------------------


async def _is_inside_polygon(db: AsyncSession, stand_id: uuid.UUID, lat: float, lng: float) -> bool:
    # ST_Contains no tiene overload nativo para geography — se castea a
    # geometry, igual que el resto de las consultas geoespaciales del repo
    # que necesitan la variante exacta en vez de ST_DWithin.
    row = (
        await db.execute(
            text(
                """
                SELECT ST_Contains(polygon::geometry, ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)) AS inside
                FROM stands WHERE id = :stand_id
                """
            ),
            {"stand_id": stand_id, "lat": lat, "lng": lng},
        )
    ).mappings().first()
    return bool(row["inside"]) if row else False


async def _queue_has_people(db: AsyncSession, stand_id: uuid.UUID) -> bool:
    result = await db.execute(
        select(StandQueue.id)
        .where(StandQueue.stand_id == stand_id, StandQueue.status == StandQueueStatus.FORMADO)
        .limit(1)
    )
    return result.first() is not None


async def _get_active_queue_row(db: AsyncSession, vehicle_id: uuid.UUID) -> StandQueue | None:
    result = await db.execute(
        select(StandQueue).where(
            StandQueue.vehicle_id == vehicle_id, StandQueue.status == StandQueueStatus.FORMADO
        )
    )
    return result.scalar_one_or_none()


async def _log_event(
    db: AsyncSession,
    vehicle_id: uuid.UUID,
    driver_id: uuid.UUID | None,
    stand_id: uuid.UUID,
    event: str,
    detail: dict | None = None,
) -> None:
    db.add(
        StandQueueEvent(
            vehicle_id=vehicle_id, driver_id=driver_id, stand_id=stand_id, event=event, detail=detail
        )
    )


async def _join_queue(
    db: AsyncSession, vehicle: Vehicle, driver_id: uuid.UUID, stand: Stand, *, position_held: bool = False
) -> StandQueue:
    entry = StandQueue(
        stand_id=stand.id,
        vehicle_id=vehicle.id,
        driver_id=driver_id,
        status=StandQueueStatus.FORMADO,
        position_held=position_held,
    )
    db.add(entry)
    await db.flush()
    await _log_event(
        db, vehicle.id, driver_id, stand.id, "joined_queue", {"position_held": position_held}
    )
    await db.commit()
    logger.info("Unidad %s se formó en el sitio %s", vehicle.id, stand.id)
    return entry


async def _confirm_exit(db: AsyncSession, entry: StandQueue, reason: str) -> None:
    entry.status = StandQueueStatus.SALIO
    entry.left_at = datetime.now(UTC)
    entry.left_reason = reason
    event = "exit_confirmed" if reason == "exit_confirmed" else "dropped_no_signal"
    await _log_event(db, entry.vehicle_id, entry.driver_id, entry.stand_id, event)
    await db.commit()
    await redis_client.delete(_signal_warned_key(str(entry.vehicle_id)))
    logger.info("Unidad %s salió del sitio %s (%s)", entry.vehicle_id, entry.stand_id, reason)


# --- Disparador 1: por evento --------------------------------------------------


async def evaluate_ping_for_queue(db: AsyncSession, vehicle: Vehicle, ping: LocationPingIn) -> None:
    """Mueve la máquina de estados de fila de esta unidad con un ping que ya
    pasó la validación (queue_eligible=True). Precondición (sección 7):
    VehicleStatus == disponible y existe un turno abierto — si no se
    cumple, no hace nada (ni siquiera limpia el estado transitorio: puede
    ser una pausa corta a media evaluación, no una salida)."""
    if vehicle.status != VehicleStatus.DISPONIBLE:
        return

    assignment = await db.execute(
        select(VehicleAssignment.driver_id).where(
            VehicleAssignment.vehicle_id == vehicle.id, VehicleAssignment.ended_at.is_(None)
        )
    )
    driver_id = assignment.scalar_one_or_none()
    if driver_id is None:
        return

    stand = await db.get(Stand, vehicle.stand_id)
    if stand is None or not stand.active:
        return

    vehicle_id_str = str(vehicle.id)
    inside = await _is_inside_polygon(db, stand.id, ping.lat, ping.lng)
    active_entry = await _get_active_queue_row(db, vehicle.id)

    if active_entry is not None:
        await _evaluate_formado_ping(db, active_entry, stand, ping, inside)
        return

    if not inside:
        await _clear_candidate_timer(vehicle_id_str)
        return

    # Candidato — requiere estar quieto, sin excepción para fila vacía
    # (decisión de negocio: la "inserción inmediata" de la spec original
    # insertaría a un carro parado en un semáforo de esquina; con fila
    # vacía no hay disputa de orden que resolver, así que el mínimo es
    # STAND_EMPTY_QUEUE_MIN_STOP_SECONDS en vez de still_seconds, pero
    # sigue existiendo un mínimo).
    queue_has_people = await _queue_has_people(db, stand.id)
    required_seconds = (
        stand.still_seconds if queue_has_people else settings.STAND_EMPTY_QUEUE_MIN_STOP_SECONDS
    )

    speed = ping.speed or 0.0
    if speed >= stand.max_speed_kmh:
        await _clear_candidate_timer(vehicle_id_str)  # se movió, el cronómetro se reinicia
        return

    since = await _get_candidate_since(vehicle_id_str, str(stand.id))
    if since is None:
        await _start_candidate_timer(vehicle_id_str, str(stand.id))
        return

    if (datetime.now(UTC) - since).total_seconds() >= required_seconds:
        await _join_queue(db, vehicle, driver_id, stand)
        await _clear_candidate_timer(vehicle_id_str)


async def _evaluate_formado_ping(
    db: AsyncSession, entry: StandQueue, stand: Stand, ping: LocationPingIn, inside: bool
) -> None:
    """Salida confirmada = 3 lecturas seguidas fuera del polígono Y speed >
    max_speed_kmh en al menos una de ellas (sección 7, "Salida confirmada").
    Sin período de gracia, pero tampoco una sola lectura — el GPS brinca
    30-50m con el carro quieto."""
    vehicle_id_str = str(entry.vehicle_id)
    if inside:
        await _reset_exit_streak(vehicle_id_str)
        # La señal volvió a confirmar que sigue aquí — ya no hace falta el
        # aviso de "sin señal" si lo hubiera.
        await redis_client.delete(_signal_warned_key(vehicle_id_str))
        return

    speed = ping.speed or 0.0
    count, any_fast = await _bump_exit_streak(vehicle_id_str, speed > stand.max_speed_kmh)
    if count >= settings.STAND_EXIT_CONSECUTIVE_PINGS and any_fast:
        await _confirm_exit(db, entry, "exit_confirmed")
        await _reset_exit_streak(vehicle_id_str)


async def run_queue_evaluation_batch(vehicle_id: uuid.UUID, pings: list[LocationPingIn]) -> None:
    """Punto de entrada para la tarea de fondo (asyncio.create_task) que
    dispara app.api.location tras persistir un lote — con su propia
    SessionLocal(), igual que dispatch_trip, porque corre fuera de la
    request que la disparó. Evalúa cada ping EN ORDEN: un lote puede traer
    varias lecturas seguidas de un buffer offline, y el cronómetro de
    detención solo tiene sentido evaluado secuencialmente, no de golpe."""
    async with SessionLocal() as db:
        vehicle = await db.get(Vehicle, vehicle_id)
        if vehicle is None:
            return
        for ping in pings:
            # Releer el vehículo no hace falta entre pings de un mismo lote:
            # nada dentro de este bucle cambia vehicle.status/stand_id.
            await evaluate_ping_for_queue(db, vehicle, ping)


# --- Disparador 2: barrido periódico ------------------------------------------


async def sweep_stand_queues() -> None:
    """Corre cada STAND_SWEEP_INTERVAL_SECONDS (ver main.py). Dos cosas que
    NO dependen de que llegue un ping nuevo:
      - Pérdida de señal de una unidad ya formada (avisar a los
        QUEUE_SIGNAL_WARN_SECONDS, sacar de la fila a los
        QUEUE_SIGNAL_DROP_SECONDS).
      - Cronómetros de candidato que ya cumplieron still_seconds pero no
        llegó un ping nuevo para confirmarlo (siguen sin señal, pero ya
        sabíamos que estaban quietos y dentro con el último ping real)."""
    async with SessionLocal() as db:
        await _sweep_signal_loss(db)
        await _sweep_candidate_timers(db)


async def _sweep_signal_loss(db: AsyncSession) -> None:
    result = await db.execute(select(StandQueue).where(StandQueue.status == StandQueueStatus.FORMADO))
    for entry in result.scalars().all():
        cached = await get_last_position(str(entry.vehicle_id))
        last_seen = datetime.fromisoformat(cached["timestamp"]) if cached else entry.entered_at
        idle_seconds = (datetime.now(UTC) - last_seen).total_seconds()

        if idle_seconds > settings.QUEUE_SIGNAL_DROP_SECONDS:
            await _confirm_exit(db, entry, "dropped_no_signal")
            continue

        if idle_seconds > settings.QUEUE_SIGNAL_WARN_SECONDS:
            warned_key = _signal_warned_key(str(entry.vehicle_id))
            if not await redis_client.exists(warned_key):
                await _log_event(db, entry.vehicle_id, entry.driver_id, entry.stand_id, "signal_lost")
                await db.commit()
                await redis_client.set(warned_key, "1", ex=settings.QUEUE_SIGNAL_DROP_SECONDS)
                logger.warning(
                    "Unidad %s sin señal hace %.0fs en el sitio %s — conserva su lugar",
                    entry.vehicle_id, idle_seconds, entry.stand_id,
                )


async def _sweep_candidate_timers(db: AsyncSession) -> None:
    async for key in redis_client.scan_iter(match="vehicle:*:stand_candidate"):
        vehicle_id_str = key.split(":")[1]
        raw = await redis_client.get(key)
        if not raw:
            continue
        data = json.loads(raw)
        since = datetime.fromisoformat(data["since"])
        stand_id = data["stand_id"]

        stand = await db.get(Stand, stand_id)
        if stand is None:
            continue
        required_seconds = (
            stand.still_seconds
            if await _queue_has_people(db, stand.id)
            else settings.STAND_EMPTY_QUEUE_MIN_STOP_SECONDS
        )
        if (datetime.now(UTC) - since).total_seconds() < required_seconds:
            continue

        vehicle = await db.get(Vehicle, vehicle_id_str)
        if (
            vehicle is None
            or vehicle.status != VehicleStatus.DISPONIBLE
            or str(vehicle.stand_id) != stand_id
        ):
            await redis_client.delete(key)
            continue

        assignment = await db.execute(
            select(VehicleAssignment.driver_id).where(
                VehicleAssignment.vehicle_id == vehicle.id, VehicleAssignment.ended_at.is_(None)
            )
        )
        driver_id = assignment.scalar_one_or_none()
        if driver_id is None:
            await redis_client.delete(key)
            continue

        if await _get_active_queue_row(db, vehicle.id) is None:
            await _join_queue(db, vehicle, driver_id, stand)
        await redis_client.delete(key)
