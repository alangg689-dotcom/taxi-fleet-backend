"""Configuración central de la aplicación (leída desde variables de entorno)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Aplicación ---
    APP_NAME: str = "Flotilla GPS API"
    DEBUG: bool = False

    # --- CORS ---
    # Coma-separado. "*" (default) es cómodo en dev pero nunca debe usarse en
    # producción: ver .env.example para el formato con el dominio real.
    CORS_ORIGINS: str = "*"

    # --- Base de datos (PostgreSQL + PostGIS + TimescaleDB) ---
    DATABASE_URL: str = (
        "postgresql+asyncpg://flotilla:flotilla@localhost:5432/flotilla"
    )

    # --- Redis ---
    REDIS_URL: str = "redis://localhost:6379/0"

    # --- JWT ---
    JWT_SECRET: str = "CAMBIAR-EN-PRODUCCION"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_MINUTES: int = 15
    REFRESH_TOKEN_DAYS: int = 30

    # --- OTP ---
    OTP_LENGTH: int = 6
    OTP_TTL_SECONDS: int = 300           # 5 minutos de vigencia
    OTP_MAX_REQUESTS: int = 3            # máx. solicitudes por ventana
    OTP_REQUEST_WINDOW: int = 900        # ventana de 15 minutos
    OTP_MAX_ATTEMPTS: int = 5            # fallos antes de bloquear
    OTP_LOCKOUT_SECONDS: int = 1800      # bloqueo de 30 minutos

    # --- Login (operadores/admin) ---
    LOGIN_MAX_ATTEMPTS: int = 5          # fallos antes de bloquear
    LOGIN_ATTEMPT_WINDOW: int = 900      # ventana de 15 minutos para contar fallos
    LOGIN_LOCKOUT_SECONDS: int = 1800    # bloqueo de 30 minutos

    # --- Telemetría ---
    LOCATION_BATCH_MAX: int = 100        # máx. pings por lote (buffer offline)
    LOCATION_CHANNEL: str = "fleet:updates"   # canal Redis pub/sub
    LAST_POSITION_TTL: int = 3600        # TTL de la última posición en cache

    # --- SMS ---
    SMS_PROVIDER: str = "console"        # "console" en dev, "twilio" en prod
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_FROM_NUMBER: str = ""

    # --- Motor de despacho automático ---
    DISPATCH_OFFER_TIMEOUT_SECONDS: int = 20   # tiempo para aceptar/rechazar antes de pasar al siguiente
    DISPATCH_SEARCH_RADIUS_METERS: int = 5000  # radio de búsqueda de unidades candidatas
    DISPATCH_MAX_CANDIDATES: int = 10          # tope de candidatos a recorrer por viaje
    DISPATCH_POSITION_FRESHNESS_SECONDS: int = 300  # un ping más viejo que esto no cuenta como "en línea"
    DISPATCH_POLL_INTERVAL_SECONDS: float = 1.0     # cada cuánto se revisa si ya respondieron

    @property
    def cors_origins(self) -> list[str]:
        if self.CORS_ORIGINS == "*":
            return ["*"]
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]


settings = Settings()
