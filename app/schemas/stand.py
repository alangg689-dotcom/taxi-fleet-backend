"""Schema de sitios. Solo lectura por ahora — POST/PATCH con el polígono vía
GeoJSON, reordenar fila y demás llegan completos en el paso 6 de la spec."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class StandOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    active: bool
    is_placeholder: bool
