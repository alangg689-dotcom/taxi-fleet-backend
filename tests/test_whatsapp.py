"""Pruebas del envío de WhatsApp, aisladas de la ruta HTTP.

Mismo patrón que test_push.py: se monkeypatchea httpx.AsyncClient en vez de
golpear la API real de Twilio.
"""

import httpx

import app.core.whatsapp as whatsapp_service
from app.config import settings


class _FakeResponse:
    def __init__(self, status_code: int, text: str = "ok"):
        self.status_code = status_code
        self.text = text


class _FakeAsyncClient:
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


async def test_send_whatsapp_message_posts_expected_payload(monkeypatch):
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "ACxxxxxxxxxxxxxxxx")
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "secret-token")
    monkeypatch.setattr(settings, "TWILIO_WHATSAPP_FROM", "whatsapp:+10000000000")
    monkeypatch.setattr(whatsapp_service.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.next_response = _FakeResponse(201)

    await whatsapp_service.send_whatsapp_message("+525512340001", "Tu taxi va en camino")

    call = _FakeAsyncClient.last_call
    assert call["url"].endswith("/Accounts/ACxxxxxxxxxxxxxxxx/Messages.json")
    assert call["auth"] == ("ACxxxxxxxxxxxxxxxx", "secret-token")
    assert call["data"]["To"] == "whatsapp:+525512340001"
    assert call["data"]["From"] == "whatsapp:+10000000000"
    assert call["data"]["Body"] == "Tu taxi va en camino"


async def test_send_whatsapp_message_does_not_raise_on_rejection(monkeypatch):
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "ACxxxxxxxxxxxxxxxx")
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "secret-token")
    monkeypatch.setattr(whatsapp_service.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.next_response = _FakeResponse(400, "número inválido")

    await whatsapp_service.send_whatsapp_message("+525512340001", "hola")


async def test_send_whatsapp_message_does_not_raise_on_network_error(monkeypatch):
    class _BrokenClient(_FakeAsyncClient):
        async def post(self, url: str, auth=None, data=None):
            raise httpx.ConnectError("no se pudo conectar")

    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "ACxxxxxxxxxxxxxxxx")
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "secret-token")
    monkeypatch.setattr(whatsapp_service.httpx, "AsyncClient", _BrokenClient)

    await whatsapp_service.send_whatsapp_message("+525512340001", "hola")


async def test_send_whatsapp_message_skips_when_credentials_empty(monkeypatch, caplog):
    import logging

    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "")
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "")
    monkeypatch.setattr(whatsapp_service.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.last_call = None

    await whatsapp_service.send_whatsapp_message("+525512340001", "hola")

    assert _FakeAsyncClient.last_call is None
    assert "TWILIO_ACCOUNT_SID" in caplog.text


async def test_notify_driver_device_key_sends_key_not_folio(monkeypatch):
    sent: list[tuple[str, str]] = []

    async def _fake_send(to_phone: str, body: str) -> None:
        sent.append((to_phone, body))

    monkeypatch.setattr(whatsapp_service, "send_whatsapp_message", _fake_send)

    await whatsapp_service.notify_driver_device_key(
        "6621234567",
        "VZE-123-A",
        "CTM-045",
        "gps-device-key-unico",
    )

    assert len(sent) == 1
    phone, body = sent[0]
    assert phone == "+526621234567"
    assert "gps-device-key-unico" in body
    assert body.endswith("gps-device-key-unico")
    assert "CTM-045" in body
    assert "VZE-123-A" in body
    assert "No es tu Folio ni tu PIN" in body
    assert not body.endswith("CTM-045")


async def test_notify_driver_device_key_skips_without_phone(monkeypatch):
    sent: list[tuple[str, str]] = []

    async def _fake_send(to_phone: str, body: str) -> None:
        sent.append((to_phone, body))

    monkeypatch.setattr(whatsapp_service, "send_whatsapp_message", _fake_send)

    await whatsapp_service.notify_driver_device_key(
        None, "ABC-123", "CTM-001", "secret-key"
    )

    assert sent == []
