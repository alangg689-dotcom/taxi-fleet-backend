# Despliegue — Flotilla GPS (Tigres)

Guía para pasar de la laptop a un servidor real. El orden importa: base de
datos → backend → HTTPS → frontends → WhatsApp.

Vive en el repo del backend porque aquí está la infraestructura crítica
(PostGIS, TimescaleDB, Redis, la API), pero cubre las tres partes del sistema.
Las rutas `../taxi-fleet-dashboard` y `../taxi-fleet-driver-app` son los otros
dos repos, hermanos de este en la carpeta del proyecto — **no submódulos**:
cada uno tiene su propio `.git` y se clona por separado.

> **Antes de empezar, lo que NO puede salir así a producción.** Estos tres
> puntos están sueltos hoy y cada uno es un agujero real, no un pendiente
> cosmético:
>
> 1. **`usesCleartextTraffic: true`** en `../taxi-fleet-driver-app/app.json`.
>    En un APK distribuido, la `device_key` de la unidad y el token del chofer
>    viajan en claro: cualquiera en el mismo wifi los lee, y son exactamente
>    las dos credenciales que permiten suplantar a una unidad y mandar
>    posiciones falsas. Se quita junto con el paso 3 (HTTPS), no antes — sin
>    HTTPS la app deja de conectar.
> 2. **`JWT_SECRET`** debe ser un valor nuevo y largo en el servidor. El del
>    `.env.example` es de desarrollo; con él, cualquiera firma tokens de admin.
> 3. **`BOT_API_KEY`** vacía deja el endpoint de bots respondiendo 503 (estado
>    seguro). Genérala antes de levantar el bot, no después.

---

## 1. Base de datos y Redis

### PostgreSQL con PostGIS y TimescaleDB

El proyecto necesita **las dos** extensiones. Eso descarta el RDS estándar de
AWS: PostGIS sí lo trae, TimescaleDB no. Tres caminos, de menos a más trabajo:

| Opción | PostGIS | TimescaleDB | Nota |
|---|---|---|---|
| **Timescale Cloud** | sí | sí | El camino corto. Managed, con backups. |
| **AWS RDS PostgreSQL** | sí | **no** | Solo sirve si se renuncia a la hypertable |
| **Postgres propio en una VM** | sí | sí | Máximo control, backups a tu cargo |

Si eliges RDS **sin** TimescaleDB, `location_pings` deja de ser hypertable: el
particionado, la compresión a los 30 días y la retención a los 365 dejan de
correr solos, y con ~100 unidades pingueando cada 5-10 s esa tabla crece
rápido. Habría que montar particionado nativo y un cron de borrado.

La migración inicial crea las extensiones, así que el usuario de la base
necesita permiso para `CREATE EXTENSION` la primera vez:

```sql
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS timescaledb;
```

Luego, desde el servidor:

```bash
alembic upgrade head
python -m scripts.seed_admin admin@flotilla.mx <contraseña-fuerte>
```

### Redis

**Con persistencia activada.** No es un caché prescindible: ahí viven la
última posición de cada unidad, los cronómetros de candidato de la fila de
sitios, el estado de conversación del bot de WhatsApp y los contadores de
throttle. Un Redis que se reinicia vacío deja a los clientes a media
conversación y borra la fila de espera de todos los sitios.

En **ElastiCache**, eso significa activar backup automático (snapshots) y no
usar un nodo `cache.t*.micro` sin réplica para producción. En un Redis propio,
dejar AOF encendido:

```
appendonly yes
appendfsync everysec
```

Grupo de seguridad: solo el backend debe alcanzar el 6379. Nunca abierto a
internet.

---

## 2. Backend

### Con systemd (Gunicorn + workers de Uvicorn)

`uvicorn` a secas sirve para desarrollo. En producción va detrás de Gunicorn,
que supervisa y reinicia workers:

```bash
pip install "gunicorn>=21" "uvicorn[standard]"
```

`/etc/systemd/system/flotilla-api.service`:

```ini
[Unit]
Description=Flotilla GPS API
After=network.target

[Service]
Type=notify
User=flotilla
WorkingDirectory=/srv/flotilla/taxi-fleet-backend
EnvironmentFile=/srv/flotilla/taxi-fleet-backend/.env
ExecStart=/srv/flotilla/venv/bin/gunicorn app.main:app \
    --worker-class uvicorn.workers.UvicornWorker \
    --workers 4 \
    --bind 127.0.0.1:8000 \
    --timeout 120 \
    --access-logfile - --error-logfile -
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now flotilla-api
sudo journalctl -u flotilla-api -f
```

**`--bind 127.0.0.1`**: el backend nunca se expone directo, siempre detrás de
Nginx. Y **`--workers`**: con varios procesos, los WebSockets de un chofer y
los del dashboard caen en workers distintos — por eso el proyecto usa Redis
Pub/Sub para conectarlos. Sin Redis, con más de un worker, un chofer en el
worker A y un dashboard en el B no se ven.

### Con Docker

El `docker-compose.yml` del repo trae `db`, `redis` y un servicio `api` de
desarrollo. Para producción, ese `api` necesita al menos: quitar `--reload`,
apuntar `DATABASE_URL`/`REDIS_URL` a los servicios gestionados, y no montar el
código como volumen.

### Barridos periódicos

El backend arranca sus tareas de fondo en el `lifespan` de FastAPI (barrido de
sitios, reintento de viajes del bot). **Con varios workers, cada uno corre su
propia copia.** Para ~100 unidades no es grave (las operaciones son
idempotentes y hay un candado en Redis para el reintento de despacho), pero si
se escala a varias máquinas conviene moverlas a un proceso aparte.

---

## 3. HTTPS con Let's Encrypt

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d api.tudominio.mx -d panel.tudominio.mx
```

Certbot instala su propio timer de renovación. Verifícalo:

```bash
sudo systemctl list-timers | grep certbot
sudo certbot renew --dry-run
```

**Los WebSockets necesitan su bloque aparte en Nginx** — sin las cabeceras de
upgrade, `/ws/driver` y `/ws/fleet` fallan con un 400 que no dice nada:

```nginx
server {
    server_name api.tudominio.mx;

    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Los WebSockets cuelgan de la RAÍZ, no de /api/v1.
    location /ws/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        # Un chofer en turno mantiene el socket abierto horas; el default de
        # 60 s lo tiraría cada minuto.
        proxy_read_timeout 3600s;
    }

    # El webhook de Twilio entra por aquí (ver sección 5).
    location /whatsapp/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
    }
}
```

Ya con HTTPS arriba, **quita `usesCleartextTraffic` del `app.json`** y
recompila el APK — es el momento, no antes.

---

## 4. Dashboard

`VITE_API_URL` se hornea en el build, no se lee en tiempo de ejecución: hay
que fijarla **antes** de compilar.

```bash
cd ../taxi-fleet-dashboard
echo "VITE_API_URL=https://api.tudominio.mx/api/v1" > .env.production
npm ci
npm run build          # corre tsc -b y deja todo en dist/
```

`dist/` es estático. Nginx:

```nginx
server {
    server_name panel.tudominio.mx;
    root /srv/flotilla/taxi-fleet-dashboard/dist;

    # SPA con React Router: cualquier ruta desconocida devuelve index.html o
    # recargar en /sitios da 404.
    location / {
        try_files $uri $uri/ /index.html;
    }

    # Los assets llevan hash en el nombre; index.html nunca se cachea o los
    # navegadores se quedan pegados en la versión anterior tras cada deploy.
    location /assets/ {
        expires 1y;
        add_header Cache-Control "public, immutable";
    }
}
```

El backend tiene que permitir ese origen en CORS, y **exponer
`X-Total-Count`** — si se queda sin exponer, el dashboard pierde la paginación
sin dar ningún error visible.

---

## 5. App del chofer

```bash
cd ../taxi-fleet-driver-app
eas build -p android --profile production
```

`production` genera un **AAB** (lo que pide Google Play). Para repartir el APK
a mano — que es lo que hará esta flotilla al principio — usa `preview`, que
está configurado con `buildType: apk`.

Antes de compilar:

- **`EXPO_PUBLIC_API_URL`** apuntando a `https://api.tudominio.mx/api/v1`. Se
  hornea en el build igual que en el dashboard. Vive en las variables de
  entorno de EAS, no en el `.env` local — el `.env` está en `.gitignore` y EAS
  compila desde git, así que no llega al servidor de build. Cada perfil declara
  su `environment` en `eas.json`.
- **Quitar `usesCleartextTraffic`** de `app.json`.
- **Credenciales FCM V1** cargadas en el proyecto de EAS
  (`eas credentials -p android` → Google Service Account → FCM V1). Sin eso el
  push compila pero no llega nada, y es la causa número uno de "las
  notificaciones no funcionan en el build".
- **Llave de Google Maps restringida** al paquete `mx.flotillagps.chofer` y a
  la huella SHA-1 del keystore de EAS. Viaja dentro del APK por diseño; la
  única protección real es la restricción.

---

## 6. WhatsApp (Twilio)

Hoy el sistema manda por el **sandbox compartido** de Twilio
(`whatsapp:+14155238886`), que solo entrega a números que se hayan unido con el
código de invitación. Sirve para probar; para clientes reales no.

Con el número de WhatsApp Business ya aprobado por Meta:

```bash
# .env del backend
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
TWILIO_WHATSAPP_FROM=whatsapp:+52...
```

En la consola de Twilio → Messaging → tu número → **A message comes in**:

```
https://api.tudominio.mx/whatsapp/webhook     (HTTP POST)
```

Tiene que ser HTTPS y estar accesible desde internet: Twilio entra desde
afuera. Si el webhook falla o tarda, Twilio **reintenta**, y el cliente recibe
mensajes duplicados — por eso el handler siempre contesta algo, aunque la
lógica de negocio falle.

### Botones interactivos

El flujo del bot está escrito con textos que se leen como botones
("✅ Responde *sí*"), pero son texto plano. Los Reply Buttons de verdad exigen
plantillas de contenido aprobadas por Meta vía la Content API de Twilio, y el
sandbox no las soporta. Migrar es registrar las plantillas y cambiar el
emisor en `app/core/whatsapp.py`; la máquina de estados del bot no cambia.

---

## Verificación post-despliegue

```bash
curl https://api.tudominio.mx/health          # {"status":"ok","redis":true}
```

`"redis": false` significa que el backend levantó sin Redis: el despacho no le
llegará a ningún chofer y la fila de sitios no avanzará. No lo dejes pasar.

Después:

1. Entrar al dashboard y ver el mapa con las unidades.
2. Mandar una ubicación por WhatsApp al número nuevo y completar el flujo hasta
   el "confirmar".
3. Con el APK instalado en un teléfono real: activar la burbuja a Disponible,
   despachar un viaje desde el panel y confirmar que **llega la notificación
   push** con la app cerrada.
4. Minimizar la app y verificar en el mapa que la unidad sigue reportando —
   ahí se comprueba el GPS en segundo plano.
