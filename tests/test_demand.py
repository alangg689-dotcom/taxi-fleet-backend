"""Pruebas de la presión de demanda (app.core.demand).

Lo que decide esta lógica no es cosmético: define cuánto espera un cliente
parado en la calle antes de que le digamos que no hay taxis, y qué se le
promete al pedirlo.
"""

from datetime import UTC, datetime, timedelta

from app.config import settings
from app.core.demand import customer_wait_message, measure_demand
from app.models import Trip, TripStatus, UserRole, VehicleStatus
from tests.factories import (
    auth_headers,
    make_driver,
    make_location_ping,
    make_open_assignment,
    make_staff_user,
    make_stand,
    make_vehicle,
)

_ORIGIN = (19.4326, -99.1332)


def _point(lat: float, lng: float) -> str:
    return f"SRID=4326;POINT({lng} {lat})"


async def _waiting_trip(db, *, minutes_ago: float = 0.0) -> Trip:
    trip = Trip(
        origin=_point(*_ORIGIN),
        status=TripStatus.SOLICITADO,
        requested_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
    )
    db.add(trip)
    await db.flush()
    return trip


async def _available_driver(db) -> None:
    """Una unidad que el motor de despacho consideraría candidata: turno
    abierto, disponible y con GPS reciente."""
    stand = await make_stand(db)
    vehicle = await make_vehicle(db, status=VehicleStatus.DISPONIBLE, stand_id=stand.id)
    driver, _ = await make_driver(db)
    await make_open_assignment(db, vehicle_id=vehicle.id, driver_id=driver.id)
    await make_location_ping(db, vehicle_id=vehicle.id, timestamp=datetime.now(UTC))


async def test_muchos_viajes_y_pocos_choferes_es_alta_demanda(db_session):
    for _ in range(settings.HIGH_DEMAND_MIN_WAITING_TRIPS):
        await _waiting_trip(db_session)
    await _available_driver(db_session)

    demand = await measure_demand(db_session)

    assert demand.waiting_trips >= settings.HIGH_DEMAND_MIN_WAITING_TRIPS
    assert demand.high_demand is True
    assert demand.max_wait_seconds == settings.BOT_TRIP_MAX_WAIT_HIGH_DEMAND_SECONDS


async def test_muchos_viajes_con_flota_de_sobra_no_es_alta_demanda(db_session):
    """Las dos condiciones tienen que darse juntas. Una racha de pedidos con
    la flota libre se resuelve sola; no hay por qué alargarle la espera a
    nadie ni prometerle 20 minutos."""
    for _ in range(settings.HIGH_DEMAND_MIN_WAITING_TRIPS + 3):
        await _waiting_trip(db_session)
    for _ in range(settings.HIGH_DEMAND_MAX_AVAILABLE_DRIVERS + 1):
        await _available_driver(db_session)

    demand = await measure_demand(db_session)

    assert demand.high_demand is False
    assert demand.max_wait_seconds == settings.BOT_TRIP_MAX_WAIT_SECONDS


async def test_los_viajes_abandonados_no_cuentan_como_demanda(db_session):
    """Un viaje manual que agotó su cascada se queda en 'solicitado' para
    siempre: nadie lo cancela. Si contaran, bastarían cinco de esos para dejar
    al sistema declarando alta demanda de aquí a la eternidad."""
    # Contra una línea base y no contra cero: la medición es global, y otras
    # pruebas del mismo archivo escriben viajes con SessionLocal (fuera del
    # SAVEPOINT), así que la tabla no está vacía al empezar. Lo que se afirma
    # es que estos siete NO suman.
    await _available_driver(db_session)
    baseline = await measure_demand(db_session)

    stale_minutes = settings.BOT_TRIP_MAX_WAIT_HIGH_DEMAND_SECONDS / 60 + 5
    for _ in range(settings.HIGH_DEMAND_MIN_WAITING_TRIPS + 2):
        await _waiting_trip(db_session, minutes_ago=stale_minutes)

    demand = await measure_demand(db_session)

    assert demand.waiting_trips == baseline.waiting_trips


async def test_una_unidad_ocupada_no_cuenta_como_chofer_libre(db_session):
    """Se cuenta con el mismo criterio que usa el motor para elegir candidato.
    Contar unidades 'disponible' a secas inflaría el número justo cuando la
    flota está saturada, que es cuando la lectura importa."""
    for _ in range(settings.HIGH_DEMAND_MIN_WAITING_TRIPS):
        await _waiting_trip(db_session)

    stand = await make_stand(db_session)
    vehicle = await make_vehicle(db_session, status=VehicleStatus.DISPONIBLE, stand_id=stand.id)
    driver, _ = await make_driver(db_session)
    await make_open_assignment(db_session, vehicle_id=vehicle.id, driver_id=driver.id)
    await make_location_ping(db_session, vehicle_id=vehicle.id, timestamp=datetime.now(UTC))
    # Ya lleva pasajero: para el despacho no existe.
    db_session.add(
        Trip(origin=_point(*_ORIGIN), status=TripStatus.EN_CURSO, vehicle_id=vehicle.id)
    )
    await db_session.flush()

    demand = await measure_demand(db_session)

    assert demand.available_drivers == 0
    assert demand.high_demand is True


async def test_mensaje_tranquilo_cuando_hay_flota(db_session):
    # Flota de sobra y no un solo chofer: con menos, un viaje que otra prueba
    # haya dejado esperando bastaría para declarar alta demanda aquí.
    for _ in range(settings.HIGH_DEMAND_MAX_AVAILABLE_DRIVERS + 1):
        await _available_driver(db_session)

    demand = await measure_demand(db_session)

    assert demand.high_demand is False
    assert "Alta demanda" not in customer_wait_message(demand)


async def test_mensaje_de_alta_demanda_da_un_rango_en_minutos(db_session):
    """Un "en breve" que no llega es lo que hace que el cliente se vaya con
    otra base; un rango explícito lo deja esperar con información."""
    for _ in range(settings.HIGH_DEMAND_MIN_WAITING_TRIPS):
        await _waiting_trip(db_session)
    await _available_driver(db_session)

    demand = await measure_demand(db_session)
    mensaje = customer_wait_message(demand)

    assert demand.high_demand is True
    assert "Alta demanda" in mensaje
    assert "15-20 minutos" in mensaje


async def test_endpoint_de_demanda_trae_los_umbrales(client, db_session):
    """El dashboard colorea con estos números en vez de llevar copias suyas."""
    _, token = await make_staff_user(db_session, role=UserRole.OPERATOR)

    response = await client.get("/api/v1/trips/demand", headers=auth_headers(token))

    assert response.status_code == 200
    body = response.json()
    assert body["normal_wait_seconds"] == settings.BOT_TRIP_MAX_WAIT_SECONDS
    assert body["high_demand_wait_seconds"] == settings.BOT_TRIP_MAX_WAIT_HIGH_DEMAND_SECONDS
    assert body["max_wait_seconds"] in (
        settings.BOT_TRIP_MAX_WAIT_SECONDS,
        settings.BOT_TRIP_MAX_WAIT_HIGH_DEMAND_SECONDS,
    )


async def test_demand_no_se_confunde_con_un_id_de_viaje(client, db_session):
    """`/trips/demand` va declarada antes que `/trips/{trip_id}`: al revés, esa
    ruta se tragaría "demand" e intentaría leerlo como UUID (mismo caso que
    /vehicles/nearby)."""
    _, token = await make_staff_user(db_session, role=UserRole.OPERATOR)

    response = await client.get("/api/v1/trips/demand", headers=auth_headers(token))

    assert response.status_code != 422
