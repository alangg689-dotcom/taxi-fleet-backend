"""Pruebas del bot de WhatsApp (app.core.whatsapp_bot y app.api.whatsapp).

handle_incoming_message usa SessionLocal() a propósito (mismo motivo que
dispatch_trip, ver docstring de tests/test_dispatch.py): corre fuera de una
request HTTP normal. Eso significa que los viajes que crea no son visibles
para el `db_session` de SAVEPOINT de las demás pruebas — aquí se verifican
consultando con el propio SessionLocal, la misma conexión real que usa el
código bajo prueba.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest_asyncio

import app.core.whatsapp_bot as bot
from app.api.location import _point
from app.config import settings
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


async def _cleanup_trip(trip_id):
    """Estos viajes se crean con SessionLocal() real (no el SAVEPOINT de
    db_session), así que no los limpia el rollback de la prueba — sin esto
    quedarían "solicitado" en flotilla_test y el barrido de la siguiente
    prueba los recogería como suyos."""
    async with SessionLocal() as db:
        trip = await db.get(Trip, trip_id)
        if trip is not None:
            await db.delete(trip)
            await db.commit()
    await bot._clear_active_trip(_PHONE)


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
    trip = Trip(status=TripStatus.SOLICITADO, requested_at=datetime.now(UTC))
    assert await bot._trip_still_active(trip) is True


async def test_trip_still_active_true_for_old_solicitado():
    """Ya no hay criterio de edad: "solicitado" viejo significa que el
    barrido lo sigue reintentando, no que quedó huérfano. Quien decide que
    ya fue demasiado es sweep_stuck_bot_trips (BOT_TRIP_MAX_WAIT_SECONDS),
    y cuando lo hace lo cancela — así que aquí basta con mirar el estado."""
    old = datetime.now(UTC) - timedelta(hours=3)
    trip = Trip(status=TripStatus.SOLICITADO, requested_at=old)
    assert await bot._trip_still_active(trip) is True


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


# --- Comando "cancelar" ---------------------------------------------------------


async def test_cancelar_cancels_the_active_trip(monkeypatch):
    monkeypatch.setattr(bot, "dispatch_trip", _noop_dispatch)

    await bot.handle_incoming_message(_PHONE, 19.4326, -99.1332)
    trip_id = await bot._get_active_trip_id(_PHONE)

    reply = await bot.handle_incoming_message(_PHONE, None, None, "cancelar")
    assert reply == bot._CANCELLED

    trip = await _fetch_trip(trip_id)
    assert trip.status == TripStatus.CANCELADO
    # La conversación queda limpia: puede volver a pedir de inmediato.
    assert await bot._get_active_trip_id(_PHONE) is None


async def test_cancelar_is_case_and_space_insensitive(monkeypatch):
    monkeypatch.setattr(bot, "dispatch_trip", _noop_dispatch)

    await bot.handle_incoming_message(_PHONE, 19.4326, -99.1332)
    reply = await bot.handle_incoming_message(_PHONE, None, None, "  CANCELAR  ")
    assert reply == bot._CANCELLED

    await bot._clear_active_trip(_PHONE)


async def test_cancelar_without_active_trip_says_so():
    reply = await bot.handle_incoming_message(_PHONE, None, None, "cancelar")
    assert reply == bot._NOTHING_TO_CANCEL


async def test_normal_text_is_not_confused_with_cancelar(monkeypatch):
    """Solo la palabra sola cancela — un mensaje que la mencione de pasada
    no debe tumbar el viaje de alguien que está esperando su taxi."""
    monkeypatch.setattr(bot, "dispatch_trip", _noop_dispatch)

    await bot.handle_incoming_message(_PHONE, 19.4326, -99.1332)
    trip_id = await bot._get_active_trip_id(_PHONE)

    reply = await bot.handle_incoming_message(
        _PHONE, None, None, "no quiero cancelar, solo pregunto cuánto falta"
    )
    assert reply == bot._ALREADY_ACTIVE

    trip = await _fetch_trip(trip_id)
    assert trip.status == TripStatus.SOLICITADO

    await bot._clear_active_trip(_PHONE)


# --- sweep_stuck_bot_trips ------------------------------------------------------


async def test_sweep_retries_a_stuck_bot_trip(monkeypatch):
    """Un viaje del bot "solicitado" sin oferta viva se vuelve a despachar:
    quizá ahora sí hay una unidad libre que no la había antes."""
    monkeypatch.setattr(bot, "dispatch_trip", _noop_dispatch)
    retried = []

    async def _spy_dispatch(trip_id):
        retried.append(trip_id)

    async with SessionLocal() as db:
        trip = Trip(origin=_point(19.4326, -99.1332), customer_phone=_PHONE)
        db.add(trip)
        await db.flush()
        trip_id = trip.id
        await db.commit()

    monkeypatch.setattr(bot, "dispatch_trip", _spy_dispatch)
    await bot.sweep_stuck_bot_trips()
    await asyncio.sleep(0.05)  # deja correr la tarea de fondo que lanza el barrido

    assert trip_id in retried

    await _cleanup_trip(trip_id)


async def test_sweep_skips_a_trip_with_a_live_offer(monkeypatch):
    """Con una oferta viva, dispatch_trip ya está cuidando ese viaje — el
    barrido no debe meterse a medio cascadeo de candidatos."""
    retried = []

    async def _spy_dispatch(trip_id):
        retried.append(trip_id)

    monkeypatch.setattr(bot, "dispatch_trip", _spy_dispatch)

    async with SessionLocal() as db:
        trip = Trip(
            origin=_point(19.4326, -99.1332),
            customer_phone=_PHONE,
            offer_expires_at=datetime.now(UTC) + timedelta(seconds=20),
        )
        db.add(trip)
        await db.flush()
        trip_id = trip.id
        await db.commit()

    await bot.sweep_stuck_bot_trips()
    await asyncio.sleep(0.05)

    assert trip_id not in retried

    await _cleanup_trip(trip_id)


async def test_sweep_gives_up_after_max_wait(monkeypatch):
    """Pasado BOT_TRIP_MAX_WAIT_SECONDS sí se cancela y se le avisa al
    cliente — esperar para siempre sería peor que un "no encontramos"."""
    sent = []

    async def _fake_send(phone, body):
        sent.append((phone, body))

    monkeypatch.setattr(bot, "send_whatsapp_message", _fake_send)

    stale = datetime.now(UTC) - timedelta(seconds=settings.BOT_TRIP_MAX_WAIT_SECONDS + 60)
    async with SessionLocal() as db:
        trip = Trip(
            origin=_point(19.4326, -99.1332), customer_phone=_PHONE, requested_at=stale
        )
        db.add(trip)
        await db.flush()
        trip_id = trip.id
        await db.commit()
    await bot._set_active_trip(_PHONE, trip_id)

    await bot.sweep_stuck_bot_trips()

    trip = await _fetch_trip(trip_id)
    assert trip.status == TripStatus.CANCELADO
    assert sent == [(_PHONE, bot._GAVE_UP)]
    assert await bot._get_active_trip_id(_PHONE) is None


async def test_sweep_ignores_operator_trips(monkeypatch):
    """Sin customer_phone no es del bot: un viaje de operador se queda
    "solicitado" a propósito para que alguien lo redespache desde el
    dashboard, y nadie a quien avisarle por WhatsApp."""
    retried = []

    async def _spy_dispatch(trip_id):
        retried.append(trip_id)

    monkeypatch.setattr(bot, "dispatch_trip", _spy_dispatch)

    async with SessionLocal() as db:
        trip = Trip(origin=_point(19.4326, -99.1332))  # sin customer_phone
        db.add(trip)
        await db.flush()
        trip_id = trip.id
        await db.commit()

    await bot.sweep_stuck_bot_trips()
    await asyncio.sleep(0.05)

    assert trip_id not in retried

    await _cleanup_trip(trip_id)


# --- POST /whatsapp/webhook --------------------------------------------------
# Contrato HTTP aislado: handle_incoming_message se parchea a un doble fijo,
# la lógica de conversación ya se prueba arriba sin pasar por HTTP.


async def test_webhook_returns_twiml_with_the_bot_reply(client, monkeypatch):
    import app.api.whatsapp as whatsapp_module

    async def _fake_handle(phone, lat, lng, body):
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


async def test_webhook_forwards_the_message_body(client, monkeypatch):
    """El cuerpo del mensaje llega hasta el bot — es lo que hace posible el
    comando "cancelar"; antes el webhook solo pasaba From/Latitude/Longitude."""
    import app.api.whatsapp as whatsapp_module

    captured = {}

    async def _fake_handle(phone, lat, lng, body):
        captured["body"] = body
        return "ok"

    monkeypatch.setattr(whatsapp_module, "handle_incoming_message", _fake_handle)

    await client.post(
        "/api/v1/whatsapp/webhook",
        data={"From": "whatsapp:+525512340099", "Body": "cancelar"},
    )
    assert captured["body"] == "cancelar"


async def test_webhook_parses_latitude_and_longitude_as_floats(client, monkeypatch):
    import app.api.whatsapp as whatsapp_module

    captured = {}

    async def _fake_handle(phone, lat, lng, body):
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
