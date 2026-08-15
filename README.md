# Flotilla GPS — Backend

API de geolocalización y monitoreo en tiempo real para flotilla de taxis (~100 unidades).

## Stack

| Pieza | Tecnología | Para qué |
|---|---|---|
| API | FastAPI (Python 3.12) | REST + WebSocket |
| Base de datos | PostgreSQL 16 + PostGIS | Datos operativos y consultas espaciales |
| Series de tiempo | TimescaleDB | Historial de posiciones, particionado y retención |
| Cache / bus | Redis | Última posición, throttle de login y de telemetría, pub/sub |
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
│   ├── security.py         hashing y JWT
│   ├── login_throttle.py   bloqueo por intentos fallidos (email o teléfono)
│   ├── dispatch.py         motor de despacho automático
│   ├── stands.py           sitios y fila de espera
│   ├── ping_throttle.py    techo de pings por unidad y ventana
│   ├── ping_validation.py  validación del GPS antes de mover la fila
│   ├── push.py             notificaciones push vía Expo
│   ├── whatsapp_bot.py     bot de clientes y barrido de viajes atorados
│   ├── redis_client.py     cache de posiciones y pub/sub
│   └── deps.py             guardas de autenticación y RBAC
├── api/                 routers REST
└── ws/fleet.py          WebSockets del chofer y del dashboard
```

## Autenticación

Tres identidades distintas, no una sola:

- **Choferes** — teléfono + **PIN**, en un solo paso (`POST /auth/driver-login`). El PIN lo asigna el operador (`POST /drivers` o `POST /drivers/{id}/pin`), el chofer no lo elige. Sin contraseñas que memorizar en campo.
- **Operadores / admin** — email + contraseña desde el dashboard (`POST /auth/login`).
- **Dispositivos** — cada unidad recibe una `device_key` al darse de alta. El endpoint de telemetría la valida en lugar de un JWT completo, porque se invoca cada 5-10 segundos por unidad.

Operadores y admin reciben un par de tokens: access JWT de 15 minutos y refresh opaco, guardado hasheado, que **rota** en cada uso — al renovarlo el anterior se revoca, así un token robado deja de servir en cuanto el dueño legítimo lo usa.

El chofer **no recibe refresh token**: `driver-login` emite directo un access token de `DRIVER_ACCESS_TOKEN_HOURS` (16 h, para que cubra un turno completo sin expirar a media jornada). Es deliberado — el PIN no se guarda en el teléfono, así que tampoco debe quedar ahí una credencial de larga vida que lo reemplace: un aparato perdido o prestado deja de servir al terminar el turno.

Ambos logins comparten el mismo throttle (`app.core.login_throttle`, sobre el contador atómico `app.core.redis_client.incr_with_ttl`): 5 fallos en 15 min → bloqueo de 30 min, configurable con `LOGIN_MAX_ATTEMPTS`/`LOGIN_ATTEMPT_WINDOW`/`LOGIN_LOCKOUT_SECONDS`. El bloqueo se cuenta igual exista o no la cuenta, y el error es el mismo (`401 Credenciales inválidas`) sea teléfono inexistente, chofer sin PIN asignado o PIN incorrecto, para no delatar qué cuentas están registradas.

> **El PIN de un chofer y la `device_key` de una unidad se devuelven en claro una sola vez**, en la respuesta que los genera o regenera. El backend solo guarda el hash. Cualquier flujo nuevo que los toque tiene que respetar eso.

El login de chofer **ya no depende de Twilio**: el PIN reemplazó al OTP por SMS (migración `20260807_1200_pin_de_chofer`). Twilio quedó únicamente para el bot de WhatsApp de clientes — ver [.env.example](.env.example) para las credenciales que hace falta llenar. Se habla con su API por REST directo con `httpx`, sin el SDK oficial, porque ese es síncrono y bloquearía el event loop.

Los choferes dados de alta antes de la migración nacieron con `pin_hash` NULL: no pueden entrar hasta que un operador les asigne uno con `POST /drivers/{id}/pin`. El campo `has_pin` del listado de choferes es justo para que el dashboard los distinga.

## Endpoints

**Auth** — `POST /auth/driver-login` (chofer: teléfono + PIN) · `/auth/login` (operador/admin: email + contraseña) · `/auth/refresh` · `/auth/logout`

**Vehículos** — `GET|POST /vehicles` (paginado, ver abajo) · `GET|PATCH /vehicles/{id}` (solo staff, cualquier campo) · `POST /vehicles/{id}/status` (el propio chofer puede marcar su unidad disponible/ocupado — ver abajo) · `POST /vehicles/{id}/device-key` (regenera la clave de dispositivo; solo staff — re-emparejar un teléfono con una unidad es decisión de operador/admin, no del chofer) · `POST /vehicles/{id}/assignments` (abre turno y cierra el anterior) · `GET /vehicles/{id}/assignments` (historial) · `POST /vehicles/{id}/assignments/close` · `GET /vehicles/{id}/queue-position` (en qué sitio y en qué lugar de la fila va esa unidad ahora mismo; `null` si no está formada)

**Choferes** — `GET|POST /drivers` (alta solo admin; listado paginado) · `GET|PATCH /drivers/{id}` · `POST /drivers/{id}/pin` (regenera el PIN de login; solo admin — el nuevo invalida el anterior de inmediato y se muestra en claro una sola vez) · `POST /drivers/{id}/deactivate|reactivate` (revoca/restaura el login; solo admin) · `POST /drivers/me/push-token` (el chofer registra el token de push de su teléfono — ver "Notificaciones push" abajo)

**Sitios y fila** — `GET|POST /stands` · `GET|PATCH /stands/{id}` · `GET /stands/{id}/queue` · `POST /stands/{id}/queue/reorder` (la operadora corrige el orden a mano; hay que mandar exactamente las unidades formadas en ese momento, o responde 422) · `DELETE /stands/{id}/queue/{vehicle_id}`

Mandar `polygon_geojson` en el `PATCH` reemplaza la geometría del sitio y apaga `is_placeholder`.

De cada sitio se guardan **dos geometrías**: `polygon` es la geocerca real (el trazo pasado por `ST_Buffer` con la holgura) y `outline` es el trazo del operador tal cual, sin holgura. La razón es que `ST_Buffer` redondea cada esquina en 8 segmentos por cuadrante: un cuadrado de 4 esquinas se guarda con ~37 vértices, y sobre eso no se pueden ajustar vértices a mano. El dashboard edita el `outline` (`outline_geojson` en `GET /stands/{id}`) y el servidor le vuelve a aplicar la holgura al guardarlo.

Dos consecuencias:

- Mandar **solo `buffer_meters`** rehace la geocerca desde el `outline` con la holgura nueva. Antes solo cambiaba el número y dejaba la forma igual.
- **`apply_buffer: false`** sigue existiendo, para cuando lo que se manda ya trae la holgura aplicada — sin eso el sitio crecería otros N metros en cada ajuste. El dashboard ya no lo usa, porque edita el trazo. Guardar así **borra el `outline`**: de qué trazo salió esa geometría ya no se sabe.

`outline` es nulo en los sitios placeholder y en aquellos donde el relleno aproximado de la migración `0012` no dio un polígono válido (pasa con sitios angostos, donde erosionar la holgura los colapsa). En esos hay que volver a trazar antes de poder ajustar vértices o cambiar la holgura.

El diseño completo de sitios y fila está en [`spec-sitios-y-fila-v2.md`](spec-sitios-y-fila-v2.md), en la raíz de este repo.

**Telemetría** — `POST /location/ping` · `GET /vehicles/locations` (snapshot de flota) · `GET /vehicles/{id}/location` · `GET /vehicles/nearby?lat=&lng=&radius=` · `GET /vehicles/{id}/history?since=&until=` (paginado, default 500/tope 2000 — ver nota abajo) · `GET /vehicles/{id}/history/summary?since=&until=` (posición promedio cada 5 min, para rangos largos)

**Viajes** — `POST /trips` (alta manual: el operador ya eligió unidad + chofer) · `POST /trips/dispatch` (despacho automático — ver abajo) · `POST /trips/street-hail` (corte de calle: el chofer toma un pasaje directo, sin operador ni motor de despacho — nace ya "en_curso") · `GET /trips?status=&vehicle_id=&driver_id=` (paginado; staff ve toda la flota, un chofer solo los suyos — `driver_id` se ignora si lo manda uno) · `GET /trips/{id}` · `POST /trips/{id}/accept` · `POST /trips/{id}/reject` (solo tiene efecto en el flujo de despacho) · `POST /trips/{id}/start` · `POST /trips/{id}/complete` · `POST /trips/{id}/cancel`

Los listados (`/vehicles`, `/drivers`, `/trips`) aceptan `limit` (default 50, máximo 200) y `offset`. `GET /vehicles/{id}/history` usa un default y un tope más altos (500/2000): con ~10-20 pings/segundo de toda la flota, un solo día de una unidad ya son varios miles de filas, y el tope de 200 de los demás listados lo haría inservible para su uso normal (dibujar una ruta completa). El total que coincide con los filtros —antes de aplicar `limit`/`offset`— va en el header de respuesta `X-Total-Count`, no en el cuerpo: así el JSON se queda como una lista plana y no rompe a nadie que ya lo consuma sin paginar. Ese header está expuesto por CORS (`Access-Control-Expose-Headers`) para que un dashboard en el navegador pueda leerlo con `fetch()`.

**WhatsApp** — `POST /whatsapp/webhook` (webhook de Twilio; sin auth de la app — ver "Bot de WhatsApp" abajo)

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

Dos formas de crear un viaje: `POST /trips` es la de siempre (el operador ya eligió unidad y chofer), y `POST /trips/dispatch` no elige nada — nace con `vehicle_id`/`driver_id` en `null` y el motor de despacho busca por su cuenta a quién ofrecérselo. El estado avanza así:

```
SOLICITADO --accept--> ASIGNADO --start--> EN_CURSO --complete--> COMPLETADO
                \_______________________________________/
                                  \--cancel--> CANCELADO
