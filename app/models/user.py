"""Modelos de identidad: USER es la base de autenticación; DRIVER y OPERATOR
son los perfiles operativos que cuelgan de ella."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import DriverStatus, DriverUnitRole, PermissionLevel, UserRole


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # email/phone son mutuamente excluyentes según el rol:
    # choferes se autentican con teléfono + PIN, operadores con email + password.
    email: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(20), unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role", values_callable=lambda e: [m.value for m in e])
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    driver: Mapped["Driver"] = relationship(back_populates="user", uselist=False)
    operator: Mapped["Operator"] = relationship(back_populates="user", uselist=False)
    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Driver(Base):
    __tablename__ = "drivers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    full_name: Mapped[str] = mapped_column(String(150))
    license_number: Mapped[str] = mapped_column(String(50), unique=True)
    # Numeral de radio (R18, R20…): con esto lo nombra la operadora, no con
    # la placa de la unidad — el chofer puede cambiar de unidad y su numeral
    # lo sigue. Es lo que se pinta en el mapa de flota.
    # Nulo para los que se dieron de alta antes de la migración 0011; un
    # operador se los asigna con PATCH /drivers/{id}.
    numeral: Mapped[str | None] = mapped_column(String(10))
    status: Mapped[DriverStatus] = mapped_column(
        Enum(DriverStatus, name="driver_status", values_callable=lambda e: [m.value for m in e]),
        default=DriverStatus.ACTIVO,
    )
    # Token de push de Expo del teléfono del chofer — la red de seguridad
    # para cuando /ws/driver no tiene un socket vivo (app en segundo plano o
    # cerrada). Se sobreescribe en cada registro; no hace falta soportar
    # varios dispositivos por chofer en una flotilla de este tamaño.
    push_token: Mapped[str | None] = mapped_column(String(255))
    # Login del chofer: teléfono + PIN. Tras autorregistro aprobado el
    # chofer lo inventa (POST /auth/driver/set-pin); el operador ya no
    # genera uno al aprobar. Nulo / must_set_pin=true = no puede entrar.
    pin_hash: Mapped[str | None] = mapped_column(String(255))
    must_set_pin: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    # ID operativo de la unidad (ej. CTM-045). Visible; varios choferes
    # (titular / turno) pueden compartir el mismo. NO es el numeral de
    # radio (R18) ni un secreto de login.
    folio_ctm: Mapped[str | None] = mapped_column(String(20), index=True)
    unit_role: Mapped[DriverUnitRole | None] = mapped_column(
        Enum(
            DriverUnitRole,
            name="driver_unit_role",
            values_callable=lambda e: [m.value for m in e],
        )
    )

    user: Mapped["User"] = relationship(back_populates="driver")
    assignments: Mapped[list["VehicleAssignment"]] = relationship(
        back_populates="driver"
    )
    devices: Mapped[list["DriverDevice"]] = relationship(
        "DriverDevice", back_populates="driver", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # Parcial a propósito: el numeral es único entre los que sí lo tienen,
        # pero los migrados de antes de la 0011 están todos en NULL y no
        # deben chocar entre sí. Ver la migración 0011.
        Index(
            "ix_drivers_numeral",
            "numeral",
            unique=True,
            postgresql_where=text("numeral IS NOT NULL"),
        ),
    )


class Operator(Base):
    __tablename__ = "operators"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    full_name: Mapped[str] = mapped_column(String(150))
    permission_level: Mapped[PermissionLevel] = mapped_column(
        Enum(
            PermissionLevel,
            name="permission_level",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=PermissionLevel.DESPACHADOR,
    )

    user: Mapped["User"] = relationship(back_populates="operator")


class RefreshToken(Base):
    """Se guarda el HASH del token, nunca el token en claro.
    device_info permite revocar la sesión de un dispositivo específico."""

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    device_info: Mapped[str | None] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="refresh_tokens")
