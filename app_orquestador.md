# Descripción general del servicio “app-orquestador” (Backup Orchestrator)

El proyecto ofrece un orquestador liviano pensado para centralizar y automatizar los respaldos de distintas aplicaciones que se ejecutan en una red interna (por defecto, `backups_net`). Se compone de una interfaz web simple, un motor programador de tareas y un cliente que se comunica con cada aplicación para solicitar la generación de copias.

---

## Funcionalidades principales y cómo se implementan

### 1. Registro y administración de aplicaciones
- Cada app que desea respaldarse se registra en la interfaz (o vía API), proporcionando:
  - Nombre identificador y URL interna (accesible dentro de la red compartida).
  - Token de autenticación para validar las solicitudes.
  - Programación de respaldo en formato cron.
  - Identificador de carpeta de Google Drive donde se almacenarán los archivos.
  - Cantidad de copias que se mantendrán (retención).
- La información queda guardada en una base SQLite mediante SQLAlchemy.
- La interfaz web (Flask + Bootstrap) muestra la lista de apps registradas y un formulario para agregar nuevas.

### 2. Planificación automática de backups
- El servicio usa **APScheduler** para convertir la expresión cron de cada app en tareas que se ejecutan en segundo plano.
- Al iniciar la aplicación se arranca el scheduler y se registran los jobs de todas las apps guardadas.
- Cada job llama al método `run_backup`, el cual se encarga de lanzar el proceso de copia.

### 3. Comunicación con las aplicaciones
- Para generar el respaldo, el orquestador sigue un pequeño protocolo basado en HTTP:
  - `GET /backup/capabilities`: verifica que la app soporte la versión de contrato “v1” y qué tipo de backup ofrece.
  - `POST /backup/export`: solicita la exportación propiamente dicha.
- Se agregan encabezados de autenticación (Bearer token) y se maneja el timeout para evitar procesos colgados.

### 4. Subida directa a la nube
- La respuesta `POST /backup/export` se recibe en streaming y se envía directamente a Google Drive usando **rclone** en modo `rcat`, de manera que el archivo no queda almacenado en disco.
- La ruta de destino (carpeta de Drive) se arma con la variable de entorno `RCLONE_REMOTE` y el `drive_folder_id` configurado en cada app.
- Esto permite que el contenedor del orquestador suba los archivos al almacenamiento remoto apenas se generan.

### 5. Política de retención
- Una vez subido el archivo, se aplica la política de retención definida por la app:
  - Se consultan los archivos existentes para esa app en el remoto (`rclone lsl`).
  - Se ordenan por fecha y se eliminan los más antiguos si superan la cantidad permitida (`rclone delete`).
- De esta forma se ahorra espacio y se mantiene un historial acotado.

### 6. Operación manual y pruebas
- La UI incluye un botón “Probar ahora” (descrito en la documentación) que desencadena un backup manual para verificar la configuración.
- Los logs de cada operación permiten detectar errores de red, fallos en las apps o problemas de almacenamiento.

### 7. Configuración centralizada
- Todas las variables sensibles (puerto de la app, clave secreta, usuario/contraseña de administración, límites de tamaño, etc.) se cargan desde un archivo `.env`.
- El orquestador se despliega como contenedor Docker; basta con levantar el `docker-compose` y configurar rclone una única vez dentro de la imagen.

---

## En conjunto

Este orquestador ofrece una manera unificada de:
- Registrar aplicaciones que quieren respaldarse.
- Programar y ejecutar las copias según un cron.
- Subirlas a Google Drive sin almacenar archivos temporales.
- Mantener solo las últimas N copias (retención).
- Interactuar con las aplicaciones mediante un contrato HTTP sencillo y seguro.

Así se automatiza todo el ciclo de respaldo, desde la solicitud a la app hasta la limpieza de archivos viejos, con una interfaz mínima y dependencias ligeras.
