"""Generación y verificación de códigos OTP para el login de choferes.

Todos los contadores (solicitudes y fallos) se manipulan con un script Lua
(ver app.core.redis_client.incr_with_ttl), que Redis ejecuta de forma atómica.
Esto evita la condición de carrera clásica: si el chofer presiona "reenviar"
tres veces en el mismo segundo, un INCR y un EXPIRE por separado podrían leer
el mismo valor antes de escribirlo y dejar pasar más intentos de los
permitidos. Dentro de un script Lua eso no ocurre, porque Redis no intercala
otros comandos mientras se ejecuta.
"""

import secrets

import httpx

from app.config import settings
from app.core.redis_client import incr_with_ttl, redis_client
from app.core.security import hash_token

_TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"


class OTPError(Exception):
    """Error de negocio del flujo OTP (bloqueo, límite excedido, código inválido)."""


class SMSDeliveryError(Exception):
    """El proveedor de SMS no pudo entregar el mensaje (Twilio caído, número
    inválido, credenciales mal puestas, etc.).

    Se distingue de OTPError a propósito: aquí el problema es del proveedor,
    no del chofer, así que no debe consumir intentos ni bloquear el teléfono
    como sí hace OTPError.
    """


def _key(prefix: str, phone: str) -> str:
    return f"otp:{prefix}:{phone}"


async def request_otp(phone: str) -> str:
    """Genera y almacena un código nuevo. Devuelve el código en claro para que
    la capa de SMS lo envíe; en Redis solo queda el hash."""

    if await redis_client.exists(_key("lock", phone)):
        ttl = await redis_client.ttl(_key("lock", phone))
        raise OTPError(f"Teléfono bloqueado temporalmente. Reintenta en {ttl}s.")

    count = await incr_with_ttl(_key("req", phone), settings.OTP_REQUEST_WINDOW)
    if count > settings.OTP_MAX_REQUESTS:
        raise OTPError("Demasiadas solicitudes. Espera unos minutos.")

    code = "".join(secrets.choice("0123456789") for _ in range(settings.OTP_LENGTH))
    await redis_client.set(
        _key("code", phone), hash_token(code), ex=settings.OTP_TTL_SECONDS
    )
    # Un código nuevo reinicia el contador de fallos del código anterior.
    await redis_client.delete(_key("fail", phone))
    return code


async def verify_otp(phone: str, code: str) -> bool:
    """Valida el código. Consume el código si es correcto (un solo uso)."""

    if await redis_client.exists(_key("lock", phone)):
        raise OTPError("Teléfono bloqueado temporalmente.")

    stored = await redis_client.get(_key("code", phone))
    if stored is None:
        raise OTPError("El código expiró o no fue solicitado.")

    if not secrets.compare_digest(stored, hash_token(code)):
        fails = await incr_with_ttl(_key("fail", phone), settings.OTP_TTL_SECONDS)
        if fails >= settings.OTP_MAX_ATTEMPTS:
            await redis_client.set(
                _key("lock", phone), "1", ex=settings.OTP_LOCKOUT_SECONDS
            )
            await redis_client.delete(_key("code", phone), _key("fail", phone))
            raise OTPError("Demasiados intentos fallidos. Teléfono bloqueado 30 min.")
        return False

    # Código correcto: se invalida para que no pueda reutilizarse.
    await redis_client.delete(_key("code", phone), _key("fail", phone))
    return True


async def send_sms(phone: str, code: str) -> None:
    """Despacha el código por SMS. En desarrollo solo lo imprime en consola."""
    if settings.SMS_PROVIDER == "console":
        print(f"[SMS-DEV] Código para {phone}: {code}")
        return

    if settings.SMS_PROVIDER == "twilio":
        minutes = settings.OTP_TTL_SECONDS // 60
        body = f"Tu código de Flotilla GPS es {code}. Vence en {minutes} minutos."
        await _send_via_twilio(phone, body)
        return

    raise NotImplementedError(f"Proveedor SMS no configurado: {settings.SMS_PROVIDER}")


async def _send_via_twilio(phone: str, body: str) -> None:
    """Envía el SMS con la API REST de Twilio directamente por httpx.

    Se evita el SDK oficial (síncrono, bloquearía el loop) ya que httpx ya es
    dependencia del proyecto y la llamada es un solo POST bien documentado.
    """
    url = f"{_TWILIO_API_BASE}/Accounts/{settings.TWILIO_ACCOUNT_SID}/Messages.json"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url,
                auth=(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN),
                data={
                    "To": phone,
                    "From": settings.TWILIO_FROM_NUMBER,
                    "Body": body,
                },
            )
    except httpx.HTTPError as exc:
        raise SMSDeliveryError(f"No se pudo contactar a Twilio: {exc}") from exc

    if response.status_code >= 400:
        try:
            detail = response.json().get("message", response.text)
        except ValueError:
            detail = response.text
        raise SMSDeliveryError(
            f"Twilio rechazó el envío ({response.status_code}): {detail}"
        )
