"""Capa de validación de pings, antes de que puedan mover la máquina de
estados de la fila de sitios (spec-sitios-y-fila-v2.md, sección 6).

No decide si un ping se guarda — eso ya pasó para cuando esto corre, todo
ping se guarda igual para el historial de rutas (routing, reportería). Lo
que decide es si ese ping puede contar para entrar/salir de un sitio o
formarse en la fila. El despacho y el mapa en vivo tampoco dependen de
esto: siguen leyendo `location_pings`/el cache de Redis tal cual.

Se evalúa en orden; el primer chequeo que falla gana:
  1. accuracy > GPS_MAX_ACCURACY_METERS
  2. salto imposible respecto al último ping conocido de la unidad
  3. velocidad y precisión en cero, repetido varias veces seguidas (posible
     GPS falso/simulado)
  4. llegó demasiado tiempo después de haberse capturado (buffer offline
     viejo, o reloj del teléfono mal puesto)
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from math import asin, cos, radians, sin, sqrt

from app.config import settings
from app.core.redis_client import redis_client
from app.schemas.location import LocationPingIn

logger = logging.getLogger(__name__)

# Cuántas lecturas seguidas de velocidad+precisión en cero antes de
# considerarlo GPS falso en vez de "el coche está parado con buena señal"
# (que da accuracy > 0 igual estacionado). La spec no fija un número exacto;
# se usa el mismo criterio de "confirmar por repetición" que STAND_EXIT_
# CONSECUTIVE_PINGS para no reaccionar a una sola lectura rara.
_FAKE_GPS_STREAK_THRESHOLD = 3
_FAKE_GPS_STREAK_TTL_SECONDS = 300


@dataclass(frozen=True)
class PingValidationResult:
    queue_eligible: bool
    flag_reason: str | None  # None cuando el ping pasó todos los chequeos


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r_earth_km = 6371.0
    p1, p2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lng2 - lng1)
    a = sin(dphi / 2) ** 2 + cos(p1) * cos(p2) * sin(dlambda / 2) ** 2
    return 2 * r_earth_km * asin(sqrt(a))


def _is_impossible_jump(previous: dict, ping: LocationPingIn) -> bool:
    prev_ts = datetime.fromisoformat(previous["timestamp"])
    elapsed_hours = (ping.timestamp - prev_ts).total_seconds() / 3600
    if elapsed_hours <= 0:
        return False  # el resguardo de pings viejos (_broadcast_latest) ya cubre este caso
    distance_km = _haversine_km(previous["lat"], previous["lng"], ping.lat, ping.lng)
    return (distance_km / elapsed_hours) > settings.GPS_MAX_JUMP_KMH


def _fake_gps_streak_key(vehicle_id: str) -> str:
    return f"vehicle:{vehicle_id}:fake_gps_streak"


async def _bump_fake_gps_streak(vehicle_id: str) -> int:
    key = _fake_gps_streak_key(vehicle_id)
    streak = await redis_client.incr(key)
    await redis_client.expire(key, _FAKE_GPS_STREAK_TTL_SECONDS)
    return streak


async def _reset_fake_gps_streak(vehicle_id: str) -> None:
    await redis_client.delete(_fake_gps_streak_key(vehicle_id))


async def validate_ping(
    vehicle_id: str, ping: LocationPingIn, previous: dict | None
) -> PingValidationResult:
    """`previous` es el último snapshot cacheado (app.core.redis_client.
    get_last_position) de esta unidad, o None si nunca ha reportado."""

    if ping.accuracy is not None and ping.accuracy > settings.GPS_MAX_ACCURACY_METERS:
        return PingValidationResult(False, "accuracy")

    if previous is not None and _is_impossible_jump(previous, ping):
        return PingValidationResult(False, "jump")

    if ping.speed == 0 and ping.accuracy == 0:
        streak = await _bump_fake_gps_streak(vehicle_id)
        if streak >= _FAKE_GPS_STREAK_THRESHOLD:
            logger.warning(
                "Posible GPS falso en unidad %s: %s lecturas seguidas de velocidad y precisión en cero",
                vehicle_id, streak,
            )
            return PingValidationResult(False, "fake_gps")
    else:
        await _reset_fake_gps_streak(vehicle_id)

    lag_seconds = (datetime.now(UTC) - ping.timestamp).total_seconds()
    if lag_seconds > settings.QUEUE_PING_MAX_LAG_SECONDS:
        return PingValidationResult(False, "lag")

    return PingValidationResult(True, None)
