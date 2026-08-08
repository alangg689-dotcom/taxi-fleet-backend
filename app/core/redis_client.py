"""Cliente Redis compartido.

Redis cumple cuatro funciones distintas en este sistema:
  1. Cache de la última posición conocida de cada unidad (lectura instantánea).
  2. Contadores atómicos para rate limiting de login (operador/admin y chofer).
  3. Pub/Sub como bus de eventos entre instancias del backend.
  4. Bloqueo temporal de intentos fallidos de login, sobre el mismo contador.
"""

import json

import redis.asyncio as aioredis

from app.config import settings

redis_client: aioredis.Redis = aioredis.from_url(
    settings.REDIS_URL,
    encoding="utf-8",
    decode_responses=True,
)


# --- Contadores atómicos --------------------------------------------------

# INCR + EXPIRE en una sola operación indivisible. El TTL se fija solo en el
# primer incremento, para que la ventana sea fija y no se renueve con cada
# intento. Sin esto, dos intentos casi simultáneos podrían leer el mismo
# valor antes de que cualquiera lo escribiera y dejar pasar más de la cuenta.
_INCR_WITH_TTL = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return current
"""


async def incr_with_ttl(key: str, ttl: int) -> int:
    """Incrementa `key` atómicamente; usado por app.core.login_throttle
    (login de operador/admin y de chofer, mismo módulo para ambos)."""
    return int(await redis_client.eval(_INCR_WITH_TTL, 1, key, ttl))


# --- Cache de última posición -------------------------------------------------

def _last_position_key(vehicle_id: str) -> str:
    return f"vehicle:{vehicle_id}:last_position"


async def set_last_position(vehicle_id: str, payload: dict) -> None:
    """Guarda la posición más reciente. El dashboard la lee de aquí en vez de
    golpear la tabla de telemetría, que puede tener millones de filas."""
    await redis_client.set(
        _last_position_key(vehicle_id),
        json.dumps(payload, default=str),
        ex=settings.LAST_POSITION_TTL,
    )


async def get_last_position(vehicle_id: str) -> dict | None:
    raw = await redis_client.get(_last_position_key(vehicle_id))
    return json.loads(raw) if raw else None


async def get_all_last_positions() -> list[dict]:
    """Snapshot inicial que recibe el dashboard al conectarse por WebSocket."""
    keys = [k async for k in redis_client.scan_iter(match="vehicle:*:last_position")]
    if not keys:
        return []
    values = await redis_client.mget(keys)
    return [json.loads(v) for v in values if v]


async def publish_vehicle_status_update(vehicle_id: str, vehicle_status: str, on_trip: bool) -> None:
    """Actualiza status/on_trip sobre el último snapshot cacheado y lo vuelve
    a publicar — así el mapa en vivo refleja un disponible/ocupado/offline (o
    que arrancó/terminó un viaje) sin esperar el siguiente ping de GPS.

    Si la unidad todavía no mandó ningún ping, no hay snapshot que actualizar
    — el mapa de todos modos solo muestra unidades que ya reportaron posición,
    así que no hay nada que publicar todavía."""
    cached = await get_last_position(vehicle_id)
    if cached is None:
        return
    cached["status"] = vehicle_status
    cached["on_trip"] = on_trip
    await set_last_position(vehicle_id, cached)
    await publish_location_update(cached)


# --- Pub/Sub ------------------------------------------------------------------

async def publish_location_update(payload: dict) -> None:
    """Publica el evento en el canal de flota.

    Esto es lo que permite escalar a varias instancias del backend: el chofer
    puede estar conectado al Servidor A y el dashboard al Servidor B. Sin este
    canal común, B nunca se enteraría del ping que recibió A.
    """
    await redis_client.publish(
        settings.LOCATION_CHANNEL, json.dumps(payload, default=str)
    )


async def publish_queue_update(payload: dict) -> None:
    """Publica un cambio en la fila de un sitio — consumido por /ws/fleet
    (panel de fila del dashboard, paso 7) y /ws/driver (el chofer formado
    filtra por su propio stand_id, ver app.ws.fleet). Mismo motivo que
    publish_location_update: la fila puede cambiar desde una instancia
    distinta a la que tiene abierta la conexión de ese cliente."""
    await redis_client.publish(settings.QUEUE_CHANNEL, json.dumps(payload, default=str))


def driver_offer_channel(driver_id: str) -> str:
    return f"driver:{driver_id}:offers"


async def publish_trip_offer(driver_id: str, payload: dict) -> None:
    """Empuja una oferta de viaje al chofer, sin importar a qué instancia del
    backend esté conectado su WebSocket (mismo motivo que publish_location_update:
    el motor de despacho puede correr en un servidor distinto al que tiene
    abierta la conexión de ese chofer)."""
    await redis_client.publish(driver_offer_channel(driver_id), json.dumps(payload, default=str))
