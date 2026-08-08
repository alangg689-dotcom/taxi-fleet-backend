"""Pruebas de POST/PATCH/GET /stands (spec-sitios-y-fila-v2.md, sección 9,
la parte de trazar sitios reales sobre los placeholders del paso 3).

Coordenadas lejos del Zócalo (default de make_stand/make_vehicle) a
propósito, para que estas pruebas nunca se encimen por accidente con un
sitio de otra fábrica dentro de la misma prueba.
"""

from app.models import UserRole
from tests.factories import auth_headers, make_staff_user

_AREA = (20.5, -101.0)


def _square(center=_AREA, *, half_side=0.001) -> dict:
    lat, lng = center
    d = half_side
    return {
        "type": "Polygon",
        "coordinates": [[
            [lng - d, lat - d], [lng + d, lat - d],
            [lng + d, lat + d], [lng - d, lat + d],
            [lng - d, lat - d],
        ]],
    }


async def test_admin_can_create_stand(client, db_session):
    _, admin_token = await make_staff_user(db_session, role=UserRole.ADMIN)

    response = await client.post(
        "/api/v1/stands",
        json={"name": "Sitio Centro", "polygon_geojson": _square()},
        headers=auth_headers(admin_token),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Sitio Centro"
    assert body["is_placeholder"] is False
    assert body["active"] is True
    assert body["polygon_geojson"]["type"] == "Polygon"
    assert round(body["center_lat"], 2) == round(_AREA[0], 2)
    assert round(body["center_lng"], 2) == round(_AREA[1], 2)
    assert body["polygon_buffer_meters"] > 0  # se aplicó la holgura default


async def test_operator_cannot_create_stand(client, db_session):
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)

    response = await client.post(
        "/api/v1/stands",
        json={"name": "Sitio Centro", "polygon_geojson": _square()},
        headers=auth_headers(operator_token),
    )
    assert response.status_code == 403


