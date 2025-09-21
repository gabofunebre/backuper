# Sidecar de Backups

Este directorio contiene los artefactos necesarios para ejecutar un contenedor *sidecar* que expone la aplicación a la red de orquestación de respaldos (`backups_net`). El sidecar actúa como un puente entre la app y el orquestador: recibe comandos, ejecuta estrategias de respaldo configurables y comparte los artefactos resultantes.

## Redes necesarias

1. **backups_net**: red externa creada por la plataforma de respaldos. Debe existir de antemano para que el orquestador pueda descubrir a los sidecars. Si aún no existe, créala una sola vez con:

   ```bash
   docker network create backups_net
   ```

2. **Red interna de la app**: red privada definida en el compose de cada proyecto para comunicar la app con su sidecar sin exponer puertos innecesarios.

El archivo [`docker-compose.yml`](./docker-compose.yml) declara ambas redes. `backups_net` se marca como `external: true` para reutilizar la red compartida, mientras que `app_internal` es local al proyecto.

## Cómo consumir el sidecar desde tu proyecto

1. Copia el directorio `sidecar` dentro del repositorio de tu app (o añade este repo como submódulo).
2. Asegúrate de que el archivo [`sidecar/docker-compose.yml`](./docker-compose.yml) esté disponible junto al compose principal de tu proyecto.
3. En tu `docker-compose.yml` principal, incluye los servicios del sidecar usando uno de los siguientes métodos:

   - **Composición de archivos**:

     ```bash
     docker compose \
       -f docker-compose.yml \
       -f sidecar/docker-compose.yml \
       up -d
     ```

   - **Servicio extendido**: importa los servicios del sidecar dentro de tu compose usando la clave [`extends`](https://docs.docker.com/compose/compose-file/extends/), declarando el servicio `sidecar` y la red `backups_net` como externas.

4. Verifica que tanto la app como el sidecar declaren la misma red `backups_net` para que el orquestador los detecte.

> **Nota:** el servicio `app` incluido en el compose del sidecar es solo un contenedor *dummy* a modo de ejemplo. Sustitúyelo por tu aplicación real cuando lo integres.

## Configuración del sidecar

El sidecar se parametriza mediante un archivo YAML y variables de entorno. En este repositorio encontrarás [`config/config.example.yaml`](./config/config.example.yaml) con la estructura completa. Los campos obligatorios son:

- `app.port`: puerto HTTP que expondrá el sidecar.
- `capabilities`: metadatos que el orquestador usa para describir el respaldo (versión, tipos, estimaciones).
- `strategy.type`: estrategia elegida (`database_dump`, `file_archive` o `custom`).
- `strategy.artifact`: nombre, formato y MIME del archivo que se expondrá al orquestador.
- `strategy.config`: parámetros específicos de la estrategia (comandos, rutas a empaquetar, variables extra, etc.).
- `paths.workdir` / `paths.artifacts` / `paths.temp_dump`: rutas dentro del contenedor con permisos de lectura/escritura.
- `secrets.api_token`: token compartido con el orquestador (se inyecta vía variable de entorno).

### Estrategias disponibles

- **`database_dump`**: ejecuta comandos como `pg_dump`, `mysqldump`, etc. Acepta `pre`/`post` (listas de comandos opcionales), `command` o `commands` (comando principal), `capture_stdout` (para volcar automáticamente la salida al artefacto) y `env` (variables adicionales). Todo comando corre dentro de `paths.workdir` y recibe los helpers `SIDE_CAR_WORKDIR`, `SIDE_CAR_ARTIFACTS_DIR`, `SIDE_CAR_TEMP_DUMP`, `SIDE_CAR_STRATEGY` y `SIDE_CAR_DRIVE_FOLDER_ID`.
- **`file_archive`**: empaqueta rutas del contenedor en un `tar` o `zip`. Se configuran los patrones con `paths` (soporta `glob`), el tipo en `format` (`tar`/`zip`), `compression` (`gz`, `bz2`, `xz` o `store`) y `follow_symlinks`. El archivo generado se marca como sólo-lectura.
- **`custom`**: delega todo en scripts propios. Expone las mismas claves que `database_dump`, permitiendo ejecutar cualquier pipeline mientras el script deje el artefacto en `SIDE_CAR_TEMP_DUMP` o escriba por stdout con `capture_stdout: true`.

> ⚠️ **Permisos y volúmenes:** montá `paths.workdir`, `paths.artifacts` y `paths.temp_dump` sobre volúmenes con permiso de escritura. Los comandos se ejecutan bajo el usuario del contenedor; si el volumen es de sólo lectura el respaldo fallará. Para estrategias de archivos asegurate de incluir únicamente rutas montadas dentro del sidecar.

### Variables de entorno expuestas a los comandos

Cada comando recibe, además del entorno del contenedor, las siguientes variables auxiliares:

| Variable | Descripción |
| --- | --- |
| `SIDE_CAR_WORKDIR` | Directorio de trabajo usado por la estrategia. |
| `SIDE_CAR_ARTIFACTS_DIR` | Carpeta donde se moverá el artefacto final. |
| `SIDE_CAR_TEMP_DUMP` | Ruta temporal en disco o FIFO esperada por la estrategia. |
| `SIDE_CAR_STRATEGY` | Nombre de la estrategia en ejecución. |
| `SIDE_CAR_DRIVE_FOLDER_ID` | Folder ID provisto por el orquestador (si aplica). |

Utilizá estas variables para componer comandos portables. Por ejemplo, un dump de PostgreSQL que se vuelca a stdout puede declararse así:

```yaml
strategy:
  type: database_dump
  artifact:
    filename: backup.sql.gz
    format: postgres-custom
    content_type: application/gzip
  config:
    command: |
      pg_dump --dbname="${DATABASE_URL}" --format=custom
    capture_stdout: true
    env:
      PGPASSWORD: ${DATABASE_PASSWORD}
```

### Pasos para parametrizar en tu proyecto

1. Copia el archivo de ejemplo:

   ```bash
   cp sidecar/config/config.example.yaml sidecar/config/config.yaml
   ```

2. Crea un archivo `sidecar/.env` (o utiliza el `.env` principal de tu proyecto) y define los valores sensibles requeridos en `config.yaml`. Ejemplo:

   ```bash
   SIDECAR_STRATEGY_TYPE=database_dump
   BACKUP_API_TOKEN=poneme_un_token
   DATABASE_URL=postgresql://user:pass@db:5432/app
   DATABASE_PASSWORD=super-secreta
   ```

3. Ajusta `config.yaml` para tu caso de uso: selecciona la estrategia adecuada, define los comandos o rutas a empaquetar y replica las variables que deban inyectarse.
4. Levanta los servicios combinando el compose de tu app con el del sidecar y verifica que el contenedor cargue la configuración correctamente (revisá los logs ante cualquier error de permisos o comandos fallidos).

Con estos pasos, cada proyecto podrá sumarse a la red `backups_net` y recibir instrucciones del orquestador de respaldos reutilizando un sidecar configurado de forma consistente.
