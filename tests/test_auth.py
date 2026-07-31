import app.core.otp as otp_service
from app.config import settings
from app.core.security import hash_password
from app.models import User, UserRole
from tests.factories import DEFAULT_PASSWORD, auth_headers, make_driver, make_staff_user
from tests.test_sms import _FakeAsyncClient, _FakeResponse


async def test_login_success(client, db_session):
    user, _ = await make_staff_user(db_session, role=UserRole.OPERATOR, email="op1@flotilla.mx")

    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "op1@flotilla.mx", "password": DEFAULT_PASSWORD},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["access_token"]
    assert body["refresh_token"]


async def test_login_wrong_password(client, db_session):
    await make_staff_user(db_session, email="op2@flotilla.mx")

    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "op2@flotilla.mx", "password": "no-es-esta"},
    )
    assert response.status_code == 401


async def test_login_rejects_password_over_bcrypt_byte_limit(client, db_session):
    """bcrypt trunca en silencio todo lo que pase de 72 bytes: dos contraseñas
    que compartan ese prefijo se autentican igual. Se rechaza con 422 antes de
    llegar a password_hash, en vez de dejar que ese truncamiento pase inadvertido."""
    await make_staff_user(db_session, email="op-largo@flotilla.mx")

    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "op-largo@flotilla.mx", "password": "x" * 73},
    )
    assert response.status_code == 422


async def test_driver_cannot_login_with_password(client, db_session):
    """Los choferes entran con teléfono + OTP; /login los rechaza aunque la
    contraseña sea correcta, para que no quede una ruta alterna sin OTP."""
    db_session.add(
        User(
            email="chofer-con-password@flotilla.mx",
            password_hash=hash_password(DEFAULT_PASSWORD),
            role=UserRole.DRIVER,
        )
    )
    await db_session.flush()

    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "chofer-con-password@flotilla.mx", "password": DEFAULT_PASSWORD},
    )
    assert response.status_code == 403


async def test_otp_flow_issues_tokens(client, db_session, monkeypatch):
    sent_codes: list[tuple[str, str]] = []

    async def _fake_send_sms(phone: str, code: str) -> None:
        sent_codes.append((phone, code))

    monkeypatch.setattr(otp_service, "send_sms", _fake_send_sms)

    driver, _ = await make_driver(db_session, phone="+525511110001")

    request_resp = await client.post(
        "/api/v1/auth/otp/request", json={"phone": "+525511110001"}
    )
    assert request_resp.status_code == 200
    assert len(sent_codes) == 1
    phone, code = sent_codes[0]
    assert phone == "+525511110001"

    verify_resp = await client.post(
        "/api/v1/auth/otp/verify", json={"phone": "+525511110001", "code": code}
    )
    assert verify_resp.status_code == 200
    assert verify_resp.json()["access_token"]


async def test_otp_verify_wrong_code_rejected(client, db_session, monkeypatch):
    async def _noop_send_sms(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(otp_service, "send_sms", _noop_send_sms)
    await make_driver(db_session, phone="+525511110002")

    await client.post("/api/v1/auth/otp/request", json={"phone": "+525511110002"})
    response = await client.post(
        "/api/v1/auth/otp/verify", json={"phone": "+525511110002", "code": "000000"}
    )
    assert response.status_code == 401


async def test_otp_request_survives_sms_delivery_failure(client, db_session, monkeypatch):
    """Si Twilio falla, el endpoint debe seguir respondiendo el mismo mensaje
    genérico: reflejar el error distinguiría un teléfono registrado (Twilio
    lo intentó y falló) de uno inexistente (nunca se intenta), rompiendo el
    diseño anti-enumeración del endpoint."""
    monkeypatch.setattr(settings, "SMS_PROVIDER", "twilio")
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "ACxxxxxxxxxxxxxxxx")
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "secret-token")
    monkeypatch.setattr(settings, "TWILIO_FROM_NUMBER", "+10000000000")
    monkeypatch.setattr(otp_service.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.next_response = _FakeResponse(500, {"message": "Twilio caído"})

    await make_driver(db_session, phone="+525511110009")

    response = await client.post(
        "/api/v1/auth/otp/request", json={"phone": "+525511110009"}
    )
    assert response.status_code == 200
    assert "registrado" in response.json()["detail"]


async def test_refresh_rotates_and_revokes_previous_token(client, db_session):
    await make_staff_user(db_session, email="op3@flotilla.mx")
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "op3@flotilla.mx", "password": DEFAULT_PASSWORD},
    )
    first_refresh = login.json()["refresh_token"]

    refreshed = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": first_refresh}
    )
    assert refreshed.status_code == 200

    reused = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": first_refresh}
    )
    assert reused.status_code == 401


async def test_logout_revokes_refresh_token(client, db_session):
    _, token = await make_staff_user(db_session, email="op4@flotilla.mx")
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "op4@flotilla.mx", "password": DEFAULT_PASSWORD},
    )
    refresh_token = login.json()["refresh_token"]
    access_token = login.json()["access_token"]

    logout = await client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": refresh_token},
        headers=auth_headers(access_token),
    )
    assert logout.status_code == 200

    reused = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert reused.status_code == 401


async def test_login_locks_after_max_failed_attempts(client, db_session):
    await make_staff_user(db_session, email="bruteforce1@flotilla.mx")

    for _ in range(settings.LOGIN_MAX_ATTEMPTS):
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": "bruteforce1@flotilla.mx", "password": "contraseña-incorrecta"},
        )
        assert response.status_code == 401

    locked = await client.post(
        "/api/v1/auth/login",
        json={"email": "bruteforce1@flotilla.mx", "password": "contraseña-incorrecta"},
    )
    assert locked.status_code == 429

    # Bloqueado incluso con la contraseña correcta: el bloqueo es del email,
    # no un simple "N contraseñas malas seguidas".
    still_locked = await client.post(
        "/api/v1/auth/login",
        json={"email": "bruteforce1@flotilla.mx", "password": DEFAULT_PASSWORD},
    )
    assert still_locked.status_code == 429


async def test_successful_login_resets_failure_counter(client, db_session):
    await make_staff_user(db_session, email="no-bruteforceado@flotilla.mx")

    for _ in range(settings.LOGIN_MAX_ATTEMPTS - 1):
        await client.post(
            "/api/v1/auth/login",
            json={"email": "no-bruteforceado@flotilla.mx", "password": "contraseña-mala"},
        )

    success = await client.post(
        "/api/v1/auth/login",
        json={"email": "no-bruteforceado@flotilla.mx", "password": DEFAULT_PASSWORD},
    )
    assert success.status_code == 200

    # El contador se reinició: un fallo aislado después no debería bloquear.
    after_reset = await client.post(
        "/api/v1/auth/login",
        json={"email": "no-bruteforceado@flotilla.mx", "password": "contraseña-mala"},
    )
    assert after_reset.status_code == 401


async def test_login_throttle_does_not_reveal_unknown_email(client, db_session):
    """El bloqueo debe verse igual exista o no la cuenta: si un email
    inexistente se bloqueara distinto que uno real, eso delataría cuáles
    emails están registrados."""
    email = "esta-cuenta-no-existe@flotilla.mx"
    for _ in range(settings.LOGIN_MAX_ATTEMPTS):
        response = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": "cualquiera"}
        )
        assert response.status_code == 401

    locked = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "cualquiera"}
    )
    assert locked.status_code == 429
