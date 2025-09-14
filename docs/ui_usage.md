# Guía de uso de la interfaz web

Esta guía explica en detalle cómo operar la UI del orquestador de backups y qué
funcionalidades ofrece cada sección.

## 1. Acceso y navegación
1. Levantar el proyecto con Docker (`docker compose up -d`).
2. Abrir `http://localhost:5550` (o el puerto configurado en `APP_PORT`).
3. Ingresar con las credenciales definidas en `APP_ADMIN_USER` y `APP_ADMIN_PASS`.
4. Una vez autenticado se muestra una barra superior con:
   - **Apps**: listado y alta de aplicaciones.
   - **Rclone**: opciones para configurar remotes y ver los existentes.
   - **Logs**: visor de logs del orquestador.
   - **Logout**: finaliza la sesión actual.

## 2. Gestión de aplicaciones
### 2.1 Listado y acciones rápidas
- En **Apps → Lista** se visualizan todas las apps registradas.
- Cada fila permite:
  - **Editar** la configuración.
  - **Eliminar** la app y sus programaciones.
  - **Probar ahora** para ejecutar un respaldo inmediato.
  - **Ver backups** ya subidos a la nube.

### 2.2 Registrar una nueva app
1. Ir a **Apps → Agregar**.
2. Completar:
   - **Nombre** de la app.
   - **URL interna** accesible desde la red `backups_net`.
   - **Token** (PSK) para autenticar contra la app.
   - **Folder ID** de Google Drive donde guardar los respaldos.
   - Opcional: *remote* distinto si la app no utiliza el global.
   - Frecuencia y política de retención.
3. Guardar y utilizar **Probar ahora** para forzar un primer respaldo.

### 2.3 Edición o eliminación
- Desde el listado se puede **Editar** una app para modificar cualquier campo.
- El botón **Eliminar** la quita definitivamente del orquestador.

## 3. Configurar rclone
1. Ingresar a **Rclone → Configurar** para ejecutar el asistente `rclone config`
   directamente desde la UI.
2. Completar **Nombre** y elegir el **Tipo** de backend (`drive`,
   `onedrive`, `sftp` o `local`) desde el desplegable para crear un *remote*.

3. Los remotes existentes se listan en **Rclone → Remotes**, donde se puede
   comprobar que hayan quedado registrados correctamente.
   - Para `sftp`, el contenedor puede alcanzar al host mediante `host.docker.internal` y se puede especificar el puerto `222` u otro durante la configuración.

## 4. Visor de logs
- El enlace **Logs** muestra las últimas líneas del log del orquestador.
- Útil para diagnosticar fallos de ejecución o problemas de configuración.

## 5. Cerrar sesión
- Hacer clic en **Logout** en la barra superior para terminar la sesión actual.

## 6. Notas adicionales
- Cada ejecución registra su resultado en la tabla de eventos de la app.
- Los archivos subidos se listan dentro de la vista de cada app.
- Para detalles del contrato de respaldo y configuración avanzada consulte
  [`registro_de_apps.md`](registro_de_apps.md).

