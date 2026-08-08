"""Punto de entrada de la API."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import auth, drivers, location, stands, trips, vehicles, whatsapp
from app.config import settings
from app.core.redis_client import redis_client
from app.core.stands import sweep_stand_queues
from app.ws import fleet

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def _stand_queue_sweep_loop() -> None:
    """Dispara sweep_stand_queues cada STAND_SWEEP_INTERVAL_SECONDS — lo que
    no depende de que llegue un ping nuevo (pérdida de señal, cronómetros de
    candidato que ya cumplieron su tiempo). Ver app.core.stands, sección 7
    de spec-sitios-y-fila-v2.md."""
    while True:
        try:
            await sweep_stand_queues()
        except Exception:
            logger.exception("Error en el barrido de sitios/fila")
        await asyncio.sleep(settings.STAND_SWEEP_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # El listener de Redis debe vivir tanto como la aplicación: es lo que
    # conecta los pings entrantes con los dashboards de esta instancia.
    listener = asyncio.create_task(fleet.redis_listener())
    sweep = asyncio.create_task(_stand_queue_sweep_loop())
    yield
    for task in (listener, sweep):
        task.cancel()
    for task in (listener, sweep):
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
