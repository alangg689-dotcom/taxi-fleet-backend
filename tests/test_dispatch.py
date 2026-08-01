"""Pruebas del motor de despacho automático (app.core.dispatch).

`dispatch_trip` (la orquestación completa: ofrecer, esperar respuesta,
pasar en cascada al siguiente candidato) usa `SessionLocal()` a propósito
—no `Depends(get_db)`—, porque corre como tarea de fondo sin una request
detrás a la cual engancharse. Eso significa que usa el engine real de la
app (`app.database.engine`), no el engine de pruebas con SAVEPOINT: dos
conexiones distintas no se ven una a la otra dentro de una transacción sin
confirmar. Probar esa orquestación completa de punta a punta necesitaría
commits reales contra flotilla_test y limpieza manual — se dejó como
verificación en vivo (igual que el refresco del agregado continuo de
TimescaleDB) en vez de forzarlo aquí.

Lo que SÍ se prueba aquí, con el session normal de SAVEPOINT:
  - find_candidate_drivers: la consulta geoespacial en sí (no toca
    SessionLocal, recibe el `db` que se le pase).
  - Las ramas de autorización y transición de accept_trip/reject_trip en el
    flujo de despacho (construyendo a mano el estado "ya se le ofreció a
    este chofer", sin pasar por dispatch_trip).
  - El contrato HTTP de POST /trips/dispatch (con dispatch_trip
    parcheado a un no-op, para no disparar la tarea de fondo real).
"""

from datetime import UTC, datetime, timedelta

from app.api.location import _point
from app.core.dispatch import find_candidate_drivers
from app.models import Trip, TripStatus, UserRole
from tests.factories import (
    auth_headers,
    make_driver,
    make_location_ping,
    make_open_assignment,
    make_staff_user,
    make_vehicle,
)

_ORIGIN = (19.4326, -99.1332)  # Zócalo


async def _make_trip(db_session, *, origin=_ORIGIN, status=TripStatus.SOLICITADO, **extra) -> Trip:
    trip = Trip(origin=_point(*origin), status=status, **extra)
    db_session.add(trip)
    await db_session.flush()
    return trip


# --- find_candidate_drivers --------------------------------------------------


async def test_find_candidates_orders_by_distance(db_session):
    trip = await _make_trip(db_session)

    far_vehicle = await make_vehicle(db_session)
    far_driver, _ = await make_driver(db_session)
    await make_open_assignment(db_session, vehicle_id=far_vehicle.id, driver_id=far_driver.id)
    await make_location_ping(
        db_session, vehicle_id=far_vehicle.id, timestamp=datetime.now(UTC), lat=19.45, lng=-99.10
    )

    near_vehicle = await make_vehicle(db_session)
    near_driver, _ = await make_driver(db_session)
    await make_open_assignment(db_session, vehicle_id=near_vehicle.id, driver_id=near_driver.id)
    await make_location_ping(
        db_session, vehicle_id=near_vehicle.id, timestamp=datetime.now(UTC), lat=19.433, lng=-99.134
    )

    candidates = await find_candidate_drivers(db_session, trip.id)

    assert [c.driver_id for c in candidates] == [near_driver.id, far_driver.id]
    assert candidates[0].distance_m < candidates[1].distance_m


async def test_find_candidates_excludes_stale_ping(db_session):
    trip = await _make_trip(db_session)
    vehicle = await make_vehicle(db_session)
    driver, _ = await make_driver(db_session)
    await make_open_assignment(db_session, vehicle_id=vehicle.id, driver_id=driver.id)
    await make_location_ping(
        db_session,
        vehicle_id=vehicle.id,
        timestamp=datetime.now(UTC) - timedelta(hours=2),
        lat=19.433,
        lng=-99.134,
    )

    candidates = await find_candidate_drivers(db_session, trip.id)
    assert candidates == []


async def test_find_candidates_excludes_out_of_radius(db_session):
    trip = await _make_trip(db_session)
    vehicle = await make_vehicle(db_session)
    driver, _ = await make_driver(db_session)
    await make_open_assignment(db_session, vehicle_id=vehicle.id, driver_id=driver.id)
    # Puebla, a ~100km del Zócalo — muy fuera del radio de búsqueda default.
    await make_location_ping(
        db_session, vehicle_id=vehicle.id, timestamp=datetime.now(UTC), lat=19.04, lng=-98.20
    )

    candidates = await find_candidate_drivers(db_session, trip.id)
    assert candidates == []


async def test_find_candidates_excludes_vehicle_without_open_assignment(db_session):
    trip = await _make_trip(db_session)
    vehicle = await make_vehicle(db_session)
    await make_location_ping(
        db_session, vehicle_id=vehicle.id, timestamp=datetime.now(UTC), lat=19.433, lng=-99.134
    )
    # Sin make_open_assignment: nadie está manejando esta unidad ahora mismo.

    candidates = await find_candidate_drivers(db_session, trip.id)
    assert candidates == []


async def test_find_candidates_excludes_vehicle_with_active_trip(db_session):
    trip = await _make_trip(db_session)
    vehicle = await make_vehicle(db_session)
    driver, _ = await make_driver(db_session)
    await make_open_assignment(db_session, vehicle_id=vehicle.id, driver_id=driver.id)
    await make_location_ping(
        db_session, vehicle_id=vehicle.id, timestamp=datetime.now(UTC), lat=19.433, lng=-99.134
    )
    await _make_trip(db_session, vehicle_id=vehicle.id, driver_id=driver.id, status=TripStatus.EN_CURSO)

    candidates = await find_candidate_drivers(db_session, trip.id)
    assert candidates == []


# --- POST /trips/dispatch ----------------------------------------------------


