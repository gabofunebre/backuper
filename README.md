# Backup Orchestrator (Docker)

Orquestador liviano que:
- Exponde **UI web** (puerto 5550) para registrar apps y programar respaldos.
- Se conecta por red **`backups_net`** a cada app que **opta por backup**.
- Pide el backup por HTTP al endpoint de la app y **sube a la nube** (inicio: Google Drive).
- Aplica **retención**, guarda **logs** y permite **“Probar ahora”**.

---

## 1) Requisitos
- Docker y Docker Compose.
- Una cuenta de Google Drive (del orquestador; **no hay usuarios finales en Drive**).
- `rclone` **dentro del contenedor** del orquestador (o empaquetado en su imagen).

## 2) Estructura sugerida
```
backup-orchestrator/
├─ docker-compose.yml
├─ .env                # variables del orquestador
└─ orchestrator/
   ├─ Dockerfile       # imagen con app + rclone
   └─ app/            # código del orquestador (UI + scheduler + runner)
```

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
# Cada app elige su carpeta destino; el orquestador guarda el folderId por app
```

> El **remote** `gdrive` se configura una sola vez y vive en el volumen `rclone_config`.

## 4) Primer arranque
```bash
docker compose up -d --build
```
Abrí `http://localhost:5550` (o tu host:5550).

## 5) Configurar Google Drive (rclone)
Dentro del contenedor (una sola vez):
```bash
docker exec -it backup-orchestrator rclone config
```
- Crear remote `gdrive` (tipo **Drive**).
- Scope recomendado: `drive.file` (o `drive` si querés listar/borrar fuera de archivos subidos).
- Autenticá (OAuth) y verificá:
```bash
docker exec -it backup-orchestrator rclone lsd gdrive:
```

## 6) Contrato v1 para las Apps
Cada app que quiera respaldo:
- Se **conecta a `backups_net`** en su propio compose (o `docker network connect`).
- Expone un endpoint **solo interno** (escuchando en su contenedor) con **token**.

Endpoints mínimos:
- `GET /backup/capabilities` → JSON: `{ "version":"v1", "types":["db"], "est_seconds":123, "est_size": 104857600 }`
- `POST /backup/export` → **stream** del backup (p.ej. `pg_dump -Fc`), con headers:
  - `X-Checksum-SHA256: ...`
  - `X-Size: <bytes>`
  - `X-Format: pg_dump_Fc`
  - Auth por header `Authorization: Bearer <TOKEN>`

> Si el backup tarda mucho, la app puede devolver `202` con `job_id` y un `status_url`/`download` para que el orquestador lo consulte y descargue luego.

## 7) Registrar una App en la UI
En **Apps → Agregar**:
- **Nombre** (identificador).
- **URL interna**: `http://NOMBRE_DEL_CONTENEDOR:PUERTO` (gracias a `backups_net`).
- **Token** (PSK) que valida la app.
- **Destino en nube**: Folder ID de Drive.
- **Frecuencia** (diario/semanal) y **retención** (p.ej. 7 diarios, 4 semanales).

Luego, **Probar ahora**. Deberías ver un archivo `NOMBRE_YYYYmmdd_HHMM.dump` en la carpeta de Drive.

## 8) Política de retención
Configurable por app. Ejemplo:
- **Diarios**: 7
- **Semanales**: 4
- (Opcional) **Mensuales**: 6

El orquestador borra lo que exceda en Drive y su índice interno.

## 9) Seguridad
- El endpoint `/backup` de la app solo debe aceptar desde **`backups_net`** + **Bearer token**.
- No loguear secretos. Rotar tokens si hace falta.
- Límite de tamaño y **timeout** en el orquestador (ver `.env`).

## 10) Restauración (prueba periódica)
- Bajá un backup desde Drive.
- Restaurá en un contenedor de prueba:
  ```bash
  pg_restore -d postgres://user:pass@host:port/db --clean --create archivo.dump
  ```
- Verificá integridad básica (tablas/filas claves).

## 11) Conectar una App existente a `backups_net`
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

## 12) Flujo de ejecución (resumen)
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
