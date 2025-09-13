# Registro de aplicaciones y ejecución de respaldos

Esta guía resume cómo integrar una aplicación con el orquestador de respaldos.
Incluye el flujo de registro, parámetros requeridos, endpoints y buenas
prácticas para preparar el contenedor de cada app.

## Flujo de registro

1. **Preparar el contenedor de la app**
   - Conéctalo a la red `backups_net`.
   - Exponé los endpoints internos `GET /backup/capabilities` y
     `POST /backup/export`, protegidos con un token.
   - Montá como _read-only_ las rutas de datos necesarias para generar el
     respaldo (p.ej. `/var/lib/postgresql/data`).
   - Asegurate de que la app escuche en `0.0.0.0` dentro del contenedor.
   - Ejemplo en `docker-compose.yml` de la app:
     ```yaml
     services:
       mi-app:
         volumes:
           - ./data:/var/lib/postgresql/data:ro
         networks:
           - backups_net
     ```

2. **Registrar la app en el orquestador**
   - Endpoint: `POST /api/apps`
   - Body JSON requerido:
     ```json
     {
       "name": "mi-app",
       "url": "http://mi-app:8000",
       "token": "secreto-compartido",
       "drive_folder_id": "1AbC2dEfG3",
       "schedule": "0 3 * * *",
       "retention": { "daily": 7, "weekly": 4 }
     }
     ```
   - Respuesta (`201 Created`):
     ```json
     { "id": "mi-app", "status": "registered" }
     ```

3. **Ejecutar un respaldo manual**
   - Endpoint: `POST /api/apps/mi-app/backups`
   - Respuesta típica (`202 Accepted`):
     ```json
     { "job_id": "123e4567", "status_url": "/api/jobs/123e4567" }
     ```

## Endpoints que debe exponer cada app

- `GET /backup/capabilities`
  ```json
  { "version": "v1", "types": ["db"], "est_seconds": 123, "est_size": 104857600 }
  ```

- `POST /backup/export`
  - Stream del respaldo.
  - Headers obligatorios:
    - `Authorization: Bearer <TOKEN>`
    - `X-Checksum-SHA256`
    - `X-Size`
    - `X-Format`
  - Puede responder `202` con un `job_id` y `status_url` para descargas
    diferidas.

## Buenas prácticas de preparación del contenedor

- Mantener el token fuera del código (variables de entorno o secretos).
- Limitar la exposición del endpoint de backup solo a `backups_net`.
- Incluir en la imagen las herramientas necesarias para generar el respaldo
  (p.ej. `pg_dump`, `tar`).
- Usar rutas de datos bien definidas y montarlas como _read-only_ cuando sea
  posible.
- Verificar periódicamente que el endpoint de backup responda y que los
  respaldos puedan restaurarse.
