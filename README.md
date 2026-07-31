# Flotilla GPS — Backend

API de geolocalización y monitoreo en tiempo real para flotilla de taxis (~100 unidades).

## Stack

| Pieza | Tecnología | Para qué |
|---|---|---|
| API | FastAPI (Python 3.12) | REST + WebSocket |
| Base de datos | PostgreSQL 16 + PostGIS | Datos operativos y consultas espaciales |
| Series de tiempo | TimescaleDB | Historial de posiciones, particionado y retención |
| Cache / bus | Redis | Última posición, rate limit de OTP, pub/sub |
| Migraciones | Alembic | Versionado del esquema |

## Arranque local

```bash
cp .env.example .env
docker compose up -d db redis        # levanta TimescaleDB+PostGIS y Redis

pip install -r requirements.txt
alembic upgrade head                 # crea extensiones, tablas e hypertable
python -m scripts.seed_admin admin@flotilla.mx MiPassword123

uvicorn app.main:app --reload
```

Documentación interactiva: <http://localhost:8000/docs>

## Pruebas

```bash
pip install -r requirements-dev.txt
pytest
```

Corren contra Postgres real (`flotilla_test`, en el mismo contenedor de `docker compose`), no contra SQLite ni mocks: media base de código son consultas espaciales y enums nativos de Postgres que un motor distinto no reproduce fielmente — ese desajuste ya costó dos bugs en producción antes de que existiera esta suite. `conftest.py` crea la base de pruebas y corre las migraciones automáticamente la primera vez; solo hace falta que `db`/`redis` estén levantados (`docker compose up -d db redis`).

