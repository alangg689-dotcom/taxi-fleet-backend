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

Única excepción: el caso "sin candidatos" de dispatch_trip() SÍ se llama de
verdad (ver test_dispatch_without_candidates_cancels_bot_trip más abajo) —
es rápido y determinista (no espera ningún timeout), así que vale la pena
cubrirlo end-to-end pese al costo de manejar el engine real (ver
`_dispose_engine_pool` ahí mismo).
"""

from datetime import UTC, datetime, timedelta

from app.api.location import _point
from app.core.dispatch import dispatch_trip, find_candidate_drivers
from app.database import SessionLocal, engine
from app.models import StandQueue, StandQueueStatus, Trip, TripStatus, UserRole, VehicleStatus
from tests.factories import (
    auth_headers,
    make_driver,
    make_location_ping,
    make_open_assignment,
    make_stand,
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

    far_vehicle = await make_vehicle(db_session, status=VehicleStatus.DISPONIBLE)
    far_driver, _ = await make_driver(db_session)
    await make_open_assignment(db_session, vehicle_id=far_vehicle.id, driver_id=far_driver.id)
    await make_location_ping(
        db_session, vehicle_id=far_vehicle.id, timestamp=datetime.now(UTC), lat=19.45, lng=-99.10
    )

    near_vehicle = await make_vehicle(db_session, status=VehicleStatus.DISPONIBLE)
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
    vehicle = await make_vehicle(db_session, status=VehicleStatus.DISPONIBLE)
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
    vehicle = await make_vehicle(db_session, status=VehicleStatus.DISPONIBLE)
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
    vehicle = await make_vehicle(db_session, status=VehicleStatus.DISPONIBLE)
    await make_location_ping(
        db_session, vehicle_id=vehicle.id, timestamp=datetime.now(UTC), lat=19.433, lng=-99.134
    )
    # Sin make_open_assignment: nadie está manejando esta unidad ahora mismo.

    candidates = await find_candidate_drivers(db_session, trip.id)
    assert candidates == []


async def test_find_candidates_excludes_vehicle_with_active_trip(db_session):
    trip = await _make_trip(db_session)
    vehicle = await make_vehicle(db_session, status=VehicleStatus.DISPONIBLE)
    driver, _ = await make_driver(db_session)
    await make_open_assignment(db_session, vehicle_id=vehicle.id, driver_id=driver.id)
    await make_location_ping(
        db_session, vehicle_id=vehicle.id, timestamp=datetime.now(UTC), lat=19.433, lng=-99.134
    )
    await _make_trip(db_session, vehicle_id=vehicle.id, driver_id=driver.id, status=TripStatus.EN_CURSO)

    candidates = await find_candidate_drivers(db_session, trip.id)
    assert candidates == []


async def test_find_candidates_excludes_vehicle_not_disponible(db_session):
    """El chofer se marcó 'ocupado' a mano (corte de calle) — el motor de
    despacho no debe ofrecerle nada aunque el resto de las condiciones se
    cumplan."""
    trip = await _make_trip(db_session)
    vehicle = await make_vehicle(db_session, status=VehicleStatus.OCUPADO)
    driver, _ = await make_driver(db_session)
    await make_open_assignment(db_session, vehicle_id=vehicle.id, driver_id=driver.id)
    await make_location_ping(
        db_session, vehicle_id=vehicle.id, timestamp=datetime.now(UTC), lat=19.433, lng=-99.134
    )

    candidates = await find_candidate_drivers(db_session, trip.id)
    assert candidates == []


# --- Escalones de prioridad de sitios (spec-sitios-y-fila-v2.md, sección 8) --
#
# DISPATCH_ETA_SPEED_KMH=25 → ~6.94 m/s. DISPATCH_TIER_ADVANTAGE_SECONDS=300s
# equivalen a ~2083m de diferencia: los offsets de abajo se eligen bien por
# debajo (no debe ganar el escalón inferior) o bien por arriba (sí debe
# ganar) de ese margen, nunca cerca, para que la prueba no dependa de un
# redondeo.


async def _formar(db, *, stand, vehicle, driver, entered_at=None):
    entry = StandQueue(
        stand_id=stand.id,
        vehicle_id=vehicle.id,
        driver_id=driver.id,
        status=StandQueueStatus.FORMADO,
        **({"entered_at": entered_at} if entered_at is not None else {}),
    )
    db.add(entry)
    await db.flush()
    return entry


async def test_head_of_queue_wins_within_advantage_margin(db_session):
    """La primera de la fila gana aunque una unidad rodando esté un poco más
    cerca — la ventaja de 300s no le alcanza a la rodando."""
    stand = await make_stand(db_session, center=_ORIGIN)
    trip = await _make_trip(db_session, origin=_ORIGIN)

    queued_vehicle = await make_vehicle(db_session, status=VehicleStatus.DISPONIBLE, stand_id=stand.id)
    queued_driver, _ = await make_driver(db_session)
    await make_open_assignment(db_session, vehicle_id=queued_vehicle.id, driver_id=queued_driver.id)
    await _formar(db_session, stand=stand, vehicle=queued_vehicle, driver=queued_driver)
    # ~300m de la unidad formada — dentro del margen, no debe desplazarla.
    await make_location_ping(
        db_session, vehicle_id=queued_vehicle.id, timestamp=datetime.now(UTC),
        lat=_ORIGIN[0] + 0.0027, lng=_ORIGIN[1],
    )

    roaming_vehicle = await make_vehicle(db_session, status=VehicleStatus.DISPONIBLE)
    roaming_driver, _ = await make_driver(db_session)
    await make_open_assignment(db_session, vehicle_id=roaming_vehicle.id, driver_id=roaming_driver.id)
    await make_location_ping(
        db_session, vehicle_id=roaming_vehicle.id, timestamp=datetime.now(UTC),
        lat=_ORIGIN[0], lng=_ORIGIN[1],
    )

    candidates = await find_candidate_drivers(db_session, trip.id)
    assert candidates[0].vehicle_id == queued_vehicle.id


async def test_much_closer_roaming_unit_beats_head_of_queue(db_session):
    """Si la unidad formada está bastante más lejos del origen del viaje que
    una disponible rodando (≥300s de diferencia), gana la rodando — la
    formada queda de respaldo en la cascada, no desaparece."""
    stand = await make_stand(db_session, center=_ORIGIN)
    trip = await _make_trip(db_session, origin=_ORIGIN)

    queued_vehicle = await make_vehicle(db_session, status=VehicleStatus.DISPONIBLE, stand_id=stand.id)
    queued_driver, _ = await make_driver(db_session)
    await make_open_assignment(db_session, vehicle_id=queued_vehicle.id, driver_id=queued_driver.id)
    await _formar(db_session, stand=stand, vehicle=queued_vehicle, driver=queued_driver)
    # ~5000m del origen — bastante más que el margen de ventaja.
    await make_location_ping(
        db_session, vehicle_id=queued_vehicle.id, timestamp=datetime.now(UTC),
        lat=_ORIGIN[0] + 0.045, lng=_ORIGIN[1],
    )

    roaming_vehicle = await make_vehicle(db_session, status=VehicleStatus.DISPONIBLE)
    roaming_driver, _ = await make_driver(db_session)
    await make_open_assignment(db_session, vehicle_id=roaming_vehicle.id, driver_id=roaming_driver.id)
    await make_location_ping(
        db_session, vehicle_id=roaming_vehicle.id, timestamp=datetime.now(UTC),
        lat=_ORIGIN[0], lng=_ORIGIN[1],
    )

    candidates = await find_candidate_drivers(db_session, trip.id)
    assert candidates[0].vehicle_id == roaming_vehicle.id
    assert queued_vehicle.id in [c.vehicle_id for c in candidates]  # sigue de respaldo


async def test_second_in_queue_is_never_offered(db_session):
    """Solo la primera de la fila es candidata (escalón 1) — la segunda no
    se ofrece ni siquiera como rodando: está formada, no cuenta ahí."""
    stand = await make_stand(db_session, center=_ORIGIN)
    trip = await _make_trip(db_session, origin=_ORIGIN)

    first_vehicle = await make_vehicle(db_session, status=VehicleStatus.DISPONIBLE, stand_id=stand.id)
    first_driver, _ = await make_driver(db_session)
    await make_open_assignment(db_session, vehicle_id=first_vehicle.id, driver_id=first_driver.id)
    await _formar(
        db_session, stand=stand, vehicle=first_vehicle, driver=first_driver,
        entered_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    await make_location_ping(
        db_session, vehicle_id=first_vehicle.id, timestamp=datetime.now(UTC),
        lat=_ORIGIN[0], lng=_ORIGIN[1],
    )

    second_vehicle = await make_vehicle(db_session, status=VehicleStatus.DISPONIBLE, stand_id=stand.id)
    second_driver, _ = await make_driver(db_session)
    await make_open_assignment(db_session, vehicle_id=second_vehicle.id, driver_id=second_driver.id)
    await _formar(db_session, stand=stand, vehicle=second_vehicle, driver=second_driver)
    await make_location_ping(
        db_session, vehicle_id=second_vehicle.id, timestamp=datetime.now(UTC),
        lat=_ORIGIN[0], lng=_ORIGIN[1],
    )

    candidates = await find_candidate_drivers(db_session, trip.id)
    vehicle_ids = [c.vehicle_id for c in candidates]
    assert vehicle_ids == [first_vehicle.id]


async def test_tier4_offers_other_stands_head_when_own_zone_has_nothing(db_session):
    """Sin nadie disponible en la zona del viaje (ni formado ni rodando), el
    escalón 4 ofrece a la primera de la fila de OTRO sitio."""
    empty_stand = await make_stand(db_session, center=_ORIGIN)
    trip = await _make_trip(db_session, origin=_ORIGIN)

    other_stand = await make_stand(db_session, center=(19.0, -98.5))
    other_vehicle = await make_vehicle(db_session, status=VehicleStatus.DISPONIBLE, stand_id=other_stand.id)
    other_driver, _ = await make_driver(db_session)
    await make_open_assignment(db_session, vehicle_id=other_vehicle.id, driver_id=other_driver.id)
    await _formar(db_session, stand=other_stand, vehicle=other_vehicle, driver=other_driver)
    await make_location_ping(
        db_session, vehicle_id=other_vehicle.id, timestamp=datetime.now(UTC), lat=19.0, lng=-98.5
    )

    candidates = await find_candidate_drivers(db_session, trip.id)
    assert [c.vehicle_id for c in candidates] == [other_vehicle.id]
    assert empty_stand is not None  # el sitio de la zona existe, solo no tiene a nadie


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


async def test_dispatch_without_candidates_cancels_bot_trip(monkeypatch):
    """Un viaje de operador se queda "solicitado" cuando no hay candidatos
    (documentado arriba y en el README) para que quede visible en el
    dashboard y alguien lo redespache a mano. Uno del bot de WhatsApp no
    tiene a nadie viendo un dashboard — dejarlo "solicitado" para siempre
    bloquearía que ese cliente pudiera volver a pedir un taxi (ver
    _trip_still_active en app.core.whatsapp_bot), así que dispatch_trip lo
    cancela en cuanto se rinde."""
    import app.core.dispatch as dispatch_module

    sent = []

    async def _fake_send(phone, body):
        sent.append((phone, body))

    monkeypatch.setattr(dispatch_module, "send_whatsapp_message", _fake_send)

    async with SessionLocal() as db:
        trip = Trip(
            origin=_point(19.4326, -99.1332),
            status=TripStatus.SOLICITADO,
            customer_phone="+525512340099",
        )
        db.add(trip)
        await db.flush()
        trip_id = trip.id
        await db.commit()

    await dispatch_trip(trip_id)

    async with SessionLocal() as db:
        trip = await db.get(Trip, trip_id)
        assert trip.status == TripStatus.CANCELADO

    assert sent == [("+525512340099", "Por ahora no hay taxis disponibles cerca de ti. Intenta de nuevo en unos minutos.")]

    await engine.dispose()
