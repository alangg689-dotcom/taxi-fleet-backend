"""Puerta de entrada de los bots de cliente (POST /bot/request-ride).

El despacho en sí ya lo cubre test_dispatch; aquí interesa lo propio de esta
puerta: que no quede abierta, que no duplique viajes del mismo cliente, y que
guarde la identidad en la columna correcta según el canal.
"""

import uuid

import pytest
from sqlalchemy import select

from app.api import bot as bot_api
from app.config import settings
from app.models import CustomerChannel, Trip, TripStatus

pytestmark = pytest.mark.asyncio

_CHAT_ID = "987654321"
_KEY = "clave-de-prueba"


@pytest.fixture(autouse=True)
def _bot_key(monkeypatch):
    monkeypatch.setattr(settings, "BOT_API_KEY", _KEY)


@pytest.fixture(autouse=True)
def _no_real_dispatch(monkeypatch):
    """El endpoint lanza dispatch_trip con create_task; sin esto la cascada de
    candidatos correría de verdad durante la prueba."""

    async def _noop(trip_id: uuid.UUID) -> None:
        return None

    monkeypatch.setattr(bot_api, "dispatch_trip", _noop)


def _payload(**overrides) -> dict:
    return {
        "channel": "telegram",
        "customer_id": _CHAT_ID,
        "lat": 19.4326,
        "lng": -99.1332,
        **overrides,
    }


async def test_sin_clave_no_se_puede_pedir_viaje(client):
    """Este endpoint crea viajes reales sin sesión de nadie: abierto, cualquiera
    llenaría la flotilla de servicios fantasma."""
    response = await client.post("/api/v1/bot/request-ride", json=_payload())
    assert response.status_code == 422  # falta el header X-Bot-Key


async def test_con_clave_incorrecta_responde_401(client):
    response = await client.post(
        "/api/v1/bot/request-ride", json=_payload(), headers={"X-Bot-Key": "otra"}
    )
    assert response.status_code == 401


async def test_sin_bot_api_key_configurada_el_canal_esta_apagado(client, monkeypatch):
    """Apagado es un estado seguro; abierto no. Con la clave vacía el endpoint
    no debe aceptar cualquier cosa."""
    monkeypatch.setattr(settings, "BOT_API_KEY", "")
    response = await client.post(
        "/api/v1/bot/request-ride", json=_payload(), headers={"X-Bot-Key": ""}
    )
    assert response.status_code == 503


async def test_la_respuesta_trae_el_mensaje_de_espera(client):
    """El bot no redacta la espera: se la da el backend, que es quien sabe si
    la calle está saturada. Sin este campo el cliente de Telegram recibiría un
    "en breve" fijo aunque el sistema ya sepa que va a tardar."""
    response = await client.post(
        "/api/v1/bot/request-ride", json=_payload(), headers={"X-Bot-Key": _KEY}
    )

    assert response.status_code == 202
    body = response.json()
    assert body["high_demand"] in (True, False)
    assert body["wait_message"]


async def test_cancelar_desde_la_base_le_avisa_al_cliente(client, db_session, monkeypatch):
    """Antes el viaje moría en la tabla sin decir nada y el cliente se quedaba
    parado en la calle esperando un taxi que ya no iba; el último mensaje que
    tenía seguía diciendo que venía uno en camino."""
    from app.api import trips as trips_api
    from tests.factories import auth_headers, make_staff_user
    from app.models import UserRole

    avisos = []

    async def _fake_notify(trip, body):
        avisos.append((trip.customer_chat_id, body))

    monkeypatch.setattr(trips_api, "notify_customer", _fake_notify)

    created = await client.post(
        "/api/v1/bot/request-ride", json=_payload(), headers={"X-Bot-Key": _KEY}
    )
    trip_id = created.json()["trip_id"]

    _, token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    response = await client.post(
        f"/api/v1/trips/{trip_id}/cancel", headers=auth_headers(token)
    )

    assert response.status_code == 200
    assert response.json()["status"] == TripStatus.CANCELADO.value
    assert len(avisos) == 1
    chat_id, body = avisos[0]
    assert chat_id == _CHAT_ID
    assert "cancelado" in body.lower()


