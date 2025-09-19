# Backup Orchestrator (Docker)

Orquestador liviano que:
- Exponde **UI web** (puerto 5550 por defecto, configurable con `APP_PORT`) para registrar apps y programar respaldos.
- Se conecta por red **`backups_net`** a cada app que **opta por backup**.
- Pide el backup por HTTP al endpoint de la app y **sube a la nube** (inicio: Google Drive).
- Aplica **retención**, guarda **logs** y permite **“Probar ahora”**.

> Para una introducción paso a paso a la interfaz web, consultá la [Guía de uso de la UI](docs/ui_usage.md).

---


## Puertos

- La interfaz **Flask** del orquestador escucha en el puerto `5550`.


## 1) Requisitos
- Docker y Docker Compose.
- Una cuenta de Google Drive (del orquestador; **no hay usuarios finales en Drive**).
- `rclone` **dentro del contenedor** del orquestador (o empaquetado en su imagen).

## 2) Estructura sugerida
```
backup-orchestrator/
├─ docker-compose.yml
├─ .env                # variables del orquestador
├─ rcloneConfig/       # configuración persistente de rclone
└─ orchestrator/
   ├─ Dockerfile       # imagen con app + rclone
   └─ app/            # código del orquestador (UI + scheduler + runner)
```

### ¿Para qué usamos el volumen `backups`?

El servicio define un volumen Docker llamado `backups` que se monta dentro del
contenedor en la ruta `/backups`. Ese espacio queda disponible para compartir
archivos temporales entre el orquestador y las apps que exportan sus datos.

Además se monta la carpeta indicada por la variable `LOCAL_DIRECTORIES_ROOT`
(por defecto `./datosPersistentes/local-directories`) en la ruta
`/local-directories`. Allí es donde deben existir las carpetas locales que se
exponen a través de los remotes de rclone. Configurá la variable
`RCLONE_LOCAL_DIRECTORIES` con esas rutas (por ejemplo,
`RCLONE_LOCAL_DIRECTORIES=Respaldos|/local-directories/mi-app`) y la UI las
ofrecerá como destino para crear remotes de tipo **Local**. Los archivos se
almacenarán en el bind mount del host, por lo que quedan disponibles fuera del
contenedor.

### ¿Para qué usamos la base de datos?

El orquestador guarda su configuración en una base SQLite (o en la base que
indique `DATABASE_URL`). Allí se almacenan las aplicaciones registradas, sus
programaciones y también los metadatos de cada remote configurado (tipo, ruta
de destino, enlace compartido, etc.). De esta manera, toda la información sigue
disponible aunque el contenedor se reinicie o se vuelva a construir.

## 3) Variables (.env)
Crear un archivo `.env` en la raíz:

```
# UI y seguridad
APP_PORT=5550
APP_SECRET_KEY=poné_una_clave_larga
APP_ADMIN_USER=admin
APP_ADMIN_PASS=cambiame

# Límites/tiempos
REQUEST_TIMEOUT_S=900
BACKUP_MAX_SIZE_MB=20480

# rclone
RCLONE_REMOTE=gdrive
RCLONE_DRIVE_CLIENT_ID=tu-client-id.apps.googleusercontent.com
RCLONE_DRIVE_CLIENT_SECRET=tu-client-secret
RCLONE_DRIVE_TOKEN={"access_token": "...", "refresh_token": "..."}
# Carpetas locales disponibles en la UI (separá con `;` para múltiples entradas)
LOCAL_DIRECTORIES_ROOT=./datosPersistentes/local-directories
RCLONE_LOCAL_DIRECTORIES=Respaldos|/local-directories/mi-app
# Opcional: ajustá el scope y los permisos de compartición
# RCLONE_DRIVE_SCOPE=drive
# RCLONE_DRIVE_SHARE_TYPE=user
# RCLONE_DRIVE_SHARE_ROLE=organizer
# Remote por defecto si la app no especifica uno propio
# Cada app elige su carpeta destino; el orquestador guarda el folderId por app
```

> El `docker-compose` monta automáticamente `LOCAL_DIRECTORIES_ROOT` dentro del
> contenedor en la ruta `/local-directories`. Podés ajustar esa variable para
> apuntarla a cualquier carpeta del host donde quieras almacenar los respaldos
> locales.

> El **remote** `gdrive` se configura una sola vez y vive en `./rcloneConfig` (montado en `/config/rclone` dentro del contenedor).
> Como es un bind mount del host, Docker no lo recrea ni lo pisa cuando corrés `docker compose down` seguido de `docker compose up`: la carpeta y el archivo `rclone.conf` quedan en tu disco.

## 4) Primer arranque
El `docker-compose` monta `./rcloneConfig` dentro del contenedor para conservar la configuración de rclone entre reinicios. La carpeta se crea automáticamente al levantar los servicios (o podés crearla manualmente con `mkdir -p rcloneConfig`). Mientras no borres esa carpeta en el host (o elimines su contenido), cualquier recreación del contenedor volverá a usar exactamente la misma configuración.

```bash
docker compose up -d --build
```
Abrí `http://localhost:5550` (o tu host:5550).

## 5) Configurar Google Drive (rclone)
El contenedor necesita un remote de Google Drive (por defecto `gdrive`) para
subir los respaldos. Podés inicializarlo de dos formas:

