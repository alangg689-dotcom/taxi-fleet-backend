"""Fixtures compartidas.

Las pruebas corren contra una base de datos Postgres real (flotilla_test, en
el mismo servidor de docker-compose), no contra SQLite ni mocks: la mitad del
código de este proyecto son consultas espaciales y de enums nativos de
Postgres que un motor distinto no reproduce fielmente. Justo ese desajuste es
el que costó dos bugs en producción antes de que existiera esta suite.

Aislamiento por prueba: cada test corre dentro de un SAVEPOINT que se revierte
al final, así ninguna prueba ve los datos de otra y no hace falta truncar
tablas entre corridas.
"""

import os
import subprocess
import sys
from collections.abc import AsyncGenerator
from pathlib import Path

# Debe fijarse ANTES de importar cualquier módulo de la app: settings.DATABASE_URL
# y settings.REDIS_URL se leen una sola vez, al importar app.config.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://flotilla:flotilla@localhost:5432/flotilla_test",
)
os.environ["REDIS_URL"] = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")

import asyncpg
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from httpx import ASGITransport, AsyncClient
from httpx_ws.transport import ASGIWebSocketTransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.core.redis_client import redis_client
from app.database import get_db
from app.main import app

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_DB_NAME = settings.DATABASE_URL.rsplit("/", 1)[1]

# Motor propio para las pruebas, con NullPool: cada test corre en su propio
# event loop (así funciona pytest-asyncio por default) y una conexión pooleada
# de un loop ya cerrado revienta si otro test intenta reciclarla en el suyo.
# Sin pool, cada conexión es nueva y se cierra por completo al terminar, así
# que nunca cruza de un loop a otro. El engine de la app (app.database.engine)
# no se usa en absoluto: cada request pasa por la sesión de prueba vía
# dependency_overrides.
test_engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)


def _maintenance_dsn() -> str:
    """DSN a la base 'flotilla' (asyncpg no entiende el sufijo +asyncpg de
    SQLAlchemy, y no se puede crear una base conectado a ella misma)."""
    plain = settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
    return plain.rsplit("/", 1)[0] + "/flotilla"


@pytest_asyncio.fixture(scope="session", autouse=True, loop_scope="session")
async def _test_database() -> None:
    """Crea flotilla_test si no existe y la deja al día con las migraciones."""
    conn = await asyncpg.connect(_maintenance_dsn())
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", TEST_DB_NAME
        )
        if not exists:
            await conn.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
    finally:
        await conn.close()

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env={**os.environ, "DATABASE_URL": settings.DATABASE_URL},
        check=True,
        capture_output=True,
    )

    # Una conexión propia y desechable: esta fixture corre en el event loop de
    # sesión, distinto del loop de cada prueba. Usar aquí el redis_client
    # compartido lo ataría a ese loop de sesión y el primer aclose() de una
    # prueba (loop distinto) reventaría con "Future attached to a different loop".
    throwaway = aioredis.from_url(settings.REDIS_URL)
    await throwaway.flushdb()
    await throwaway.aclose()


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Sesión unida a una transacción externa que siempre se revierte.

    join_transaction_mode="create_savepoint" hace que cada `commit()` de la
    sesión (el que dispara get_db al final de cada request) cierre un
    SAVEPOINT en vez de la transacción real, así el rollback final deshace
    todo lo que hizo la prueba de un solo golpe.
    """
    async with test_engine.connect() as connection:
        await connection.begin()
        session_factory = async_sessionmaker(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        session = session_factory()

        async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
            yield session

        app.dependency_overrides[get_db] = _override_get_db
        try:
            yield session
        finally:
            app.dependency_overrides.pop(get_db, None)
            await session.close()
            await connection.rollback()


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def ws_client_factory(db_session: AsyncSession):
    """Fábrica (no un cliente ya armado) para las pruebas de WebSockets.

    `ASGIWebSocketTransport.__aenter__` crea un anyio task group que su
    `__aexit__` exige cerrar desde la MISMA task. Un fixture generador de
    pytest-asyncio corre su `yield` y su finalizer en tasks distintas, así que
    envolver el `async with` aquí revienta con "Attempted to exit cancel scope
    in a different task". Por eso esto no entra al context manager: cada
    prueba lo hace ella misma con `async with ws_client_factory() as ws:`, todo
    dentro de una sola task.
    """

    def _make() -> AsyncClient:
        return AsyncClient(transport=ASGIWebSocketTransport(app=app), base_url="http://test")

    return _make


@pytest_asyncio.fixture(autouse=True)
async def _reset_redis_pool() -> AsyncGenerator[None, None]:
    """redis_client es un singleton creado una sola vez al importar la app (tal
    como en producción, donde vive un solo event loop toda la vida del
    proceso). pytest-asyncio le da a cada test su propio event loop, y una
    conexión abierta en el loop de un test revienta si el siguiente test
    intenta reusarla en el suyo. Cerrar el pool al final de cada test fuerza
    una reconexión limpia en el loop del que sigue.
    """
    yield
    await redis_client.aclose()
