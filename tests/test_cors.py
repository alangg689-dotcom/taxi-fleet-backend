from app.config import Settings
from app.models import UserRole
from tests.factories import auth_headers, make_staff_user


def test_cors_origins_wildcard_default():
    assert Settings(CORS_ORIGINS="*").cors_origins == ["*"]


def test_cors_origins_parses_comma_separated_list():
    settings = Settings(CORS_ORIGINS="https://a.com, https://b.com")
    assert settings.cors_origins == ["https://a.com", "https://b.com"]


def test_cors_origins_single_domain():
    assert Settings(CORS_ORIGINS="https://dashboard.flotilla.mx").cors_origins == [
        "https://dashboard.flotilla.mx"
    ]


async def test_preflight_allows_wildcard_without_credentials(client):
    """Con CORS_ORIGINS=* (el default de estas pruebas) el preflight responde
    el wildcard tal cual — no un origin reflejado — precisamente porque no hay
    Access-Control-Allow-Credentials: la API no usa cookies, solo Bearer
    tokens, así que ese header no debería aparecer nunca. Reflejar el origin
    en vez del wildcard literal es lo que hace Starlette cuando sí hay
    credenciales, que es el anti-patrón que este cambio evita."""
    response = await client.options(
        "/health",
        headers={
            "Origin": "https://cualquier-sitio.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in response.headers


async def test_total_count_header_is_exposed_for_browser_clients(client, db_session):
    """X-Total-Count (paginación) va en toda respuesta de listado, pero sin
    Access-Control-Expose-Headers el navegador lo descarta antes de que el
    JS del dashboard pueda leerlo con fetch()."""
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)

    response = await client.get(
        "/api/v1/vehicles",
        headers={**auth_headers(operator_token), "Origin": "https://dashboard.flotilla.mx"},
    )
    assert response.status_code == 200
    assert "X-Total-Count" in response.headers["access-control-expose-headers"]