async def test_el_bucle_de_cancelar_y_repedir_topa_en_429(client, monkeypatch):
    """Lo que se limita es la CREACIÓN de viajes. Un script que cancela y
    vuelve a pedir en bucle llenaría la flotilla de servicios fantasma; a la
    tercera creación dentro de la ventana, 429."""
    monkeypatch.setattr(settings, "BOT_RIDE_MAX_PER_WINDOW", 2)
    customer = f"rl-{uuid.uuid4().hex[:10]}"

    async def _pedir():
        return await client.post(
            "/api/v1/bot/request-ride",
            json=_payload(customer_id=customer),
            headers={"X-Bot-Key": _KEY},
        )

    async def _cancelar():
        await client.post(
            "/api/v1/bot/cancel-ride",
            json={"channel": "telegram", "customer_id": customer},
            headers={"X-Bot-Key": _KEY},
        )

    assert (await _pedir()).status_code == 202
    await _cancelar()
    assert (await _pedir()).status_code == 202
    await _cancelar()
    assert (await _pedir()).status_code == 429


async def test_reenviar_la_ubicacion_esperando_no_gasta_el_limite(client, monkeypatch):
    """Alguien parado en la calle reenvía su ubicación varias veces mientras
    espera. Esas peticiones devuelven already_active y NO deben gastarle el
    presupuesto: cobrárselas lo dejaría con un "demasiadas solicitudes" sin
    haber hecho nada malo."""
    monkeypatch.setattr(settings, "BOT_RIDE_MAX_PER_WINDOW", 1)
    customer = f"rl-{uuid.uuid4().hex[:10]}"

    async def _pedir():
        return await client.post(
            "/api/v1/bot/request-ride",
            json=_payload(customer_id=customer),
            headers={"X-Bot-Key": _KEY},
        )

    assert (await _pedir()).status_code == 202
    for _ in range(5):
        again = await _pedir()
        assert again.status_code == 202
        assert again.json()["already_active"] is True


