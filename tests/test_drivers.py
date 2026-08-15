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
            "numeral": "R101",
        },
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["phone"] == "+525512340099"
    assert body["full_name"] == "Juan Pérez"
    assert body["numeral"] == "R101"
    assert body["status"] == "activo"
    assert len(body["pin"]) == 6
    assert body["pin"].isdigit()
    assert body["has_pin"] is True

    # El PIN funciona de inmediato para /auth/driver-login.
    login = await client.post(
        "/api/v1/auth/driver-login",
        json={"phone": "+525512340099", "pin": body["pin"]},
    )
    assert login.status_code == 200


async def test_driver_without_pin_shows_has_pin_false(client, db_session):
    """Los migrados del login por OTP nacen con pin_hash NULL — el
    dashboard usa has_pin para saber a quién le falta habilitar."""
    await make_driver(db_session, phone="+525512340095")
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)

    response = await client.get("/api/v1/drivers", headers=auth_headers(operator_token))
    driver_row = next(d for d in response.json() if d["phone"] == "+525512340095")
    assert driver_row["has_pin"] is False


async def test_operator_cannot_create_driver(client, db_session):
    """Alta de choferes es solo para admin, igual que alta de unidades."""
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)

    response = await client.post(
        "/api/v1/drivers",
        json={
            "phone": "+525512340098",
            "full_name": "Alguien",
            "license_number": "LIC-99001",
            "numeral": "R102",
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
            "numeral": "R103",
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
            "numeral": "R104",
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
            "numeral": "R105",
        },
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 409


async def test_numeral_is_required_on_create(client, db_session):
    """Es como la operadora nombra al chofer por radio: dar de alta uno sin
    numeral dejaría una unidad imposible de llamar en el mapa."""
    _, admin_token = await make_staff_user(db_session, role=UserRole.ADMIN)

    response = await client.post(
        "/api/v1/drivers",
        json={
            "phone": "+525512340092",
            "full_name": "Sin numeral",
            "license_number": "LIC-99004",
        },
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 422


async def test_duplicate_numeral_is_conflict(client, db_session):
    _, admin_token = await make_staff_user(db_session, role=UserRole.ADMIN)
    await make_driver(db_session, numeral="R777")

    response = await client.post(
        "/api/v1/drivers",
        json={
            "phone": "+525512340091",
            "full_name": "Otro",
            "license_number": "LIC-99005",
            "numeral": "R777",
        },
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 409


async def test_numeral_is_normalized_to_uppercase(client, db_session):
    """"r18" y "R18" son el mismo numeral para la operadora; sin normalizar
    serían dos distintos para el índice único."""
    _, admin_token = await make_staff_user(db_session, role=UserRole.ADMIN)

    created = await client.post(
        "/api/v1/drivers",
        json={
            "phone": "+525512340090",
            "full_name": "Minúsculas",
            "license_number": "LIC-99006",
            "numeral": " r18 ",
        },
        headers=auth_headers(admin_token),
    )
    assert created.status_code == 201
    assert created.json()["numeral"] == "R18"

    duplicate = await client.post(
        "/api/v1/drivers",
        json={
            "phone": "+525512340089",
            "full_name": "Mayúsculas",
            "license_number": "LIC-99007",
            "numeral": "R18",
        },
        headers=auth_headers(admin_token),
    )
    assert duplicate.status_code == 409


async def test_operator_can_assign_numeral_to_migrated_driver(client, db_session):
    """Los choferes anteriores a la 0011 nacieron con numeral NULL, igual que
    con pin_hash: el operador se los asigna después."""
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    driver, _ = await make_driver(db_session)
    driver.numeral = None
    await db_session.flush()

    response = await client.patch(
        f"/api/v1/drivers/{driver.id}",
        json={"numeral": "R20"},
        headers=auth_headers(operator_token),
    )
    assert response.status_code == 200
    assert response.json()["numeral"] == "R20"


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


async def test_operator_can_regenerate_driver_pin(client, db_session):
    """A diferencia de revocar acceso (solo admin), regenerar el PIN es
    operación diaria — el chofer lo olvida, o hay que dárselo por primera
    vez a uno migrado del login por OTP."""
    driver, old_pin = await make_driver(db_session, phone="+525512340094", with_pin=True)
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)

    response = await client.post(
        f"/api/v1/drivers/{driver.id}/pin", headers=auth_headers(operator_token)
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["pin"]) == 6
    assert body["pin"] != old_pin

    old_login = await client.post(
        "/api/v1/auth/driver-login", json={"phone": "+525512340094", "pin": old_pin}
    )
    assert old_login.status_code == 401

    new_login = await client.post(
        "/api/v1/auth/driver-login", json={"phone": "+525512340094", "pin": body["pin"]}
    )
    assert new_login.status_code == 200


async def test_driver_cannot_regenerate_own_pin(client, db_session):
    """Lo asigna el operador — el chofer no puede dárselo a sí mismo."""
    driver, driver_token = await make_driver(db_session)

    response = await client.post(
        f"/api/v1/drivers/{driver.id}/pin", headers=auth_headers(driver_token)
    )
    assert response.status_code == 403


async def test_regenerate_pin_unknown_driver_is_404(client, db_session):
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)

    response = await client.post(
        "/api/v1/drivers/00000000-0000-0000-0000-000000000000/pin",
        headers=auth_headers(operator_token),
    )
    assert response.status_code == 404


async def test_deactivated_driver_cannot_login_even_with_correct_pin(
    client, db_session
):
    """Al revocar el acceso (User.is_active=False), el PIN correcto ya no
    debe bastar para entrar — mismo 401 genérico que un teléfono
    inexistente o un PIN incorrecto, para no delatar la diferencia."""
    _, admin_token = await make_staff_user(db_session, role=UserRole.ADMIN)
    driver, pin = await make_driver(db_session, phone="+525512340093", with_pin=True)

    await client.post(
        f"/api/v1/drivers/{driver.id}/deactivate", headers=auth_headers(admin_token)
    )

    response = await client.post(
        "/api/v1/auth/driver-login", json={"phone": "+525512340093", "pin": pin}
    )
    assert response.status_code == 401


async def test_driver_can_register_push_token(client, db_session):
    driver, driver_token = await make_driver(db_session)

    response = await client.post(
        "/api/v1/drivers/me/push-token",
        json={"push_token": "ExponentPushToken[abc123]"},
        headers=auth_headers(driver_token),
    )
    assert response.status_code == 204

    await db_session.refresh(driver)
    assert driver.push_token == "ExponentPushToken[abc123]"


async def test_staff_cannot_register_push_token(client, db_session):
    """Es una acción del chofer sobre su propio perfil; un operador no tiene
    un perfil de chofer al que registrarle un token."""
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)

    response = await client.post(
        "/api/v1/drivers/me/push-token",
        json={"push_token": "ExponentPushToken[abc123]"},
        headers=auth_headers(operator_token),
    )
    assert response.status_code == 403
