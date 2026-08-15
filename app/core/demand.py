"""Presión de demanda: cuántos viajes esperan chofer y cuántos choferes hay
libres para tomarlos.

Alimenta dos decisiones que antes eran constantes:

  - Cuánto aguanta un viaje antes de que el barrido se rinda y le avise al
    cliente que no hay taxis (`app.core.whatsapp_bot.sweep_stuck_bot_trips`).
  - Qué se le promete al cliente al pedir el viaje: prometer "en breve" con la
    calle saturada es lo que hace que el cliente se vaya con otra base.

Se mide contra la base en cada consulta, no se cachea: son dos COUNT sobre
índices que ya existen, y una lectura vieja tomaría la decisión equivocada
justo en el minuto en que la situación cambia, que es cuando importa.
"""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings


@dataclass(frozen=True)
class Demand:
    """Foto del momento. `max_wait_seconds` ya viene resuelto para que quien
    lo use no tenga que volver a aplicar la regla."""

    waiting_trips: int
    available_drivers: int
    high_demand: bool
    max_wait_seconds: int


# Solo los que siguen esperando de verdad. Un viaje manual que agotó su
# cascada se queda en 'solicitado' para siempre (nadie lo cancela), y contarlo
# dejaría al sistema declarando alta demanda eternamente por viajes que ya
# nadie espera. El corte es el techo de alta demanda: más viejo que eso, ni
# con la regla más laxa seguiría vivo.
_WAITING_TRIPS_SQL = """
    SELECT count(*) FROM trips
    WHERE status = 'solicitado'
      AND requested_at > now() - make_interval(secs => :ceiling)
"""

# Mismos criterios que usa el motor de despacho para considerar candidata a una
# unidad (ver _GUARDAS_CANDIDATO en app.core.dispatch): turno abierto, estado
# disponible, GPS reciente, sin viaje activo y sin una oferta viva encima.
# Contar de otra forma daría un número que no se parece al que el motor ve.
_AVAILABLE_DRIVERS_SQL = """
    SELECT count(*)
    FROM vehicles v
    JOIN vehicle_assignments va ON va.vehicle_id = v.id AND va.ended_at IS NULL
    WHERE v.status = 'disponible'
      AND EXISTS (
          SELECT 1 FROM location_pings lp
          WHERE lp.vehicle_id = v.id
            AND lp.timestamp > now() - make_interval(secs => :freshness)
      )
      AND NOT EXISTS (
          SELECT 1 FROM trips t
          WHERE t.vehicle_id = v.id AND t.status IN ('asignado', 'en_curso')
      )
      AND NOT EXISTS (
          SELECT 1 FROM trips t
          WHERE t.offered_vehicle_id = v.id
            AND t.status = 'solicitado'
            AND (t.offer_expires_at IS NULL OR t.offer_expires_at > now())
      )
"""


async def measure_demand(db: AsyncSession) -> Demand:
    waiting = (
        await db.execute(
            text(_WAITING_TRIPS_SQL),
            {"ceiling": settings.BOT_TRIP_MAX_WAIT_HIGH_DEMAND_SECONDS},
        )
    ).scalar_one()

    available = (
        await db.execute(
            text(_AVAILABLE_DRIVERS_SQL),
            {"freshness": settings.DISPATCH_POSITION_FRESHNESS_SECONDS},
        )
    ).scalar_one()

    high = (
        waiting >= settings.HIGH_DEMAND_MIN_WAITING_TRIPS
        and available < settings.HIGH_DEMAND_MAX_AVAILABLE_DRIVERS
    )

    return Demand(
        waiting_trips=waiting,
        available_drivers=available,
        high_demand=high,
        max_wait_seconds=(
            settings.BOT_TRIP_MAX_WAIT_HIGH_DEMAND_SECONDS
            if high
            else settings.BOT_TRIP_MAX_WAIT_SECONDS
        ),
    )


def customer_wait_message(demand: Demand, queue_position: int | None = None) -> str:
    """Lo que se le dice al cliente al aceptarle el viaje.

    En alta demanda se da la posición en la fila y un rango en minutos en vez
    de un "en breve": el cliente que sabe que va quinto y que son quince
    minutos espera; el que espera un "en breve" que no llega, se va.
    """
    if demand.high_demand:
        position = f"Estás en la posición #{queue_position}. " if queue_position else ""
        return (
            f"⏳ Alta demanda en este momento. {position}"
            "Tiempo estimado de espera: 15-20 minutos. "
            "Te avisaremos en cuanto haya un taxi. Responde *cancelar* si ya no lo necesitas."
        )
    return "Buscando un taxi cerca de ti… te avisamos en cuanto uno confirme. 🔎"
