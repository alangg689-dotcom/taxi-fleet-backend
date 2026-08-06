"""Pruebas del bot de WhatsApp (app.core.whatsapp_bot y app.api.whatsapp).

handle_incoming_message usa SessionLocal() a propósito (mismo motivo que
dispatch_trip, ver docstring de tests/test_dispatch.py): corre fuera de una
request HTTP normal. Eso significa que los viajes que crea no son visibles
para el `db_session` de SAVEPOINT de las demás pruebas — aquí se verifican
consultando con el propio SessionLocal, la misma conexión real que usa el
código bajo prueba.
"""

from datetime import UTC, datetime, timedelta

import pytest_asyncio

import app.core.whatsapp_bot as bot
from app.database import SessionLocal, engine
from app.models import Trip, TripStatus

_PHONE = "+525512340099"


@pytest_asyncio.fixture(autouse=True)
async def _dispose_engine_pool_between_tests():
    """Cada prueba de pytest-asyncio corre en su propio event loop; una
    conexión que el pool de `engine` (SessionLocal real, no el de
    SAVEPOINT) haya dejado abierta de una prueba anterior revienta con
    "Event loop is closed" al reusarse en el loop de la siguiente. Mismo
    motivo que _reset_redis_pool en conftest.py, pero para Postgres."""
    yield
    await engine.dispose()


async def _noop_dispatch(trip_id):
    return None


async def _fetch_trip(trip_id):
    async with SessionLocal() as db:
        return await db.get(Trip, trip_id)


# --- _trip_still_active (función pura, sin SessionLocal) --------------------


async def test_trip_still_active_true_for_asignado():
    trip = Trip(status=TripStatus.ASIGNADO)
    assert await bot._trip_still_active(trip) is True


async def test_trip_still_active_true_for_en_curso():
    trip = Trip(status=TripStatus.EN_CURSO)
    assert await bot._trip_still_active(trip) is True


async def test_trip_still_active_false_for_completado():
    trip = Trip(status=TripStatus.COMPLETADO)
    assert await bot._trip_still_active(trip) is False


async def test_trip_still_active_false_for_cancelado():
    trip = Trip(status=TripStatus.CANCELADO)
    assert await bot._trip_still_active(trip) is False


async def test_trip_still_active_false_for_none():
    assert await bot._trip_still_active(None) is False


async def test_trip_still_active_true_for_recent_solicitado():
    """dispatch_trip cancela un viaje del bot en cuanto se rinde (ver
    app.core.dispatch) — mientras siga "solicitado" y reciente, se asume
    que todavía lo está recorriendo de verdad."""
    trip = Trip(status=TripStatus.SOLICITADO, requested_at=datetime.now(UTC))
    assert await bot._trip_still_active(trip) is True


async def test_trip_still_active_false_for_orphaned_solicitado():
    """Red de seguridad: si el backend se cae a media tarea, dispatch_trip
    nunca llega a cancelar el viaje — sin este tope de edad, ese cliente se
    quedaría bloqueado para siempre sin poder pedir otro taxi."""
    stale = datetime.now(UTC) - timedelta(seconds=bot._ORPHAN_TRIP_MAX_AGE_SECONDS + 60)
    trip = Trip(status=TripStatus.SOLICITADO, requested_at=stale)
    assert await bot._trip_still_active(trip) is False


# --- handle_incoming_message --------------------------------------------------


async def test_message_without_location_sends_greeting():
    reply = await bot.handle_incoming_message(_PHONE, None, None)
    assert reply == bot._GREETING


async def test_message_with_location_creates_trip_and_dispatches(monkeypatch):
    monkeypatch.setattr(bot, "dispatch_trip", _noop_dispatch)

    reply = await bot.handle_incoming_message(_PHONE, 19.4326, -99.1332)
    assert reply == bot._SEARCHING

    trip_id = await bot._get_active_trip_id(_PHONE)
    assert trip_id is not None

    trip = await _fetch_trip(trip_id)
    assert trip is not None
    assert trip.customer_phone == _PHONE
    assert trip.status == TripStatus.SOLICITADO

    await bot._clear_active_trip(_PHONE)


async def test_message_with_active_trip_does_not_create_another(monkeypatch):
    monkeypatch.setattr(bot, "dispatch_trip", _noop_dispatch)

    await bot.handle_incoming_message(_PHONE, 19.4326, -99.1332)
    first_trip_id = await bot._get_active_trip_id(_PHONE)

    reply = await bot.handle_incoming_message(_PHONE, 19.5, -99.2)
    assert reply == bot._ALREADY_ACTIVE
    assert await bot._get_active_trip_id(_PHONE) == first_trip_id

    await bot._clear_active_trip(_PHONE)


async def test_message_after_trip_finished_allows_new_request(monkeypatch):
    monkeypatch.setattr(bot, "dispatch_trip", _noop_dispatch)

    await bot.handle_incoming_message(_PHONE, 19.4326, -99.1332)
    first_trip_id = await bot._get_active_trip_id(_PHONE)

    async with SessionLocal() as db:
        trip = await db.get(Trip, first_trip_id)
        trip.status = TripStatus.COMPLETADO
        await db.commit()

    reply = await bot.handle_incoming_message(_PHONE, 19.5, -99.2)
    assert reply == bot._SEARCHING

    second_trip_id = await bot._get_active_trip_id(_PHONE)
    assert second_trip_id != first_trip_id

    await bot._clear_active_trip(_PHONE)


# --- POST /whatsapp/webhook --------------------------------------------------
# Contrato HTTP aislado: handle_incoming_message se parchea a un doble fijo,
# la lógica de conversación ya se prueba arriba sin pasar por HTTP.


async def test_webhook_returns_twiml_with_the_bot_reply(client, monkeypatch):
    import app.api.whatsapp as whatsapp_module

    async def _fake_handle(phone, lat, lng):
        assert phone == "+525512340099"
        assert lat is None
        assert lng is None
        return "hola, mándame tu ubicación"

    monkeypatch.setattr(whatsapp_module, "handle_incoming_message", _fake_handle)

    response = await client.post(
        "/api/v1/whatsapp/webhook",
        data={"From": "whatsapp:+525512340099", "Body": "hola"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    assert "mándame tu ubicación" in response.text


async def test_webhook_parses_latitude_and_longitude_as_floats(client, monkeypatch):
    import app.api.whatsapp as whatsapp_module

    captured = {}

    async def _fake_handle(phone, lat, lng):
        captured["lat"] = lat
        captured["lng"] = lng
        return "ok"

    monkeypatch.setattr(whatsapp_module, "handle_incoming_message", _fake_handle)

    await client.post(
        "/api/v1/whatsapp/webhook",
        data={
            "From": "whatsapp:+525512340099",
            "Latitude": "19.4326",
            "Longitude": "-99.1332",
        },
    )
    assert captured["lat"] == 19.4326
    assert captured["lng"] == -99.1332
