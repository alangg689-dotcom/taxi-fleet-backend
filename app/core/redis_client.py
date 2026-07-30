"""Cliente Redis compartido.

Redis cumple tres funciones distintas en este sistema:
  1. Cache de la última posición conocida de cada unidad (lectura instantánea).
  2. Contadores atómicos para rate limiting de OTP.
  3. Pub/Sub como bus de eventos entre instancias del backend.
"""

import json

import redis.asyncio as aioredis

from app.config import settings

redis_client: aioredis.Redis = aioredis.from_url(
    settings.REDIS_URL,
    encoding="utf-8",
    decode_responses=True,
)


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
