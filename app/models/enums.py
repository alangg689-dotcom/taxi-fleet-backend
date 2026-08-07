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


class StandQueueStatus(str, enum.Enum):
    """Estado persistido de un lugar en la fila de un sitio — no confundir
    con los sub-estados de la máquina (fuera/dentro/candidato), que son
    derivados y no se guardan (ver spec-sitios-y-fila-v2.md, sección 7)."""

    FORMADO = "formado"
    ASIGNADO = "asignado"
    SALIO = "salio"