async def test_dispatch_endpoint_creates_trip_without_driver(client, db_session, monkeypatch):
    """No se prueba la tarea de fondo real aquí (ver docstring del módulo);
    se parchea a un no-op para aislar el contrato HTTP del endpoint."""
    import app.api.trips as trips_module

    async def _noop_dispatch(trip_id):
        return None

    monkeypatch.setattr(trips_module, "dispatch_trip", _noop_dispatch)

    _, admin_token = await make_staff_user(db_session, role=UserRole.ADMIN)
    response = await client.post(
        "/api/v1/trips/dispatch",
        json={"origin_lat": 19.4326, "origin_lng": -99.1332, "origin_address": "Zócalo"},
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "solicitado"
    assert body["vehicle_id"] is None
    assert body["driver_id"] is None


async def test_dispatch_endpoint_requires_staff(client, db_session):
    _, driver_token = await make_driver(db_session)
    response = await client.post(
        "/api/v1/trips/dispatch",
        json={"origin_lat": 19.4326, "origin_lng": -99.1332},
        headers=auth_headers(driver_token),
    )
    assert response.status_code == 403


# --- accept/reject en el flujo de despacho -----------------------------------


async def _make_offered_trip(db_session, *, driver_id, vehicle_id, expires_in=timedelta(seconds=20)):
    trip = await _make_trip(db_session)
    trip.offered_driver_id = driver_id
    trip.offered_vehicle_id = vehicle_id
    trip.offer_expires_at = datetime.now(UTC) + expires_in
    await db_session.flush()
    return trip


async def test_offered_driver_can_accept(client, db_session):
    vehicle = await make_vehicle(db_session)
    driver, driver_token = await make_driver(db_session)
    trip = await _make_offered_trip(db_session, driver_id=driver.id, vehicle_id=vehicle.id)

    response = await client.post(
        f"/api/v1/trips/{trip.id}/accept", headers=auth_headers(driver_token)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "asignado"
    assert body["driver_id"] == str(driver.id)
    assert body["vehicle_id"] == str(vehicle.id)
    # El dashboard deja de mostrar "ofreciendo a..." en cuanto se asigna.
    assert body["offered_driver_id"] is None
    assert body["offered_vehicle_id"] is None
    assert body["offer_expires_at"] is None


async def test_trip_out_exposes_current_offer_for_the_dashboard(client, db_session):
    """El operador necesita ver a quién se le está ofreciendo el viaje ahora
    mismo mientras el motor de despacho recorre candidatos."""
    _, admin_token = await make_staff_user(db_session, role=UserRole.ADMIN)
    vehicle = await make_vehicle(db_session)
    driver, _ = await make_driver(db_session)
    trip = await _make_offered_trip(db_session, driver_id=driver.id, vehicle_id=vehicle.id)

    response = await client.get(
        f"/api/v1/trips/{trip.id}", headers=auth_headers(admin_token)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["offered_driver_id"] == str(driver.id)
    assert body["offered_vehicle_id"] == str(vehicle.id)
    assert body["offer_expires_at"] is not None


async def test_other_driver_cannot_accept_someone_elses_offer(client, db_session):
    vehicle = await make_vehicle(db_session)
    offered_driver, _ = await make_driver(db_session)
    _, other_driver_token = await make_driver(db_session)
    trip = await _make_offered_trip(db_session, driver_id=offered_driver.id, vehicle_id=vehicle.id)

    response = await client.post(
        f"/api/v1/trips/{trip.id}/accept", headers=auth_headers(other_driver_token)
    )
    assert response.status_code == 403


async def test_cannot_accept_expired_offer(client, db_session):
    vehicle = await make_vehicle(db_session)
    driver, driver_token = await make_driver(db_session)
    trip = await _make_offered_trip(
        db_session, driver_id=driver.id, vehicle_id=vehicle.id, expires_in=timedelta(seconds=-5)
    )

    response = await client.post(
        f"/api/v1/trips/{trip.id}/accept", headers=auth_headers(driver_token)
    )
    assert response.status_code == 409


async def test_offered_driver_can_reject(client, db_session):
    vehicle = await make_vehicle(db_session)
    driver, driver_token = await make_driver(db_session)
    trip = await _make_offered_trip(db_session, driver_id=driver.id, vehicle_id=vehicle.id)

    response = await client.post(
        f"/api/v1/trips/{trip.id}/reject", headers=auth_headers(driver_token)
    )
    assert response.status_code == 200
    assert response.json()["status"] == "solicitado"

    await db_session.refresh(trip)
    assert trip.offered_driver_id is None
    assert trip.offered_vehicle_id is None
    assert trip.offer_expires_at is None


async def test_other_driver_cannot_reject_someone_elses_offer(client, db_session):
    vehicle = await make_vehicle(db_session)
    offered_driver, _ = await make_driver(db_session)
    _, other_driver_token = await make_driver(db_session)
    trip = await _make_offered_trip(db_session, driver_id=offered_driver.id, vehicle_id=vehicle.id)

    response = await client.post(
        f"/api/v1/trips/{trip.id}/reject", headers=auth_headers(other_driver_token)
    )
    assert response.status_code == 403


async def test_staff_cannot_reject(client, db_session):
    """Rechazar es una decisión del chofer; un operador no tiene nada que
    rechazar (para eso está /cancel)."""
    vehicle = await make_vehicle(db_session)
    driver, _ = await make_driver(db_session)
    _, operator_token = await make_staff_user(db_session, role=UserRole.OPERATOR)
    trip = await _make_offered_trip(db_session, driver_id=driver.id, vehicle_id=vehicle.id)

    response = await client.post(
        f"/api/v1/trips/{trip.id}/reject", headers=auth_headers(operator_token)
    )
    assert response.status_code == 403
