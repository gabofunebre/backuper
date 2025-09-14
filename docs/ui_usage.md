# Guía de uso de la interfaz web

Esta guía resume los pasos básicos para operar la UI del orquestador de backups.

## 1. Acceder y autenticarse
1. Levantar el proyecto con Docker (`docker compose up -d`).
2. Abrir `http://localhost:5550` (o el puerto configurado en `APP_PORT`).
3. Ingresar con las credenciales definidas en las variables `APP_ADMIN_USER` y `APP_ADMIN_PASS`.

## 2. Configurar rclone
1. Desde el menú **Rclone → Configurar**, seguir el asistente interactivo para crear un *remote* (p.ej. `gdrive`).
2. Una vez configurado, verificar los remotes disponibles en **Rclone → Remotes**.

## 3. Registrar una aplicación
1. Ir a **Apps → Agregar**.
2. Completar:
   - **Nombre** de la app.
   - **URL interna** accesible desde la red `backups_net`.
   - **Token** (PSK) para autenticar contra la app.
   - **Folder ID** de Google Drive donde guardar los respaldos.
   - Opcional: *remote* distinto si la app no utiliza el global.
   - Frecuencia y política de retención.
3. Guardar y utilizar **Probar ahora** para forzar un primer respaldo.

## 4. Consultar logs y backups
- Cada ejecución registra su resultado en la tabla de eventos.
- Los logs completos pueden verse desde **Logs** en la barra superior.
- Los archivos subidos se listan en la sección de cada app.

## 5. Administración adicional
- Editar o eliminar apps desde **Apps → Lista**.
- Ajustar variables globales reiniciando el contenedor con nuevos valores en `.env`.

Esta guía cubre las operaciones comunes. Para detalles del contrato de respaldo y configuración avanzada consulte [`registro_de_apps.md`](registro_de_apps.md).
