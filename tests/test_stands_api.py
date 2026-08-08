"""Pruebas de POST/PATCH/GET /stands (spec-sitios-y-fila-v2.md, sección 9,
la parte de trazar sitios reales sobre los placeholders del paso 3).

Coordenadas lejos del Zócalo (default de make_stand/make_vehicle) a
propósito, para que estas pruebas nunca se encimen por accidente con un
sitio de otra fábrica dentro de la misma prueba.
"""

from datetime import UTC, datetime, timedelta

from app.models import StandQueue, StandQueueStatus, UserRole
from tests.factories import (
    auth_headers,
    make_driver,
    make_open_assignment,
    make_staff_user,
    make_stand,
    make_vehicle,
)

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


# --- Fila del sitio: GET/reorder/DELETE (operador, no solo admin) -----------


async def _formar(db, *, stand, entered_at=None):
    """Unidad formada de una vez: sitio, chofer, turno abierto y la fila."""
    vehicle = await make_vehicle(db, stand_id=stand.id)
    driver, _ = await make_driver(db)
    await make_open_assignment(db, vehicle_id=vehicle.id, driver_id=driver.id)
    entry = StandQueue(
        stand_id=stand.id, vehicle_id=vehicle.id, driver_id=driver.id,
        status=StandQueueStatus.FORMADO,
        **({"entered_at": entered_at} if entered_at is not None else {}),
    )
    db.add(entry)
    await db.flush()
    return vehicle, driver


async def test_get_queue_returns_ordered_positions(client, db_session):
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    stand = await make_stand(db_session)
    first, _ = await _formar(db_session, stand=stand, entered_at=datetime.now(UTC) - timedelta(minutes=10))
    second, _ = await _formar(db_session, stand=stand)

    response = await client.get(
        f"/api/v1/stands/{stand.id}/queue", headers=auth_headers(operator_token)
    )
    assert response.status_code == 200
    body = response.json()
    assert [row["vehicle_id"] for row in body] == [str(first.id), str(second.id)]
    assert [row["position"] for row in body] == [1, 2]
    assert body[0]["plate"] == first.plate


async def test_get_queue_unknown_stand_is_404(client, db_session):
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    response = await client.get(
        "/api/v1/stands/00000000-0000-0000-0000-000000000000/queue",
        headers=auth_headers(operator_token),
    )
    assert response.status_code == 404


async def test_reorder_changes_positions(client, db_session):
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    stand = await make_stand(db_session)
    first, _ = await _formar(db_session, stand=stand, entered_at=datetime.now(UTC) - timedelta(minutes=10))
    second, _ = await _formar(db_session, stand=stand)

    response = await client.post(
        f"/api/v1/stands/{stand.id}/queue/reorder",
        json={"vehicle_ids": [str(second.id), str(first.id)]},
        headers=auth_headers(operator_token),
    )
    assert response.status_code == 200
    body = response.json()
    assert [row["vehicle_id"] for row in body] == [str(second.id), str(first.id)]
    assert body[0]["position_held"] is False


async def test_reorder_rejects_mismatched_vehicle_set(client, db_session):
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    stand = await make_stand(db_session)
    await _formar(db_session, stand=stand)

    response = await client.post(
        f"/api/v1/stands/{stand.id}/queue/reorder",
        json={"vehicle_ids": ["00000000-0000-0000-0000-000000000000"]},
        headers=auth_headers(operator_token),
    )
    assert response.status_code == 422


async def test_delete_removes_vehicle_from_queue(client, db_session):
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    stand = await make_stand(db_session)
    vehicle, _ = await _formar(db_session, stand=stand)

    response = await client.delete(
        f"/api/v1/stands/{stand.id}/queue/{vehicle.id}", headers=auth_headers(operator_token)
    )
    assert response.status_code == 204

    listed = await client.get(
        f"/api/v1/stands/{stand.id}/queue", headers=auth_headers(operator_token)
    )
    assert listed.json() == []


async def test_delete_vehicle_not_in_queue_is_404(client, db_session):
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    stand = await make_stand(db_session)
    vehicle = await make_vehicle(db_session, stand_id=stand.id)

    response = await client.delete(
        f"/api/v1/stands/{stand.id}/queue/{vehicle.id}", headers=auth_headers(operator_token)
    )
    assert response.status_code == 404


async def test_driver_cannot_manage_queue(client, db_session):
    stand = await make_stand(db_session)
    _, driver_token = await make_driver(db_session)

    response = await client.get(
        f"/api/v1/stands/{stand.id}/queue", headers=auth_headers(driver_token)
    )
    assert response.status_code == 403
