"""Agregado continuo `vehicle_position_5min` (ver alembic/versions/..._agregados_continuos_timescaledb.py).

TimescaleDB solo materializa esta vista fuera de una transacción explícita
(refresh_continuous_aggregate no puede correr dentro de un bloque
transaccional), y estas pruebas viven dentro del SAVEPOINT por-test de
conftest.py — así que aquí no se valida el refresco en sí (eso ya se probó a
mano contra la base de dev), solo el contrato del endpoint: shape, RBAC y el
caso sin datos.
"""

from app.models import UserRole
from tests.factories import auth_headers, make_driver, make_staff_user, make_vehicle


async def test_history_summary_empty_for_vehicle_without_pings(client, db_session):
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    vehicle = await make_vehicle(db_session)

    response = await client.get(
        f"/api/v1/vehicles/{vehicle.id}/history/summary"
        "?since=2026-01-01T00:00:00Z&until=2026-12-31T23:59:59Z",
        headers=auth_headers(operator_token),
    )
    assert response.status_code == 200
    assert response.json() == []


async def test_history_summary_requires_staff(client, db_session):
    _, driver_token = await make_driver(db_session)
    vehicle = await make_vehicle(db_session)

    response = await client.get(
        f"/api/v1/vehicles/{vehicle.id}/history/summary"
        "?since=2026-01-01T00:00:00Z&until=2026-12-31T23:59:59Z",
        headers=auth_headers(driver_token),
    )
    assert response.status_code == 403
