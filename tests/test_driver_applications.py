"""Autorregistro de choferes: alta → aprobación → PIN → login → bind.

El folio CTM es ID operativo, nunca credencial. El PIN lo inventa el
chofer; al aprobar no se genera uno. Sin OTP y sin foto de tarjetón.
"""

from pathlib import Path

from app.core.security import hash_token
from app.models import Driver, DriverAccountStatus, UserRole, Vehicle
from tests.factories import auth_headers, make_driver, make_staff_user, make_stand, make_vehicle

_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)

_APPLY = {
    "full_name": "Juan Pérez",
    "phone": "6621234567",
    "email": None,
    "folio_ctm": "CTM-045",
    "license_plate": "VZE-123-A",
    "unit_role": "SHIFT_DRIVER",
}


async def test_happy_path_apply_approve_set_pin_login_bind(client, db_session):
    created = await client.post("/api/v1/driver-applications", json=_APPLY)
    assert created.status_code == 201
    body = created.json()
    assert body["status"] == "PENDING_APPROVAL"
    application_id = body["application_id"]
    assert "pin" not in body

    status_pending = await client.get(
        "/api/v1/driver-applications/status",
        params={"application_id": application_id},
    )
    assert status_pending.status_code == 200
    assert status_pending.json()["status"] == "PENDING_APPROVAL"
    assert status_pending.json()["must_set_pin"] is False

    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    headers = auth_headers(operator_token)

    listed = await client.get(
        "/api/v1/driver-applications",
        params={"status": "PENDING_APPROVAL"},
        headers=headers,
    )
    assert listed.status_code == 200
    assert any(row["id"] == application_id for row in listed.json())

    stand = await make_stand(db_session)
    approved = await client.post(
        f"/api/v1/driver-applications/{application_id}/approve",
        json={"stand_id": str(stand.id), "model": "Nissan Tsuru"},
        headers=headers,
    )
    assert approved.status_code == 200
    approve_body = approved.json()
    assert approve_body["status"] == "ACTIVE"
    assert approve_body["must_set_pin"] is True
    assert approve_body["folio_ctm"] == "CTM-045"
    assert "pin" not in approve_body
    driver_id = approve_body["driver_id"]
    vehicle_id = approve_body["vehicle_id"]
    assert vehicle_id is not None

    driver = await db_session.get(Driver, driver_id)
    assert driver is not None
    assert driver.pin_hash is None
    assert driver.must_set_pin is True
    assert driver.folio_ctm == "CTM-045"
    assert driver.numeral is None

    vehicle = await db_session.get(Vehicle, vehicle_id)
    assert vehicle is not None
    assert vehicle.folio_ctm == "CTM-045"
    assert vehicle.device_key_hash is None
    assert vehicle.device_key_hash != hash_token("CTM-045")

    cannot_login_yet = await client.post(
        "/api/v1/auth/driver-login",
        json={"phone": "6621234567", "pin": "482910"},
    )
    assert cannot_login_yet.status_code == 401

    set_pin = await client.post(
        "/api/v1/auth/driver/set-pin",
        json={
            "phone": "6621234567",
            "folio_ctm": "ctm-045",
            "pin": "482910",
            "pin_confirm": "482910",
        },
    )
    assert set_pin.status_code == 200

    status_ready = await client.get(
        "/api/v1/driver-applications/status",
        params={"application_id": application_id},
    )
    assert status_ready.json()["must_set_pin"] is False

    db_session.expire_all()
    driver = await db_session.get(Driver, driver_id)
    assert driver is not None
    assert driver.must_set_pin is False
    assert driver.pin_hash == hash_token("482910")
    assert driver.pin_hash != hash_token("CTM-045")

    login = await client.post(
        "/api/v1/auth/driver-login",
        json={"phone": "+526621234567", "pin": "482910"},
    )
    assert login.status_code == 200
    assert "refresh_token" not in login.json()
    driver_token = login.json()["access_token"]

    bind = await client.post(
        "/api/v1/driver-devices/bind",
        json={"device_id": "expo-device-abc", "push_token": "ExponentPushToken[xyz]"},
        headers=auth_headers(driver_token),
    )
    assert bind.status_code == 200
    bind_body = bind.json()
    assert bind_body["folio_ctm"] == "CTM-045"
    assert bind_body["device_token"]
    assert bind_body["device_token"] != "CTM-045"
    assert bind_body["device_token"] != "482910"

    await db_session.refresh(driver)
    assert driver.push_token == "ExponentPushToken[xyz]"


