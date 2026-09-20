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
from app.core.demand import Demand
from app.database import SessionLocal, engine
from app.models import Trip, TripStatus

_PHONE = "+525512340099"


def _pin_demand(monkeypatch, *, high: bool = False) -> None:
    """Fija la presión de demanda en vez de dejar que la mida.

    `measure_demand` cuenta TODOS los viajes esperando del sistema, y estas
    pruebas escriben con SessionLocal —fuera del SAVEPOINT que revierte al
    final—, así que lo que dejó otra prueba en la tabla decidiría aquí si el
    viaje se cancela o no. Lo que se prueba en este archivo es el barrido, no
    la medición: esa tiene sus propias pruebas en test_demand.py.
    """
    demand = Demand(
        waiting_trips=0,
        available_drivers=0,
        high_demand=high,
        max_wait_seconds=(
            settings.BOT_TRIP_MAX_WAIT_HIGH_DEMAND_SECONDS
            if high
            else settings.BOT_TRIP_MAX_WAIT_SECONDS
        ),
    )

    async def _fake_measure(_db):
        return demand

    monkeypatch.setattr(bot, "measure_demand", _fake_measure)


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


# --- handle_incoming_message: el flujo completo -------------------------------


async def _request_full_trip(destination: str = "Clínica 4") -> tuple[str, str, str]:
    """Recorre el flujo entero: ubicación → destino → confirmación. Devuelve
    las tres respuestas del bot en orden."""
    r1 = await bot.handle_incoming_message(_PHONE, 19.4326, -99.1332)
    r2 = await bot.handle_incoming_message(_PHONE, None, None, destination)
    r3 = await bot.handle_incoming_message(_PHONE, None, None, "sí")
    return r1, r2, r3


async def test_message_without_location_sends_greeting():
    reply = await bot.handle_incoming_message(_PHONE, None, None)
    assert reply == bot._GREETING


async def test_flujo_completo_ubicacion_destino_confirmacion(monkeypatch):
    """El viaje ya no se crea con la pura ubicación: primero se pide el
    destino y luego la confirmación. Nada toca la base hasta el "sí"."""
    monkeypatch.setattr(bot, "dispatch_trip", _noop_dispatch)
    _pin_demand(monkeypatch)

    ask_dest, confirm_prompt, searching = await _request_full_trip("Clínica 4")

    assert "envíanos tu destino" in ask_dest
    assert "Clínica 4" in confirm_prompt and "Confirmas" in confirm_prompt
    assert "Buscando un taxi" in searching

    trip_id = await bot._get_active_trip_id(_PHONE)
    assert trip_id is not None
    trip = await _fetch_trip(trip_id)
    assert trip.customer_phone == _PHONE
    assert trip.status == TripStatus.SOLICITADO
    assert trip.destination_address == "Clínica 4"

    await _cleanup_trip(trip_id)


async def test_no_se_crea_viaje_antes_de_confirmar(monkeypatch):
    monkeypatch.setattr(bot, "dispatch_trip", _noop_dispatch)

    await bot.handle_incoming_message(_PHONE, 19.4326, -99.1332)
    await bot.handle_incoming_message(_PHONE, None, None, "Clínica 4")

    # En medio de la conversación no hay viaje todavía.
    assert await bot._get_active_trip_id(_PHONE) is None

    await bot._clear_active_trip(_PHONE)


async def test_destino_por_ubicacion_compartida(monkeypatch):
    """El destino puede llegar como segunda ubicación en vez de texto."""
    monkeypatch.setattr(bot, "dispatch_trip", _noop_dispatch)
    _pin_demand(monkeypatch)

    await bot.handle_incoming_message(_PHONE, 19.4326, -99.1332)
    confirm_prompt = await bot.handle_incoming_message(_PHONE, 19.44, -99.14)
    assert "Confirmas" in confirm_prompt

    await bot.handle_incoming_message(_PHONE, None, None, "sí")
    trip_id = await bot._get_active_trip_id(_PHONE)
    trip = await _fetch_trip(trip_id)
    assert trip.destination is not None

    await _cleanup_trip(trip_id)


async def test_cancelar_a_media_solicitud_la_descarta(monkeypatch):
    """Antes del "sí" no hay viaje: cancelar solo tira el borrador, sin
    preguntar dos veces ni tocar la base."""
    monkeypatch.setattr(bot, "dispatch_trip", _noop_dispatch)

    await bot.handle_incoming_message(_PHONE, 19.4326, -99.1332)
    reply = await bot.handle_incoming_message(_PHONE, None, None, "cancelar")

    assert reply == bot._REQUEST_DISCARDED
    assert await bot._get_active_trip_id(_PHONE) is None


