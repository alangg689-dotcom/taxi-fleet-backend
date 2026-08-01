import uuid

from app.models import UserRole, VehicleStatus
from tests.factories import auth_headers, make_driver, make_staff_user, make_vehicle


async def _dispatch(client, headers, vehicle_id, driver_id, **extra):
    payload = {
        "vehicle_id": str(vehicle_id),
        "driver_id": str(driver_id),
        "origin_lat": 19.4326,
        "origin_lng": -99.1332,
        **extra,
    }
    return await client.post("/api/v1/trips", json=payload, headers=headers)


async def test_create_trip_round_trips_coordinates(client, db_session):
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    vehicle = await make_vehicle(db_session)
    driver, _ = await make_driver(db_session)
    headers = auth_headers(operator_token)

    response = await _dispatch(
        client,
        headers,
        vehicle.id,
        driver.id,
        origin_address="Zócalo",
        destination_lat=19.4429,
        destination_lng=-99.2013,
        destination_address="Polanco",
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "solicitado"
    assert body["origin_lat"] == 19.4326
    assert body["origin_lng"] == -99.1332
    assert body["destination_lat"] == 19.4429
    assert body["destination_address"] == "Polanco"
    assert body["started_at"] is None


async def test_create_trip_without_destination(client, db_session):
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    vehicle = await make_vehicle(db_session)
    driver, _ = await make_driver(db_session)

    response = await _dispatch(client, auth_headers(operator_token), vehicle.id, driver.id)

    assert response.status_code == 201
    body = response.json()
    assert body["destination_lat"] is None
    assert body["destination_address"] is None


async def test_cannot_dispatch_same_vehicle_twice(client, db_session):
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    vehicle = await make_vehicle(db_session)
    driver_one, _ = await make_driver(db_session)
    driver_two, _ = await make_driver(db_session)
    headers = auth_headers(operator_token)

    first = await _dispatch(client, headers, vehicle.id, driver_one.id)
    assert first.status_code == 201

    second = await _dispatch(client, headers, vehicle.id, driver_two.id)
    assert second.status_code == 409


async def test_create_trip_unknown_vehicle_or_driver_is_404(client, db_session):
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    driver, _ = await make_driver(db_session)
    headers = auth_headers(operator_token)

    missing_vehicle = await _dispatch(client, headers, uuid.uuid4(), driver.id)
    assert missing_vehicle.status_code == 404

    vehicle = await make_vehicle(db_session)
    missing_driver = await _dispatch(client, headers, vehicle.id, uuid.uuid4())
    assert missing_driver.status_code == 404


async def test_driver_cannot_see_another_drivers_trip(client, db_session):
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    vehicle = await make_vehicle(db_session)
    owner, _owner_token = await make_driver(db_session)
    _, other_driver_token = await make_driver(db_session)

    created = await _dispatch(
        client, auth_headers(operator_token), vehicle.id, owner.id
    )
    trip_id = created.json()["id"]

    response = await client.get(
        f"/api/v1/trips/{trip_id}", headers=auth_headers(other_driver_token)
    )
    assert response.status_code == 403


async def test_full_lifecycle_accept_start_complete(client, db_session):
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    vehicle = await make_vehicle(db_session)
    driver, driver_token = await make_driver(db_session)
    driver_headers = auth_headers(driver_token)

    created = await _dispatch(
        client, auth_headers(operator_token), vehicle.id, driver.id
    )
    trip_id = created.json()["id"]

    accepted = await client.post(f"/api/v1/trips/{trip_id}/accept", headers=driver_headers)
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "asignado"

    started = await client.post(f"/api/v1/trips/{trip_id}/start", headers=driver_headers)
    assert started.status_code == 200
    assert started.json()["status"] == "en_curso"
    assert started.json()["started_at"] is not None

    completed = await client.post(f"/api/v1/trips/{trip_id}/complete", headers=driver_headers)
    assert completed.status_code == 200
    assert completed.json()["status"] == "completado"
    assert completed.json()["completed_at"] is not None


async def test_accept_marks_vehicle_ocupado_and_complete_frees_it(client, db_session):
    """El chofer no tiene que acordarse de marcarse ocupado/disponible a mano
    durante un viaje despachado por la app: accept/complete lo hacen solos."""
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    vehicle = await make_vehicle(db_session, status=VehicleStatus.DISPONIBLE)
    driver, driver_token = await make_driver(db_session)
    driver_headers = auth_headers(driver_token)

    created = await _dispatch(
        client, auth_headers(operator_token), vehicle.id, driver.id
    )
    trip_id = created.json()["id"]

    await client.post(f"/api/v1/trips/{trip_id}/accept", headers=driver_headers)
    await db_session.refresh(vehicle)
    assert vehicle.status == VehicleStatus.OCUPADO

    await client.post(f"/api/v1/trips/{trip_id}/start", headers=driver_headers)
    completed = await client.post(
        f"/api/v1/trips/{trip_id}/complete",
        json={"fare": 85.5},
        headers=driver_headers,
    )
    assert completed.status_code == 200
    assert completed.json()["fare"] == 85.5

    await db_session.refresh(vehicle)
    assert vehicle.status == VehicleStatus.DISPONIBLE


async def test_complete_without_fare_leaves_it_null(client, db_session):
    """`fare` es opcional: no todos los operadores van a querer llevar este
    registro."""
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    vehicle = await make_vehicle(db_session)
    driver, driver_token = await make_driver(db_session)
    driver_headers = auth_headers(driver_token)

    created = await _dispatch(
        client, auth_headers(operator_token), vehicle.id, driver.id
    )
    trip_id = created.json()["id"]
    await client.post(f"/api/v1/trips/{trip_id}/accept", headers=driver_headers)
    await client.post(f"/api/v1/trips/{trip_id}/start", headers=driver_headers)

    completed = await client.post(f"/api/v1/trips/{trip_id}/complete", headers=driver_headers)
    assert completed.status_code == 200
    assert completed.json()["fare"] is None


async def test_cancel_frees_vehicle_marked_ocupado(client, db_session):
    vehicle = await make_vehicle(db_session, status=VehicleStatus.OCUPADO)
    driver, _ = await make_driver(db_session)
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    headers = auth_headers(operator_token)

    created = await _dispatch(client, headers, vehicle.id, driver.id)
    trip_id = created.json()["id"]

    cancelled = await client.post(f"/api/v1/trips/{trip_id}/cancel", headers=headers)
    assert cancelled.status_code == 200

    await db_session.refresh(vehicle)
    assert vehicle.status == VehicleStatus.DISPONIBLE


async def test_accept_does_not_touch_vehicle_in_mantenimiento(client, db_session):
    """_set_vehicle_status no debe pisar un estado que decidió un operador."""
    vehicle = await make_vehicle(db_session, status=VehicleStatus.MANTENIMIENTO)
    driver, driver_token = await make_driver(db_session)
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)

    created = await _dispatch(client, auth_headers(operator_token), vehicle.id, driver.id)
    trip_id = created.json()["id"]

    await client.post(f"/api/v1/trips/{trip_id}/accept", headers=auth_headers(driver_token))
    await db_session.refresh(vehicle)
    assert vehicle.status == VehicleStatus.MANTENIMIENTO


