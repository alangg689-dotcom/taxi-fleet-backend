"""Bot de Telegram para clientes de Los Tigres.

Proceso APARTE del backend: se ejecuta con `python -m bot.telegram_bot` y habla
con la API por HTTP, igual que lo haría cualquier otro cliente. No importa nada
de `app.*` a propósito — si un día el bot se muda a su propio repo o a otra
máquina, solo cambia BACKEND_API_URL.

El tráfico va en un solo sentido por aquí: este proceso atiende lo que ENTRA
(la ubicación que comparte el cliente, /cancelar). Todo lo que SALE hacia el
cliente — "un taxi va en camino", "tu taxi ya llegó", "no encontramos taxi" —
lo manda el backend directo a la API de Telegram con el mismo token
(app/core/telegram.py), porque quien conoce esos eventos es el backend y no
tendría forma de despertar a este proceso para pedírselo. Por eso este archivo
no tiene ningún ciclo de sondeo del estado del viaje.

Arranque:

    export TELEGRAM_BOT_TOKEN=...        # el mismo que el .env del backend
    export BACKEND_API_URL=http://localhost:8000/api/v1
    export BOT_API_KEY=...               # el mismo que el .env del backend
    pip install -r bot/requirements.txt
    python -m bot.telegram_bot
"""

import logging
import os

import httpx
from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
BACKEND_API_URL = os.environ.get("BACKEND_API_URL", "http://localhost:8000/api/v1")
BOT_API_KEY = os.environ.get("BOT_API_KEY", "")

_CHANNEL = "telegram"
_TIMEOUT_SECONDS = 15.0

_GREETING = (
    "¡Hola! Soy el asistente de Los Tigres 🐯\n\n"
    "Para pedir un taxi, toca el botón *📍 Enviar mi ubicación* de aquí abajo. "
    "Buscamos la unidad más cercana y te avisamos por este chat en cuanto un "
    "chofer confirme.\n\n"
    "Usa /cancelar si te arrepientes."
)
_SEARCHING = "Buscando un taxi cerca de ti… te avisamos en cuanto uno confirme. 🔎"
_ALREADY_ACTIVE = (
    "Ya tienes un viaje en curso. En cuanto haya novedades te escribimos por aquí."
)
_CANCELLED = "Tu viaje quedó cancelado. Escríbenos cuando quieras pedir otro."
_NOTHING_TO_CANCEL = "No tienes ningún viaje activo para cancelar."
_BACKEND_DOWN = (
    "No pudimos comunicarnos con la central en este momento. "
    "Intenta de nuevo en un minuto, por favor."
)


def _location_keyboard() -> ReplyKeyboardMarkup:
    """Teclado persistente con un solo botón. `request_location=True` hace que
    Telegram pida el permiso de ubicación por su cuenta y mande las
    coordenadas: el cliente no tiene que buscar el clip de adjuntar ni
    escribir nada."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📍 Enviar mi ubicación", request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


async def _post(path: str, payload: dict) -> dict | None:
    """None = no se pudo hablar con el backend. El llamador le dice al cliente
    que reintente; nunca se traga el error en silencio, porque del otro lado
    hay alguien parado en la calle esperando."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{BACKEND_API_URL}{path}",
                json=payload,
                headers={"X-Bot-Key": BOT_API_KEY},
            )
        if response.status_code >= 400:
            logger.error(
                "El backend rechazó %s (%s): %s", path, response.status_code, response.text
            )
            return None
        return response.json()
    except httpx.HTTPError:
        logger.error("No se pudo contactar al backend en %s", path, exc_info=True)
        return None


async def start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        _GREETING, reply_markup=_location_keyboard(), parse_mode="Markdown"
    )


async def pedir_viaje(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """El comando por si el cliente lo escribe en vez de tocar el botón. No
    puede pedir el viaje solo: sin coordenadas no hay nada que despachar, así
    que lo único que hace es volver a ofrecer el botón."""
    await update.message.reply_text(
        "Para pedir tu taxi necesito saber dónde estás. "
        "Toca el botón de aquí abajo 👇",
        reply_markup=_location_keyboard(),
    )


async def recibir_ubicacion(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    location = update.message.location
    chat_id = str(update.effective_chat.id)

    data = await _post(
        "/bot/request-ride",
        {
            "channel": _CHANNEL,
            "customer_id": chat_id,
            "lat": location.latitude,
            "lng": location.longitude,
        },
    )

    if data is None:
        await update.message.reply_text(_BACKEND_DOWN)
        return

    if data.get("already_active"):
        await update.message.reply_text(_ALREADY_ACTIVE)
    else:
        # El texto lo redacta el backend, que es quien sabe si la calle está
        # saturada: en alta demanda avisa un rango de espera en vez de un "en
        # breve" que no se va a cumplir. _SEARCHING queda de respaldo por si
        # una versión vieja del backend no manda el campo.
        await update.message.reply_text(data.get("wait_message") or _SEARCHING)

    logger.info(
        "Viaje %s solicitado por el chat %s (alta demanda: %s)",
        data.get("trip_id"),
        chat_id,
        data.get("high_demand"),
    )


async def cancelar(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    data = await _post(
        "/bot/cancel-ride", {"channel": _CHANNEL, "customer_id": chat_id}
    )

    if data is None:
        await update.message.reply_text(_BACKEND_DOWN)
        return

    await update.message.reply_text(
        _CANCELLED if data.get("cancelled") else _NOTHING_TO_CANCEL
    )


async def cualquier_texto(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Sin árbol de menús, igual que el bot de WhatsApp: cualquier cosa que no
    entienda vuelve a ofrecer el botón de ubicación. Agregar pasos de
    confirmación es agregar fricción a alguien que quiere un taxi ya."""
    await update.message.reply_text(_GREETING, reply_markup=_location_keyboard(), parse_mode="Markdown")


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("Falta TELEGRAM_BOT_TOKEN (te lo da @BotFather en Telegram)")
    if not BOT_API_KEY:
        raise SystemExit(
            "Falta BOT_API_KEY — debe ser la misma que el .env del backend, "
            "o el backend rechazará todas las peticiones con 401"
        )

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("pedir_viaje", pedir_viaje))
    application.add_handler(CommandHandler("cancelar", cancelar))
    application.add_handler(MessageHandler(filters.LOCATION, recibir_ubicacion))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, cualquier_texto)
    )

    logger.info("Bot de Telegram escuchando; backend en %s", BACKEND_API_URL)
    application.run_polling()


if __name__ == "__main__":
    main()
