"""Techo de telemetría por unidad, para acotar el daño de un device_key filtrado.

Mismo patrón que app.core.login_throttle (contador atómico en Redis con
ventana fija, ver incr_with_ttl), con dos diferencias que vienen de que aquí
no se está frenando un ataque de fuerza bruta sino un exceso de volumen:

  - Se cuentan PINGS, no peticiones. Una sola petición puede traer hasta
    LOCATION_BATCH_MAX; contarla como una dejaría pasar cien veces el
    presupuesto real.
  - No hay bloqueo aparte tras superar el techo. El login castiga con
    LOGIN_LOCKOUT_SECONDS porque cada intento fallido es evidencia de un
    ataque; aquí superar el techo casi siempre es una app con un bug de
    reintentos, y dejar a una unidad legítima sin reportar posición media
    hora es peor que el exceso que se está frenando. Al vencer la ventana,
    la unidad vuelve sola.

Qué NO resuelve: alguien con un device_key válido puede seguir mandando
posiciones falsas a cadencia normal para colarse en la fila de un sitio.
De eso se encarga app.core.ping_validation (precisión, saltos imposibles,
GPS falso). Esto solo acota el volumen.

Por unidad y no por IP, igual que login_throttle cuenta por identificador:
detrás de un carrier móvil muchas unidades comparten IP, así que limitar por
IP castigaría a unidades legítimas por culpa de la de junto.
"""

import logging

from app.config import settings
from app.core.redis_client import incr_with_ttl, redis_client

logger = logging.getLogger(__name__)


class PingRateLimitExceeded(Exception):
    """La unidad superó su techo de pings para la ventana actual."""

    def __init__(self, retry_after: int) -> None:
        self.retry_after = max(1, retry_after)
        super().__init__(
            f"Demasiados pings. Reintenta en {self.retry_after}s."
        )


def _counter_key(vehicle_id: str) -> str:
    return f"ping:count:{vehicle_id}"


async def check_and_count(vehicle_id: str, ping_count: int) -> None:
    """Suma `ping_count` al presupuesto de la unidad y lanza
    PingRateLimitExceeded si con eso se pasa del techo.

    Cuenta ANTES de decidir (no después de persistir) a propósito: si se
    contara solo lo que alcanzó a entrar, un lote enorme se escribiría
    completo antes de que nadie lo frenara, que es justo lo que se quiere
    evitar. El costo es que el lote que cruza el límite se rechaza entero,
    no a medias — más simple de explicar y de reintentar desde la app.
    """
    total = await incr_with_ttl(
        _counter_key(vehicle_id), settings.PING_WINDOW_SECONDS, ping_count
    )
    if total > settings.PING_MAX_PER_WINDOW:
        ttl = await redis_client.ttl(_counter_key(vehicle_id))
        logger.warning(
            "Unidad %s superó el techo de pings (%s en la ventana de %ss)",
            vehicle_id, total, settings.PING_WINDOW_SECONDS,
        )
        raise PingRateLimitExceeded(retry_after=ttl)


async def reset(vehicle_id: str) -> None:
    """Limpia el contador de una unidad. No lo usa el camino normal (la
    ventana vence sola); existe para las pruebas y para poder desatorar a
    una unidad a mano sin esperar a que expire."""
    await redis_client.delete(_counter_key(vehicle_id))