async def test_cannot_skip_states_out_of_order(client, db_session):
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    vehicle = await make_vehicle(db_session)
    driver, driver_token = await make_driver(db_session)
    driver_headers = auth_headers(driver_token)

    created = await _dispatch(
        client, auth_headers(operator_token), vehicle.id, driver.id
    )
    trip_id = created.json()["id"]

    # SOLICITADO -> start (se salta accept) debe rechazarse.
    response = await client.post(f"/api/v1/trips/{trip_id}/start", headers=driver_headers)
    assert response.status_code == 409


async def test_cancel_trip_then_cancel_again_conflicts(client, db_session):
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    vehicle = await make_vehicle(db_session)
    driver, _ = await make_driver(db_session)
    headers = auth_headers(operator_token)

    created = await _dispatch(client, headers, vehicle.id, driver.id)
    trip_id = created.json()["id"]

    cancelled = await client.post(f"/api/v1/trips/{trip_id}/cancel", headers=headers)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelado"

    again = await client.post(f"/api/v1/trips/{trip_id}/cancel", headers=headers)
    assert again.status_code == 409


async def test_cancelling_a_trip_frees_the_vehicle_for_redispatch(client, db_session):
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    vehicle = await make_vehicle(db_session)
    driver_one, _ = await make_driver(db_session)
    driver_two, _ = await make_driver(db_session)
    headers = auth_headers(operator_token)

    created = await _dispatch(client, headers, vehicle.id, driver_one.id)
    trip_id = created.json()["id"]
    await client.post(f"/api/v1/trips/{trip_id}/cancel", headers=headers)

    redispatched = await _dispatch(client, headers, vehicle.id, driver_two.id)
    assert redispatched.status_code == 201


