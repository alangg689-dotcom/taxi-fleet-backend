"""Canal en tiempo real.

Dos WebSockets con propósitos opuestos:

  /ws/driver  -> el chofer EMITE su posición (una conexión por unidad).
  /ws/fleet   -> la operadora RECIBE las posiciones de toda la flota.

El puente entre ambos es Redis Pub/Sub, y esa es la pieza que permite correr
más de una instancia del backend detrás de un balanceador. Si el chofer queda
conectado al Servidor A y el dashboard al Servidor B, el ping que recibe A no
existiría para B sin un canal común. Con Pub/Sub, A publica el evento en
`fleet:updates` y TODAS las instancias suscritas lo retransmiten a sus propios
clientes conectados.

Flujo completo de un ping:
    app chofer --ws--> Servidor A
    Servidor A --> TimescaleDB (historial) + Redis (última posición)
    Servidor A --> PUBLISH fleet:updates
    Redis --> Servidores A, B, C... (todos suscritos)
    cada servidor --ws--> sus dashboards conectados
"""

import asyncio
import contextlib
import json
import logging

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect, status
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.location import _broadcast_latest, _persist_pings
from app.config import settings
from app.core.redis_client import get_all_last_positions, redis_client
from app.core.security import decode_access_token, hash_token
from app.database import get_db
from app.models import UserRole, Vehicle
from app.schemas.location import LocationPingIn

logger = logging.getLogger(__name__)
router = APIRouter()


class DashboardManager:
    """Mantiene las conexiones de dashboard vivas en ESTA instancia."""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._connections.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(ws)

    async def broadcast(self, message: str) -> None:
        async with self._lock:
            targets = list(self._connections)

        dead: list[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)

        if dead:
            async with self._lock:
                self._connections.difference_update(dead)


manager = DashboardManager()


async def redis_listener() -> None:
    """Tarea de fondo: escucha el canal de Redis y reenvía a los dashboards.

    Se arranca una vez al iniciar la aplicación (ver main.py). Cada instancia
    del backend corre su propia copia de este listener.
    """
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(settings.LOCATION_CHANNEL)
    logger.info("Suscrito al canal %s", settings.LOCATION_CHANNEL)

    try:
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            await manager.broadcast(
                json.dumps({"type": "location_update", "data": json.loads(message["data"])})
            )
    except asyncio.CancelledError:
        raise
    finally:
        with contextlib.suppress(Exception):
            await pubsub.unsubscribe(settings.LOCATION_CHANNEL)
            await pubsub.aclose()


# --- Dashboard de la operadora ------------------------------------------------

@router.websocket("/ws/fleet")
async def fleet_socket(websocket: WebSocket, token: str = Query(...)):
    """El navegador no permite enviar cabeceras en el handshake de WebSocket,
    por eso el JWT viaja como query param (siempre sobre WSS en producción)."""
    payload = decode_access_token(token)
    if payload is None or payload.get("role") not in (
        UserRole.OPERATOR.value,
        UserRole.ADMIN.value,
    ):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await manager.connect(websocket)
    try:
        # Snapshot inicial: el mapa se dibuja completo sin esperar al primer
        # ping de cada unidad.
        await websocket.send_text(
            json.dumps({"type": "snapshot", "data": await get_all_last_positions()})
        )
        while True:
            # Mantiene la conexión abierta; el cliente puede mandar pings de
            # keepalive, que aquí se descartan.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(websocket)


# --- App del chofer -----------------------------------------------------------

@router.websocket("/ws/driver")
async def driver_socket(
    websocket: WebSocket,
    device_key: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """La app del chofer mantiene esta conexión abierta y emite su posición.

    Acepta un objeto suelto o un array, igual que el endpoint REST: al salir de
    un túnel la app descarga de golpe el buffer que acumuló sin señal.

    `db` llega por Depends como en cualquier endpoint REST (y no por un
    `SessionLocal()` manual): así la conexión completa —desde la búsqueda de
    la unidad hasta cada commit del loop— pasa por la misma sesión que la
    suite de pruebas puede sustituir con `dependency_overrides`.
    """
    result = await db.execute(
        select(Vehicle).where(Vehicle.device_key_hash == hash_token(device_key))
    )
    vehicle = result.scalar_one_or_none()

    if vehicle is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    logger.info("Chofer conectado; unidad %s", vehicle.plate)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
                items = data if isinstance(data, list) else [data]
                pings = sorted(
                    (LocationPingIn(**item) for item in items),
                    key=lambda p: p.timestamp,
                )
            except (json.JSONDecodeError, ValidationError, TypeError) as exc:
                await websocket.send_text(
                    json.dumps({"type": "error", "detail": str(exc)})
                )
                continue

            accepted = await _persist_pings(db, vehicle.id, pings)
            await db.commit()

            await _broadcast_latest(vehicle.id, pings[-1])

            # ACK explícito: hasta recibirlo, la app NO debe borrar su buffer
            # local, o un corte de red haría perder posiciones.
            await websocket.send_text(
                json.dumps({"type": "ack", "accepted": accepted, "received": len(pings)})
            )
    except WebSocketDisconnect:
        logger.info("Chofer desconectado; unidad %s", vehicle.plate)
