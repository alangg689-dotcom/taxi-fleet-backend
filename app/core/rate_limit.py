"""Rate limit genérico por clave Redis (IP, teléfono, folio…).

Mismo contador atómico que login_throttle y ping_throttle (`incr_with_ttl`
con ventana fija). Sin bloqueo extra al superar el techo: al vencer la
ventana, el cliente vuelve a poder llamar. Pensado para endpoints públicos
de autorregistro, no para fuerza bruta de PIN (eso sigue en login_throttle).
"""

from app.core.redis_client import incr_with_ttl, redis_client


class RateLimitExceeded(Exception):
    """La clave superó su techo para la ventana actual."""

    def __init__(self, retry_after: int) -> None:
        self.retry_after = max(1, retry_after)
        super().__init__(f"Demasiadas solicitudes. Reintenta en {self.retry_after}s.")


async def hit(key: str, max_per_window: int, window_seconds: int) -> None:
    """Suma 1 al contador y lanza RateLimitExceeded si se pasa del techo."""
    total = await incr_with_ttl(key, window_seconds)
    if total > max_per_window:
        ttl = await redis_client.ttl(key)
        raise RateLimitExceeded(retry_after=ttl)
