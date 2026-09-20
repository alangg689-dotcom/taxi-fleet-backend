"""Hilo cliente↔chofer: intención, persistencia, API del chofer y techos.

La conversación del bot (cuándo abrir el chat, TwiML) vive en
test_whatsapp_bot.py. Aquí se prueba lo que ve la app del chofer y las
reglas de seguridad que el bot también usa (app.core.trip_chat).
"""

from app.api.location import _point
from app.config import settings
from app.core import trip_chat
from app.models import CustomerChannel, Trip, TripMessageSender, TripStatus, UserRole, VehicleStatus
from tests.factories import auth_headers, make_driver, make_staff_user, make_vehicle


async def _assigned_whatsapp_trip(
    db, driver, vehicle, *, phone="+525512349900", status=TripStatus.ASIGNADO
) -> Trip:
    trip = Trip(
        origin=_point(19.4326, -99.1332),
        status=status,
        driver_id=driver.id,
        vehicle_id=vehicle.id,
        customer_phone=phone,
        customer_channel=CustomerChannel.WHATSAPP.value,
    )
    db.add(trip)
    await db.flush()
    return trip


# --- Intención (función pura) ------------------------------------------------


def test_wants_driver_chat_detects_comando_y_frases():
    assert trip_chat.wants_driver_chat("chofer") is True
    assert trip_chat.wants_driver_chat("Quiero hablarle al conductor") is True
    assert trip_chat.wants_driver_chat("¿Dónde estás?") is True
    assert trip_chat.wants_driver_chat("estoy en la esquina de Reforma") is True
    assert trip_chat.wants_driver_chat("te espero afuera") is True


def test_wants_driver_chat_ignora_ok_y_ruido():
    assert trip_chat.wants_driver_chat("ok") is False
    assert trip_chat.wants_driver_chat("sí") is False
    assert trip_chat.wants_driver_chat("gracias") is False
    assert trip_chat.wants_driver_chat("") is False


def test_open_command_only_y_cierre():
    assert trip_chat.is_chat_open_command_only("Chofer") is True
    assert trip_chat.is_chat_open_command_only("estoy en la esquina") is False
    assert trip_chat.is_chat_close("listo") is True
    assert trip_chat.is_chat_close("salir") is True
    assert trip_chat.is_chat_close("estoy listo en la puerta") is False


# --- API del chofer ----------------------------------------------------------


