"""Enumeraciones del dominio, compartidas por modelos y schemas."""

import enum


class UserRole(str, enum.Enum):
    DRIVER = "driver"
    OPERATOR = "operator"
    ADMIN = "admin"


class DriverStatus(str, enum.Enum):
    ACTIVO = "activo"
    INACTIVO = "inactivo"


class PermissionLevel(str, enum.Enum):
    ADMIN = "admin"
    DESPACHADOR = "despachador"


class VehicleStatus(str, enum.Enum):
    DISPONIBLE = "disponible"
    OCUPADO = "ocupado"
    OFFLINE = "offline"
    MANTENIMIENTO = "mantenimiento"


class TripStatus(str, enum.Enum):
    SOLICITADO = "solicitado"
    ASIGNADO = "asignado"
    EN_CURSO = "en_curso"
    COMPLETADO = "completado"
    CANCELADO = "cancelado"