```

`accept`/`start`/`complete` los dispara normalmente la app del chofer (o el operador, en su nombre); `cancel` puede venir de cualquiera de los dos lados mientras el viaje no haya terminado. Un chofer solo puede actuar sobre sus propios viajes; operador/admin sobre cualquiera.

`POST /trips` valida dos cosas antes de crear nada: que la unidad no tenga ya un viaje activo (**409**, para no despachar la misma unidad dos veces) y que no esté en `offline` ni `mantenimiento` (**400**, `"La unidad no está disponible para recibir viajes"`). Lo segundo existe porque esos dos estados son decisión exclusiva de un operador y `set_vehicle_status` no los pisa: sin el corte, el viaje se creaba igual y quedaba asignado a una unidad que no está trabajando, sin nada que lo moviera después.

`POST /trips/{id}/complete` acepta un body opcional `{"fare": 85.5}`: lo que cobró el chofer, capturado a mano — no hay tarifa calculada por distancia ni por tiempo en este proyecto. Sirve para que el chofer lleve su propio registro de ingresos (día/semana/mes) desde la app, no para facturar al pasajero. Si se omite, `fare` queda en `null`.

## `Vehicle.status` y el corte de calle

`Vehicle.status` (`disponible` / `ocupado` / `offline` / `mantenimiento`) es lo que de verdad decide si una unidad es candidata en `find_candidate_drivers`; solo `disponible` cuenta. Se mueve solo en estos momentos:

- **Al conectar `/ws/driver`**: si estaba `offline`, pasa a `disponible`. Es la señal de "el chofer prendió la app".
- **`POST /trips`** (alta manual): pasa a `ocupado` **desde el alta**, no hasta que el chofer acepte — el operador ya eligió la unidad. Antes no lo hacía: la unidad se quedaba `disponible` y, peor, su renglón de fila seguía en `formado`, así que el dashboard la mostraba formada mientras llevaba pasajero.
- **`POST /trips/{id}/accept`**: pasa a `ocupado` (ya sea que el viaje lo haya creado un operador o el motor de despacho).
- **`POST /trips/{id}/complete`** y **`POST /trips/{id}/cancel`**: vuelve a `disponible`.
- **`POST /vehicles/{id}/status`**: el propio chofer la pone en `disponible`/`ocupado` a mano, pensado para el corte de calle — un pasajero que para el taxi en la calle, sin pasar por operador ni por el motor de despacho. Solo puede tocar la unidad de su turno abierto actual (`vehicle_assignments.ended_at IS NULL`); staff/admin pueden tocar cualquiera.

Ninguna de las transiciones automáticas (conectar, accept, complete, cancel) pisa `offline`/`mantenimiento`: esos dos siguen siendo decisión exclusiva de un operador vía `PATCH /vehicles/{id}`.

## Motor de despacho automático

`POST /trips/dispatch` es el camino que en el futuro llamará un bot de WhatsApp (o cualquier canal que no sepa de antemano qué chofer va a tomar el viaje): recibe únicamente el origen/destino, y `app.core.dispatch` hace el resto.

```
POST /trips/dispatch  (sin vehicle_id/driver_id)
        │
        ▼