async def test_driver_lists_empty_thread_and_can_reply(client, db_session):
    vehicle = await make_vehicle(db_session, status=VehicleStatus.OCUPADO)
    driver, token = await make_driver(db_session)
    trip = await _assigned_whatsapp_trip(db_session, driver, vehicle)

    response = await client.get(
        f"/api/v1/trips/{trip.id}/messages", headers=auth_headers(token)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["trip_id"] == str(trip.id)
    assert body["trip_status"] == "asignado"
    assert body["can_reply"] is True
    assert body["messages"] == []
    assert response.headers["X-Total-Count"] == "0"


async def test_happy_path_customer_then_driver_reply(client, db_session, monkeypatch):
    """El pasajero deja un mensaje, el chofer lo lee y responde; la
    respuesta sale hacia el cliente (notify_customer), no por el socket."""
    published = []
    sent_to_customer = []

    async def _fake_publish(driver_id, payload):
        published.append((driver_id, payload))

    async def _fake_notify(trip, body):
        sent_to_customer.append(body)

    monkeypatch.setattr(trip_chat, "publish_trip_chat", _fake_publish)
    monkeypatch.setattr(trip_chat, "notify_customer", _fake_notify)

    vehicle = await make_vehicle(db_session, status=VehicleStatus.OCUPADO)
    driver, token = await make_driver(db_session)
    trip = await _assigned_whatsapp_trip(db_session, driver, vehicle)

    inbound = await trip_chat.post_message(
        db_session, trip, TripMessageSender.CUSTOMER, "Estoy en la esquina"
    )
    await trip_chat.notify_driver_of_message(trip, inbound, push_token=None)

    assert published and published[0][0] == str(driver.id)
    assert published[0][1]["body"] == "Estoy en la esquina"
    assert published[0][1]["sender"] == "customer"

    listed = await client.get(
        f"/api/v1/trips/{trip.id}/messages", headers=auth_headers(token)
    )
    assert listed.status_code == 200
    assert listed.json()["messages"][0]["body"] == "Estoy en la esquina"
    assert listed.json()["messages"][0]["sender"] == "customer"

    reply = await client.post(
        f"/api/v1/trips/{trip.id}/messages",
        json={"body": "Ya voy, 2 minutos"},
        headers=auth_headers(token),
    )
    assert reply.status_code == 201
    assert reply.json()["sender"] == "driver"
    assert reply.json()["body"] == "Ya voy, 2 minutos"
    assert sent_to_customer == [
        f"{trip_chat.DRIVER_REPLY_PREFIX}Ya voy, 2 minutos"
    ]

    listed_again = await client.get(
        f"/api/v1/trips/{trip.id}/messages", headers=auth_headers(token)
    )
    assert listed_again.headers["X-Total-Count"] == "2"
    senders = [m["sender"] for m in listed_again.json()["messages"]]
    assert senders == ["customer", "driver"]


async def test_other_driver_cannot_read_or_reply(client, db_session):
    vehicle = await make_vehicle(db_session, status=VehicleStatus.OCUPADO)
    owner, _ = await make_driver(db_session)
    _, other_token = await make_driver(db_session)
    trip = await _assigned_whatsapp_trip(db_session, owner, vehicle)

    listed = await client.get(
        f"/api/v1/trips/{trip.id}/messages", headers=auth_headers(other_token)
    )
    assert listed.status_code == 403

    posted = await client.post(
        f"/api/v1/trips/{trip.id}/messages",
        json={"body": "hola"},
        headers=auth_headers(other_token),
    )
    assert posted.status_code == 403


async def test_staff_can_read_but_not_reply(client, db_session):
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    vehicle = await make_vehicle(db_session, status=VehicleStatus.OCUPADO)
    driver, _ = await make_driver(db_session)
    trip = await _assigned_whatsapp_trip(db_session, driver, vehicle)

    listed = await client.get(
        f"/api/v1/trips/{trip.id}/messages", headers=auth_headers(operator_token)
    )
    assert listed.status_code == 200
    assert listed.json()["can_reply"] is False

    posted = await client.post(
        f"/api/v1/trips/{trip.id}/messages",
        json={"body": "desde la base"},
        headers=auth_headers(operator_token),
    )
    assert posted.status_code == 403


async def test_cannot_chat_before_assignment(client, db_session):
    vehicle = await make_vehicle(db_session, status=VehicleStatus.DISPONIBLE)
    driver, token = await make_driver(db_session)
    trip = Trip(
        origin=_point(19.4326, -99.1332),
        status=TripStatus.SOLICITADO,
        offered_driver_id=driver.id,
        offered_vehicle_id=vehicle.id,
        customer_phone="+525512349901",
        customer_channel=CustomerChannel.WHATSAPP.value,
    )
    db_session.add(trip)
    await db_session.flush()

    listed = await client.get(
        f"/api/v1/trips/{trip.id}/messages", headers=auth_headers(token)
    )
    assert listed.status_code == 200
    assert listed.json()["can_reply"] is False

    posted = await client.post(
        f"/api/v1/trips/{trip.id}/messages",
        json={"body": "voy"},
        headers=auth_headers(token),
    )
    assert posted.status_code == 409


async def test_cannot_chat_after_complete(client, db_session):
    vehicle = await make_vehicle(db_session, status=VehicleStatus.DISPONIBLE)
    driver, token = await make_driver(db_session)
    trip = await _assigned_whatsapp_trip(
        db_session, driver, vehicle, status=TripStatus.COMPLETADO
    )

    posted = await client.post(
        f"/api/v1/trips/{trip.id}/messages",
        json={"body": "gracias"},
        headers=auth_headers(token),
    )
    assert posted.status_code == 409


async def test_cannot_reply_without_customer(client, db_session):
    """Viaje de operador: no hay a quién escribirle por WhatsApp."""
    vehicle = await make_vehicle(db_session, status=VehicleStatus.OCUPADO)
    driver, token = await make_driver(db_session)
    trip = Trip(
        origin=_point(19.4326, -99.1332),
        status=TripStatus.ASIGNADO,
        driver_id=driver.id,
        vehicle_id=vehicle.id,
    )
    db_session.add(trip)
    await db_session.flush()

    posted = await client.post(
        f"/api/v1/trips/{trip.id}/messages",
        json={"body": "hola"},
        headers=auth_headers(token),
    )
    assert posted.status_code == 409
    assert "cliente" in posted.json()["detail"]


async def test_empty_body_is_422(client, db_session):
    vehicle = await make_vehicle(db_session, status=VehicleStatus.OCUPADO)
    driver, token = await make_driver(db_session)
    trip = await _assigned_whatsapp_trip(db_session, driver, vehicle)

    posted = await client.post(
        f"/api/v1/trips/{trip.id}/messages",
        json={"body": "   "},
        headers=auth_headers(token),
    )
    assert posted.status_code == 422


async def test_chat_rate_limit_returns_429(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "CHAT_MAX_PER_WINDOW", 2)
    monkeypatch.setattr(settings, "CHAT_WINDOW_SECONDS", 60)

    vehicle = await make_vehicle(db_session, status=VehicleStatus.OCUPADO)
    driver, token = await make_driver(db_session)
    trip = await _assigned_whatsapp_trip(db_session, driver, vehicle)

    async def _noop_notify(trip, body):
        return None

    monkeypatch.setattr("app.api.trips.notify_customer_of_reply", _noop_notify)

    first = await client.post(
        f"/api/v1/trips/{trip.id}/messages",
        json={"body": "uno"},
        headers=auth_headers(token),
    )
    second = await client.post(
        f"/api/v1/trips/{trip.id}/messages",
        json={"body": "dos"},
        headers=auth_headers(token),
    )
    third = await client.post(
        f"/api/v1/trips/{trip.id}/messages",
        json={"body": "tres"},
        headers=auth_headers(token),
    )
    assert first.status_code == 201
    assert second.status_code == 201
    assert third.status_code == 429

    await trip_chat.reset_rate_limit(trip.id, TripMessageSender.DRIVER.value)


async def test_en_curso_still_allows_chat(client, db_session, monkeypatch):
    """In-trip es menor prioridad de producto, pero el hilo no se cierra
    al recoger: el cliente puede seguir coordinando."""

    async def _noop_notify(trip, body):
        return None

    monkeypatch.setattr("app.api.trips.notify_customer_of_reply", _noop_notify)

    vehicle = await make_vehicle(db_session, status=VehicleStatus.OCUPADO)
    driver, token = await make_driver(db_session)
    trip = await _assigned_whatsapp_trip(
        db_session, driver, vehicle, status=TripStatus.EN_CURSO
    )

    posted = await client.post(
        f"/api/v1/trips/{trip.id}/messages",
        json={"body": "¿Bajamos en la siguiente?"},
        headers=auth_headers(token),
    )
    assert posted.status_code == 201
