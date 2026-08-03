"""Pruebas del envío de push notifications, aisladas de la ruta HTTP.

Mismo patrón que test_sms.py: se monkeypatchea httpx.AsyncClient en vez de
golpear el servicio de push real de Expo.
"""

import httpx

import app.core.push as push_service


class _FakeResponse:
    def __init__(self, status_code: int, text: str = "ok"):
        self.status_code = status_code
        self.text = text


class _FakeAsyncClient:
    """Doble de httpx.AsyncClient: registra la última llamada y devuelve
    `next_response` (fijado por cada prueba antes de invocar send_push_notification)."""

    next_response: _FakeResponse
    last_call: dict | None = None

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *exc_info) -> None:
        return None

    async def post(self, url: str, json=None, headers=None) -> _FakeResponse:
        _FakeAsyncClient.last_call = {"url": url, "json": json, "headers": headers}
        return _FakeAsyncClient.next_response


async def test_send_push_notification_posts_expected_payload(monkeypatch):
    monkeypatch.setattr(push_service.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.next_response = _FakeResponse(200)

    await push_service.send_push_notification(
        "ExponentPushToken[abc123]",
        title="Nuevo viaje",
        body="Cerca de ti",
        data={"type": "trip_offer", "trip_id": "11111111-1111-1111-1111-111111111111"},
    )

    call = _FakeAsyncClient.last_call
    assert call["url"] == "https://exp.host/--/api/v2/push/send"
    assert call["json"]["to"] == "ExponentPushToken[abc123]"
    assert call["json"]["title"] == "Nuevo viaje"
    assert call["json"]["data"]["type"] == "trip_offer"


async def test_send_push_notification_does_not_raise_on_rejection(monkeypatch):
    """Un push es un complemento del WebSocket, no el camino principal —
    Expo rechazando un token vencido no debe tumbar el despacho del viaje."""
    monkeypatch.setattr(push_service.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.next_response = _FakeResponse(400, "DeviceNotRegistered")

    await push_service.send_push_notification(
        "ExponentPushToken[vencido]", title="x", body="y", data={}
    )


async def test_send_push_notification_does_not_raise_on_network_error(monkeypatch):
    class _BrokenClient(_FakeAsyncClient):
        async def post(self, url: str, json=None, headers=None):
            raise httpx.ConnectError("no se pudo conectar")

    monkeypatch.setattr(push_service.httpx, "AsyncClient", _BrokenClient)

    await push_service.send_push_notification(
        "ExponentPushToken[abc123]", title="x", body="y", data={}
    )
