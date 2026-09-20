"""Solicitud de autorregistro de chofer y dispositivo vinculado al teléfono.

La solicitud vive aparte de `drivers` a propósito: hasta que un operador
aprueba, no hay User ni Driver, y por tanto no hay PIN ni device_token.
El folio CTM aquí es el ID operativo de la unidad (visible, compartible),
nunca una credencial.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import DriverAccountStatus, DriverDeviceStatus, DriverUnitRole

if TYPE_CHECKING:
    from app.models.user import Driver, User


class DriverApplication(Base):
    __tablename__ = "driver_applications"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    full_name: Mapped[str] = mapped_column(String(150), nullable=False)
    # Opcional por ahora: útil "por si un día" (OTP). 10 dígitos MX si viene.
    phone: Mapped[str | None] = mapped_column(String(20))
    email: Mapped[str | None] = mapped_column(String(255))
    # ID operativo de la unidad (ej. CTM-045). Visible. Nunca password.
    folio_ctm: Mapped[str] = mapped_column(String(20), nullable=False)
    license_plate: Mapped[str] = mapped_column(String(15), nullable=False)
    unit_role: Mapped[DriverUnitRole] = mapped_column(
        Enum(
            DriverUnitRole,
            name="driver_unit_role",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    photo_profile_url: Mapped[str | None] = mapped_column(String(512))
    photo_license_url: Mapped[str | None] = mapped_column(String(512))
    # No hay photo_union_card_url: el tarjetón no se pide.
    status: Mapped[DriverAccountStatus] = mapped_column(
        Enum(
            DriverAccountStatus,
            name="driver_account_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=DriverAccountStatus.PENDING_APPROVAL,
        nullable=False,
    )
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    driver_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("drivers.id")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    driver: Mapped[Driver | None] = relationship("Driver", foreign_keys=[driver_id])
    reviewer: Mapped[User | None] = relationship("User", foreign_keys=[reviewed_by])

    __table_args__ = (
        Index("ix_driver_applications_status", "status"),
        Index("ix_driver_applications_phone", "phone"),
        Index("ix_driver_applications_folio_ctm", "folio_ctm"),
        Index("ix_driver_applications_created_at", "created_at"),
    )


class DriverDevice(Base):
    """Token de dispositivo del teléfono del chofer — distinto de
    Vehicle.device_key (GPS de la unidad). El token en claro se muestra
    una sola vez al hacer bind; aquí solo queda el hash."""

    __tablename__ = "driver_devices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    driver_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("drivers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    device_token_hash: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    device_id_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[DriverDeviceStatus] = mapped_column(
        Enum(
            DriverDeviceStatus,
            name="driver_device_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=DriverDeviceStatus.ACTIVE,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    driver: Mapped[Driver] = relationship(back_populates="devices")

    __table_args__ = (
        Index("ix_driver_devices_device_id_hash", "device_id_hash"),
        Index(
            "uq_driver_devices_active_driver",
            "driver_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )
