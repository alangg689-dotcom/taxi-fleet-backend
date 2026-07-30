from app.models import UserRole
from tests.factories import auth_headers, make_driver, make_staff_user


async def test_admin_can_create_driver(client, db_session):
    _, admin_token = await make_staff_user(db_session, role=UserRole.ADMIN)

    response = await client.post(
        "/api/v1/drivers",
        json={
            "phone": "+525512340099",
            "full_name": "Juan Pérez",
            "license_number": "LIC-99000",
        },
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["phone"] == "+525512340099"
    assert body["full_name"] == "Juan Pérez"
    assert body["status"] == "activo"


async def test_operator_cannot_create_driver(client, db_session):
    """Alta de choferes es solo para admin, igual que alta de unidades."""
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)

    response = await client.post(
        "/api/v1/drivers",
        json={
            "phone": "+525512340098",
            "full_name": "Alguien",
            "license_number": "LIC-99001",
        },
        headers=auth_headers(operator_token),
    )
    assert response.status_code == 403


async def test_driver_cannot_create_driver(client, db_session):
    _, driver_token = await make_driver(db_session)

    response = await client.post(
        "/api/v1/drivers",
        json={
            "phone": "+525512340097",
            "full_name": "Alguien",
            "license_number": "LIC-99002",
        },
        headers=auth_headers(driver_token),
    )
    assert response.status_code == 403


async def test_duplicate_phone_is_conflict(client, db_session):
    _, admin_token = await make_staff_user(db_session, role=UserRole.ADMIN)
    await make_driver(db_session, phone="+525512340096")

    response = await client.post(
        "/api/v1/drivers",
        json={
            "phone": "+525512340096",
            "full_name": "Otro",
            "license_number": "LIC-99003",
        },
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 409


async def test_duplicate_license_is_conflict(client, db_session):
    _, admin_token = await make_staff_user(db_session, role=UserRole.ADMIN)
    await make_driver(db_session, license_number="LIC-DUPLICADA")

    response = await client.post(
        "/api/v1/drivers",
        json={
            "phone": "+525512340095",
            "full_name": "Otro",
            "license_number": "LIC-DUPLICADA",
        },
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 409


async def test_list_and_get_driver(client, db_session):
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    driver, _ = await make_driver(db_session, phone="+525512340094")
    headers = auth_headers(operator_token)

    listed = await client.get("/api/v1/drivers", headers=headers)
    assert listed.status_code == 200
    assert any(d["id"] == str(driver.id) for d in listed.json())

    fetched = await client.get(f"/api/v1/drivers/{driver.id}", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json()["phone"] == "+525512340094"


async def test_get_unknown_driver_is_404(client, db_session):
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)

    response = await client.get(
        "/api/v1/drivers/00000000-0000-0000-0000-000000000000",
        headers=auth_headers(operator_token),
    )
    assert response.status_code == 404


async def test_operator_can_deactivate_driver(client, db_session):
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    driver, _ = await make_driver(db_session)

    response = await client.patch(
        f"/api/v1/drivers/{driver.id}",
        json={"status": "inactivo"},
        headers=auth_headers(operator_token),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "inactivo"
