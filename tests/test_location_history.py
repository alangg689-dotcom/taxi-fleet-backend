"""Pruebas de GET /vehicles/{id}/history (recorrido crudo de pings GPS).

No existían pruebas de este endpoint antes de agregarle paginación — se
cubre aquí junto con el cambio, en vez de asumir que ya estaba probado en
otro lado.
"""

from datetime import UTC, datetime, timedelta

from app.models import UserRole
from tests.factories import (
    auth_headers,
    make_driver,
    make_location_ping,
    make_staff_user,
    make_vehicle,
)

_BASE = datetime(2026, 1, 1, tzinfo=UTC)


async def _seed_pings(db_session, vehicle_id, count: int):
    for i in range(count):
        await make_location_ping(
            db_session, vehicle_id=vehicle_id, timestamp=_BASE + timedelta(minutes=i)
        )


async def test_history_empty_range_returns_empty_list(client, db_session):
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    vehicle = await make_vehicle(db_session)

    response = await client.get(
        f"/api/v1/vehicles/{vehicle.id}/history"
        f"?since=2026-01-01T00:00:00Z&until=2026-01-02T00:00:00Z",
        headers=auth_headers(operator_token),
    )
    assert response.status_code == 200
    assert response.json() == []
    assert response.headers["X-Total-Count"] == "0"


async def test_history_filters_by_date_range_and_vehicle(client, db_session):
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    vehicle = await make_vehicle(db_session)
    other_vehicle = await make_vehicle(db_session)

    await make_location_ping(db_session, vehicle_id=vehicle.id, timestamp=_BASE)
    outside_range = await make_location_ping(
        db_session, vehicle_id=vehicle.id, timestamp=_BASE + timedelta(days=2)
    )
    await make_location_ping(db_session, vehicle_id=other_vehicle.id, timestamp=_BASE)

    response = await client.get(
        f"/api/v1/vehicles/{vehicle.id}/history"
        f"?since=2026-01-01T00:00:00Z&until=2026-01-01T01:00:00Z",
        headers=auth_headers(operator_token),
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["vehicle_id"] == str(vehicle.id)
    assert outside_range.timestamp.isoformat() not in {p["timestamp"] for p in body}


async def test_history_paginates_with_total_count_header(client, db_session):
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    vehicle = await make_vehicle(db_session)
    await _seed_pings(db_session, vehicle.id, count=5)

    first_page = await client.get(
        f"/api/v1/vehicles/{vehicle.id}/history"
        f"?since=2026-01-01T00:00:00Z&until=2026-01-02T00:00:00Z&limit=2&offset=0",
        headers=auth_headers(operator_token),
    )
    assert first_page.status_code == 200
    assert len(first_page.json()) == 2
    assert int(first_page.headers["X-Total-Count"]) == 5

    second_page = await client.get(
        f"/api/v1/vehicles/{vehicle.id}/history"
        f"?since=2026-01-01T00:00:00Z&until=2026-01-02T00:00:00Z&limit=2&offset=2",
        headers=auth_headers(operator_token),
    )
    assert second_page.status_code == 200
    first_ts = {p["timestamp"] for p in first_page.json()}
    second_ts = {p["timestamp"] for p in second_page.json()}
    assert first_ts.isdisjoint(second_ts)


async def test_history_requires_staff(client, db_session):
    _, driver_token = await make_driver(db_session)
    vehicle = await make_vehicle(db_session)

    response = await client.get(
        f"/api/v1/vehicles/{vehicle.id}/history"
        f"?since=2026-01-01T00:00:00Z&until=2026-01-02T00:00:00Z",
        headers=auth_headers(driver_token),
    )
    assert response.status_code == 403


async def test_history_limit_out_of_range_is_rejected(client, db_session):
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    vehicle = await make_vehicle(db_session)

    response = await client.get(
        f"/api/v1/vehicles/{vehicle.id}/history"
        f"?since=2026-01-01T00:00:00Z&until=2026-01-02T00:00:00Z&limit=5000",
        headers=auth_headers(operator_token),
    )
    assert response.status_code == 422