find_candidate_drivers  — PostGIS ST_DWithin sobre el turno abierto
        │                 más cercano con GPS reciente y sin otro viaje activo
        ▼
ofrece al más cercano ──PUBLISH──> Redis ──> WS /ws/driver del chofer (trip_offer)
        │
        │  chofer acepta ──POST /trips/{id}/accept──> ASIGNADO, fin
        │  chofer rechaza ─POST /trips/{id}/reject──> siguiente candidato
        │  no contesta en DISPATCH_OFFER_TIMEOUT_SECONDS (default 25s) ─> siguiente candidato
        ▼
   se acaban los candidatos → la oferta queda vacía, el viaje sigue "solicitado"
                              (si es del bot de WhatsApp, un barrido lo reintenta — ver abajo)
```

Quién cuenta como candidato para un viaje (`find_candidate_drivers`): tiene un turno abierto en `vehicle_assignments` (alguien la está manejando ahora mismo), esa unidad mandó un ping de GPS dentro de `DISPATCH_POSITION_FRESHNESS_SECONDS` (default 420 s = 7 min, alineado a propósito con `QUEUE_SIGNAL_DROP_SECONDS`: si no coincidieran habría una ventana donde alguien sigue en la fila del sitio pero el despacho ya no lo considera candidato, y la fila avanzaría sin razón visible) y dentro de `DISPATCH_SEARCH_RADIUS_METERS` (default 15 km — los 5 km originales se quedaban cortos para los 30-40 km de cobertura real) del origen, y no tiene ya otro viaje activo.

### Escalones de prioridad

Con sitios de por medio, `find_candidate_drivers` no ordena por distancia a secas. Primero decide la **zona del viaje** (`nearest_stand_id_for_trip`: el sitio **activo** cuyo `center` queda más cerca del origen) y con ella arma cuatro grupos:

| Escalón | Quién es |
|---|---|
| **1** | La **primera de la fila** del sitio de esa zona. Solo esa — si rechaza, la cascada sigue con los otros escalones, no con la segunda de la fila. |
| **2** | Unidades disponibles **rodando** (no formadas en ninguna fila) cuyo sitio de origen **es** esa zona. |
| **3** | Unidades disponibles rodando cuyo sitio es **otro**. |
| **4** | La primera de la fila de **cada otro sitio** activo. |

Gana el escalón más alto que tenga a alguien. Un escalón inferior solo le arrebata el viaje si su ETA es al menos `DISPATCH_TIER_ADVANTAGE_SECONDS` mejor (default 300 s; a `DISPATCH_ETA_SPEED_KMH` = 25 km/h eso son ~2 083 m de ventaja). Los demás quedan de respaldo en la cascada. En una frase: **se respeta la fila, salvo que alguien más llegue muchísimo antes.**

Las cuatro consultas comparten las mismas guardas: turno abierto, ping fresco, `status = 'disponible'`, sin otro viaje activo **y sin una oferta viva** (`offered_vehicle_id` con `offer_expires_at` en el futuro). Esa última es la que cierra la carrera real: durante la ventana de oferta el viaje todavía tiene `vehicle_id` NULO, así que mirar solo `vehicle_id` no basta cuando dos `dispatch_trip` corren a la vez — y se lanzan con `asyncio.create_task` por viaje, así que corren a la vez de verdad. Los escalones 1 y 4 no las tenían: una simulación de jornada completa (`scripts/sim_tiempo_real.py`) medía ~20 % de los viajes automáticos asignados a una unidad que ya traía otro encima.

### Salir de la fila al completar un viaje

**Al completar (o cancelar) un viaje, la unidad sale de la fila siempre**, venga el viaje de su propia zona o de otra. Se vuelve a formar sola cuando **regresa físicamente** a su polígono, al final de la fila, como cualquier otra. Queda `left_after_trip` en la bitácora, con `detail = {"zona_propia": bool}`.

`stand_queue.position_held` es una **columna reservada, no activa**: nada la pone en `True`. Existía para la "compensación" de la sección 8 de `spec-sitios-y-fila-v2.md` — la unidad que cubría un viaje de otra zona reingresaba de inmediato conservando su lugar. Se retiró porque reinsertaba a la unidad **estando todavía a kilómetros del sitio** (se midieron hasta 9.6 km) y, como `position_held` ordena primero, la dejaba de cabeza de fila y por tanto candidata del escalón 1 para viajes que no podía atender. Aparte de la regla de operación: un turno que acaba de completar un viaje no conserva el primer lugar frente a los que llevan formados esperando; cubre el que sigue.

Se conservan la columna, el `ORDER BY ... position_held DESC` y `reorder_queue` apagándola, para que revertir la decisión sea volver a escribirla en `handle_vehicle_freed`. **No borrarla pensando que es código muerto.**

La espera de respuesta (`_wait_for_response`) no usa Pub/Sub: es más simple releer el propio renglón de `trips` cada `DISPATCH_POLL_INTERVAL_SECONDS` que armar un segundo canal de eventos solo para esto. Pub/Sub sí hace falta para *empujar* la oferta al WebSocket del chofer (`driver:{id}:offers`), porque ese socket puede estar conectado a otra instancia del backend — mismo motivo que el resto del Pub/Sub del proyecto.

El WebSocket `/ws/driver` ahora es bidireccional: además de recibir pings del chofer, se suscribe (mientras dura la conexión) al canal de ofertas del chofer que tenga el turno abierto de esa unidad, y le reenvía cualquier `trip_offer` que le llegue.

Al aceptar la conexión manda, antes que nada, un mensaje `{"type": "connected", "vehicle_id": ..., "vehicle_status": ..., "vehicle_plate": ...}`: es la única forma que tiene la app de enterarse de su propio `vehicle_id` (el `device_key` no es un JWT, no trae claims), y lo necesita para llamar `POST /vehicles/{id}/status` (corte de calle). `vehicle_plate` es solo para mostrarle al chofer qué unidad es.

## Notificaciones push

El Pub/Sub de arriba solo le llega a un WebSocket `/ws/driver` que esté vivo en ese momento — si el chofer trae la app en segundo plano o cerrada del todo, esa oferta nunca la ve. `app.core.push` es la red de seguridad: cada oferta que manda `dispatch_trip` también se manda como notificación push (si el chofer tiene un token registrado vía `POST /drivers/me/push-token`), independiente de si el WebSocket está conectado.

Se usa el servicio de push de Expo (`https://exp.host/--/api/v2/push/send`) en vez de hablar con FCM/APNs directamente: un solo POST HTTP, sin SDKs nativos ni credenciales por plataforma de nuestro lado — Expo hace de intermediario con Google/Apple usando las credenciales que la app ya tiene configuradas en EAS. `send_push_notification` nunca lanza: un push es un complemento del WebSocket, no el camino principal del despacho, así que Expo caído o un token vencido no debe tumbar el resto de `dispatch_trip`.

