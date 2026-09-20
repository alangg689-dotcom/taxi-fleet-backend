"""Autorregistro público de choferes y cola de aprobación del dashboard.

Público (rate-limited, sin OTP, sin tarjetón):
  POST /driver-applications
  POST /driver-applications/uploads   kind=profile|license
  GET  /driver-applications/status

Staff (JWT operador/admin):
  GET  /driver-applications
  GET  /driver-applications/{id}
  POST /driver-applications/{id}/approve   — crea User+Driver, NO genera PIN
  POST /driver-applications/{id}/reject
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import rate_limit
from app.core.deps import require_roles
from app.core.phone import find_user_by_phone
from app.core.storage import UPLOAD_KINDS, save_image
from app.database import get_db
from app.models import (
    Driver,
    DriverAccountStatus,
    DriverApplication,
    Stand,
    User,
    UserRole,
    Vehicle,
)
from app.schemas.driver_application import (
    ApplicationApproveOut,
    DriverApplicationApprove,
    DriverApplicationCreate,
    DriverApplicationCreated,
    DriverApplicationOut,
    DriverApplicationReject,
    DriverApplicationStatusOut,
    UploadCreated,
)

router = APIRouter(prefix="/driver-applications", tags=["autorregistro"])
uploads_public_router = APIRouter(prefix="/uploads", tags=["uploads"])

staff_only = require_roles(UserRole.OPERATOR, UserRole.ADMIN)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def _limit_public(request: Request, *extra_keys: str) -> None:
    """Rate-limit por IP y, si vienen, por teléfono/folio."""
    keys = [f"rl:driver-app:ip:{_client_ip(request)}", *extra_keys]
    try:
        for key in keys:
            await rate_limit.hit(
                key,
                settings.DRIVER_APP_RATE_LIMIT_MAX,
                settings.DRIVER_APP_RATE_LIMIT_WINDOW_SECONDS,
            )
    except rate_limit.RateLimitExceeded as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc


async def _limit_uploads(request: Request) -> None:
    try:
        await rate_limit.hit(
            f"rl:driver-upload:ip:{_client_ip(request)}",
            settings.DRIVER_UPLOAD_RATE_LIMIT_MAX,
            settings.DRIVER_UPLOAD_RATE_LIMIT_WINDOW_SECONDS,
        )
    except rate_limit.RateLimitExceeded as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc


async def _limit_status(request: Request) -> None:
    try:
        await rate_limit.hit(
            f"rl:driver-status:ip:{_client_ip(request)}",
            settings.DRIVER_STATUS_RATE_LIMIT_MAX,
            settings.DRIVER_STATUS_RATE_LIMIT_WINDOW_SECONDS,
        )
    except rate_limit.RateLimitExceeded as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc


async def _get_application_or_404(
    db: AsyncSession, application_id: uuid.UUID
) -> DriverApplication:
    application = await db.get(DriverApplication, application_id)
    if application is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Solicitud no encontrada")
    return application


async def _pending_phone_conflict(db: AsyncSession, phone: str) -> bool:
    result = await db.execute(
        select(DriverApplication.id).where(
            DriverApplication.phone == phone,
            DriverApplication.status == DriverAccountStatus.PENDING_APPROVAL,
        )
    )
    return result.scalar_one_or_none() is not None


# --- Público -----------------------------------------------------------------


@router.post("", response_model=DriverApplicationCreated, status_code=status.HTTP_201_CREATED)
async def create_application(
    payload: DriverApplicationCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    extra = []
    if payload.phone:
        extra.append(f"rl:driver-app:phone:{payload.phone}")
    extra.append(f"rl:driver-app:folio:{payload.folio_ctm}")
    await _limit_public(request, *extra)

    if payload.phone:
        existing_user = await find_user_by_phone(db, payload.phone)
        if existing_user is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "Ya existe una cuenta con ese teléfono"
            )
        if await _pending_phone_conflict(db, payload.phone):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Ya hay una solicitud en validación con ese teléfono",
            )

    application = DriverApplication(
        full_name=payload.full_name,
        phone=payload.phone,
        email=payload.email,
        folio_ctm=payload.folio_ctm,
        license_plate=payload.license_plate,
        unit_role=payload.unit_role,
        photo_profile_url=payload.photo_profile_url,
        photo_license_url=payload.photo_license_url,
        status=DriverAccountStatus.PENDING_APPROVAL,
    )
    db.add(application)
    await db.flush()
    return DriverApplicationCreated(
        application_id=application.id, status=application.status
    )


@router.post("/uploads", response_model=UploadCreated)
async def upload_application_photo(
    request: Request,
    kind: str = Form(..., examples=["profile"]),
    file: UploadFile = File(...),
):
    """Sube foto de rostro o de licencia. No existe kind=union_card."""
    await _limit_uploads(request)
    if kind not in UPLOAD_KINDS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "kind debe ser profile o license",
        )
    url = await save_image(file)
    return UploadCreated(url=url, kind=kind)  # type: ignore[arg-type]


@router.get("/status", response_model=DriverApplicationStatusOut)
async def get_application_status(
    request: Request,
    db: AsyncSession = Depends(get_db),
    phone: str | None = Query(None),
    application_id: uuid.UUID | None = Query(None),
):
    await _limit_status(request)
    if application_id is None and not phone:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Indica phone o application_id",
        )

    application: DriverApplication | None = None
    if application_id is not None:
        application = await db.get(DriverApplication, application_id)
    else:
        from app.core.phone import InvalidPhoneError, normalize_mx_phone

        try:
            normalized = normalize_mx_phone(phone)
        except InvalidPhoneError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        if normalized is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "Indica phone o application_id"
            )
        result = await db.execute(
            select(DriverApplication)
            .where(DriverApplication.phone == normalized)
            .order_by(DriverApplication.created_at.desc())
            .limit(1)
        )
        application = result.scalar_one_or_none()

    if application is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Solicitud no encontrada")

    must_set_pin = False
    if application.driver_id is not None:
        driver = await db.get(Driver, application.driver_id)
        if driver is not None:
            must_set_pin = driver.must_set_pin

    return DriverApplicationStatusOut(
        application_id=application.id,
        status=application.status,
        rejection_reason=application.rejection_reason,
        must_set_pin=must_set_pin,
        folio_ctm=application.folio_ctm,
    )


# --- Staff -------------------------------------------------------------------


@router.get("", response_model=list[DriverApplicationOut])
async def list_applications(
    response: Response,
    status_filter: DriverAccountStatus | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(staff_only),
):
    query = select(DriverApplication)
    count_query = select(func.count()).select_from(DriverApplication)
    if status_filter is not None:
        query = query.where(DriverApplication.status == status_filter)
        count_query = count_query.where(DriverApplication.status == status_filter)

    total = await db.scalar(count_query)
    response.headers["X-Total-Count"] = str(total or 0)

    result = await db.execute(
        query.order_by(DriverApplication.created_at.desc()).limit(limit).offset(offset)
    )
    return list(result.scalars().all())


@router.get("/{application_id}", response_model=DriverApplicationOut)
async def get_application(
    application_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(staff_only),
):
    return await _get_application_or_404(db, application_id)


@router.post("/{application_id}/approve", response_model=ApplicationApproveOut)
async def approve_application(
    application_id: uuid.UUID,
    payload: DriverApplicationApprove,
    db: AsyncSession = Depends(get_db),
    reviewer: User = Depends(staff_only),
):
    """Crea User (role=DRIVER) + Driver. No genera PIN: pin_hash queda
    vacío y must_set_pin=true. El folio CTM se copia como campo operativo,
    nunca como secreto. Si la placa ya existe, se le asocia el folio; si
    no y viene stand_id, se da de alta la unidad sin device_key GPS."""
    application = await _get_application_or_404(db, application_id)
    if application.status != DriverAccountStatus.PENDING_APPROVAL:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "La solicitud ya fue revisada"
        )

    if application.phone:
        existing_user = await find_user_by_phone(db, application.phone)
        if existing_user is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "Ya existe una cuenta con ese teléfono"
            )

    user = User(phone=application.phone, role=UserRole.DRIVER)
    db.add(user)
    await db.flush()

    driver = Driver(
        user_id=user.id,
        full_name=application.full_name,
        license_number=f"APP-{application.id.hex}",
        numeral=None,
        pin_hash=None,
        must_set_pin=True,
        folio_ctm=application.folio_ctm,
        unit_role=application.unit_role,
    )
    db.add(driver)
    await db.flush()

    vehicle = await _associate_or_create_vehicle(db, application, payload)

    now = datetime.now(UTC)
    application.status = DriverAccountStatus.ACTIVE
    application.reviewed_by = reviewer.id
    application.reviewed_at = now
    application.driver_id = driver.id
    application.updated_at = now

    return ApplicationApproveOut(
        application_id=application.id,
        status=application.status,
        driver_id=driver.id,
        user_id=user.id,
        must_set_pin=True,
        folio_ctm=application.folio_ctm,
        vehicle_id=vehicle.id if vehicle is not None else None,
    )


@router.post("/{application_id}/reject", response_model=DriverApplicationOut)
async def reject_application(
    application_id: uuid.UUID,
    payload: DriverApplicationReject,
    db: AsyncSession = Depends(get_db),
    reviewer: User = Depends(staff_only),
):
    application = await _get_application_or_404(db, application_id)
    if application.status != DriverAccountStatus.PENDING_APPROVAL:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "La solicitud ya fue revisada"
        )

    now = datetime.now(UTC)
    application.status = DriverAccountStatus.REJECTED
    application.rejection_reason = payload.reason
    application.reviewed_by = reviewer.id
    application.reviewed_at = now
    application.updated_at = now
    return application


async def _associate_or_create_vehicle(
    db: AsyncSession,
    application: DriverApplication,
    payload: DriverApplicationApprove,
) -> Vehicle | None:
    plate = application.license_plate
    result = await db.execute(
        select(Vehicle).where(func.upper(Vehicle.plate) == plate.upper())
    )
    vehicle = result.scalar_one_or_none()

    if vehicle is None:
        by_folio = await db.execute(
            select(Vehicle).where(Vehicle.folio_ctm == application.folio_ctm)
        )
        vehicle = by_folio.scalar_one_or_none()

    if vehicle is None and payload.stand_id is not None:
        if await db.get(Stand, payload.stand_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Sitio no encontrado")
        vehicle = Vehicle(
            plate=plate,
            model=payload.model or f"Unidad {application.folio_ctm}",
            stand_id=payload.stand_id,
            folio_ctm=application.folio_ctm,
        )
        db.add(vehicle)
        await db.flush()
        return vehicle

    if vehicle is None:
        return None

    if vehicle.folio_ctm is None:
        vehicle.folio_ctm = application.folio_ctm
    return vehicle


@uploads_public_router.get("/{filename}")
async def get_uploaded_file(filename: str):
    from fastapi.responses import FileResponse

    from app.core.storage import resolve_filename

    path = resolve_filename(filename)
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Archivo no encontrado")
    return FileResponse(path)
