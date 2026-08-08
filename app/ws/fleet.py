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
from app.core.redis_client import driver_offer_channel, get_all_last_positions, redis_client
from app.core.security import decode_access_token, hash_token
from app.database import get_db
from app.models import UserRole, Vehicle, VehicleAssignment, VehicleStatus
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


async def _forward_trip_offers(websocket: WebSocket, pubsub) -> None:
    """Tarea de fondo por conexión: reenvía al chofer las ofertas de viaje
    que el motor de despacho (app.core.dispatch) le publique mientras esta
    conexión siga abierta.

    Recibe el `pubsub` ya suscrito (ver driver_socket) en vez de suscribirse
    aquí mismo: si la suscripción se hiciera dentro de esta tarea de fondo,
    lanzada con `asyncio.create_task` sin esperar a que termine, quedaría una
    ventana entre que el chofer "se conecta" (candidato válido para el motor
    de despacho) y que el canal queda realmente suscrito. Un PUBLISH de
    Redis que caiga en esa ventana se pierde para siempre — a diferencia de
    una cola, Pub/Sub no le entrega nada a quien se suscribe tarde.
    """
    try:
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            await websocket.send_text(
                json.dumps({"type": "trip_offer", "data": json.loads(message["data"])})
            )
    except asyncio.CancelledError:
        raise
    finally:
        with contextlib.suppress(Exception):
            await pubsub.unsubscribe()
            await pubsub.aclose()


async def _forward_queue_updates(websocket: WebSocket, pubsub, stand_id: str) -> None:
    """Igual que _forward_trip_offers pero para la fila del propio sitio:
    stand:queue es un canal único para todos los sitios (ver
    app.core.stands._broadcast_queue), así que aquí se descarta lo que no
    sea del sitio de esta unidad — el chofer no necesita ver la fila de
    otro sitio."""
    try:
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            data = json.loads(message["data"])
            if data.get("stand_id") != stand_id:
                continue
            await websocket.send_text(json.dumps({"type": "queue_update", "data": data}))
    except asyncio.CancelledError:
        raise
    finally:
        with contextlib.suppress(Exception):
            await pubsub.unsubscribe()
            await pubsub.aclose()


_CHANNEL_MESSAGE_TYPES = {
    settings.LOCATION_CHANNEL: "location_update",
    settings.QUEUE_CHANNEL: "queue_update",
}


async def redis_listener() -> None:
    """Tarea de fondo: escucha los canales de Redis y reenvía a los
    dashboards — posición en vivo (fleet:updates) y fila de sitios
    (stand:queue, sección 9) por la misma conexión.

    Se arranca una vez al iniciar la aplicación (ver main.py). Cada instancia
    del backend corre su propia copia de este listener.
    """
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(*_CHANNEL_MESSAGE_TYPES)
    logger.info("Suscrito a los canales %s", list(_CHANNEL_MESSAGE_TYPES))

    try:
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            message_type = _CHANNEL_MESSAGE_TYPES.get(message["channel"])
            if message_type is None:
                continue
            await manager.broadcast(
                json.dumps({"type": message_type, "data": json.loads(message["data"])})
            )
    except asyncio.CancelledError:
        raise
    finally:
        with contextlib.suppress(Exception):
            await pubsub.unsubscribe(*_CHANNEL_MESSAGE_TYPES)
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

    # Conectarse ya NO pone la unidad "disponible" sola — el chofer decide
    # cuándo con el switch de "entrar a trabajar" (POST /vehicles/{id}/status)
    # en su app. Así puede abrir la app a checar el menú/ingresos sin que le
    # empiecen a caer viajes de inmediato.

    # Turno abierto de esta unidad: si hay alguien manejándola ahora mismo,
    # también recibe por este mismo socket las ofertas de viaje que le mande
    # el motor de despacho (app.core.dispatch). Se resuelve una sola vez al
    # conectar; un cambio de turno a media conexión no lo actualiza.
    assignment_result = await db.execute(
        select(VehicleAssignment.driver_id).where(
            VehicleAssignment.vehicle_id == vehicle.id,
            VehicleAssignment.ended_at.is_(None),
        )
    )
    current_driver_id = assignment_result.scalar_one_or_none()

    # Suscrito ANTES de aceptar la conexión (y por lo tanto antes de que este
    # chofer pueda volverse candidato de un despacho): ver el porqué en el
    # docstring de _forward_trip_offers. Misma razón para la fila: sin turno
    # abierto la unidad nunca puede estar formada (ver evaluate_ping_for_queue),
    # así que no vale la pena suscribirse.
    offers_pubsub = None
    queue_pubsub = None
    if current_driver_id is not None:
        offers_pubsub = redis_client.pubsub()
        await offers_pubsub.subscribe(driver_offer_channel(str(current_driver_id)))
        queue_pubsub = redis_client.pubsub()
        await queue_pubsub.subscribe(settings.QUEUE_CHANNEL)

    await websocket.accept()
    logger.info("Chofer conectado; unidad %s", vehicle.plate)

    # La app no tiene otra forma de enterarse de su propio vehicle_id/status
    # (device_key no es un JWT, no trae claims) — se lo manda una vez al
    # conectar para que pueda usar POST /vehicles/{id}/status (corte de calle).
    # `vehicle_plate` es solo para mostrarle al chofer qué unidad es (pantalla
    # de inicio de la app), no tiene otro uso.
    await websocket.send_text(
        json.dumps(
            {
                "type": "connected",
                "vehicle_id": str(vehicle.id),
                "vehicle_status": vehicle.status.value,
                "vehicle_plate": vehicle.plate,
            }
        )
    )

    offers_task: asyncio.Task | None = None
    queue_task: asyncio.Task | None = None
    try:
        if offers_pubsub is not None:
            offers_task = asyncio.create_task(_forward_trip_offers(websocket, offers_pubsub))
        if queue_pubsub is not None:
            queue_task = asyncio.create_task(
                _forward_queue_updates(websocket, queue_pubsub, str(vehicle.stand_id))
            )

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

            await _broadcast_latest(vehicle, pings[-1])

            # ACK explícito: hasta recibirlo, la app NO debe borrar su buffer
            # local, o un corte de red haría perder posiciones.
            await websocket.send_text(
                json.dumps({"type": "ack", "accepted": accepted, "received": len(pings)})
            )
    except WebSocketDisconnect:
        logger.info("Chofer desconectado; unidad %s", vehicle.plate)
    finally:
        if offers_task is not None:
            offers_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await offers_task
        if queue_task is not None:
            queue_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await queue_task