## Bot de WhatsApp

`POST /whatsapp/webhook` recibe los mensajes entrantes de Twilio y llama directo a `dispatch_trip()` — el mismo motor de despacho que ya usan el dashboard y `POST /trips/dispatch`, tal como se dejó anotado desde que se armó ese endpoint. No hay árbol de menús: cualquier mensaje de texto contesta pidiendo ubicación (`app.core.whatsapp_bot._GREETING`); en cuanto Twilio manda una con `Latitude`/`Longitude` (el cliente comparte su ubicación de WhatsApp), se crea el viaje y se despacha.

```
cliente escribe / comparte ubicación
        │
        ▼
POST /whatsapp/webhook  ──Form (From, Body, Latitude, Longitude)──> handle_incoming_message
        │
        ├─ "cancelar" ────────────────> cancela el viaje activo y libera la unidad (TwiML)
        ├─ sin ubicación ─────────────> responde pidiendo compartir ubicación (TwiML)
        ├─ ya tiene un viaje en curso ─> responde que espere (TwiML)
        └─ con ubicación ──────────────> crea Trip(customer_phone=From) + dispatch_trip() en background
                                          responde "buscando taxi…" (TwiML, inmediato)
                                                  │
                                     chofer acepta ──> WhatsApp al cliente: "unidad X va en camino"
                                     nadie acepta / sin candidatos ──> se queda "solicitado", lo reintenta el barrido
```

**Un viaje del bot sin candidatos no se cancela.** Antes sí: `dispatch_trip` lo cancelaba en cuanto se rendía en la primera pasada, lo que le contestaba "no hay taxis" a alguien parado en la calle por una unidad que segundos después ya estaba libre. Ahora se queda en `solicitado` y `sweep_stuck_bot_trips` (`app.core.whatsapp_bot`, corre cada `BOT_TRIP_SWEEP_INTERVAL_SECONDS` desde el lifespan de `main.py`) lo vuelve a despachar mientras la flota cambia de disponibilidad. Solo se cancela —y ahí sí se le avisa al cliente— al llegar a `BOT_TRIP_MAX_WAIT_SECONDS` (20 min por default). El barrido salta los viajes con una oferta viva (`offer_expires_at` en el futuro) para no meterse a medio cascadeo de candidatos, y usa un lock en Redis (`wa:dispatch_retry:{trip_id}`, `SET NX`) para no relanzar `dispatch_trip` sobre un intento que sigue corriendo.

El cliente puede escribir **`cancelar`** en cualquier momento: cancela el viaje, libera la unidad si ya tenía una asignada y limpia la conversación. Solo la palabra sola cuenta — un mensaje que la mencione de pasada ("no quiero cancelar, ¿cuánto falta?") no tumba el viaje.

