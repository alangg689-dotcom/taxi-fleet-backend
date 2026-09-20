"""Normalización de teléfonos mexicanos a 10 dígitos.

La flotilla es mexicana: la operadora captura los 10 dígitos y ya, sin +52
(ver scripts/seed_drivers_units.py). El login histórico y varias pruebas
guardan E.164 (`+52XXXXXXXXXX`). Estas funciones unifican ambos formatos
para guardar (10 dígitos) y para buscar (los dos).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from sqlalchemy import select

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models import User

_NON_DIGITS = re.compile(r"\D")


class InvalidPhoneError(ValueError):
    """El valor no es un teléfono mexicano de 10 dígitos."""


def normalize_mx_phone(value: str | None) -> str | None:
    """Devuelve 10 dígitos o None si el campo viene vacío.

    Acepta `6621234567`, `+52 662 123 4567` y `526621234567`. Lanza
    InvalidPhoneError si después de limpiar no quedan 10 dígitos.
    """
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    digits = _NON_DIGITS.sub("", stripped)
    # E.164 México: 52 + 10 dígitos. Un móvil de 10 dígitos nunca empieza
    # por 52 (ladas: 55, 33, 81, 662…), así que recortar solo con len>=12
    # no pisa un número local legítimo.
    if digits.startswith("52") and len(digits) >= 12:
        digits = digits[2:]
    if len(digits) != 10:
        raise InvalidPhoneError("El teléfono debe tener 10 dígitos")
    return digits


def phone_lookup_values(value: str) -> list[str]:
    """Valores con los que el mismo teléfono puede estar guardado en `users`."""
    normalized = normalize_mx_phone(value)
    if normalized is None:
        return []
    return [normalized, f"+52{normalized}"]


async def find_user_by_phone(db: AsyncSession, phone: str) -> User | None:
    """Busca un User por teléfono en 10 dígitos o en E.164 +52."""
    from app.models import User

    try:
        values = phone_lookup_values(phone)
    except InvalidPhoneError:
        values = [phone.strip()] if phone and phone.strip() else []
    if not values:
        return None
    result = await db.execute(select(User).where(User.phone.in_(values)))
    return result.scalar_one_or_none()
