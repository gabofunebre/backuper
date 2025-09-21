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

El sidecar se parametriza mediante un archivo YAML y variables de entorno. En este repositorio encontrarás [`config/config.example.yaml`](./config/config.example.yaml) con las claves mínimas:

- `strategy.type`: define la estrategia de respaldo (por ejemplo, `filesystem`, `postgres`, `mysql`).
- `strategy.commands`: lista de comandos shell que ejecutará la estrategia (pre, backup y post).
- `paths.*`: rutas temporales donde se almacenarán los artefactos.
- `secrets`: claves que el contenedor leerá desde variables de entorno definidas en el `.env`.

### Pasos para parametrizar en tu proyecto

1. Copia el archivo de ejemplo:

   ```bash
   cp sidecar/config/config.example.yaml sidecar/config/config.yaml
   ```

2. Crea un archivo `sidecar/.env` (o utiliza el `.env` principal de tu proyecto) y define los valores sensibles requeridos en `config.yaml`. Ejemplo:

   ```bash
   SIDECAR_STRATEGY_TYPE=filesystem
   DATABASE_URL=postgresql://user:pass@db:5432/app
   SSH_PRIVATE_KEY="-----BEGIN OPENSSH PRIVATE KEY-----..."
   ```

3. Ajusta `config.yaml` para tu caso de uso: rutas de trabajo, comandos de respaldo y nombres de las variables sensibles.
4. Levanta los servicios combinando el compose de tu app con el del sidecar y verifica que el contenedor cargue la configuración correctamente.

Con estos pasos, cada proyecto podrá sumarse a la red `backups_net` y recibir instrucciones del orquestador de respaldos reutilizando un sidecar configurado de forma consistente.