El estado de la conversación (`wa:conv:{phone}` → id del viaje activo) vive en Redis con una hora de TTL — es solo para saber si ya hay un viaje en curso para ese número, no un historial de chat. Un viaje "ya no bloquea una solicitud nueva" cuando está completado o cancelado, sin más: ya no hay criterio de edad, porque un `solicitado` viejo ahora significa que el barrido lo sigue reintentando (y es el propio barrido quien lo cancela si se agota la espera), no que quedó huérfano.

Por ahora corre contra el **sandbox compartido de Twilio** (`TWILIO_WHATSAPP_FROM`, el número público `whatsapp:+14155238886`) — solo le contesta a números que se hayan unido al sandbox mandando el código que da Twilio. Pasar a un número de WhatsApp Business propio requiere aprobación de Meta y reemplazar ese número; también queda pendiente validar la firma `X-Twilio-Signature` del webhook (mientras se prueba en el sandbox compartido no hay nada sensible que proteger todavía).

## Bot de Telegram

Mismo motor de despacho, otra puerta. A diferencia de WhatsApp —donde Twilio manda la conversación cruda al webhook y `app.core.whatsapp_bot` la interpreta— aquí la conversación vive **fuera** del backend, en `bot/telegram_bot.py`, un proceso aparte que solo llama a la API cuando ya tiene una ubicación.

