"""Pruebas de los WebSockets en tiempo real.

Usa httpx-ws (`aconnect_ws`) en vez de `starlette.testclient.TestClient`: este
último corre la app en un hilo aparte con su propio event loop, y la sesión de
prueba (ligada al loop del test vía SAVEPOINT) reventaría con "Future attached
to a different loop" si algo la tocara desde ahí. httpx-ws, en cambio, viaja
sobre `ws_client_factory` (transporte ASGI de httpx-ws, que sí entiende el
scope "websocket" — el `ASGITransport` normal de httpx solo entiende "http" y
devuelve 404 ante cualquier upgrade): todo corre en el loop del test, igual
que las pruebas HTTP normales. Cada prueba abre su propio `async with
ws_client_factory() as ws_client:` en vez de recibir un cliente ya entrado por
un fixture — ver el docstring de `ws_client_factory` en conftest.py.
"""

import asyncio
import contextlib
from datetime import UTC, datetime

import pytest
from httpx_ws import WebSocketDisconnect, aconnect_ws
from sqlalchemy import select

from app.models import LocationPing, UserRole
from app.ws import fleet as fleet_module
from tests.factories import make_driver, make_staff_user, make_vehicle


def _ping(**overrides) -> dict:
    payload = {
        "lat": 19.4326,
        "lng": -99.1332,
        "speed": 35.5,
        "heading": 180.0,
        "accuracy": 5.0,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    payload.update(overrides)
    return payload


# --- /ws/fleet -----------------------------------------------------------------


async def test_fleet_socket_rejects_missing_token(ws_client_factory, db_session):
    async with ws_client_factory() as ws_client:
        with pytest.raises(WebSocketDisconnect):
            async with aconnect_ws("/ws/fleet", ws_client):
                pass


async def test_fleet_socket_rejects_driver_role(ws_client_factory, db_session):
    """El dashboard es solo para operador/admin; un chofer no debe poder abrirlo."""
    _, driver_token = await make_driver(db_session)

    async with ws_client_factory() as ws_client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            async with aconnect_ws(f"/ws/fleet?token={driver_token}", ws_client):
                pass
        assert exc_info.value.code == 1008


async def test_fleet_socket_rejects_garbage_token(ws_client_factory, db_session):
    async with ws_client_factory() as ws_client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            async with aconnect_ws("/ws/fleet?token=esto-no-es-un-jwt", ws_client):
                pass
        assert exc_info.value.code == 1008


async def test_fleet_socket_sends_initial_snapshot(ws_client_factory, db_session):
    _, admin_token = await make_staff_user(db_session, role=UserRole.ADMIN)

    async with ws_client_factory() as ws_client:
        async with aconnect_ws(f"/ws/fleet?token={admin_token}", ws_client) as ws:
            message = await ws.receive_json()
            assert message["type"] == "snapshot"
            assert isinstance(message["data"], list)


# --- /ws/driver ------------------------------------------------------------


async def test_driver_socket_rejects_unknown_device_key(ws_client_factory, db_session):
    async with ws_client_factory() as ws_client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            async with aconnect_ws("/ws/driver?device_key=llave-inventada", ws_client):
                pass
        assert exc_info.value.code == 1008


async def test_driver_socket_sends_connected_with_vehicle_id_and_status(
    ws_client_factory, db_session
):
    """La app no tiene otra forma de saber su propio vehicle_id (device_key no
    es un JWT) — lo necesita para POST /vehicles/{id}/status (corte de calle)."""
    vehicle, device_key = await make_vehicle(db_session, with_device_key=True)

    async with ws_client_factory() as ws_client:
        async with aconnect_ws(f"/ws/driver?device_key={device_key}", ws_client) as ws:
            connected = await ws.receive_json()
            assert connected == {
                "type": "connected",
                "vehicle_id": str(vehicle.id),
                "vehicle_status": "disponible",
            }


async def test_driver_socket_accepts_valid_device_key_and_persists_ping(
    ws_client_factory, db_session
):
    vehicle, device_key = await make_vehicle(db_session, with_device_key=True)

    async with ws_client_factory() as ws_client:
        async with aconnect_ws(f"/ws/driver?device_key={device_key}", ws_client) as ws:
            await ws.receive_json()  # connected
            await ws.send_json(_ping())
            ack = await ws.receive_json()
            assert ack == {"type": "ack", "accepted": 1, "received": 1}

    result = await db_session.execute(
        select(LocationPing).where(LocationPing.vehicle_id == vehicle.id)
    )
    assert len(result.scalars().all()) == 1


async def test_driver_socket_accepts_batch_and_dedupes_by_timestamp(
    ws_client_factory, db_session
):
    """Mismo (vehicle_id, timestamp) dos veces: el segundo debe descartarse,
    igual que en el endpoint REST — el buffer offline reenvía si no ve el ACK."""
    vehicle, device_key = await make_vehicle(db_session, with_device_key=True)
    shared_ts = datetime.now(UTC).isoformat()

    async with ws_client_factory() as ws_client:
        async with aconnect_ws(f"/ws/driver?device_key={device_key}", ws_client) as ws:
            await ws.receive_json()  # connected
            await ws.send_json([_ping(timestamp=shared_ts), _ping(timestamp=shared_ts)])
            ack = await ws.receive_json()
            assert ack == {"type": "ack", "accepted": 1, "received": 2}


async def test_driver_socket_reports_malformed_ping_without_closing(
    ws_client_factory, db_session
):
    _, device_key = await make_vehicle(db_session, with_device_key=True)

    async with ws_client_factory() as ws_client:
        async with aconnect_ws(f"/ws/driver?device_key={device_key}", ws_client) as ws:
            await ws.receive_json()  # connected
            await ws.send_json({"lat": "no-es-un-numero"})
            error = await ws.receive_json()
            assert error["type"] == "error"

            # La conexión sigue viva: un ping válido después del error se procesa.
            await ws.send_json(_ping())
            ack = await ws.receive_json()
            assert ack["type"] == "ack"


# --- Puente Redis Pub/Sub entre ambos sockets -------------------------------


async def test_driver_ping_broadcasts_to_fleet_dashboard(ws_client_factory, db_session):
    """El camino completo: chofer -> Redis PUBLISH -> listener -> dashboard.

    `redis_listener` normalmente lo arranca el lifespan de la app (main.py) y
    vive mientras el proceso esté arriba; ASGITransport no dispara ese
    lifespan, así que aquí se levanta a mano, igual de efímero que una prueba.
    """
    vehicle, device_key = await make_vehicle(db_session, with_device_key=True)
    _, admin_token = await make_staff_user(db_session, role=UserRole.ADMIN)

    listener_task = asyncio.create_task(fleet_module.redis_listener())
    await asyncio.sleep(0.1)  # deja que el subscribe() al canal se complete

    try:
        async with ws_client_factory() as ws_client:
            async with aconnect_ws(f"/ws/fleet?token={admin_token}", ws_client) as fleet_ws:
                snapshot = await fleet_ws.receive_json()
                assert snapshot["type"] == "snapshot"

                async with aconnect_ws(
                    f"/ws/driver?device_key={device_key}", ws_client
                ) as driver_ws:
                    await driver_ws.receive_json()  # connected
                    await driver_ws.send_json(_ping())
                    await driver_ws.receive_json()  # ack

                update = await asyncio.wait_for(fleet_ws.receive_json(), timeout=2)
                assert update["type"] == "location_update"
                assert update["data"]["vehicle_id"] == str(vehicle.id)
    finally:
        listener_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await listener_task


async def test_driver_socket_uses_savepoint_session_not_a_separate_connection(
    ws_client_factory, db_session
):
    """Regresión: driver_socket solía abrir su propia conexión con SessionLocal()
    en vez de recibir `db` por Depends. Eso escribía directo a flotilla_test por
    fuera del SAVEPOINT de la prueba y dejaba filas huérfanas que el rollback
    final nunca limpiaba. Si esta prueba pasa junto con la de arriba, confirma
    que ambas comparten la misma sesión/transacción."""
    vehicle, device_key = await make_vehicle(db_session, with_device_key=True)

    async with ws_client_factory() as ws_client:
        async with aconnect_ws(f"/ws/driver?device_key={device_key}", ws_client) as ws:
            await ws.receive_json()  # connected
            await ws.send_json(_ping())
            await ws.receive_json()

    # Visible en la MISMA sesión sin volver a hacer flush/commit externo:
    # si viviera en otra conexión, esta lectura (todavía dentro del SAVEPOINT)
    # no la vería por aislamiento de transacciones.
    result = await db_session.execute(
        select(LocationPing).where(LocationPing.vehicle_id == vehicle.id)
    )
    assert len(result.scalars().all()) == 1
