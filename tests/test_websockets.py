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
from datetime import UTC, datetime, timedelta

import pytest
from httpx_ws import WebSocketDisconnect, aconnect_ws
from sqlalchemy import select

from app.core.redis_client import get_last_position, publish_trip_offer
from app.models import LocationPing, UserRole, VehicleStatus
from app.ws import fleet as fleet_module
from tests.factories import make_driver, make_open_assignment, make_staff_user, make_vehicle


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
    es un JWT) — lo necesita para POST /vehicles/{id}/status (switch de
    "entrar a trabajar" / corte de calle). Conectarse ya no cambia el status
    solo: una unidad nueva (offline por default) sigue offline hasta que el
    chofer prenda el switch."""
    vehicle, device_key = await make_vehicle(db_session, with_device_key=True)

    async with ws_client_factory() as ws_client:
        async with aconnect_ws(f"/ws/driver?device_key={device_key}", ws_client) as ws:
            connected = await ws.receive_json()
            assert connected == {
                "type": "connected",
                "vehicle_id": str(vehicle.id),
                "vehicle_status": "offline",
                "vehicle_plate": vehicle.plate,
            }


async def test_driver_socket_receives_offer_published_right_after_connecting(
    ws_client_factory, db_session
):
    """Regresión: el canal de ofertas se suscribe ANTES de aceptar la
    conexión (ver docstring de _forward_trip_offers en app/ws/fleet.py). Con
    el bug, un PUBLISH que cayera justo después del mensaje "connected" se
    perdía para siempre — Redis Pub/Sub no le entrega nada a quien se
    suscribe tarde. Publicar aquí en cuanto llega "connected" reproduce esa
    ventana exacta."""
    vehicle, device_key = await make_vehicle(db_session, with_device_key=True)
    driver, _ = await make_driver(db_session)
    await make_open_assignment(db_session, vehicle_id=vehicle.id, driver_id=driver.id)

    async with ws_client_factory() as ws_client:
        async with aconnect_ws(f"/ws/driver?device_key={device_key}", ws_client) as ws:
            await ws.receive_json()  # "connected"

            await publish_trip_offer(str(driver.id), {"trip_id": "abc123"})

            offer = await asyncio.wait_for(ws.receive_json(), timeout=2)
            assert offer == {"type": "trip_offer", "data": {"trip_id": "abc123"}}


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
    vehicle, device_key = await make_vehicle(
        db_session, with_device_key=True, status=VehicleStatus.DISPONIBLE
    )
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
                assert update["data"]["status"] == "disponible"
                assert update["data"]["on_trip"] is False
    finally:
        listener_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await listener_task


async def test_stale_ping_from_offline_buffer_does_not_rebroadcast(
    ws_client_factory, db_session
):
    """Regresión: al vaciarse el buffer offline de una unidad, un ping viejo
    no debe hacer retroceder su posición cacheada si mientras tanto ya
    llegó una más reciente (ver docstring de _broadcast_latest)."""
    vehicle, device_key = await make_vehicle(db_session, with_device_key=True)
    newer = datetime.now(UTC)
    older = newer - timedelta(seconds=30)

    async with ws_client_factory() as ws_client:
        async with aconnect_ws(f"/ws/driver?device_key={device_key}", ws_client) as ws:
            await ws.receive_json()  # connected

            await ws.send_json(_ping(lat=19.5, lng=-99.2, timestamp=newer.isoformat()))
            await ws.receive_json()  # ack

            await ws.send_json(_ping(lat=10.0, lng=10.0, timestamp=older.isoformat()))
            await ws.receive_json()  # ack (se guarda en location_pings igual, solo no se difunde)

    cached = await get_last_position(str(vehicle.id))
    assert cached is not None
    assert cached["lat"] == 19.5
    assert cached["lng"] == -99.2


async def test_vehicle_status_change_broadcasts_to_fleet_dashboard(
    ws_client_factory, db_session, client
):
    """POST /vehicles/{id}/status (el switch de "Disponible" del chofer)
    debe reflejarse en el mapa en vivo sin esperar el siguiente ping."""
    vehicle, device_key = await make_vehicle(db_session, with_device_key=True)
    driver, driver_token = await make_driver(db_session)
    await make_open_assignment(db_session, vehicle_id=vehicle.id, driver_id=driver.id)
    _, admin_token = await make_staff_user(db_session, role=UserRole.ADMIN)

    listener_task = asyncio.create_task(fleet_module.redis_listener())
    await asyncio.sleep(0.1)

    try:
        async with ws_client_factory() as ws_client:
            # Primero un ping, para que exista un snapshot cacheado que actualizar
            # (sin ping previo no hay nada que mostrar en el mapa todavía).
            async with aconnect_ws(f"/ws/driver?device_key={device_key}", ws_client) as driver_ws:
                await driver_ws.receive_json()  # connected
                await driver_ws.send_json(_ping())
                await driver_ws.receive_json()  # ack

            async with aconnect_ws(f"/ws/fleet?token={admin_token}", ws_client) as fleet_ws:
                await fleet_ws.receive_json()  # snapshot

                response = await client.post(
                    f"/api/v1/vehicles/{vehicle.id}/status",
                    json={"status": "ocupado"},
                    headers={"Authorization": f"Bearer {driver_token}"},
                )
                assert response.status_code == 200

                update = await asyncio.wait_for(fleet_ws.receive_json(), timeout=2)
                assert update["type"] == "location_update"
                assert update["data"]["vehicle_id"] == str(vehicle.id)
                assert update["data"]["status"] == "ocupado"
                assert update["data"]["on_trip"] is False
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