async def test_message_with_active_trip_does_not_create_another(monkeypatch):
    monkeypatch.setattr(bot, "dispatch_trip", _noop_dispatch)
    _pin_demand(monkeypatch)

    await _request_full_trip()
    first_trip_id = await bot._get_active_trip_id(_PHONE)

    reply = await bot.handle_incoming_message(_PHONE, 19.5, -99.2)
    assert reply == bot._ALREADY_ACTIVE
    assert await bot._get_active_trip_id(_PHONE) == first_trip_id

    await _cleanup_trip(first_trip_id)


async def test_message_after_trip_finished_allows_new_request(monkeypatch):
    monkeypatch.setattr(bot, "dispatch_trip", _noop_dispatch)
    _pin_demand(monkeypatch)

    await _request_full_trip()
    first_trip_id = await bot._get_active_trip_id(_PHONE)

    async with SessionLocal() as db:
        trip = await db.get(Trip, first_trip_id)
        trip.status = TripStatus.COMPLETADO
        await db.commit()

    reply = await bot.handle_incoming_message(_PHONE, 19.5, -99.2)
    assert "destino" in reply  # arranca una solicitud nueva

    await _cleanup_trip(first_trip_id)


# --- Comando "cancelar" ---------------------------------------------------------


async def test_cancelar_pide_confirmacion_y_luego_cancela(monkeypatch):
    """Con un viaje real de por medio, "cancelar" pregunta antes: tecleado a
    medias o por error dejaría a alguien sin el taxi que sí quería."""
    monkeypatch.setattr(bot, "dispatch_trip", _noop_dispatch)
    _pin_demand(monkeypatch)

    await _request_full_trip()
    trip_id = await bot._get_active_trip_id(_PHONE)

    ask = await bot.handle_incoming_message(_PHONE, None, None, "cancelar")
    assert "seguro" in ask.lower()
    # Todavía nada cambió.
    assert (await _fetch_trip(trip_id)).status == TripStatus.SOLICITADO

    reply = await bot.handle_incoming_message(_PHONE, None, None, "sí")
    assert reply == bot._CANCELLED
    assert (await _fetch_trip(trip_id)).status == TripStatus.CANCELADO
    assert await bot._get_active_trip_id(_PHONE) is None

    await _cleanup_trip(trip_id)


async def test_arrepentirse_de_cancelar_conserva_el_viaje(monkeypatch):
    monkeypatch.setattr(bot, "dispatch_trip", _noop_dispatch)
    _pin_demand(monkeypatch)

    await _request_full_trip()
    trip_id = await bot._get_active_trip_id(_PHONE)

    await bot.handle_incoming_message(_PHONE, None, None, "cancelar")
    reply = await bot.handle_incoming_message(_PHONE, None, None, "mejor no")

    assert reply == bot._ALREADY_ACTIVE
    assert (await _fetch_trip(trip_id)).status == TripStatus.SOLICITADO
    assert await bot._get_active_trip_id(_PHONE) == trip_id

    await _cleanup_trip(trip_id)


async def test_cancelar_without_active_trip_says_so():
    reply = await bot.handle_incoming_message(_PHONE, None, None, "cancelar")
    assert reply == bot._NOTHING_TO_CANCEL


async def test_chofer_sin_asignar_no_abre_chat(monkeypatch):
    """Pedir *chofer* mientras el viaje sigue 'solicitado' no debe
    inventar un destinatario — todavía no hay a quién escribirle."""
    monkeypatch.setattr(bot, "dispatch_trip", _noop_dispatch)
    _pin_demand(monkeypatch)

    await _request_full_trip()
    trip_id = await bot._get_active_trip_id(_PHONE)

    reply = await bot.handle_incoming_message(_PHONE, None, None, "chofer")
    assert reply == bot.CHAT_NO_DRIVER
    state = await bot._get_state(_PHONE)
    assert state.get("stage") == "active"

    await _cleanup_trip(trip_id)


async def test_chofer_abre_el_chat_sin_reenviar_la_palabra(monkeypatch):
    monkeypatch.setattr(bot, "dispatch_trip", _noop_dispatch)
    _pin_demand(monkeypatch)

    await _request_full_trip()
    trip_id = await bot._get_active_trip_id(_PHONE)
    async with SessionLocal() as db:
        trip = await db.get(Trip, trip_id)
        trip.status = TripStatus.ASIGNADO
        await db.commit()

    relayed = []

    async def _fake_relay(trip_id, body, *, first=False):
        relayed.append(body)
        return "no-deberia-verse"

    monkeypatch.setattr(bot, "relay_customer_message", _fake_relay)

    reply = await bot.handle_incoming_message(_PHONE, None, None, "chofer")
    assert reply == bot.CHAT_OPEN
    assert relayed == []
    state = await bot._get_state(_PHONE)
    assert state["stage"] == "chat"

    await _cleanup_trip(trip_id)


async def test_frase_de_recogida_abre_y_reenvia(monkeypatch):
    monkeypatch.setattr(bot, "dispatch_trip", _noop_dispatch)
    _pin_demand(monkeypatch)

    await _request_full_trip()
    trip_id = await bot._get_active_trip_id(_PHONE)
    async with SessionLocal() as db:
        trip = await db.get(Trip, trip_id)
        trip.status = TripStatus.ASIGNADO
        await db.commit()

    relayed = []

    async def _fake_relay(got_id, body, *, first=False):
        relayed.append((got_id, body, first))
        return bot.CHAT_SENT_FIRST

    monkeypatch.setattr(bot, "relay_customer_message", _fake_relay)

    reply = await bot.handle_incoming_message(
        _PHONE, None, None, "estoy en la esquina de Reforma"
    )
    assert reply == bot.CHAT_SENT_FIRST
    assert relayed == [(trip_id, "estoy en la esquina de Reforma", True)]
    assert (await bot._get_state(_PHONE))["stage"] == "chat"

    await _cleanup_trip(trip_id)


async def test_ok_con_viaje_asignado_no_se_reenvia(monkeypatch):
    """Un 'ok' no es intención de hablar con el chofer — se le recuerda
    que puede escribirle, sin spamear al conductor."""
    monkeypatch.setattr(bot, "dispatch_trip", _noop_dispatch)
    _pin_demand(monkeypatch)

    await _request_full_trip()
    trip_id = await bot._get_active_trip_id(_PHONE)
    async with SessionLocal() as db:
        trip = await db.get(Trip, trip_id)
        trip.status = TripStatus.ASIGNADO
        await db.commit()

    relayed = []

    async def _fake_relay(*args, **kwargs):
        relayed.append(args)
        return "no"

    monkeypatch.setattr(bot, "relay_customer_message", _fake_relay)

    reply = await bot.handle_incoming_message(_PHONE, None, None, "ok")
    assert reply == bot._ALREADY_ASSIGNED
    assert relayed == []

    await _cleanup_trip(trip_id)


async def test_en_chat_reenvia_y_listo_cierra_sin_cancelar(monkeypatch):
    monkeypatch.setattr(bot, "dispatch_trip", _noop_dispatch)
    _pin_demand(monkeypatch)

    await _request_full_trip()
    trip_id = await bot._get_active_trip_id(_PHONE)
    async with SessionLocal() as db:
        trip = await db.get(Trip, trip_id)
        trip.status = TripStatus.ASIGNADO
        await db.commit()
    await bot._set_chat_trip(_PHONE, trip_id)

    relayed = []

    async def _fake_relay(got_id, body, *, first=False):
        relayed.append(body)
        return bot.CHAT_SENT

    monkeypatch.setattr(bot, "relay_customer_message", _fake_relay)

    reply = await bot.handle_incoming_message(_PHONE, None, None, "voy con blusa roja")
    assert reply == bot.CHAT_SENT
    assert relayed == ["voy con blusa roja"]

    closed = await bot.handle_incoming_message(_PHONE, None, None, "listo")
    assert closed == bot.CHAT_CLOSED
    assert (await bot._get_state(_PHONE))["stage"] == "active"
    assert (await _fetch_trip(trip_id)).status == TripStatus.ASIGNADO

    await _cleanup_trip(trip_id)


async def test_cancelar_desde_el_chat_sigue_pidiendo_confirmacion(monkeypatch):
    monkeypatch.setattr(bot, "dispatch_trip", _noop_dispatch)
    _pin_demand(monkeypatch)

    await _request_full_trip()
    trip_id = await bot._get_active_trip_id(_PHONE)
    async with SessionLocal() as db:
        trip = await db.get(Trip, trip_id)
        trip.status = TripStatus.ASIGNADO
        await db.commit()
    await bot._set_chat_trip(_PHONE, trip_id)

    ask = await bot.handle_incoming_message(_PHONE, None, None, "cancelar")
    assert "seguro" in ask.lower()
    assert (await _fetch_trip(trip_id)).status == TripStatus.ASIGNADO

    reply = await bot.handle_incoming_message(_PHONE, None, None, "sí")
    assert reply == bot._CANCELLED
    assert (await _fetch_trip(trip_id)).status == TripStatus.CANCELADO

    await _cleanup_trip(trip_id)


