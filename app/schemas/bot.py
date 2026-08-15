"""Schemas de la puerta de entrada de los bots de cliente."""

import uuid

from pydantic import BaseModel, Field

from app.models.enums import CustomerChannel, TripStatus


class BotRideRequest(BaseModel):
    channel: CustomerChannel
    # Quién es el cliente DENTRO de su canal: el chat_id en Telegram, el
    # teléfono en WhatsApp. El endpoint lo guarda en la columna que
    # corresponda, para que la operadora siga viendo un teléfono como
    # teléfono y no un identificador opaco.
    customer_id: str = Field(..., min_length=1, max_length=64)
    lat: float = Field(..., ge=-90, le=90)
    lng: float = Field(..., ge=-180, le=180)


class BotRideResponse(BaseModel):
    trip_id: uuid.UUID
    status: TripStatus
    # True cuando el cliente ya tenía un viaje activo y se le devuelve ese en
    # vez de crear otro. El bot cambia el texto que le muestra; no es un error
    # y por eso no es un 409: pedir taxi dos veces seguidas es lo que hace
    # alguien impaciente parado en la calle, no un cliente mal portado.
    already_active: bool = False
    # Presión de demanda al momento de aceptar el viaje. `wait_message` viene
    # listo para mostrarse tal cual; `high_demand` está aparte para el bot que
    # prefiera redactar lo suyo o cambiar de tono. Nulos en las respuestas que
    # no crean viaje (consulta de estado), donde no aplica prometer nada.
    high_demand: bool | None = None
    wait_message: str | None = None


class BotCancelRequest(BaseModel):
    channel: CustomerChannel
    customer_id: str = Field(..., min_length=1, max_length=64)


class BotCancelResponse(BaseModel):
    cancelled: bool
    trip_id: uuid.UUID | None = None