async def test_list_trips_filters_by_status(client, db_session):
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    headers = auth_headers(operator_token)

    vehicle_a = await make_vehicle(db_session)
    driver_a, _ = await make_driver(db_session)
    trip_a = (await _dispatch(client, headers, vehicle_a.id, driver_a.id)).json()

    vehicle_b = await make_vehicle(db_session)
    driver_b, _ = await make_driver(db_session)
    trip_b_resp = await _dispatch(client, headers, vehicle_b.id, driver_b.id)
    trip_b_id = trip_b_resp.json()["id"]
    await client.post(f"/api/v1/trips/{trip_b_id}/cancel", headers=headers)

    solicitados = await client.get("/api/v1/trips?status=solicitado", headers=headers)
    ids = {t["id"] for t in solicitados.json()}
    assert trip_a["id"] in ids
    assert trip_b_id not in ids

    cancelados = await client.get("/api/v1/trips?status=cancelado", headers=headers)
    ids = {t["id"] for t in cancelados.json()}
    assert trip_b_id in ids
    assert trip_a["id"] not in ids


async def test_driver_lists_only_their_own_trips(client, db_session):
    """'Mis viajes': un chofer no necesita conocer IDs de antemano, GET /trips
    a secas ya le devuelve solo lo suyo."""
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    operator_headers = auth_headers(operator_token)

    vehicle_a = await make_vehicle(db_session)
    owner, owner_token = await make_driver(db_session)
    own_trip = (
        await _dispatch(client, operator_headers, vehicle_a.id, owner.id)
    ).json()

    vehicle_b = await make_vehicle(db_session)
    other_driver, _ = await make_driver(db_session)
    other_trip = (
        await _dispatch(client, operator_headers, vehicle_b.id, other_driver.id)
    ).json()

    response = await client.get("/api/v1/trips", headers=auth_headers(owner_token))
    assert response.status_code == 200
    ids = {t["id"] for t in response.json()}
    assert ids == {own_trip["id"]}
    assert other_trip["id"] not in ids


async def test_driver_cannot_use_driver_id_filter_to_see_others_trips(client, db_session):
    """driver_id en la query se ignora si lo manda un chofer: se fuerza al
    suyo, para que no pueda pedir los viajes de otro cambiando el parámetro."""
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    operator_headers = auth_headers(operator_token)

    vehicle = await make_vehicle(db_session)
    _, requester_token = await make_driver(db_session)
    other_driver, _ = await make_driver(db_session)
    other_trip = (
        await _dispatch(client, operator_headers, vehicle.id, other_driver.id)
    ).json()

    response = await client.get(
        f"/api/v1/trips?driver_id={other_driver.id}",
        headers=auth_headers(requester_token),
    )
    assert response.status_code == 200
    ids = {t["id"] for t in response.json()}
    assert other_trip["id"] not in ids


async def test_driver_without_trips_gets_empty_list(client, db_session):
    _, driver_token = await make_driver(db_session)

    response = await client.get("/api/v1/trips", headers=auth_headers(driver_token))
    assert response.status_code == 200
    assert response.json() == []
    assert response.headers["X-Total-Count"] == "0"


async def test_list_trips_paginates_with_total_count_header(client, db_session):
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    headers = auth_headers(operator_token)

    for _ in range(5):
        vehicle = await make_vehicle(db_session)
        driver, _ = await make_driver(db_session)
        await _dispatch(client, headers, vehicle.id, driver.id)

    first_page = await client.get("/api/v1/trips?limit=2&offset=0", headers=headers)
    assert first_page.status_code == 200
    assert len(first_page.json()) == 2
    assert int(first_page.headers["X-Total-Count"]) >= 5

    second_page = await client.get("/api/v1/trips?limit=2&offset=2", headers=headers)
    assert second_page.status_code == 200
    first_ids = {t["id"] for t in first_page.json()}
    second_ids = {t["id"] for t in second_page.json()}
    assert first_ids.isdisjoint(second_ids)
