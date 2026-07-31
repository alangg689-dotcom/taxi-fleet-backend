from app.models import UserRole
from tests.factories import auth_headers, make_driver, make_staff_user, make_vehicle


async def test_admin_can_create_vehicle(client, db_session):
    _, admin_token = await make_staff_user(db_session, role=UserRole.ADMIN)

    response = await client.post(
        "/api/v1/vehicles",
        json={"plate": "ABC-123", "model": "Nissan Versa", "year": 2022},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["plate"] == "ABC-123"
    assert "device_key" in body


async def test_operator_cannot_create_vehicle(client, db_session):
    """Alta de unidades es solo para admin; el operador solo opera turnos."""
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)

    response = await client.post(
        "/api/v1/vehicles",
        json={"plate": "DEF-456", "model": "Toyota Corolla"},
        headers=auth_headers(operator_token),
    )
    assert response.status_code == 403


async def test_driver_cannot_list_vehicles(client, db_session):
    _, driver_token = await make_driver(db_session)

    response = await client.get("/api/v1/vehicles", headers=auth_headers(driver_token))
    assert response.status_code == 403


async def test_duplicate_plate_is_conflict(client, db_session):
    _, admin_token = await make_staff_user(db_session, role=UserRole.ADMIN)
    await make_vehicle(db_session, plate="GHI-789")

    response = await client.post(
        "/api/v1/vehicles",
        json={"plate": "GHI-789", "model": "Otro modelo"},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 409


async def test_get_and_list_vehicle(client, db_session):
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    vehicle = await make_vehicle(db_session)

    listed = await client.get("/api/v1/vehicles", headers=auth_headers(operator_token))
    assert listed.status_code == 200
    assert any(v["id"] == str(vehicle.id) for v in listed.json())

    fetched = await client.get(
        f"/api/v1/vehicles/{vehicle.id}", headers=auth_headers(operator_token)
    )
    assert fetched.status_code == 200
    assert fetched.json()["plate"] == vehicle.plate


async def test_get_unknown_vehicle_is_404(client, db_session):
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)

    response = await client.get(
        "/api/v1/vehicles/00000000-0000-0000-0000-000000000000",
        headers=auth_headers(operator_token),
    )
    assert response.status_code == 404


async def test_list_vehicles_paginates_with_total_count_header(client, db_session):
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    headers = auth_headers(operator_token)

    plates = [f"PAG-{i:03d}" for i in range(5)]
    for plate in plates:
        await make_vehicle(db_session, plate=plate)

    first_page = await client.get(
        "/api/v1/vehicles?limit=2&offset=0", headers=headers
    )
    assert first_page.status_code == 200
    assert len(first_page.json()) == 2
    assert int(first_page.headers["X-Total-Count"]) >= 5

    second_page = await client.get(
        "/api/v1/vehicles?limit=2&offset=2", headers=headers
    )
    assert second_page.status_code == 200
    first_ids = {v["id"] for v in first_page.json()}
    second_ids = {v["id"] for v in second_page.json()}
    assert first_ids.isdisjoint(second_ids)


async def test_open_assignment_closes_previous_shift(client, db_session):
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    vehicle = await make_vehicle(db_session)
    driver_one, _ = await make_driver(db_session)
    driver_two, _ = await make_driver(db_session)
    headers = auth_headers(operator_token)

    first = await client.post(
        f"/api/v1/vehicles/{vehicle.id}/assignments",
        json={"driver_id": str(driver_one.id)},
        headers=headers,
    )
    assert first.status_code == 201

    second = await client.post(
        f"/api/v1/vehicles/{vehicle.id}/assignments",
        json={"driver_id": str(driver_two.id)},
        headers=headers,
    )
    assert second.status_code == 201

    history = await client.get(
        f"/api/v1/vehicles/{vehicle.id}/assignments", headers=headers
    )
    shifts = history.json()
    assert len(shifts) == 2
    closed = next(s for s in shifts if s["driver_id"] == str(driver_one.id))
    open_shift = next(s for s in shifts if s["driver_id"] == str(driver_two.id))
    assert closed["ended_at"] is not None
    assert open_shift["ended_at"] is None


async def test_close_assignment_without_open_shift_is_404(client, db_session):
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    vehicle = await make_vehicle(db_session)

    response = await client.post(
        f"/api/v1/vehicles/{vehicle.id}/assignments/close",
        headers=auth_headers(operator_token),
    )
    assert response.status_code == 404
