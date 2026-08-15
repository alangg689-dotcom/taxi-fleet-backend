"""Punto de entrada de la API."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import auth, bot, drivers, location, stands, trips, vehicles, whatsapp
from app.config import settings
from app.core.redis_client import redis_client
from app.core.stands import sweep_stand_queues
from app.core.whatsapp_bot import sweep_stuck_bot_trips
from app.ws import fleet

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def _sweep_loop(
    sweep: Callable[[], Awaitable[None]], interval_seconds: int, description: str
) -> None:
    """Corre `sweep` cada `interval_seconds`, para siempre. El try/except es
    por iteración a propósito: un error en una pasada (Redis parpadeó, un
    deadlock) no debe matar el barrido para el resto de la vida del
    proceso."""
    while True:
        try:
            await sweep()
        except Exception:
            logger.exception("Error en el barrido de %s", description)
        await asyncio.sleep(interval_seconds)


def _warn_on_insecure_config() -> None:
    """Avisa fuerte al arrancar si quedó puesta una config que solo debería
    existir en desarrollo.

    CORS_ORIGINS="*" no es una puerta abierta a la sesión de nadie —
    la auth viaja en el header Authorization, no en cookies, y con
    allow_credentials=False el navegador no la manda sola desde otro
    origen. Pero sí deja que cualquier página sondee la API desde un
    navegador, y deja de ser inofensivo el día que algo pase a usar
    cookies. Como cerrarlo requiere saber el dominio real del dashboard,
    esto no lo cierra solo: lo hace imposible de ignorar en el arranque.
    """
    if settings.DEBUG:
        return
    if settings.cors_origins == ["*"]:
        logger.warning(
            "CORS_ORIGINS=* con DEBUG=false — cualquier origen puede llamar a "
            "esta API desde un navegador. Pon el dominio real del dashboard "
            "en CORS_ORIGINS antes de exponer esto a internet."
        )
    if settings.JWT_SECRET == "CAMBIAR-EN-PRODUCCION":
        logger.warning(
            "JWT_SECRET sigue con el valor de ejemplo — cualquiera que lea el "
            "repositorio puede firmar tokens válidos. Genera uno con "
            "'openssl rand -hex 32'."
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    _warn_on_insecure_config()

    # El listener de Redis debe vivir tanto como la aplicación: es lo que
    # conecta los pings entrantes con los dashboards de esta instancia.
    tasks = [
        asyncio.create_task(fleet.redis_listener()),
        # Fila de sitios: pérdida de señal y cronómetros de candidato que
        # cumplieron sin que llegara un ping nuevo (spec de sitios, sección 7).
        asyncio.create_task(
            _sweep_loop(
                sweep_stand_queues, settings.STAND_SWEEP_INTERVAL_SECONDS, "sitios/fila"
            )
        ),
        # Viajes del bot que quedaron "solicitado" sin candidatos: se
        # reintentan aquí en vez de cancelarse a la primera pasada.
        asyncio.create_task(
            _sweep_loop(
                sweep_stuck_bot_trips,
                settings.BOT_TRIP_SWEEP_INTERVAL_SECONDS,
                "viajes del bot",
            )
        ),
    ]
    yield
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await redis_client.aclose()


app = FastAPI(
    title=settings.APP_NAME,
    version="0.1.0",
    description="Geolocalización y monitoreo GPS de flotilla de taxis en tiempo real",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,  # "*" en dev; dominio real en CORS_ORIGINS en prod
    # Sin cookies: la auth viaja en el header Authorization, que el navegador
    # no manda automático. allow_credentials no aplica y con "*" sería inválido
    # (los navegadores rechazan wildcard + credenciales en el mismo origin).
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    # Sin esto el header queda en la respuesta pero es invisible para
    # fetch()/XHR en el navegador: por CORS, solo un puñado de headers
    # "simples" son legibles desde JS a menos que se expongan a propósito.
    expose_headers=["X-Total-Count"],
)

app.include_router(auth.router, prefix="/api/v1")

# El orden importa: FastAPI resuelve las rutas de arriba hacia abajo. El router
# de telemetría define rutas estáticas como /vehicles/nearby y
# /vehicles/locations; si se registrara después del router de vehículos, el
# patrón /vehicles/{vehicle_id} las capturaría primero e intentaría leer
# "nearby" como un UUID.
app.include_router(location.router, prefix="/api/v1")
app.include_router(vehicles.router, prefix="/api/v1")
app.include_router(stands.router, prefix="/api/v1")
app.include_router(trips.router, prefix="/api/v1")
app.include_router(drivers.router, prefix="/api/v1")
app.include_router(whatsapp.router, prefix="/api/v1")
app.include_router(bot.router, prefix="/api/v1")
app.include_router(fleet.router)


@app.get("/health", tags=["health"])
async def health():
    """Sonda de salud para el balanceador de carga."""
    try:
        await redis_client.ping()
        redis_ok = True
    except Exception:
        redis_ok = False
    return {"status": "ok", "redis": redis_ok}