```
cliente toca "📍 Enviar mi ubicación"
        │
        ▼
bot/telegram_bot.py ──POST /bot/request-ride (X-Bot-Key)──> crea Trip + dispatch_trip()
        │                                                     202 {trip_id, status, already_active}
        ▼
   "buscando taxi…"
                          chofer acepta ─────> app/core/telegram.py ──> "unidad X va en camino"
                          unidad a <30 m ────> app/core/arrival.py  ──> "¡tu taxi ya llegó!"
                          se agota la espera ─> sweep_stuck_bot_trips ──> "no encontramos taxi"
```

**El tráfico va en un solo sentido por el proceso del bot.** Ese proceso atiende lo que *entra*; todo lo que *sale* hacia el cliente lo manda el backend directo a la Bot API con el mismo `TELEGRAM_BOT_TOKEN` (`app/core/telegram.py`), porque quien conoce esos eventos es el backend y no tendría forma de despertar al proceso del bot para pedírselo. Por eso el bot no sondea el estado del viaje.

**Endpoints** (todos con header `X-Bot-Key`):

| Método | Ruta | Para qué |
|---|---|---|
| `POST` | `/bot/request-ride` | Crea el viaje y lanza el despacho. `202`, no `201`: al contestar todavía no hay chofer |
| `POST` | `/bot/cancel-ride` | El cliente se arrepiente; libera la unidad y su lugar en la fila |
| `GET` | `/bot/trips/{id}/status` | Consulta puntual, para un bot que se reinició y perdió su estado |

`BOT_API_KEY` no es opcional: ese endpoint crea viajes reales sin sesión de operador ni de chofer, así que abierto cualquiera podría llenar la flotilla de servicios fantasma. Con la variable vacía el endpoint contesta **503**, nunca queda abierto.

Pedir dos veces seguidas devuelve el viaje que ya existe con `already_active: true` en vez de abrir otro — no es un `409` porque pedir taxi dos veces es lo que hace alguien impaciente parado en la calle.

Levantarlo:

```bash
pip install -r bot/requirements.txt
export TELEGRAM_BOT_TOKEN=...   # el mismo del .env del backend
export BOT_API_KEY=...          # el mismo del .env del backend
export BACKEND_API_URL=http://localhost:8000/api/v1
python -m bot.telegram_bot
```

## Identidad del cliente: dos canales

`trips.customer_channel` (`whatsapp` | `telegram`) decide en qué columna vive la identidad: `customer_phone` para WhatsApp, `customer_chat_id` para Telegram. No se unificaron a propósito — el teléfono le sirve a la operadora por sí solo (puede marcarle), el `chat_id` no le sirve a nadie fuera del bot.

Nadie manda mensajes directo: todo pasa por `app.core.customer_notify.notify_customer(trip, texto)`, que enruta por canal. Con dos canales, repetir el `if` en cada punto de aviso garantizaba olvidarlo en alguno y dejar mudo a medio padrón.

`customer_channel` es **texto, no un enum nativo** de Postgres, a diferencia del resto de los enums del proyecto: agregar un canal debe ser desplegar código, no un `ALTER TYPE` con su migración.

