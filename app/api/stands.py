"""Endpoints de sitios.

Deliberadamente solo lectura por ahora: lo mínimo para que el selector de
"+ Nueva unidad" del dashboard deje de estar roto sin esperar al paso 6 de
la spec (POST/PATCH con el polígono vía GeoJSON, fila, reordenar,
broadcast stand:queue)."""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_roles
from app.database import get_db
from app.models import Stand, User, UserRole
from app.schemas.stand import StandOut

router = APIRouter(prefix="/stands", tags=["stands"])

staff_only = require_roles(UserRole.OPERATOR, UserRole.ADMIN)


@router.get("", response_model=list[StandOut])
async def list_stands(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(staff_only),
):
    result = await db.execute(select(Stand).order_by(Stand.name))
    return list(result.scalars().all())
