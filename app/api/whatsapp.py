"""Webhook de Twilio para el bot de WhatsApp.

A diferencia del resto de la API, esto no lo llama nuestro frontend, lo llama
Twilio directo cuando un cliente le escribe al número de WhatsApp — por eso
no lleva auth de JWT (Twilio no tiene uno nuestro) y recibe el cuerpo como
form-urlencoded, no JSON (así lo manda Twilio, sin opción de cambiarlo).

Validar la firma X-Twilio-Signature queda pendiente para cuando se pase a un
número de WhatsApp Business propio — mientras se prueba en el sandbox
compartido, no hay nada verdaderamente sensible que proteger todavía.
"""

from xml.sax.saxutils import escape

from fastapi import APIRouter, Form
from fastapi.responses import Response

from app.core.whatsapp_bot import handle_incoming_message

router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])


def _twiml_reply(message: str) -> Response:
    body = f"<?xml version='1.0' encoding='UTF-8'?><Response><Message>{escape(message)}</Message></Response>"
    return Response(content=body, media_type="application/xml")


@router.post("/webhook")
async def whatsapp_webhook(
    From: str = Form(...),
    Latitude: str | None = Form(None),
    Longitude: str | None = Form(None),
) -> Response:
    phone = From.removeprefix("whatsapp:")
    latitude = float(Latitude) if Latitude else None
    longitude = float(Longitude) if Longitude else None
    reply = await handle_incoming_message(phone, latitude, longitude)
    return _twiml_reply(reply)
