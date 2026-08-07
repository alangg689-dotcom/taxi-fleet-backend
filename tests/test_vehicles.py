from app.models import UserRole, VehicleStatus
from tests.factories import (
    auth_headers,
    make_driver,
    make_open_assignment,
    make_staff_user,
    make_stand,
    make_vehicle,
)


async def test_admin_can_create_vehicle(client, db_session):
    _, admin_token = await make_staff_user(db_session, role=UserRole.ADMIN)
    stand = await make_stand(db_session)

    response = await client.post(
        "/api/v1/vehicles",
        json={
            "plate": "ABC-123",
            "model": "Nissan Versa",
            "year": 2022,
            "stand_id": str(stand.id),
        },
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["plate"] == "ABC-123"
    assert body["stand_id"] == str(stand.id)
    assert "device_key" in body


async def test_create_vehicle_unknown_stand_is_404(client, db_session):
    _, admin_token = await make_staff_user(db_session, role=UserRole.ADMIN)

    response = await client.post(
        "/api/v1/vehicles",
        json={
            "plate": "NOSTAND-1",
            "model": "Nissan Versa",
            "stand_id": "00000000-0000-0000-0000-000000000000",
        },
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 404


async def test_operator_cannot_create_vehicle(client, db_session):
    """Alta de unidades es solo para admin; el operador solo opera turnos."""
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    stand = await make_stand(db_session)

    response = await client.post(
        "/api/v1/vehicles",
        json={"plate": "DEF-456", "model": "Toyota Corolla", "stand_id": str(stand.id)},
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
    stand = await make_stand(db_session)

    response = await client.post(
        "/api/v1/vehicles",
        json={"plate": "GHI-789", "model": "Otro modelo", "stand_id": str(stand.id)},
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


async def test_driver_can_set_own_vehicle_status(client, db_session):
    """Corte de calle: el chofer se marca ocupado/disponible él mismo, sin
    pasar por operador ni por el motor de despacho."""
    vehicle = await make_vehicle(db_session)
    driver, driver_token = await make_driver(db_session)
    await make_open_assignment(db_session, vehicle_id=vehicle.id, driver_id=driver.id)

    response = await client.post(
        f"/api/v1/vehicles/{vehicle.id}/status",
        json={"status": "ocupado"},
        headers=auth_headers(driver_token),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ocupado"


async def test_driver_can_toggle_offline_disponible(client, db_session):
    """El switch de "entrar/salir a trabajar" de la app: conectarse ya no
    pone la unidad disponible sola (ver app.ws.fleet), el chofer decide
    cuándo con este mismo endpoint."""
    vehicle = await make_vehicle(db_session, status=VehicleStatus.OFFLINE)
    driver, driver_token = await make_driver(db_session)
    await make_open_assignment(db_session, vehicle_id=vehicle.id, driver_id=driver.id)
    headers = auth_headers(driver_token)

    online = await client.post(
        f"/api/v1/vehicles/{vehicle.id}/status", json={"status": "disponible"}, headers=headers
    )
    assert online.status_code == 200
    assert online.json()["status"] == "disponible"

    offline = await client.post(
        f"/api/v1/vehicles/{vehicle.id}/status", json={"status": "offline"}, headers=headers
    )
    assert offline.status_code == 200
    assert offline.json()["status"] == "offline"


async def test_driver_cannot_set_status_of_vehicle_without_their_shift(client, db_session):
    vehicle = await make_vehicle(db_session)
    _, driver_token = await make_driver(db_session)
    # Sin make_open_assignment: este chofer no tiene el turno de esta unidad.

    response = await client.post(
        f"/api/v1/vehicles/{vehicle.id}/status",
        json={"status": "ocupado"},
        headers=auth_headers(driver_token),
    )
    assert response.status_code == 403


async def test_staff_can_set_status_of_any_vehicle(client, db_session):
    vehicle = await make_vehicle(db_session)
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)

    response = await client.post(
        f"/api/v1/vehicles/{vehicle.id}/status",
        json={"status": "disponible"},
        headers=auth_headers(operator_token),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "disponible"


async def test_vehicle_status_accepts_offline(client, db_session):
    """El switch de "entrar/salir a trabajar" de la app del chofer manda
    "offline" por este mismo endpoint (ver docstring de VehicleStatusUpdate)."""
    vehicle = await make_vehicle(db_session, status=VehicleStatus.DISPONIBLE)
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)

    response = await client.post(
        f"/api/v1/vehicles/{vehicle.id}/status",
        json={"status": "offline"},
        headers=auth_headers(operator_token),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "offline"


async def test_vehicle_status_rejects_mantenimiento(client, db_session):
    """Mantenimiento sigue siendo decisión de un operador vía PATCH
    /vehicles/{id}, no algo que se autoasigne por este endpoint."""
    vehicle = await make_vehicle(db_session)
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)

    response = await client.post(
        f"/api/v1/vehicles/{vehicle.id}/status",
        json={"status": "mantenimiento"},
        headers=auth_headers(operator_token),
    )
    assert response.status_code == 422


async def test_operator_can_regenerate_device_key(client, db_session):
    """La unidad ya existe, con su device_key original — regenerarla no debe
    romper nada más que invalidar la vieja."""
    vehicle, old_key = await make_vehicle(db_session, with_device_key=True)
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)

    response = await client.post(
        f"/api/v1/vehicles/{vehicle.id}/device-key",
        headers=auth_headers(operator_token),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(vehicle.id)
    assert "device_key" in body
    assert body["device_key"] != old_key


async def test_driver_cannot_regenerate_device_key(client, db_session):
    """Re-emparejar un teléfono con una unidad es decisión de operador/admin,
    no algo que el chofer pueda hacer por su cuenta."""
    vehicle = await make_vehicle(db_session)
    _, driver_token = await make_driver(db_session)

    response = await client.post(
        f"/api/v1/vehicles/{vehicle.id}/device-key",
        headers=auth_headers(driver_token),
    )
    assert response.status_code == 403


async def test_regenerate_device_key_unknown_vehicle_is_404(client, db_session):
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)

    response = await client.post(
        "/api/v1/vehicles/00000000-0000-0000-0000-000000000000/device-key",
        headers=auth_headers(operator_token),
    )
    assert response.status_code == 404
