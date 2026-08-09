"""Pruebas del techo de telemetría por unidad (app.core.ping_throttle).

Redis NO se limpia entre pruebas (ver _reset_redis_pool en conftest.py: cierra
el pool, no borra claves), así que cada prueba usa su propia unidad —
make_vehicle acuña un UUID nuevo cada vez, y el contador va por vehicle_id.
Las que reusan una unidad llaman ping_throttle.reset() explícitamente.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.config import settings
from app.core import ping_throttle
from tests.factories import make_vehicle

_DEVICE_HEADER = "X-Device-Key"


def _ping_payload(count: int) -> dict:
    """`count` pings con timestamps distintos: el índice único
    (vehicle_id, timestamp) descarta duplicados, y aquí lo que se está
    midiendo es el techo, no la deduplicación."""
    base = datetime.now(UTC)
    return {
        "pings": [
            {
                "lat": 19.4326,
                "lng": -99.1332,
                "speed": 30.0,
                "accuracy": 8.0,
                "timestamp": (base - timedelta(seconds=i)).isoformat(),
            }
            for i in range(count)
        ]
    }


# --- La unidad de lógica, sin HTTP --------------------------------------------


async def test_under_the_cap_does_not_raise(db_session):
    vehicle = await make_vehicle(db_session)
    await ping_throttle.check_and_count(str(vehicle.id), settings.PING_MAX_PER_WINDOW - 1)


async def test_exceeding_the_cap_raises(db_session):
    vehicle = await make_vehicle(db_session)

    await ping_throttle.check_and_count(str(vehicle.id), settings.PING_MAX_PER_WINDOW)
    with pytest.raises(ping_throttle.PingRateLimitExceeded):
        await ping_throttle.check_and_count(str(vehicle.id), 1)


async def test_counts_pings_not_calls(db_session):
    """El punto del limitador: una sola petición puede traer un lote entero,
    así que contar peticiones dejaría pasar LOCATION_BATCH_MAX veces más de
    lo previsto."""
    vehicle = await make_vehicle(db_session)
    batch = settings.PING_MAX_PER_WINDOW // 2 + 1

    await ping_throttle.check_and_count(str(vehicle.id), batch)
    with pytest.raises(ping_throttle.PingRateLimitExceeded):
        # Dos llamadas nada más, pero entre las dos se pasan del techo.
        await ping_throttle.check_and_count(str(vehicle.id), batch)


async def test_cap_is_per_vehicle(db_session):
    """Una unidad excedida no debe frenar a la de junto — por eso el
    contador va por vehicle_id y no por IP (detrás de un carrier móvil
    muchas unidades comparten IP)."""
    noisy = await make_vehicle(db_session)
    quiet = await make_vehicle(db_session)

    with pytest.raises(ping_throttle.PingRateLimitExceeded):
        await ping_throttle.check_and_count(str(noisy.id), settings.PING_MAX_PER_WINDOW + 1)

    await ping_throttle.check_and_count(str(quiet.id), 1)


async def test_retry_after_is_positive(db_session):
    vehicle = await make_vehicle(db_session)

    with pytest.raises(ping_throttle.PingRateLimitExceeded) as exc_info:
        await ping_throttle.check_and_count(str(vehicle.id), settings.PING_MAX_PER_WINDOW + 1)
    assert exc_info.value.retry_after > 0
    assert exc_info.value.retry_after <= settings.PING_WINDOW_SECONDS


async def test_reset_clears_the_counter(db_session):
    vehicle = await make_vehicle(db_session)

    with pytest.raises(ping_throttle.PingRateLimitExceeded):
        await ping_throttle.check_and_count(str(vehicle.id), settings.PING_MAX_PER_WINDOW + 1)

    await ping_throttle.reset(str(vehicle.id))
    await ping_throttle.check_and_count(str(vehicle.id), 1)


# --- POST /location/ping ------------------------------------------------------


async def test_ping_endpoint_accepts_a_normal_batch(client, db_session):
    vehicle, device_key = await make_vehicle(db_session, with_device_key=True)

    response = await client.post(
        "/api/v1/location/ping",
        json=_ping_payload(3),
        headers={_DEVICE_HEADER: device_key},
    )
    assert response.status_code == 202
    assert response.json()["accepted"] == 3


async def test_ping_endpoint_returns_429_over_the_cap(client, db_session):
    vehicle, device_key = await make_vehicle(db_session, with_device_key=True)
    # Se consume el presupuesto sin pasar por HTTP: mandar 600 pings de
    # verdad por el endpoint solo haría la prueba lenta, no más honesta.
    await ping_throttle.check_and_count(str(vehicle.id), settings.PING_MAX_PER_WINDOW)

    response = await client.post(
        "/api/v1/location/ping",
        json=_ping_payload(1),
        headers={_DEVICE_HEADER: device_key},
    )
    assert response.status_code == 429
    assert response.headers["Retry-After"]


async def test_ping_endpoint_still_rejects_oversized_batches(client, db_session):
    """El tope por petición (LocationBatchIn) sigue vivo y es independiente
    del techo por ventana — 422 de validación, no 429."""
    _, device_key = await make_vehicle(db_session, with_device_key=True)

    response = await client.post(
        "/api/v1/location/ping",
        json=_ping_payload(settings.LOCATION_BATCH_MAX + 1),
        headers={_DEVICE_HEADER: device_key},
    )
    assert response.status_code == 422
