"""Rate limiting y bloqueo por fuerza bruta, compartido por /auth/login
(email de operador/admin) y /auth/driver-login (teléfono de chofer).

Contador atómico en Redis (incr_with_ttl) y bloqueo temporal tras
demasiados fallos. Se cuenta por identificador (email o teléfono), no por
IP — así que un atacante probando muchos identificadores distintos no se
ve frenado en conjunto, solo uno por uno. Limitación conocida, no un
descuido: cubrir por IP además requeriría otra dimensión de rate limiting
que hoy no hace falta para el tamaño de esta flotilla.
"""

from app.config import settings
from app.core.redis_client import incr_with_ttl, redis_client


class LoginLockedError(Exception):
    """Demasiados intentos fallidos: la cuenta está temporalmente bloqueada."""


def _lock_key(identifier: str) -> str:
    return f"login:lock:{identifier}"


def _fail_key(identifier: str) -> str:
    return f"login:fail:{identifier}"


async def check_not_locked(identifier: str) -> None:
    if await redis_client.exists(_lock_key(identifier)):
        ttl = await redis_client.ttl(_lock_key(identifier))
        raise LoginLockedError(
            f"Demasiados intentos fallidos. Reintenta en {ttl}s."
        )


async def record_failure(identifier: str) -> None:
    """Cuenta un fallo; bloquea el identificador al llegar al máximo.

    Se llama tanto si no existe (email o teléfono) como si la credencial es
    incorrecta: distinguir los dos casos con distinto comportamiento de
    bloqueo revelaría qué cuentas están registradas, el mismo hueco de
    enumeración que ambos logins evitan respondiendo siempre el mismo 401
    genérico.
    """
    fails = await incr_with_ttl(_fail_key(identifier), settings.LOGIN_ATTEMPT_WINDOW)
    if fails >= settings.LOGIN_MAX_ATTEMPTS:
        await redis_client.set(
            _lock_key(identifier), "1", ex=settings.LOGIN_LOCKOUT_SECONDS
        )
        await redis_client.delete(_fail_key(identifier))


async def reset(identifier: str) -> None:
    """Un login válido borra el historial de fallos previos."""
    await redis_client.delete(_fail_key(identifier), _lock_key(identifier))
