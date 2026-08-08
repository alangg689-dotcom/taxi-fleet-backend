"""Schemas de sitios.

El polígono viaja como GeoJSON (coordenadas [lng, lat], como manda el
estándar) — el servidor le aplica ST_Buffer con el margen de holgura antes
de guardarlo, así el operador traza el contorno real del sitio sin tener
que calcular la holgura a mano. StandOut es lo mínimo para listas/selectores
(dashboard); StandDetail trae la geometría real, para POST/PATCH/GET de un
sitio puntual.
"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _validate_polygon_geojson(v: dict | None) -> dict | None:
    if v is not None and v.get("type") != "Polygon":
        raise ValueError('polygon_geojson debe ser un GeoJSON de tipo "Polygon"')
    return v


class StandOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    active: bool
    is_placeholder: bool


class StandDetail(StandOut):
    """A diferencia de StandOut, no sale de un simple `from_attributes`: la
    geometría (Geography) se proyecta a GeoJSON/lat-lng en la propia
    consulta SQL, igual que el resto de las columnas espaciales del repo."""

    model_config = ConfigDict(from_attributes=False)

    center_lat: float
    center_lng: float
    polygon_geojson: dict
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
    polígono (y apaga is_placeholder) sin tocar la fila existente; mandar
    solo buffer_meters actualiza el número guardado pero NO vuelve a
    aplicar el buffer sobre la forma ya guardada (no se conserva el trazo
    original sin holgura) — para cambiar la holgura hay que volver a
    mandar polygon_geojson junto con el buffer_meters nuevo."""

    name: str | None = Field(None, max_length=100)
    polygon_geojson: dict | None = None
    buffer_meters: int | None = Field(None, ge=0, le=100)
    still_seconds: int | None = Field(None, ge=0)
    max_speed_kmh: float | None = Field(None, ge=0)
    active: bool | None = None

    _validate_polygon = field_validator("polygon_geojson")(_validate_polygon_geojson)
