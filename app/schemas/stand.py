"""Schemas de sitios.

El polígono viaja como GeoJSON (coordenadas [lng, lat], como manda el
estándar) — el servidor le aplica ST_Buffer con el margen de holgura antes
de guardarlo, así el operador traza el contorno real del sitio sin tener
que calcular la holgura a mano. StandOut es lo mínimo para listas/selectores
(dashboard); StandDetail trae la geometría real, para POST/PATCH/GET de un
sitio puntual.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _validate_polygon_geojson(v: dict | None) -> dict | None:
    if v is not None and v.get("type") != "Polygon":
        raise ValueError('polygon_geojson debe ser un GeoJSON de tipo "Polygon"')
    return v


class StandOut(BaseModel):
    """Lo mínimo para listas y selectores. Trae el centro porque el mapa de
    flota necesita saber dónde opera la flotilla para encuadrar mientras
    ninguna unidad reporta posición, y pedir el detalle de los 6 sitios solo
    para eso serían 6 llamadas."""

    model_config = ConfigDict(from_attributes=False)

    id: UUID
    name: str
    active: bool
    is_placeholder: bool
    center_lat: float
    center_lng: float


class StandDetail(StandOut):
    """A diferencia de StandOut, trae la geometría completa. Ninguno de los
    dos sale de un simple `from_attributes`: la geometría (Geography) se
    proyecta a GeoJSON/lat-lng en la propia consulta SQL, igual que el resto
    de las columnas espaciales del repo."""

    polygon_geojson: dict
    # El trazo del operador sin holgura (ver migración 0012). Es lo que el
    # dashboard abre para ajustar vértices: `polygon_geojson` trae las
    # esquinas redondeadas de ST_Buffer y es inmanejable a mano. Nulo en los
    # sitios que no lo tienen guardado — ahí hay que volver a trazar.
    outline_geojson: dict | None
    still_seconds: int
    max_speed_kmh: float
    polygon_buffer_meters: int


class StandCreate(BaseModel):
    name: str = Field(..., max_length=100)
    polygon_geojson: dict = Field(..., description="GeoJSON Polygon, coordenadas [lng, lat]")
    # None = usa el default de app.config (holgura/still/velocidad).
    buffer_meters: int | None = Field(None, ge=0, le=100)
    still_seconds: int | None = Field(None, ge=0)
    max_speed_kmh: float | None = Field(None, ge=0)
    active: bool = True

    _validate_polygon = field_validator("polygon_geojson")(_validate_polygon_geojson)


class StandUpdate(BaseModel):
    """Todo opcional — PATCH parcial. Mandar polygon_geojson reemplaza el
    polígono (y apaga is_placeholder) sin tocar la fila existente.

    Mandar solo buffer_meters rehace la geocerca a partir del trazo original
    (`outline`, ver migración 0012) con la holgura nueva. En los sitios que
    no tienen ese trazo guardado — los placeholder de la 0008 y aquellos
    donde la aproximación de la 0012 no dio un polígono válido — solo se
    actualiza el número, y hay que volver a trazarlos para que la holgura
    tenga efecto sobre la forma."""

    name: str | None = Field(None, max_length=100)
    polygon_geojson: dict | None = None
    buffer_meters: int | None = Field(None, ge=0, le=100)
    still_seconds: int | None = Field(None, ge=0)
    max_speed_kmh: float | None = Field(None, ge=0)
    active: bool | None = None
    # False cuando el polígono que se manda YA trae la holgura aplicada —
    # el caso del dashboard al mover vértices sobre la forma que este mismo
    # endpoint devolvió. Sin esto, cada ajuste la inflaría de nuevo. No
    # cambia la holgura configurada del sitio, solo si se aplica ahora.
    apply_buffer: bool = True

    _validate_polygon = field_validator("polygon_geojson")(_validate_polygon_geojson)


class QueuePositionOut(BaseModel):
    """Una fila de la fila de un sitio (sección 9). `position` sale de
    ROW_NUMBER() en la consulta — nunca se guarda como número, ver
    app.models.stand.StandQueue."""

    stand_id: UUID
    vehicle_id: UUID
    driver_id: UUID
    plate: str
    driver_name: str
    position: int
    position_held: bool
    entered_at: datetime


class QueueReorderRequest(BaseModel):
    """El orden nuevo, completo — debe incluir exactamente las unidades hoy
    formadas en el sitio, ni más ni menos (ver app.core.stands.reorder_queue)."""

    vehicle_ids: list[UUID] = Field(..., min_length=1)
