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


class CustomerChannel(str, enum.Enum):
    """Por dónde pidió el viaje el cliente, y por dónde hay que contestarle.

    No es un enum nativo de Postgres como los demás: se guarda como texto
    (ver la migración 20260811_2200_canal_del_cliente). Agregar un canal es
    entonces desplegar código, no una migración de tipo — que es justo lo que
    se quiere de una lista que va a crecer (Messenger, web, app del cliente).
    """

    WHATSAPP = "whatsapp"
    TELEGRAM = "telegram"


class StandQueueStatus(str, enum.Enum):
    """Estado persistido de un lugar en la fila de un sitio — no confundir
    con los sub-estados de la máquina (fuera/dentro/candidato), que son
    derivados y no se guardan (ver spec-sitios-y-fila-v2.md, sección 7)."""

    FORMADO = "formado"
    ASIGNADO = "asignado"
    SALIO = "salio"


class DriverAccountStatus(str, enum.Enum):
    """Estado de una solicitud de autorregistro (y de la cuenta que nace
    al aprobarla). No confundir con DriverStatus (activo/inactivo operativo
    — vacaciones, etc.)."""

    PENDING_APPROVAL = "PENDING_APPROVAL"
    ACTIVE = "ACTIVE"
    REJECTED = "REJECTED"
    SUSPENDED = "SUSPENDED"


class DriverUnitRole(str, enum.Enum):
    """Rol del chofer respecto a la unidad identificada por folio CTM.
    Un mismo folio puede tener titular y choferes de turno."""

    OWNER = "OWNER"
    SHIFT_DRIVER = "SHIFT_DRIVER"


class DriverDeviceStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"