async def test_create_stand_rejects_non_polygon_geojson(client, db_session):
    _, admin_token = await make_staff_user(db_session, role=UserRole.ADMIN)

    response = await client.post(
        "/api/v1/stands",
        json={
            "name": "Sitio inválido",
            "polygon_geojson": {"type": "Point", "coordinates": [-101.0, 20.5]},
        },
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 422


async def test_create_stand_overlapping_active_stand_is_409(client, db_session):
    _, admin_token = await make_staff_user(db_session, role=UserRole.ADMIN)
    headers = auth_headers(admin_token)
    await client.post(
        "/api/v1/stands", json={"name": "Sitio A", "polygon_geojson": _square()}, headers=headers
    )

    response = await client.post(
        "/api/v1/stands",
        json={"name": "Sitio B (se encima)", "polygon_geojson": _square()},
        headers=headers,
    )
    assert response.status_code == 409


async def test_create_stand_ignores_overlap_with_inactive_stand(client, db_session):
    _, admin_token = await make_staff_user(db_session, role=UserRole.ADMIN)
    headers = auth_headers(admin_token)
    await client.post(
        "/api/v1/stands",
        json={"name": "Sitio desactivado", "polygon_geojson": _square(), "active": False},
        headers=headers,
    )

    response = await client.post(
        "/api/v1/stands",
        json={"name": "Sitio nuevo", "polygon_geojson": _square()},
        headers=headers,
    )
    assert response.status_code == 201


async def test_get_stand_detail(client, db_session):
    _, admin_token = await make_staff_user(db_session, role=UserRole.ADMIN)
    headers = auth_headers(admin_token)
    created = await client.post(
        "/api/v1/stands", json={"name": "Sitio Centro", "polygon_geojson": _square()}, headers=headers
    )
    stand_id = created.json()["id"]

    response = await client.get(f"/api/v1/stands/{stand_id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["name"] == "Sitio Centro"


async def test_get_unknown_stand_is_404(client, db_session):
    _, admin_token = await make_staff_user(db_session, role=UserRole.ADMIN)
    response = await client.get(
        "/api/v1/stands/00000000-0000-0000-0000-000000000000",
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 404


async def test_patching_polygon_clears_placeholder_flag(client, db_session):
    """El caso real que motiva esto: reemplazar uno de los 6 placeholders
    del paso 3 con su polígono de verdad."""
    _, admin_token = await make_staff_user(db_session, role=UserRole.ADMIN)
    headers = auth_headers(admin_token)
    created = await client.post(
        "/api/v1/stands",
        json={"name": "Sitio placeholder", "polygon_geojson": _square((20.5, -101.0))},
        headers=headers,
    )
    stand_id = created.json()["id"]
    # No hay endpoint para forzar is_placeholder=true desde la API (solo lo
    # hace la migración 0009) — se fuerza aquí directo en la base para
    # simular ese estado y probar que PATCH lo apaga.
    from sqlalchemy import text
    await db_session.execute(
        text("UPDATE stands SET is_placeholder = true WHERE id = :id"), {"id": stand_id}
    )
    await db_session.commit()

    response = await client.patch(
        f"/api/v1/stands/{stand_id}",
        json={"polygon_geojson": _square((20.5, -101.02))},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["is_placeholder"] is False


async def test_patch_only_touches_given_fields(client, db_session):
    _, admin_token = await make_staff_user(db_session, role=UserRole.ADMIN)
    headers = auth_headers(admin_token)
    created = await client.post(
        "/api/v1/stands",
        json={"name": "Sitio original", "polygon_geojson": _square(), "still_seconds": 60},
        headers=headers,
    )
    stand_id = created.json()["id"]

    response = await client.patch(
        f"/api/v1/stands/{stand_id}", json={"active": False}, headers=headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["active"] is False
    assert body["name"] == "Sitio original"  # no se tocó
    assert body["still_seconds"] == 60  # no se tocó


async def test_patch_overlap_check_excludes_self(client, db_session):
    """Re-trazar el mismo sitio (polígono casi idéntico) no debe chocar
    contra sí mismo."""
    _, admin_token = await make_staff_user(db_session, role=UserRole.ADMIN)
    headers = auth_headers(admin_token)
    created = await client.post(
        "/api/v1/stands", json={"name": "Sitio Centro", "polygon_geojson": _square()}, headers=headers
    )
    stand_id = created.json()["id"]

    response = await client.patch(
        f"/api/v1/stands/{stand_id}",
        json={"polygon_geojson": _square(half_side=0.0012)},  # un poco más grande, mismo lugar
        headers=headers,
    )
    assert response.status_code == 200


async def test_patch_overlapping_another_stand_is_409(client, db_session):
    _, admin_token = await make_staff_user(db_session, role=UserRole.ADMIN)
    headers = auth_headers(admin_token)
    await client.post(
        "/api/v1/stands", json={"name": "Sitio A", "polygon_geojson": _square((20.5, -101.0))}, headers=headers
    )
    created_b = await client.post(
        "/api/v1/stands", json={"name": "Sitio B", "polygon_geojson": _square((20.6, -101.1))}, headers=headers
    )
    stand_b_id = created_b.json()["id"]

    response = await client.patch(
        f"/api/v1/stands/{stand_b_id}",
        json={"polygon_geojson": _square((20.5, -101.0))},  # ahora se mueve encima de A
        headers=headers,
    )
    assert response.status_code == 409


async def test_operator_cannot_patch_stand(client, db_session):
    _, admin_token = await make_staff_user(db_session, role=UserRole.ADMIN)
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    created = await client.post(
        "/api/v1/stands",
        json={"name": "Sitio Centro", "polygon_geojson": _square()},
        headers=auth_headers(admin_token),
    )
    stand_id = created.json()["id"]

    response = await client.patch(
        f"/api/v1/stands/{stand_id}", json={"active": False}, headers=auth_headers(operator_token)
    )
    assert response.status_code == 403
