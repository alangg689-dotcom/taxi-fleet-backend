"""Pruebas del motor de fila de sitios (app.core.stands, sección 7 de
spec-sitios-y-fila-v2.md) con pings sintéticos, tal como pide la propia spec
antes de tocar la app: "Tests con pings sintéticos antes de tocar la app".

evaluate_ping_for_queue recibe `db` (no crea su propia SessionLocal), así
que se prueba igual que find_candidate_drivers en test_dispatch.py: con el
`db_session` normal de SAVEPOINT. run_queue_evaluation_batch y
sweep_stand_queues sí crean su propia SessionLocal (ver el docstring de
test_dispatch.py sobre por qué eso no ve datos sin commit de otra sesión) —
por eso aquí se llaman directo _sweep_signal_loss/_sweep_candidate_timers
con el `db_session` de la prueba, igual que se prueba find_candidate_drivers
en vez de dispatch_trip completo.

El cronómetro de candidato vive en Redis con la hora real (`datetime.now`),
no con `ping.timestamp` — adelantarlo sin esperar en tiempo real de la
prueba requiere manipular esa clave directamente, de ahí `_backdate_candidate`.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

from app.config import settings
from app.core.redis_client import redis_client, set_last_position
from app.core.stands import (
    _candidate_key,
    _sweep_candidate_timers,
    _sweep_signal_loss,
    evaluate_ping_for_queue,
)
from app.models import StandQueue, StandQueueEvent, StandQueueStatus, VehicleStatus
from app.schemas.location import LocationPingIn
from tests.factories import make_driver, make_open_assignment, make_stand, make_vehicle

_CENTER = (19.4326, -99.1332)  # Zócalo — mismo default que make_stand
_OUTSIDE = (19.4430, -99.1420)  # ~1.5km del centro, claramente fuera del cuadro de prueba


def _ping(*, lat=_CENTER[0], lng=_CENTER[1], speed=0.0, **overrides) -> LocationPingIn:
    payload = {"lat": lat, "lng": lng, "speed": speed, "timestamp": datetime.now(UTC)}
    payload.update(overrides)
    return LocationPingIn(**payload)


async def _setup(db, *, stand=None, vehicle_status=VehicleStatus.DISPONIBLE, with_assignment=True):
    stand = stand or await make_stand(db, center=_CENTER)
    vehicle = await make_vehicle(db, status=vehicle_status, stand_id=stand.id)
    driver, _ = await make_driver(db)
    if with_assignment:
        await make_open_assignment(db, vehicle_id=vehicle.id, driver_id=driver.id)
    return stand, vehicle, driver


async def _backdate_candidate(vehicle_id: uuid.UUID, stand_id: uuid.UUID, seconds_ago: float) -> None:
    since = (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat()
    await redis_client.set(
        _candidate_key(str(vehicle_id)),
        json.dumps({"stand_id": str(stand_id), "since": since}),
        ex=3600,
    )


async def _active_entry(db, vehicle_id: uuid.UUID) -> StandQueue | None:
    result = await db.execute(
        StandQueue.__table__.select().where(
            StandQueue.vehicle_id == vehicle_id, StandQueue.status == StandQueueStatus.FORMADO
        )
    )
    row = result.first()
    return row


# --- Fila vacía: la decisión 5 exige un mínimo aunque no haya nadie ---------


async def test_empty_queue_does_not_join_immediately(db_session):
    """La spec original dice "inserción inmediata" con fila vacía; la
    decisión de negocio que la corrige exige un mínimo de todos modos
    (semáforo de esquina) — un solo ping no debe formar a nadie."""
    stand, vehicle, _ = await _setup(db_session)

    await evaluate_ping_for_queue(db_session, vehicle, _ping())

    assert await _active_entry(db_session, vehicle.id) is None


async def test_empty_queue_joins_after_min_stop_seconds(db_session):
    stand, vehicle, _ = await _setup(db_session)
    await evaluate_ping_for_queue(db_session, vehicle, _ping())  # arranca el cronómetro
    await _backdate_candidate(
        vehicle.id, stand.id, settings.STAND_EMPTY_QUEUE_MIN_STOP_SECONDS + 1
    )

    await evaluate_ping_for_queue(db_session, vehicle, _ping())

    entry = await _active_entry(db_session, vehicle.id)
    assert entry is not None


async def test_empty_queue_requires_full_min_stop_before_joining(db_session):
    stand, vehicle, _ = await _setup(db_session)
    await evaluate_ping_for_queue(db_session, vehicle, _ping())
    await _backdate_candidate(
        vehicle.id, stand.id, settings.STAND_EMPTY_QUEUE_MIN_STOP_SECONDS - 3
    )

    await evaluate_ping_for_queue(db_session, vehicle, _ping())

    assert await _active_entry(db_session, vehicle.id) is None


# --- Fila con gente: el umbral es still_seconds, más largo ------------------


async def test_non_empty_queue_does_not_join_at_empty_queue_threshold(db_session):
    """El umbral corto (decisión 5) es SOLO para fila vacía — con alguien ya
    formado, moverse antes de still_seconds no debe colarse."""
    stand, vehicle, driver = await _setup(db_session)
    other_vehicle = await make_vehicle(db_session, status=VehicleStatus.DISPONIBLE, stand_id=stand.id)
    db_session.add(
        StandQueue(
            stand_id=stand.id, vehicle_id=other_vehicle.id, driver_id=driver.id,
            status=StandQueueStatus.FORMADO,
        )
    )
    await db_session.flush()

    await evaluate_ping_for_queue(db_session, vehicle, _ping())
    await _backdate_candidate(
        vehicle.id, stand.id, settings.STAND_EMPTY_QUEUE_MIN_STOP_SECONDS + 1
    )
    await evaluate_ping_for_queue(db_session, vehicle, _ping())

    assert await _active_entry(db_session, vehicle.id) is None


async def test_non_empty_queue_joins_after_still_seconds(db_session):
    stand, vehicle, driver = await _setup(db_session)
    other_vehicle = await make_vehicle(db_session, status=VehicleStatus.DISPONIBLE, stand_id=stand.id)
    db_session.add(
        StandQueue(
            stand_id=stand.id, vehicle_id=other_vehicle.id, driver_id=driver.id,
            status=StandQueueStatus.FORMADO,
        )
    )
    await db_session.flush()

    await evaluate_ping_for_queue(db_session, vehicle, _ping())
    await _backdate_candidate(vehicle.id, stand.id, stand.still_seconds + 1)
    await evaluate_ping_for_queue(db_session, vehicle, _ping())

    assert await _active_entry(db_session, vehicle.id) is not None


# --- Movimiento / fuera del polígono resetean el candidato -------------------


async def test_moving_above_max_speed_resets_candidate_timer(db_session):
    stand, vehicle, _ = await _setup(db_session)
    await evaluate_ping_for_queue(db_session, vehicle, _ping())
    await _backdate_candidate(
        vehicle.id, stand.id, settings.STAND_EMPTY_QUEUE_MIN_STOP_SECONDS + 1
    )

    # Un ping rápido de por medio reinicia el cronómetro antes de que cuente.
    await evaluate_ping_for_queue(db_session, vehicle, _ping(speed=stand.max_speed_kmh + 1))
    assert await redis_client.get(_candidate_key(str(vehicle.id))) is None

    await evaluate_ping_for_queue(db_session, vehicle, _ping())  # reinicia desde cero
    assert await _active_entry(db_session, vehicle.id) is None


async def test_ping_outside_polygon_does_not_join(db_session):
    stand, vehicle, _ = await _setup(db_session)

    await evaluate_ping_for_queue(db_session, vehicle, _ping(lat=_OUTSIDE[0], lng=_OUTSIDE[1]))

    assert await _active_entry(db_session, vehicle.id) is None
    assert await redis_client.get(_candidate_key(str(vehicle.id))) is None


# --- Precondición: disponible + turno abierto --------------------------------


async def test_occupied_vehicle_never_joins_queue(db_session):
    stand, vehicle, _ = await _setup(db_session, vehicle_status=VehicleStatus.OCUPADO)

    await evaluate_ping_for_queue(db_session, vehicle, _ping())
    await _backdate_candidate(vehicle.id, stand.id, 3600)
    await evaluate_ping_for_queue(db_session, vehicle, _ping())

    assert await _active_entry(db_session, vehicle.id) is None


async def test_vehicle_without_open_assignment_never_joins_queue(db_session):
    stand, vehicle, _ = await _setup(db_session, with_assignment=False)

    await evaluate_ping_for_queue(db_session, vehicle, _ping())
    await _backdate_candidate(vehicle.id, stand.id, 3600)
    await evaluate_ping_for_queue(db_session, vehicle, _ping())

    assert await _active_entry(db_session, vehicle.id) is None


# --- Salida confirmada: 3 lecturas seguidas fuera Y al menos una rápida -----


async def test_exit_confirmed_after_three_outside_pings_with_one_fast(db_session):
    stand, vehicle, driver = await _setup(db_session)
    db_session.add(
        StandQueue(
            stand_id=stand.id, vehicle_id=vehicle.id, driver_id=driver.id,
            status=StandQueueStatus.FORMADO,
        )
    )
    await db_session.flush()

    fast_outside = _ping(lat=_OUTSIDE[0], lng=_OUTSIDE[1], speed=stand.max_speed_kmh + 10)
    await evaluate_ping_for_queue(db_session, vehicle, fast_outside)
    await evaluate_ping_for_queue(db_session, vehicle, fast_outside)
    assert await _active_entry(db_session, vehicle.id) is not None  # todavía no, van 2

    await evaluate_ping_for_queue(db_session, vehicle, fast_outside)
    assert await _active_entry(db_session, vehicle.id) is None  # la 3a confirma la salida

    events = (
        await db_session.execute(
            StandQueueEvent.__table__.select().where(StandQueueEvent.vehicle_id == vehicle.id)
        )
    ).all()
    assert any(e.event == "exit_confirmed" for e in events)


async def test_exit_not_confirmed_without_any_fast_reading(db_session):
    """Lectura literal de la spec: 3 lecturas fuera sin ninguna rápida no
    confirma salida — puede quedar así con GPS al borde del polígono."""
    stand, vehicle, driver = await _setup(db_session)
    db_session.add(
        StandQueue(
            stand_id=stand.id, vehicle_id=vehicle.id, driver_id=driver.id,
            status=StandQueueStatus.FORMADO,
        )
    )
    await db_session.flush()

    slow_outside = _ping(lat=_OUTSIDE[0], lng=_OUTSIDE[1], speed=0.0)
    for _ in range(3):
        await evaluate_ping_for_queue(db_session, vehicle, slow_outside)

    assert await _active_entry(db_session, vehicle.id) is not None


async def test_reentering_polygon_resets_exit_streak(db_session):
    stand, vehicle, driver = await _setup(db_session)
    db_session.add(
        StandQueue(
            stand_id=stand.id, vehicle_id=vehicle.id, driver_id=driver.id,
            status=StandQueueStatus.FORMADO,
        )
    )
    await db_session.flush()

    fast_outside = _ping(lat=_OUTSIDE[0], lng=_OUTSIDE[1], speed=stand.max_speed_kmh + 10)
    await evaluate_ping_for_queue(db_session, vehicle, fast_outside)
    await evaluate_ping_for_queue(db_session, vehicle, fast_outside)
    await evaluate_ping_for_queue(db_session, vehicle, _ping())  # vuelve a entrar, reinicia racha
    await evaluate_ping_for_queue(db_session, vehicle, fast_outside)
    await evaluate_ping_for_queue(db_session, vehicle, fast_outside)

    assert await _active_entry(db_session, vehicle.id) is not None  # van solo 2 desde el reinicio


# --- Barrido periódico: pérdida de señal y cronómetros sin ping nuevo -------


async def test_sweep_warns_on_signal_loss_but_keeps_place(db_session):
    stand, vehicle, driver = await _setup(db_session)
    entry = StandQueue(
        stand_id=stand.id, vehicle_id=vehicle.id, driver_id=driver.id,
        status=StandQueueStatus.FORMADO,
    )
    db_session.add(entry)
    await db_session.flush()

    stale = (datetime.now(UTC) - timedelta(seconds=settings.QUEUE_SIGNAL_WARN_SECONDS + 5)).isoformat()
    await set_last_position(str(vehicle.id), {"lat": _CENTER[0], "lng": _CENTER[1], "timestamp": stale})

    await _sweep_signal_loss(db_session)

    await db_session.refresh(entry)
    assert entry.status == StandQueueStatus.FORMADO
    events = (
        await db_session.execute(
            StandQueueEvent.__table__.select().where(StandQueueEvent.vehicle_id == vehicle.id)
        )
    ).all()
    assert any(e.event == "signal_lost" for e in events)


async def test_sweep_drops_after_signal_drop_threshold(db_session):
    stand, vehicle, driver = await _setup(db_session)
    entry = StandQueue(
        stand_id=stand.id, vehicle_id=vehicle.id, driver_id=driver.id,
        status=StandQueueStatus.FORMADO,
    )
    db_session.add(entry)
    await db_session.flush()

    stale = (datetime.now(UTC) - timedelta(seconds=settings.QUEUE_SIGNAL_DROP_SECONDS + 5)).isoformat()
    await set_last_position(str(vehicle.id), {"lat": _CENTER[0], "lng": _CENTER[1], "timestamp": stale})

    await _sweep_signal_loss(db_session)

    await db_session.refresh(entry)
    assert entry.status == StandQueueStatus.SALIO
    assert entry.left_reason == "dropped_no_signal"


async def test_sweep_promotes_expired_candidate_without_new_ping(db_session):
    """Si el cronómetro ya cumplió pero no llegó un ping nuevo que lo
    confirme, el barrido lo completa igual — no depende de recibir un ping."""
    stand, vehicle, _ = await _setup(db_session)
    await evaluate_ping_for_queue(db_session, vehicle, _ping())  # arranca el cronómetro
    await _backdate_candidate(
        vehicle.id, stand.id, settings.STAND_EMPTY_QUEUE_MIN_STOP_SECONDS + 1
    )

    await _sweep_candidate_timers(db_session)

    assert await _active_entry(db_session, vehicle.id) is not None


# --- Broadcast (sección 9: canal stand:queue) --------------------------------


async def test_joining_queue_broadcasts_to_stand_queue_channel(db_session):
    stand, vehicle, _ = await _setup(db_session)
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(settings.QUEUE_CHANNEL)
    # get_message() solo lee UN mensaje por llamada — la primera es la
    # confirmación del propio SUBSCRIBE, no un mensaje publicado.
    await pubsub.get_message(timeout=1.0)
    try:
        await evaluate_ping_for_queue(db_session, vehicle, _ping())  # arranca el cronómetro
        await _backdate_candidate(
            vehicle.id, stand.id, settings.STAND_EMPTY_QUEUE_MIN_STOP_SECONDS + 1
        )
        await evaluate_ping_for_queue(db_session, vehicle, _ping())  # ahora sí se forma

        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=2.0)
        assert message is not None
        payload = json.loads(message["data"])
        assert payload["stand_id"] == str(stand.id)
        assert payload["event"] == "joined_queue"
        assert payload["vehicle_id"] == str(vehicle.id)
        assert [row["vehicle_id"] for row in payload["queue"]] == [str(vehicle.id)]
    finally:
        await pubsub.unsubscribe(settings.QUEUE_CHANNEL)
        await pubsub.aclose()
