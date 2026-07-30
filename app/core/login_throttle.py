"""Rate limiting y bloqueo de /auth/login por fuerza bruta.

Mismo patrón que app.core.otp: contador atómico en Redis (incr_with_ttl) y
bloqueo temporal tras demasiados fallos. Se cuenta por email, no por IP —
igual que el OTP se cuenta por teléfono, no por IP — así que un atacante
probando muchos emails distintos no se ve frenado en conjunto, solo email por
email. Es la misma limitación que ya tiene el flujo de OTP, no una nueva.
"""

from app.config import settings
from app.core.redis_client import incr_with_ttl, redis_client


class LoginLockedError(Exception):
    """Demasiados intentos fallidos: la cuenta está temporalmente bloqueada."""


def _lock_key(email: str) -> str:
    return f"login:lock:{email}"


def _fail_key(email: str) -> str:
    return f"login:fail:{email}"


async def check_not_locked(email: str) -> None:
    if await redis_client.exists(_lock_key(email)):
        ttl = await redis_client.ttl(_lock_key(email))
        raise LoginLockedError(
            f"Demasiados intentos fallidos. Reintenta en {ttl}s."
        )


async def record_failure(email: str) -> None:
    """Cuenta un fallo; bloquea el email al llegar al máximo.

    Se llama tanto si el email no existe como si la contraseña es incorrecta:
    distinguir los dos casos con distinto comportamiento de bloqueo revelaría
    qué emails están registrados, el mismo hueco de enumeración que /login ya
    evita respondiendo siempre el mismo 401 genérico.
    """
    fails = await incr_with_ttl(_fail_key(email), settings.LOGIN_ATTEMPT_WINDOW)
    if fails >= settings.LOGIN_MAX_ATTEMPTS:
        await redis_client.set(
            _lock_key(email), "1", ex=settings.LOGIN_LOCKOUT_SECONDS
        )
        await redis_client.delete(_fail_key(email))


async def reset(email: str) -> None:
    """Un login válido borra el historial de fallos previos."""
    await redis_client.delete(_fail_key(email), _lock_key(email))