async def test_reject_path(client, db_session):
    created = await client.post("/api/v1/driver-applications", json=_APPLY)
    application_id = created.json()["application_id"]
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)

    rejected = await client.post(
        f"/api/v1/driver-applications/{application_id}/reject",
        json={"reason": "Placas no coinciden con el padrón"},
        headers=auth_headers(operator_token),
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "REJECTED"
    assert "pin" not in rejected.json()

    status_out = await client.get(
        "/api/v1/driver-applications/status",
        params={"phone": "6621234567"},
    )
    assert status_out.status_code == 200
    assert status_out.json()["status"] == "REJECTED"
    assert status_out.json()["rejection_reason"] == "Placas no coinciden con el padrón"

    set_pin = await client.post(
        "/api/v1/auth/driver/set-pin",
        json={
            "phone": "6621234567",
            "folio_ctm": "CTM-045",
            "pin": "482910",
            "pin_confirm": "482910",
        },
    )
    assert set_pin.status_code == 401


async def test_folio_is_never_a_credential(client, db_session):
    """Folio CTM no autentica: ni como teléfono, ni como PIN, ni como
    device_token. Tras aprobar, el operador no recibe un PIN inventado."""
    created = await client.post("/api/v1/driver-applications", json=_APPLY)
    application_id = created.json()["application_id"]
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    approved = await client.post(
        f"/api/v1/driver-applications/{application_id}/approve",
        json={},
        headers=auth_headers(operator_token),
    )
    assert approved.status_code == 200
    assert "pin" not in approved.json()
    assert approved.json()["must_set_pin"] is True

    login_folio_as_phone = await client.post(
        "/api/v1/auth/driver-login",
        json={"phone": "CTM-045CTM", "pin": "CTM-045"},
    )
    assert login_folio_as_phone.status_code in {401, 422}

    login_folio_as_pin = await client.post(
        "/api/v1/auth/driver-login",
        json={"phone": "6621234567", "pin": "CTM-045"},
    )
    assert login_folio_as_pin.status_code == 401

    set_pin_without_phone = await client.post(
        "/api/v1/auth/driver/set-pin",
        json={
            "phone": "0000000000",
            "folio_ctm": "CTM-045",
            "pin": "1234",
            "pin_confirm": "1234",
        },
    )
    assert set_pin_without_phone.status_code == 401

    set_pin_wrong_folio = await client.post(
        "/api/v1/auth/driver/set-pin",
        json={
            "phone": "6621234567",
            "folio_ctm": "CTM-999",
            "pin": "1234",
            "pin_confirm": "1234",
        },
    )
    assert set_pin_wrong_folio.status_code == 401


async def test_duplicate_active_phone_is_conflict(client, db_session):
    await make_driver(db_session, phone="6621987654")
    response = await client.post(
        "/api/v1/driver-applications",
        json={**_APPLY, "phone": "6621987654", "folio_ctm": "CTM-099"},
    )
    assert response.status_code == 409


async def test_duplicate_pending_phone_is_conflict(client, db_session):
    first = await client.post("/api/v1/driver-applications", json=_APPLY)
    assert first.status_code == 201
    second = await client.post(
        "/api/v1/driver-applications",
        json={**_APPLY, "folio_ctm": "CTM-046"},
    )
    assert second.status_code == 409


async def test_upload_profile_and_license_only(client, tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))

    profile = await client.post(
        "/api/v1/driver-applications/uploads",
        files={"file": ("rostro.png", _PNG, "image/png")},
        data={"kind": "profile"},
    )
    assert profile.status_code == 200
    assert profile.json()["url"].startswith("/api/v1/uploads/")
    assert profile.json()["kind"] == "profile"
    stored = Path(tmp_path) / Path(profile.json()["url"]).name
    assert stored.is_file()

    fetched = await client.get(profile.json()["url"])
    assert fetched.status_code == 200

    union_card = await client.post(
        "/api/v1/driver-applications/uploads",
        files={"file": ("tarjeton.png", _PNG, "image/png")},
        data={"kind": "union_card"},
    )
    assert union_card.status_code == 422


async def test_staff_required_for_queue(client, db_session):
    created = await client.post("/api/v1/driver-applications", json=_APPLY)
    application_id = created.json()["application_id"]

    listed = await client.get("/api/v1/driver-applications")
    assert listed.status_code == 401

    approve = await client.post(
        f"/api/v1/driver-applications/{application_id}/approve", json={}
    )
    assert approve.status_code == 401


