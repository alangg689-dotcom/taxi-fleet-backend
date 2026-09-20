"""Schemas de choferes."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import DriverStatus


def _normalize_numeral(value: str | None) -> str | None:
    """Sin esto "r18" y "R18" serían dos numerales distintos para el índice
    único y el mismo para la operadora, que es la que importa."""
    if value is None:
        return None
    return value.strip().upper()


class DriverCreate(BaseModel):
    # Sin lada de país: la flotilla es mexicana y se capturan los 10 dígitos.
    phone: str = Field(..., min_length=10, max_length=20, examples=["6441234567"])
    full_name: str = Field(..., max_length=150)
    license_number: str = Field(..., max_length=50)
    # Requerido al dar de alta: es como la operadora nombra al chofer por
    # radio, no un dato accesorio. Los que ya existían quedaron en NULL (ver
    # migración 0011) y se les asigna con PATCH /drivers/{id}.
    numeral: str = Field(..., min_length=1, max_length=10, examples=["R18"])

    _normalize = field_validator("numeral")(_normalize_numeral)


class DriverUpdate(BaseModel):
    full_name: str | None = Field(None, max_length=150)
    status: DriverStatus | None = None
    numeral: str | None = Field(None, min_length=1, max_length=10)
    folio_ctm: str | None = Field(None, min_length=1, max_length=20)

    _normalize = field_validator("numeral")(_normalize_numeral)

    @field_validator("folio_ctm")
    @classmethod
    def _folio(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip().upper() or None


class PushTokenUpdate(BaseModel):
    """Token de push de Expo del teléfono del chofer — se sobreescribe en
    cada registro, así que el que quede es siempre el del dispositivo activo."""

    push_token: str = Field(..., min_length=1, max_length=255)


class DriverOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    phone: str | None
    full_name: str
    license_number: str
    # Nulo solo para los choferes anteriores a la migración 0011, que todavía
    # no tienen numeral asignado.
    numeral: str | None
    status: DriverStatus
    is_active: bool
    # Derivado (pin_hash is not None) — nunca el hash mismo. Para que el
    # dashboard distinga a quién le falta asignarle un PIN todavía (los
    # migrados del login por OTP nacieron sin uno).
    has_pin: bool
    # True tras aprobar una solicitud o tras un force-reset: el chofer
    # tiene que inventar el PIN (POST /auth/driver/set-pin). El operador
    # no lo genera.
    must_set_pin: bool = False
    # ID operativo de la unidad (ej. CTM-045). No es el numeral de radio.
    folio_ctm: str | None = None
    unit_role: str | None = None
    # Unidad del turno abierto, si trae uno. Nulos = chofer libre, y son los
    # únicos que el dashboard ofrece al asignar una unidad.
    current_vehicle_id: UUID | None = None
    current_vehicle_plate: str | None = None


class DriverCreated(DriverOut):
    """El PIN se muestra UNA sola vez, al dar de alta al chofer o al
    regenerarlo (POST /drivers/{id}/pin). Después solo queda su hash."""

    pin: str