Los WebSockets (`/ws/driver`, `/ws/fleet`) se prueban con [httpx-ws](https://github.com/frankie567/httpx-ws) en vez de `starlette.testclient.TestClient`: este último corre la app en un hilo aparte con su propio event loop, y la sesión de prueba (ligada al loop del test vía SAVEPOINT) reventaría con un error de "Future attached to a different loop" en cuanto la tocara desde ahí. httpx-ws viaja sobre el mismo tipo de transporte ASGI que el resto de las pruebas, así que todo corre en un único loop.

Cada test corre dentro de un SAVEPOINT que se revierte al final, así ninguno ve los datos de otro y nunca se toca la base de datos de desarrollo.

## Estructura

```
app/
├── main.py              punto de entrada, lifespan, routers
├── config.py            configuración desde variables de entorno
├── database.py          engine y sesión async de SQLAlchemy
├── models/              tablas ORM (el ERD traducido a código)
├── schemas/             validación de entrada/salida (Pydantic)
├── core/
│   ├── security.py      hashing y JWT
│   ├── otp.py           códigos OTP con contadores atómicos en Redis
│   ├── redis_client.py  cache de posiciones y pub/sub
│   └── deps.py          guardas de autenticación y RBAC
├── api/                 routers REST
└── ws/fleet.py          WebSockets del chofer y del dashboard
```

## Autenticación

Dos caminos que terminan en el mismo par de tokens:

- **Choferes** — teléfono + OTP por SMS. Sin contraseñas que memorizar en campo.
- **Operadores / admin** — email + contraseña desde el dashboard.
- **Dispositivos** — cada unidad recibe una `device_key` al darse de alta. El endpoint de telemetría la valida en lugar de un JWT completo, porque se invoca cada 5-10 segundos por unidad.

El access token es un JWT de 15 minutos. El refresh token es opaco, se guarda hasheado y **rota** en cada uso: al renovarlo, el anterior se revoca, de modo que un token robado deja de servir en cuanto el dueño legítimo lo usa.

Los límites del OTP (3 solicitudes por 15 min, 5 fallos antes de bloquear 30 min) se aplican con scripts Lua en Redis. Es lo que evita que tres toques al botón "reenviar" en el mismo segundo se salten el contador.

`/auth/login` tiene el mismo mecanismo (5 fallos por email en 15 min → bloqueo de 30 min, `LOGIN_MAX_ATTEMPTS`/`LOGIN_ATTEMPT_WINDOW`/`LOGIN_LOCKOUT_SECONDS`), reusando el contador atómico de OTP (`app.core.redis_client.incr_with_ttl`). El bloqueo se cuenta igual exista o no el email, para no delatar qué cuentas están registradas.

El SMS se manda con `SMS_PROVIDER=twilio` (REST directo por `httpx`, sin el SDK oficial porque es síncrono y bloquearía el loop — ver [.env.example](.env.example) para las tres variables que hace falta llenar). Si Twilio falla, el endpoint **no lo refleja al cliente**: sigue respondiendo el mismo mensaje genérico y solo deja el error en el log. Devolver un código distinto revelaría que ese teléfono sí está registrado (a uno inexistente nunca se le intenta mandar SMS), justo el hueco de enumeración que este endpoint ya evita a propósito.

## Endpoints

**Auth** — `POST /auth/otp/request` · `/auth/otp/verify` · `/auth/login` · `/auth/refresh` · `/auth/logout`

**Vehículos** — `GET|POST /vehicles` (paginado, ver abajo) · `GET|PATCH /vehicles/{id}` · `POST /vehicles/{id}/assignments` (abre turno y cierra el anterior) · `GET /vehicles/{id}/assignments` (historial) · `POST /vehicles/{id}/assignments/close`

**Choferes** — `GET|POST /drivers` (alta solo admin; listado paginado) · `GET|PATCH /drivers/{id}` · `POST /drivers/{id}/deactivate|reactivate` (revoca/restaura el login; solo admin)

**Telemetría** — `POST /location/ping` · `GET /vehicles/locations` (snapshot de flota) · `GET /vehicles/{id}/location` · `GET /vehicles/nearby?lat=&lng=&radius=` · `GET /vehicles/{id}/history?since=&until=` (paginado, default 500/tope 2000 — ver nota abajo) · `GET /vehicles/{id}/history/summary?since=&until=` (posición promedio cada 5 min, para rangos largos)

**Viajes** — `POST /trips` (despacha unidad + chofer) · `GET /trips?status=&vehicle_id=&driver_id=` (paginado; staff ve toda la flota, un chofer solo los suyos — `driver_id` se ignora si lo manda uno) · `GET /trips/{id}` · `POST /trips/{id}/accept` · `POST /trips/{id}/start` · `POST /trips/{id}/complete` · `POST /trips/{id}/cancel`

Los listados (`/vehicles`, `/drivers`, `/trips`) aceptan `limit` (default 50, máximo 200) y `offset`. `GET /vehicles/{id}/history` usa un default y un tope más altos (500/2000): con ~10-20 pings/segundo de toda la flota, un solo día de una unidad ya son varios miles de filas, y el tope de 200 de los demás listados lo haría inservible para su uso normal (dibujar una ruta completa). El total que coincide con los filtros —antes de aplicar `limit`/`offset`— va en el header de respuesta `X-Total-Count`, no en el cuerpo: así el JSON se queda como una lista plana y no rompe a nadie que ya lo consuma sin paginar. Ese header está expuesto por CORS (`Access-Control-Expose-Headers`) para que un dashboard en el navegador pueda leerlo con `fetch()`.

**Tiempo real** — `WS /ws/driver?device_key=...` · `WS /ws/fleet?token=...`

> El router de telemetría se registra **antes** que el de vehículos en `main.py`. FastAPI resuelve rutas en orden, y si fuera al revés, `/vehicles/{vehicle_id}` capturaría `/vehicles/nearby` e intentaría leer "nearby" como UUID.

## Tiempo real

```
app chofer ──ws──> Servidor A
                   ├─> TimescaleDB   (historial)
                   ├─> Redis SET     (última posición)
                   └─> Redis PUBLISH fleet:updates
                                          │
                       ┌──────────────────┴──────────────────┐
                   Servidor A          Servidor B         Servidor C
                       │                   │                   │
                   dashboards          dashboards          dashboards
```

Redis Pub/Sub es lo que permite correr más de una instancia detrás de un balanceador. Si el chofer queda conectado al Servidor A y el dashboard al Servidor B, sin ese canal común B nunca se enteraría del ping que llegó a A.

Con ~100 unidades reportando cada 5-10 s son unos 10-20 mensajes por segundo — muy por debajo del punto donde Redis Pub/Sub se queda corto y haría falta algo como NATS.

## Buffer offline

Cuando el taxi pasa por un túnel o zona sin cobertura, la app acumula posiciones localmente y las descarga de golpe al recuperar señal. El diseño lo contempla en cuatro puntos:

1. `POST /location/ping` acepta un ping suelto o un lote de hasta 100.
2. La columna `timestamp` guarda la hora del **GPS del dispositivo**, no la de llegada al servidor. `received_at` conserva la hora de llegada solo para medir latencia.
3. Restricción única `(vehicle_id, timestamp)` + `ON CONFLICT DO NOTHING`: si la app reenvía un lote porque no recibió el ACK, los repetidos se descartan sin duplicar el historial.
4. El WebSocket responde un ACK explícito. **La app no debe borrar su buffer local hasta recibirlo.**

Del lote solo se difunde al mapa el ping más reciente: el dashboard únicamente necesita la posición actual, el resto ya quedó guardado para el historial de rutas.

## Viajes

No hay un "marketplace" de choferes disponibles: es una operadora despachando llamadas, así que `vehicle_id`/`driver_id` se capturan desde el alta del viaje, no después. El estado avanza así:

```
SOLICITADO --accept--> ASIGNADO --start--> EN_CURSO --complete--> COMPLETADO
                \_______________________________________/
                                  \--cancel--> CANCELADO
```

`accept`/`start`/`complete` los dispara normalmente la app del chofer (o el operador, en su nombre); `cancel` puede venir de cualquiera de los dos lados mientras el viaje no haya terminado. Un chofer solo puede actuar sobre sus propios viajes; operador/admin sobre cualquiera. Antes de crear un viaje se verifica que la unidad no tenga ya uno activo, para no despachar la misma unidad dos veces.

## Notas sobre el modelo de datos

- **`VEHICLE_ASSIGNMENT` en vez de `current_driver_id`.** Una columna suelta pierde el historial en cuanto rota el segundo chofer. Con `started_at`/`ended_at` queda la trazabilidad completa de turnos; el chofer actual es la asignación con `ended_at IS NULL`. Un índice único parcial impide dos turnos abiertos en la misma unidad.
- **`GEOGRAPHY(Point, 4326)` en vez de dos columnas decimal.** Con un índice GiST, `ST_DWithin` resuelve cercanía y geofencing en milisegundos. Con lat/lng sueltos habría que calcular Haversine fila por fila, sin índice.
- **`location_pings` es una hypertable de TimescaleDB.** El particionado por fecha (fragmentos de 7 días), la compresión a los 30 días y la retención a los 365 se declaran una vez en la migración inicial y corren solos. Ajustar la retención según los requisitos legales de la operación.
- **`vehicle_position_5min` es un agregado continuo.** Vista materializada con posición/velocidad promedio por unidad cada 5 minutos, mantenida al día por su propia política de refresco de TimescaleDB (hasta ~5 min de rezago). Existe para que la reportería sobre rangos largos (`GET /vehicles/{id}/history/summary`) no tenga que promediar millones de pings crudos en cada consulta.
- **`Driver.status` no es lo mismo que `User.is_active`.** El primero es operativo (activo/inactivo — de vacaciones, por ejemplo, y sigue pudiendo entrar); el segundo es la cuenta (`POST /drivers/{id}/deactivate`), y corta el login de inmediato porque `get_current_user` revisa `is_active` en cada request, no solo al emitir el token.

## Pendiente

- [x] Router de viajes (`/trips`)
- [x] Integración real de Twilio (`SMS_PROVIDER=twilio`; `console` sigue disponible para dev)
- [x] Suite de pruebas con pytest (auth, vehicles, trips, reportería)
- [x] Restringir CORS al dominio del dashboard antes de producción (`CORS_ORIGINS` en `.env`)
- [x] Agregados continuos de TimescaleDB para reportería (`vehicle_position_5min`, cada 5 min)
- [x] Pruebas de los WebSockets (`/ws/driver`, `/ws/fleet`)
- [x] "Mis viajes" — un chofer ahora puede hacer `GET /trips` y ver los suyos sin conocer el ID de antemano
- [x] Probar la integración de Twilio contra una cuenta real (verificado en vivo: SMS entregado a un teléfono real)
- [x] Paginación en los endpoints de listado (`/vehicles`, `/drivers`, `/trips`; total en el header `X-Total-Count`)