async def test_change_pin_and_force_reset(client, db_session):
    created = await client.post("/api/v1/driver-applications", json=_APPLY)
    application_id = created.json()["application_id"]
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    await client.post(
        f"/api/v1/driver-applications/{application_id}/approve",
        json={},
        headers=auth_headers(operator_token),
    )
    await client.post(
        "/api/v1/auth/driver/set-pin",
        json={
            "phone": "6621234567",
            "folio_ctm": "CTM-045",
            "pin": "482910",
            "pin_confirm": "482910",
        },
    )

    changed = await client.post(
        "/api/v1/auth/driver/change-pin",
        json={
            "phone": "6621234567",
            "current_pin": "482910",
            "new_pin": "119933",
            "new_pin_confirm": "119933",
        },
    )
    assert changed.status_code == 200

    old_login = await client.post(
        "/api/v1/auth/driver-login",
        json={"phone": "6621234567", "pin": "482910"},
    )
    assert old_login.status_code == 401

    new_login = await client.post(
        "/api/v1/auth/driver-login",
        json={"phone": "6621234567", "pin": "119933"},
    )
    assert new_login.status_code == 200

    driver, _ = await make_driver(db_session, phone="6621112233", with_pin=True)
    # El de make_driver no es el de la solicitud; usamos el aprobado.
    from sqlalchemy import select

    from app.models import User

    user = (
        await db_session.execute(select(User).where(User.phone == "6621234567"))
    ).scalar_one()
    approved_driver = (
        await db_session.execute(select(Driver).where(Driver.user_id == user.id))
    ).scalar_one()

    reset = await client.post(
        f"/api/v1/drivers/{approved_driver.id}/force-reset-pin",
        headers=auth_headers(operator_token),
    )
    assert reset.status_code == 200
    assert reset.json()["must_set_pin"] is True
    assert reset.json()["has_pin"] is False
    assert "pin" not in reset.json()

    blocked = await client.post(
        "/api/v1/auth/driver-login",
        json={"phone": "6621234567", "pin": "119933"},
    )
    assert blocked.status_code == 401

    again = await client.post(
        "/api/v1/auth/driver/set-pin",
        json={
            "phone": "6621234567",
            "folio_ctm": "CTM-045",
            "pin": "334455",
            "pin_confirm": "334455",
        },
    )
    assert again.status_code == 200

    restored = await client.post(
        "/api/v1/auth/driver-login",
        json={"phone": "6621234567", "pin": "334455"},
    )
    assert restored.status_code == 200
    # El factory de arriba no debe interferir; silencia unused.
    assert driver.id != approved_driver.id


async def test_bind_requires_pin_and_revoke_device(client, db_session):
    created = await client.post("/api/v1/driver-applications", json=_APPLY)
    application_id = created.json()["application_id"]
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    approved = await client.post(
        f"/api/v1/driver-applications/{application_id}/approve",
        json={},
        headers=auth_headers(operator_token),
    )
    driver_id = approved.json()["driver_id"]

    # Token JWT de chofer sin PIN: bind debe fallar.
    from app.core.security import create_access_token
    from app.models import User
    from sqlalchemy import select

    user = (
        await db_session.execute(select(User).where(User.phone == "6621234567"))
    ).scalar_one()
    premature_token = create_access_token(str(user.id), UserRole.DRIVER.value)
    premature = await client.post(
        "/api/v1/driver-devices/bind",
        json={"device_id": "hw-1"},
        headers=auth_headers(premature_token),
    )
    assert premature.status_code == 403

    await client.post(
        "/api/v1/auth/driver/set-pin",
        json={
            "phone": "6621234567",
            "folio_ctm": "CTM-045",
            "pin": "482910",
            "pin_confirm": "482910",
        },
    )
    login = await client.post(
        "/api/v1/auth/driver-login",
        json={"phone": "6621234567", "pin": "482910"},
    )
    bind = await client.post(
        "/api/v1/driver-devices/bind",
        json={"device_id": "hw-1"},
        headers=auth_headers(login.json()["access_token"]),
    )
    assert bind.status_code == 200
    first_token = bind.json()["device_token"]

    rebind = await client.post(
        "/api/v1/driver-devices/bind",
        json={"device_id": "hw-2"},
        headers=auth_headers(login.json()["access_token"]),
    )
    assert rebind.status_code == 200
    assert rebind.json()["device_token"] != first_token

    revoked = await client.post(
        f"/api/v1/drivers/{driver_id}/devices/revoke",
        headers=auth_headers(operator_token),
    )
    assert revoked.status_code == 200
    assert revoked.json()["revoked"] == 1


async def test_approve_associates_existing_vehicle_by_plate(client, db_session):
    stand = await make_stand(db_session)
    vehicle = await make_vehicle(db_session, plate="VZE-123-A", stand_id=stand.id)
    created = await client.post("/api/v1/driver-applications", json=_APPLY)
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    approved = await client.post(
        f"/api/v1/driver-applications/{created.json()['application_id']}/approve",
        json={},
        headers=auth_headers(operator_token),
    )
    assert approved.status_code == 200
    assert approved.json()["vehicle_id"] == str(vehicle.id)
    # Misma sesión que el endpoint (el override de get_db no hace commit):
    # el objeto del identity map es el que se mutó al aprobar.
    assert vehicle.folio_ctm == "CTM-045"
    assert vehicle.device_key_hash is None


async def test_status_by_phone(client, db_session):
    created = await client.post("/api/v1/driver-applications", json=_APPLY)
    response = await client.get(
        "/api/v1/driver-applications/status", params={"phone": "+52 662 123 4567"}
    )
    assert response.status_code == 200
    assert response.json()["application_id"] == created.json()["application_id"]
    assert response.json()["folio_ctm"] == "CTM-045"


async def test_pin_mismatch_is_422(client, db_session):
    created = await client.post("/api/v1/driver-applications", json=_APPLY)
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    await client.post(
        f"/api/v1/driver-applications/{created.json()['application_id']}/approve",
        json={},
        headers=auth_headers(operator_token),
    )
    response = await client.post(
        "/api/v1/auth/driver/set-pin",
        json={
            "phone": "6621234567",
            "folio_ctm": "CTM-045",
            "pin": "482910",
            "pin_confirm": "000000",
        },
    )
    assert response.status_code == 422
