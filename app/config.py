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
    # El chofer no usa refresh token (ver POST /auth/driver-login): un access
    # token largo que cubre un turno completo en vez de renovación en
    # silencio — así el teléfono nunca guarda una credencial reutilizable
    # más allá de lo que dure el turno. 16h porque los turnos se estiran y no
    # debe expirar a media jornada.
    DRIVER_ACCESS_TOKEN_HOURS: int = 16

    # --- Login (throttle compartido: email de operador/admin o teléfono de chofer) ---
    LOGIN_MAX_ATTEMPTS: int = 5          # fallos antes de bloquear
    LOGIN_ATTEMPT_WINDOW: int = 900      # ventana de 15 minutos para contar fallos
    LOGIN_LOCKOUT_SECONDS: int = 1800    # bloqueo de 30 minutos

    # --- Telemetría ---
    LOCATION_BATCH_MAX: int = 100        # máx. pings por lote (buffer offline)
    LOCATION_CHANNEL: str = "fleet:updates"   # canal Redis pub/sub
    LAST_POSITION_TTL: int = 3600        # TTL de la última posición en cache
    # Techo de pings por unidad y ventana (app.core.ping_throttle). El
    # device_key es una credencial fija que vive en el teléfono y no expira:
    # si se filtra, esto acota el daño. Holgado a propósito — la cadencia
    # normal es 3s dentro de un sitio (20/min) y 15s fuera (4/min), pero al
    # recuperar señal la app vacía su buffer en lotes de LOCATION_BATCH_MAX,
    # y un apagón de dos horas son ~480 pings acumulados que deben entrar sin
    # perderse. 600/min deja pasar eso y aun así corta un flood real, que
    # serían miles por segundo.
    PING_MAX_PER_WINDOW: int = 600
    PING_WINDOW_SECONDS: int = 60

    # --- Bot de WhatsApp (Twilio) ---
    # El login de chofer ya no usa Twilio (ver spec de PIN, app.api.auth).
    # Estas credenciales sirven al bot de clientes y a la entrega de la
    # device_key GPS al chofer (notify_driver_device_key). Vacías: se
    # registra un warning y no se manda nada; el JSON sigue trayendo la clave.
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    # Número compartido del sandbox de Twilio por default — el mismo para
    # cualquier cuenta mientras se prueba. Al pasar a un perfil de WhatsApp
    # Business propio, se reemplaza por el número real aprobado por Meta.
    TWILIO_WHATSAPP_FROM: str = "whatsapp:+14155238886"

    # --- Bot de Telegram ---
    # Token que da @BotFather. Vacío = el canal de Telegram queda apagado: el
    # endpoint del bot lo rechaza en vez de crear viajes que nunca se van a
    # poder contestar.
    TELEGRAM_BOT_TOKEN: str = ""

    # --- Puerta de entrada de los bots (POST /bot/request-ride) ---
    # Ese endpoint crea viajes reales sin sesión de operador ni de chofer, así
    # que no puede quedar abierto: quien lo alcance puede llenar la flotilla de
    # servicios fantasma. El proceso del bot manda esta clave en X-Bot-Key.
    # Vacía = el endpoint responde 503 (apagado), nunca "abierto a todos".
    BOT_API_KEY: str = ""

    # --- Aviso de llegada al punto de recogida ---
    # Radio en metros para dar por llegada la unidad. 30 m es el orden del
    # error típico de un GPS de celular en calle con edificios: más chico da
    # falsos negativos (el taxi está en la puerta y nunca avisa), más grande
    # avisa desde la esquina anterior.
    TRIP_ARRIVAL_RADIUS_METERS: float = 30.0

    # --- Motor de despacho automático ---
    DISPATCH_OFFER_TIMEOUT_SECONDS: int = 25   # tiempo para aceptar/rechazar antes de pasar al siguiente
    DISPATCH_SEARCH_RADIUS_METERS: int = 15000  # 5000 se quedaba corto para 30-40km de cobertura real
    DISPATCH_MAX_CANDIDATES: int = 10          # tope de candidatos a recorrer por viaje
    # Alineado con QUEUE_SIGNAL_DROP_SECONDS (spec de sitios, sección 5): si no
    # coinciden, hay una ventana donde alguien sigue en la fila pero el
    # despacho ya no lo considera candidato, y la fila avanza sin razón visible.
    DISPATCH_POSITION_FRESHNESS_SECONDS: int = 420  # un ping más viejo que esto no cuenta como "en línea"
    DISPATCH_POLL_INTERVAL_SECONDS: float = 1.0     # cada cuánto se revisa si ya respondieron
    DISPATCH_TIER_ADVANTAGE_SECONDS: int = 300      # ventaja mínima para que un escalón inferior salte la fila (sitios)
    DISPATCH_POST_TRIP_COOLDOWN_SECONDS: int = 60   # antes de que una unidad recién liberada cuente para otra zona
    DISPATCH_ETA_SPEED_KMH: float = 25.0            # proxy de velocidad para ETA — no hay ruteo real todavía

    # --- Bot de WhatsApp: reintento de viajes atorados ---
    # Un viaje del bot sin candidatos (o que nadie aceptó) ya NO se cancela
    # solo — se queda "solicitado" y este barrido lo reintenta, porque la
    # disponibilidad de la flota cambia con el tiempo (ver app.core.whatsapp_bot).
    BOT_TRIP_SWEEP_INTERVAL_SECONDS: int = 30
    # Cuánto aguanta un viaje esperando chofer antes de que se le avise al
    # cliente que no hay taxis. Dejó de ser fijo: en operación normal, más de
    # 10 minutos sin colocar un viaje casi siempre significa que no va a
    # haber, y decirlo pronto es mejor que dejar a alguien parado en la calle.
    # Con la calle saturada la lectura se invierte — sí va a haber taxi, solo
    # que tarda — y rendirse a los 10 minutos sería tirar un viaje que se
    # habría podido servir.
    BOT_TRIP_MAX_WAIT_SECONDS: int = 600                # 10 min — operación normal
    BOT_TRIP_MAX_WAIT_HIGH_DEMAND_SECONDS: int = 1200   # 20 min — alta demanda
    # "Alta demanda" exige las DOS condiciones a la vez. Seis viajes esperando
    # con veinte choferes libres no es saturación, es una racha de un minuto; y
    # dos choferes libres sin viajes en cola tampoco es problema de nadie.
    HIGH_DEMAND_MIN_WAITING_TRIPS: int = 5
    HIGH_DEMAND_MAX_AVAILABLE_DRIVERS: int = 3
    # Techo de viajes CREADOS por cliente en /bot/request-ride (las peticiones
    # que solo devuelven already_active no cuentan, ver app.api.bot). Generoso
    # a propósito: el abuso real es un script en bucle, que haría cientos —
    # un cliente que se arrepiente tres veces seguidas no es abuso. Mismo
    # contador atómico de Redis que el throttle de login y el de pings.
    BOT_RIDE_MAX_PER_WINDOW: int = 10
    BOT_RIDE_WINDOW_SECONDS: int = 300

    # --- Autorregistro de choferes (público, sin OTP) ---
    # Fotos de rostro/licencia. UUID como nombre; se sirven en /api/v1/uploads.
    UPLOAD_DIR: str = "uploads"
    UPLOAD_MAX_BYTES: int = 5 * 1024 * 1024  # 5 MB
    # Techos por IP / teléfono / folio. Ventana fija, mismo incr_with_ttl
    # que el resto de throttles. Holgados: un alta real son 1 solicitud +
    # 2 fotos; esto frena un script, no a un chofer con mala señal.
    DRIVER_APP_RATE_LIMIT_MAX: int = 8
    DRIVER_APP_RATE_LIMIT_WINDOW_SECONDS: int = 3600
    DRIVER_UPLOAD_RATE_LIMIT_MAX: int = 20
    DRIVER_UPLOAD_RATE_LIMIT_WINDOW_SECONDS: int = 3600
    DRIVER_STATUS_RATE_LIMIT_MAX: int = 30
    DRIVER_STATUS_RATE_LIMIT_WINDOW_SECONDS: int = 600
    DRIVER_SET_PIN_RATE_LIMIT_MAX: int = 10
    DRIVER_SET_PIN_RATE_LIMIT_WINDOW_SECONDS: int = 900

    # --- Sitios y fila de espera ---
    # Ver spec-sitios-y-fila-v2.md. Los defaults de sitio individual
    # (still_seconds, max_speed_kmh, buffer del polígono) viven como columnas
    # en `stands` una vez exista esa tabla (sección 4 de la spec) — estos son
    # el fallback mientras tanto / para sitios que no los personalizan.
    STAND_STILL_SECONDS_DEFAULT: int = 45        # cronómetro de detención para disputar orden
    STAND_MAX_SPEED_KMH: float = 5.0
    STAND_EXIT_CONSECUTIVE_PINGS: int = 3        # lecturas seguidas fuera para confirmar salida
    STAND_POLYGON_BUFFER_METERS: int = 15        # holgura al trazar (8-10 en sitios de banqueta angosta)
    # Decisión de negocio (no está en la spec original): la "inserción
    # inmediata" con fila vacía deja un hueco — un sitio de esquina con
    # semáforo insertaría a un carro parado en un alto. Con fila vacía no
    # hay disputa de orden que resolver, así que el mínimo es mucho más
    # corto que still_seconds, pero tiene que existir.
    STAND_EMPTY_QUEUE_MIN_STOP_SECONDS: int = 12
    STAND_SWEEP_INTERVAL_SECONDS: int = 30       # barrido de cronómetros/pérdida de señal (sección 7)
    QUEUE_PING_MAX_LAG_SECONDS: int = 90         # ping más viejo que esto no mueve la fila
    CLOCK_DRIFT_MAX_SECONDS: int = 60            # |timestamp - received_at| más allá de esto es sospechoso
    QUEUE_SIGNAL_WARN_SECONDS: int = 120         # avisar a la operadora, conserva el lugar
    QUEUE_SIGNAL_DROP_SECONDS: int = 420         # sacar de la fila por falta de señal
    GPS_MAX_ACCURACY_METERS: float = 50.0        # peor precisión de ping aceptada para mover la fila
    GPS_MAX_JUMP_KMH: float = 200.0              # salto imposible respecto al ping anterior
    QUEUE_CHANNEL: str = "stand:queue"           # canal Redis pub/sub — vive en fleet:updates aparte

    @property
    def cors_origins(self) -> list[str]:
        if self.CORS_ORIGINS == "*":
            return ["*"]
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]


settings = Settings()