## Aviso de llegada

`app/core/arrival.py` cuelga del mismo camino que la evaluación de la fila: cada lote de pings dispara una comprobación en una `asyncio.create_task` aparte, para no meter otra consulta geoespacial en el camino caliente de `_persist_pings`.

- Solo mira viajes en **`asignado`** (el chofer va en camino a recoger). En `en_curso` el pasajero ya va a bordo.
- La distancia la calcula Postgres con `ST_DWithin` sobre `geography`, que ya trabaja en metros y usa el índice espacial de `trips.origin`. Radio: `TRIP_ARRIVAL_RADIUS_METERS` (30 m).
- Solo usa el **último** ping elegible del lote: al vaciarse un buffer offline los anteriores son historia, y avisar "ya llegué" por una posición de hace diez minutos sería mentira.
- Se avisa **una sola vez** por viaje, con un `SET NX` en Redis (`trip:arrived:{trip_id}`). La unidad sigue mandando pings mientras espera afuera; sin candado el cliente recibiría el aviso cada cinco segundos.

## Notas sobre el modelo de datos

- **`VEHICLE_ASSIGNMENT` en vez de `current_driver_id`.** Una columna suelta pierde el historial en cuanto rota el segundo chofer. Con `started_at`/`ended_at` queda la trazabilidad completa de turnos; el chofer actual es la asignación con `ended_at IS NULL`. Un índice único parcial impide dos turnos abiertos en la misma unidad.
- **`GEOGRAPHY(Point, 4326)` en vez de dos columnas decimal.** Con un índice GiST, `ST_DWithin` resuelve cercanía y geofencing en milisegundos. Con lat/lng sueltos habría que calcular Haversine fila por fila, sin índice.
- **`location_pings` es una hypertable de TimescaleDB.** El particionado por fecha (fragmentos de 7 días), la compresión a los 30 días y la retención a los 365 se declaran una vez en la migración inicial y corren solos. Ajustar la retención según los requisitos legales de la operación.
- **`vehicle_position_5min` es un agregado continuo.** Vista materializada con posición/velocidad promedio por unidad cada 5 minutos, mantenida al día por su propia política de refresco de TimescaleDB (hasta ~5 min de rezago). Existe para que la reportería sobre rangos largos (`GET /vehicles/{id}/history/summary`) no tenga que promediar millones de pings crudos en cada consulta.
- **`Driver.status` no es lo mismo que `User.is_active`.** El primero es operativo (activo/inactivo — de vacaciones, por ejemplo, y sigue pudiendo entrar); el segundo es la cuenta (`POST /drivers/{id}/deactivate`), y corta el login de inmediato porque `get_current_user` revisa `is_active` en cada request, no solo al emitir el token.

## Pendiente

- [x] Router de viajes (`/trips`)
- [x] Integración real de Twilio — en su momento para el OTP por SMS del chofer; ese login se reemplazó por PIN y hoy Twilio solo mueve el bot de WhatsApp de clientes (la variable `SMS_PROVIDER` ya no existe)
- [x] Login de chofer por PIN en un paso, sin dependencia de SMS (`POST /auth/driver-login`)
- [x] Suite de pruebas con pytest (auth, vehicles, trips, reportería)
- [x] Restringir CORS al dominio del dashboard antes de producción (`CORS_ORIGINS` en `.env`)
- [x] Agregados continuos de TimescaleDB para reportería (`vehicle_position_5min`, cada 5 min)
- [x] Pruebas de los WebSockets (`/ws/driver`, `/ws/fleet`)
- [x] "Mis viajes" — un chofer ahora puede hacer `GET /trips` y ver los suyos sin conocer el ID de antemano
- [x] Probar la integración de Twilio contra una cuenta real (verificado en vivo: SMS entregado a un teléfono real)
- [x] Paginación en los endpoints de listado (`/vehicles`, `/drivers`, `/trips`; total en el header `X-Total-Count`)