- **Automática**: completá `RCLONE_DRIVE_CLIENT_ID`,
  `RCLONE_DRIVE_CLIENT_SECRET` y `RCLONE_DRIVE_TOKEN` en tu `.env`. Al arrancar
  la UI, el orquestador verifica si existe el remote `gdrive` y, si falta,
  ejecuta `rclone config create` con esas credenciales. La configuración queda
  persistida en `./rcloneConfig/rclone.conf` gracias al volumen bind mount.
- **Manual**: entrá al contenedor y corré el asistente de rclone una sola vez:
  ```bash
  docker exec -it backup-orchestrator rclone config
  ```
  - Creá el remote `gdrive` (tipo **Drive**).
  - Scope recomendado: `drive.file` (o `drive` si querés listar/borrar fuera de archivos subidos).
  - Autenticá (OAuth) y verificá:
    ```bash
    docker exec -it backup-orchestrator rclone lsd gdrive:
    ```

## 6) Configurar rclone desde la UI
Si preferís evitar la consola, la interfaz web incluye una sección para inicializar y ver los remotes de rclone.
- Ingresá a **Rclone → Configurar** desde la UI.
- El formulario crea "perfiles" listos para usar desde las apps:
  - Para **Google Drive**, la opción predeterminada "Usar la cuenta provista por el orquestador" crea una carpeta dentro del
    remote global (`RCLONE_REMOTE`, por defecto `gdrive`), la comparte con el correo que indiques y genera un alias
    `rclone config create <nombre> alias remote gdrive:<carpeta>`. Así cada perfil apunta a una carpeta dedicada sin pedir
    credenciales nuevas.
  - También podés elegir "Usar mi propia cuenta" y pegar un token OAuth si necesitás operar con otra cuenta de Drive.
  - Para **local** se muestran las carpetas habilitadas mediante `RCLONE_LOCAL_DIRECTORIES`.
  - Para **SFTP** se crea la carpeta objetivo dentro del servidor remoto y se valida la conexión antes de guardar.

- La configuración se guarda en `./rcloneConfig`, por lo que no se pierde al reiniciar el contenedor.

## 7) Contrato v1 para las Apps
Cada app que quiera respaldo debe conectarse a `backups_net` y exponer los
endpoints internos `GET /backup/capabilities` y `POST /backup/export`,
protegidos con token. La especificación completa, incluidos headers
obligatorios y comportamiento asíncrono opcional, está en el
[apartado de endpoints del registro de apps](docs/registro_de_apps.md#endpoints-que-debe-exponer-cada-app).

## 8) Registrar una App en la UI
En **Apps → Agregar**:
- **Nombre** (identificador).
- **URL interna**: `http://NOMBRE_DEL_CONTENEDOR:PUERTO` (gracias a `backups_net`).
- **Token** (PSK) que valida la app.
- **Destino en nube**: Folder ID de Drive.
- **Remote rclone**: (opcional) para usar un remote distinto al global.
- **Frecuencia** (diario/semanal) y **retención** (p.ej. 7 diarios, 4 semanales).

Luego, **Probar ahora**. Deberías ver un archivo `NOMBRE_YYYYmmdd_HHMM.dump` en la carpeta de Drive.

Para un detalle del flujo de registro vía API, ejemplos de peticiones HTTP y
buenas prácticas de preparación de contenedores, ver el
[Flujo de registro](docs/registro_de_apps.md#flujo-de-registro).

## 9) Política de retención
Configurable por app. Ejemplo:
- **Diarios**: 7
- **Semanales**: 4
- (Opcional) **Mensuales**: 6

El orquestador borra lo que exceda en Drive y su índice interno.

## 10) Seguridad
- El endpoint `/backup` de la app solo debe aceptar desde **`backups_net`** + **Bearer token**.
- No loguear secretos. Rotar tokens si hace falta.
- Límite de tamaño y **timeout** en el orquestador (ver `.env`).

## 11) Restauración (prueba periódica)
- Bajá un backup desde Drive.
- Restaurá en un contenedor de prueba:
  ```bash
  pg_restore -d postgres://user:pass@host:port/db --clean --create archivo.dump
  ```
- Verificá integridad básica (tablas/filas claves).

## 12) Conectar una App existente a `backups_net`
En su `docker-compose.yml`:
```yaml
networks:
  backups_net:
    external: true
    name: backups_net
```
y en el servicio:
```yaml
services:
  mi-app:
    networks:
      - backups_net
```
La app responderá internamente en `http://mi-app:PUERTO`.

## 13) Flujo de ejecución (resumen)
1. Scheduler dispara tarea de una app.
2. Orquestador pide `GET /backup/capabilities`.
3. Llama `POST /backup/export` (stream).
4. Sube a Drive (pipe directo con `rclone rcat` o usando `/app/tmp`).
5. Verifica checksum/tamaño. Registra logs.
6. Aplica retención.

---

## Roadmap corto
- v1: solo **DB → Drive**.
- v1.1: **alertas** (fallo/retención) por mail/Telegram.
- v1.2: modo **asíncrono** (jobs) para backups largos.
- v1.3: soportes de **otras nubes** (S3, etc.).

---

## Troubleshooting
- **No aparece en Drive**: `docker exec -it backup-orchestrator rclone ls gdrive:carpeta`
- **DNS interno**: probá `curl http://nombre_contenedor:puerto/backup/capabilities` desde el orquestador.
- **Permisos**: asegurate de que la app escuche en `0.0.0.0` dentro del contenedor.
