"""Pruebas de la capa de validación de pings (app.core.ping_validation,
sección 6 de spec-sitios-y-fila-v2.md).

No usa `db_session`/`client`: validate_ping no toca Postgres, solo Redis (la
racha de posible GPS falso) — un `vehicle_id` aleatorio por prueba evita que
esa racha se contamine entre pruebas dentro de la misma corrida.
"""

import uuid
from datetime import UTC, datetime, timedelta

from app.config import settings
from app.core.ping_validation import validate_ping
from app.schemas.location import LocationPingIn

_ORIGIN = {"lat": 19.4326, "lng": -99.1332}  # Zócalo


def _ping(**overrides) -> LocationPingIn:
    payload = {
        "lat": _ORIGIN["lat"],
        "lng": _ORIGIN["lng"],
        "speed": 35.0,
        "heading": 180.0,
        "accuracy": 8.0,
        "timestamp": datetime.now(UTC),
    }
    payload.update(overrides)
    return LocationPingIn(**payload)


def _cached(**overrides) -> dict:
    payload = {
        "lat": _ORIGIN["lat"],
        "lng": _ORIGIN["lng"],
        "timestamp": datetime.now(UTC).isoformat(),
    }
    payload.update(overrides)
    return payload


async def test_valid_ping_passes_with_no_previous():
    vehicle_id = str(uuid.uuid4())
    result = await validate_ping(vehicle_id, _ping(), previous=None)
    assert result.queue_eligible is True
    assert result.flag_reason is None


async def test_rejects_poor_accuracy():
    vehicle_id = str(uuid.uuid4())
    result = await validate_ping(
        vehicle_id, _ping(accuracy=settings.GPS_MAX_ACCURACY_METERS + 1), previous=None
    )
    assert result.queue_eligible is False
    assert result.flag_reason == "accuracy"


async def test_rejects_impossible_jump():
    vehicle_id = str(uuid.uuid4())
    previous = _cached(timestamp=(datetime.now(UTC) - timedelta(seconds=10)).isoformat())
    # Zócalo a Puebla (~100 km) en 10 s es imposible a cualquier velocidad razonable.
    far_ping = _ping(lat=19.04, lng=-98.20)

    result = await validate_ping(vehicle_id, far_ping, previous)
    assert result.queue_eligible is False
    assert result.flag_reason == "jump"


async def test_accepts_plausible_movement_between_pings():
    vehicle_id = str(uuid.uuid4())
    previous = _cached(timestamp=(datetime.now(UTC) - timedelta(seconds=10)).isoformat())
    # ~110 m en 10 s (~40 km/h) — normal en tráfico urbano.
    nearby_ping = _ping(lat=19.4336, lng=-99.1332)

    result = await validate_ping(vehicle_id, nearby_ping, previous)
    assert result.queue_eligible is True


async def test_single_zero_speed_zero_accuracy_ping_is_not_flagged():
    """Un carro parado con buena señal también puede dar speed=0 una vez —
    solo la racha repetida es sospechosa, no una lectura suelta."""
    vehicle_id = str(uuid.uuid4())
    result = await validate_ping(vehicle_id, _ping(speed=0, accuracy=0), previous=None)
    assert result.queue_eligible is True


async def test_repeated_zero_speed_zero_accuracy_flags_fake_gps():
    vehicle_id = str(uuid.uuid4())
    suspicious = _ping(speed=0, accuracy=0)

    first = await validate_ping(vehicle_id, suspicious, previous=None)
    second = await validate_ping(vehicle_id, suspicious, previous=None)
    third = await validate_ping(vehicle_id, suspicious, previous=None)

    assert first.queue_eligible is True
    assert second.queue_eligible is True
    assert third.queue_eligible is False
    assert third.flag_reason == "fake_gps"


async def test_normal_ping_resets_fake_gps_streak():
    vehicle_id = str(uuid.uuid4())
    suspicious = _ping(speed=0, accuracy=0)

    await validate_ping(vehicle_id, suspicious, previous=None)
    await validate_ping(vehicle_id, suspicious, previous=None)
    # Una lectura normal de por medio reinicia la racha.
    await validate_ping(vehicle_id, _ping(), previous=None)

    third = await validate_ping(vehicle_id, suspicious, previous=None)
    assert third.queue_eligible is True  # racha reiniciada, esta es la 1a otra vez


async def test_rejects_ping_older_than_max_lag():
    vehicle_id = str(uuid.uuid4())
    stale = _ping(
        timestamp=datetime.now(UTC) - timedelta(seconds=settings.QUEUE_PING_MAX_LAG_SECONDS + 30)
    )
    result = await validate_ping(vehicle_id, stale, previous=None)
    assert result.queue_eligible is False
    assert result.flag_reason == "lag"


async def test_accepts_ping_within_max_lag():
    vehicle_id = str(uuid.uuid4())
    recent = _ping(
        timestamp=datetime.now(UTC) - timedelta(seconds=settings.QUEUE_PING_MAX_LAG_SECONDS - 10)
    )
    result = await validate_ping(vehicle_id, recent, previous=None)
    assert result.queue_eligible is True
