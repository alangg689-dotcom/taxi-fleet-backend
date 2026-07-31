import app.core.otp as otp_service
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


async def test_list_drivers_paginates_with_total_count_header(client, db_session):
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    headers = auth_headers(operator_token)

    for i in range(5):
        await make_driver(db_session, phone=f"+52551122{i:04d}")

    first_page = await client.get("/api/v1/drivers?limit=2&offset=0", headers=headers)
    assert first_page.status_code == 200
    assert len(first_page.json()) == 2
    assert int(first_page.headers["X-Total-Count"]) >= 5

    second_page = await client.get("/api/v1/drivers?limit=2&offset=2", headers=headers)
    assert second_page.status_code == 200
    first_ids = {d["id"] for d in first_page.json()}
    second_ids = {d["id"] for d in second_page.json()}
    assert first_ids.isdisjoint(second_ids)


async def test_operator_can_set_operational_status(client, db_session):
    """DriverStatus (activo/inactivo operativo, ej. vacaciones) es distinto de
    User.is_active (login revocado): esto solo toca el primero."""
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    driver, _ = await make_driver(db_session)

    response = await client.patch(
        f"/api/v1/drivers/{driver.id}",
        json={"status": "inactivo"},
        headers=auth_headers(operator_token),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "inactivo"
    assert response.json()["is_active"] is True


async def test_admin_can_revoke_and_restore_driver_access(client, db_session):
    _, admin_token = await make_staff_user(db_session, role=UserRole.ADMIN)
    driver, _ = await make_driver(db_session)
    headers = auth_headers(admin_token)

    deactivated = await client.post(
        f"/api/v1/drivers/{driver.id}/deactivate", headers=headers
    )
    assert deactivated.status_code == 200
    assert deactivated.json()["is_active"] is False

    reactivated = await client.post(
        f"/api/v1/drivers/{driver.id}/reactivate", headers=headers
    )
    assert reactivated.status_code == 200
    assert reactivated.json()["is_active"] is True


async def test_operator_cannot_revoke_driver_access(client, db_session):
    """Revocar acceso es más sensible que activar/inactivar operativamente:
    solo admin, igual que la alta."""
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    driver, _ = await make_driver(db_session)

    response = await client.post(
        f"/api/v1/drivers/{driver.id}/deactivate",
        headers=auth_headers(operator_token),
    )
    assert response.status_code == 403


async def test_deactivated_driver_otp_request_yields_no_code(
    client, db_session, monkeypatch
):
    """Al revocar el acceso, pedir OTP para ese teléfono no debe generar
    código — el mismo camino silencioso que un teléfono inexistente, para no
    delatar la diferencia."""
    sent_codes: list[tuple[str, str]] = []

    async def _fake_send_sms(phone: str, code: str) -> None:
        sent_codes.append((phone, code))

    monkeypatch.setattr(otp_service, "send_sms", _fake_send_sms)

    _, admin_token = await make_staff_user(db_session, role=UserRole.ADMIN)
    driver, _ = await make_driver(db_session, phone="+525512340093")

    await client.post(
        f"/api/v1/drivers/{driver.id}/deactivate", headers=auth_headers(admin_token)
    )

    response = await client.post(
        "/api/v1/auth/otp/request", json={"phone": "+525512340093"}
    )
    assert response.status_code == 200
    assert sent_codes == []
