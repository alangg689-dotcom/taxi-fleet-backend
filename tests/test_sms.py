"""Pruebas del envío de OTP por Twilio, aisladas de la ruta HTTP.

Se monkeypatchea httpx.AsyncClient en vez de golpear la API real de Twilio:
no hay credenciales de verdad en este entorno y no queremos que la suite
dependa de un servicio externo.
"""

import httpx
import pytest

import app.core.otp as otp_service
from app.config import settings


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)
        self.content = b"1"

    def json(self) -> dict:
        return self._payload


class _FakeAsyncClient:
    """Doble de httpx.AsyncClient: registra la última llamada y devuelve
    `next_response` (fijado por cada prueba antes de invocar send_sms)."""

    next_response: _FakeResponse
    last_call: dict | None = None

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *exc_info) -> None:
        return None

    async def post(self, url: str, auth=None, data=None) -> _FakeResponse:
        _FakeAsyncClient.last_call = {"url": url, "auth": auth, "data": data}
        return _FakeAsyncClient.next_response


@pytest.fixture
def twilio_settings(monkeypatch):
    monkeypatch.setattr(settings, "SMS_PROVIDER", "twilio")
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "ACxxxxxxxxxxxxxxxx")
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "secret-token")
    monkeypatch.setattr(settings, "TWILIO_FROM_NUMBER", "+10000000000")
    monkeypatch.setattr(otp_service.httpx, "AsyncClient", _FakeAsyncClient)


async def test_send_sms_via_twilio_posts_expected_payload(twilio_settings):
    _FakeAsyncClient.next_response = _FakeResponse(201, {"sid": "SM123"})

    await otp_service.send_sms("+525512340001", "654321")

    call = _FakeAsyncClient.last_call
    assert call["url"].endswith("/Accounts/ACxxxxxxxxxxxxxxxx/Messages.json")
    assert call["auth"] == ("ACxxxxxxxxxxxxxxxx", "secret-token")
    assert call["data"]["To"] == "+525512340001"
    assert call["data"]["From"] == "+10000000000"
    assert "654321" in call["data"]["Body"]


async def test_send_sms_via_twilio_error_raises_delivery_error(twilio_settings):
    _FakeAsyncClient.next_response = _FakeResponse(
        400, {"message": "The 'To' number is not a valid phone number."}
    )

    with pytest.raises(otp_service.SMSDeliveryError):
        await otp_service.send_sms("+525512340001", "654321")


async def test_send_sms_via_twilio_network_error_raises_delivery_error(
    twilio_settings, monkeypatch
):
    class _BrokenClient(_FakeAsyncClient):
        async def post(self, url: str, auth=None, data=None):
            raise httpx.ConnectError("no se pudo conectar")

    monkeypatch.setattr(otp_service.httpx, "AsyncClient", _BrokenClient)

    with pytest.raises(otp_service.SMSDeliveryError):
        await otp_service.send_sms("+525512340001", "654321")


async def test_send_sms_console_provider_does_not_touch_network(monkeypatch, capsys):
    monkeypatch.setattr(settings, "SMS_PROVIDER", "console")

    await otp_service.send_sms("+525512340001", "654321")

    assert "654321" in capsys.readouterr().out