async def test_normal_text_is_not_confused_with_cancelar(monkeypatch):
    """Solo la palabra sola cancela — un mensaje que la mencione de pasada
    no debe tumbar el viaje de alguien que está esperando su taxi."""
    monkeypatch.setattr(bot, "dispatch_trip", _noop_dispatch)
    _pin_demand(monkeypatch)

    await _request_full_trip()
    trip_id = await bot._get_active_trip_id(_PHONE)

    reply = await bot.handle_incoming_message(
        _PHONE, None, None, "no quiero cancelar, solo pregunto"
    )
    assert reply == bot._ALREADY_ACTIVE

    trip = await _fetch_trip(trip_id)
    assert trip.status == TripStatus.SOLICITADO

    await _cleanup_trip(trip_id)


# --- Calificación ---------------------------------------------------------------


async def test_calificacion_se_guarda_tras_completar(monkeypatch):
    monkeypatch.setattr(bot, "dispatch_trip", _noop_dispatch)
    _pin_demand(monkeypatch)

    await _request_full_trip()
    trip_id = await bot._get_active_trip_id(_PHONE)
    async with SessionLocal() as db:
        trip = await db.get(Trip, trip_id)
        trip.status = TripStatus.COMPLETADO
        await db.commit()
        await bot.prompt_rating(trip)

    reply = await bot.handle_incoming_message(_PHONE, None, None, "5")
    assert reply == bot._RATING_THANKS
    assert (await _fetch_trip(trip_id)).rating == 5

    await _cleanup_trip(trip_id)


async def test_calificacion_fuera_de_rango_se_rechaza(monkeypatch):
    monkeypatch.setattr(bot, "dispatch_trip", _noop_dispatch)
    _pin_demand(monkeypatch)

    await _request_full_trip()
    trip_id = await bot._get_active_trip_id(_PHONE)
    async with SessionLocal() as db:
        trip = await db.get(Trip, trip_id)
        trip.status = TripStatus.COMPLETADO
        await db.commit()
        await bot.prompt_rating(trip)

    reply = await bot.handle_incoming_message(_PHONE, None, None, "9")
    assert reply == bot._RATING_INVALID
    assert (await _fetch_trip(trip_id)).rating is None

    # Y una ubicación nueva la descarta sin drama: el cliente pasó a otra cosa.
    reply = await bot.handle_incoming_message(_PHONE, 19.4326, -99.1332)
    assert "destino" in reply

    await _cleanup_trip(trip_id)


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

    # notify_customer y no send_whatsapp_message: desde que hay más de un canal,
    # el barrido no sabe (ni debe saber) por dónde le contesta al cliente — eso
    # lo decide app.core.customer_notify a partir del viaje.
    async def _fake_notify(trip, body):
        sent.append((trip.customer_phone, body))

    monkeypatch.setattr(bot, "notify_customer", _fake_notify)

    _pin_demand(monkeypatch)

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
    # `in` y no igualdad: el barrido recorre TODOS los viajes de la tabla, y
    # otras pruebas de este archivo dejan los suyos (escriben con SessionLocal,
    # fuera del SAVEPOINT). Que además cancele esos es correcto — ya vencieron
    # — pero no es lo que esta prueba afirma.
    assert (_PHONE, bot._GAVE_UP) in sent
    assert await bot._get_active_trip_id(_PHONE) is None


async def test_sweep_aguanta_mas_en_alta_demanda(monkeypatch):
    """El mismo viaje que se cancelaría en operación normal sobrevive con la
    calle saturada: ahí sí va a haber taxi, solo que tarda, y rendirse al tope
    corto tiraría un servicio que se habría podido dar."""
    sent = []

    async def _fake_notify(trip, body):
        sent.append((trip.customer_phone, body))

    monkeypatch.setattr(bot, "notify_customer", _fake_notify)
    monkeypatch.setattr(bot, "dispatch_trip", _noop_dispatch)
    _pin_demand(monkeypatch, high=True)

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
    assert trip.status == TripStatus.SOLICITADO
    assert sent == []

    await bot._clear_active_trip(_PHONE)
    await _cleanup_trip(trip_id)


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
