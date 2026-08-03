"""Notificaciones push al chofer.

Redis Pub/Sub (ver app.core.redis_client.publish_trip_offer) solo le llega a
un WebSocket /ws/driver que esté vivo en ese momento — si el chofer trae la
app en segundo plano o cerrada del todo, esa oferta nunca la ve. Esta es la
red de seguridad: una notificación nativa del sistema operativo que le llega
sin importar si la app está abierta.

Se usa el servicio de push de Expo en vez de hablar con FCM/APNs
directamente: un solo POST HTTP, sin SDKs nativos ni credenciales por
plataforma de nuestro lado — Expo hace de intermediario con Google/Apple
usando las credenciales que la app ya tiene configuradas en EAS.
"""

import logging

import httpx

logger = logging.getLogger(__name__)

_EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"


async def send_push_notification(
    push_token: str, title: str, body: str, data: dict
) -> None:
    """Un push es un complemento del WebSocket, no el camino principal del
    despacho — por eso nunca lanza: que Expo esté caído o rechace un token
    vencido no debe tumbar el resto de dispatch_trip()."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                _EXPO_PUSH_URL,
                json={
                    "to": push_token,
                    "title": title,
                    "body": body,
                    "data": data,
                    "sound": "default",
                    "priority": "high",
                    "channelId": "trip-offers",
                },
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
        if response.status_code >= 400:
            logger.error(
                "Expo push rechazó el envío (%s): %s", response.status_code, response.text
            )
    except httpx.HTTPError:
        logger.error("No se pudo contactar el servicio de push de Expo", exc_info=True)