async def test_cancelar_con_chofer_por_llegar_exige_force(client, db_session, monkeypatch):
    """Con el chofer a <2 min del cliente, la cancelación de staff se frena
    con un 409 DRIVER_ARRIVING para que el dashboard pregunte otra vez; con
    force=true sí procede."""
    from datetime import UTC, datetime

    from app.api import trips as trips_api
    from app.models import TripStatus as TS, UserRole, VehicleStatus
    from tests.factories import (
        auth_headers,
        make_driver,
        make_location_ping,
        make_open_assignment,
        make_staff_user,
        make_stand,
        make_vehicle,
    )

    monkeypatch.setattr(trips_api, "notify_customer", _noop_notify)

    stand = await make_stand(db_session)
    vehicle = await make_vehicle(db_session, status=VehicleStatus.OCUPADO, stand_id=stand.id)
    driver, _ = await make_driver(db_session)
    await make_open_assignment(db_session, vehicle_id=vehicle.id, driver_id=driver.id)

    created = await client.post(
        "/api/v1/bot/request-ride", json=_payload(), headers={"X-Bot-Key": _KEY}
    )
    trip_id = created.json()["trip_id"]

    # Se asigna a mano y se planta la unidad casi encima del punto de
    # recogida: a 25 km/h, ~200 m son ~29 segundos de ETA.
    from sqlalchemy import text as sa_text

    await db_session.execute(
        sa_text(
            "UPDATE trips SET status='asignado', vehicle_id=:v, driver_id=:d WHERE id=:t"
        ),
        {"v": str(vehicle.id), "d": str(driver.id), "t": trip_id},
    )
    await db_session.flush()

    async def _fake_last_position(vehicle_id: str):
        return {"lat": 19.4326 + 0.0018, "lng": -99.1332}

    monkeypatch.setattr(trips_api, "get_last_position", _fake_last_position)

    _, token = await make_staff_user(db_session, role=UserRole.OPERATOR)

    blocked = await client.post(
        f"/api/v1/trips/{trip_id}/cancel", headers=auth_headers(token)
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"].startswith("DRIVER_ARRIVING:")

    forced = await client.post(
        f"/api/v1/trips/{trip_id}/cancel?force=true", headers=auth_headers(token)
    )
    assert forced.status_code == 200
    assert forced.json()["status"] == TS.CANCELADO.value


async def _noop_notify(trip, body):
    return None


async def test_crea_el_viaje_con_la_identidad_de_telegram(client, db_session):
    response = await client.post(
        "/api/v1/bot/request-ride", json=_payload(), headers={"X-Bot-Key": _KEY}
    )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == TripStatus.SOLICITADO.value
    assert body["already_active"] is False

    trip = await db_session.get(Trip, uuid.UUID(body["trip_id"]))
    assert trip is not None
    assert trip.customer_channel == CustomerChannel.TELEGRAM.value
    assert trip.customer_chat_id == _CHAT_ID
    # El chat_id no es un teléfono aunque los dos sean dígitos: mezclarlos haría
    # que la operadora intentara marcarle a un número que no existe.
    assert trip.customer_phone is None


async def test_whatsapp_guarda_el_telefono_no_el_chat_id(client, db_session):
    response = await client.post(
        "/api/v1/bot/request-ride",
        json=_payload(channel="whatsapp", customer_id="+525512345678"),
        headers={"X-Bot-Key": _KEY},
    )
    assert response.status_code == 202

    trip = await db_session.get(Trip, uuid.UUID(response.json()["trip_id"]))
    assert trip.customer_phone == "+525512345678"
    assert trip.customer_chat_id is None


async def test_pedir_dos_veces_devuelve_el_mismo_viaje(client, db_session):
    """Pedir taxi dos veces seguidas es lo que hace alguien impaciente parado en
    la calle, no un cliente mal portado: se le devuelve el viaje que ya tiene en
    vez de abrirle otro que ocuparía una segunda unidad."""
    first = await client.post(
        "/api/v1/bot/request-ride", json=_payload(), headers={"X-Bot-Key": _KEY}
    )
    second = await client.post(
        "/api/v1/bot/request-ride", json=_payload(), headers={"X-Bot-Key": _KEY}
    )

    assert second.status_code == 202
    assert second.json()["already_active"] is True
    assert second.json()["trip_id"] == first.json()["trip_id"]

    result = await db_session.execute(
        select(Trip).where(Trip.customer_chat_id == _CHAT_ID)
    )
    assert len(result.scalars().all()) == 1


async def test_cancelar_libera_el_viaje_activo(client, db_session):
    created = await client.post(
        "/api/v1/bot/request-ride", json=_payload(), headers={"X-Bot-Key": _KEY}
    )
    trip_id = created.json()["trip_id"]

    response = await client.post(
        "/api/v1/bot/cancel-ride",
        json={"channel": "telegram", "customer_id": _CHAT_ID},
        headers={"X-Bot-Key": _KEY},
    )
    assert response.status_code == 200
    assert response.json() == {"cancelled": True, "trip_id": trip_id}

    trip = await db_session.get(Trip, uuid.UUID(trip_id))
    await db_session.refresh(trip)
    assert trip.status == TripStatus.CANCELADO


async def test_cancelar_sin_viaje_activo_no_es_un_error(client):
    response = await client.post(
        "/api/v1/bot/cancel-ride",
        json={"channel": "telegram", "customer_id": "000"},
        headers={"X-Bot-Key": _KEY},
    )
    assert response.status_code == 200
    assert response.json()["cancelled"] is False
