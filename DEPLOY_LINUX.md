# Despliegue en el servidor Linux

Ubicación recomendada: **`/opt/broken_links_checker`**, como aplicación hermana de `/opt/analytics` (nombre observado en el servidor). Se ejecuta con el usuario **`analytics-svc`** ya existente, pero tendrá su propio entorno Python, SQLite, informes y cron. El instalador no modifica `/opt/analytics` ni sobrescribe un `config.ini` o `smtp.env` existentes.

1. Copia los archivos del repositorio (o descomprime el ZIP) en una carpeta temporal del servidor y ejecuta desde ella:

   ```bash
   sudo bash install_linux.sh
   ```

2. Edita `/opt/broken_links_checker/config.ini`: servidor SMTP, destinatarios, niveles y límites. Edita `/opt/broken_links_checker/smtp.env` con el usuario y la contraseña reales. El instalador crea este último con permisos `600` y propietario `analytics-svc`. Cambia `mail.enabled` a `yes` cuando los datos sean correctos.

3. Para una prueba controlada, pon temporalmente `max_pages_per_run = 10` en el INI y ejecuta:

   ```bash
   sudo runuser -u analytics-svc -- /bin/sh -c '. /opt/broken_links_checker/smtp.env && cd /opt/broken_links_checker && venv/bin/python broken_links_checker.py --config config.ini'
   ```

   Revisa el CSV y el log en `/opt/broken_links_checker/reports/`. Después restaura el límite deseado.

4. Activa la ejecución diaria cuando la prueba y el correo funcionen:

   ```bash
   sudo bash install_linux.sh --enable-cron
   ```

   El cron queda en `/etc/cron.d/upv-broken-links-checker`, a las 03:00 **según la zona horaria del servidor**. La zona `report_timezone = Europe/Madrid` solo afecta a las fechas del informe. Comprueba la zona del servidor con `timedatectl` y ajusta la hora del cron si hace falta.

Para actualizar el programa, descarga la nueva versión y repite `sudo bash install_linux.sh`. Preserva `config.ini`, `smtp.env`, `state.sqlite3` e informes. El archivo `smtp.env` ficticio del ZIP no se instala: se parte de `smtp.env.example` al crear el archivo privado en el servidor.

## Actualizar desde GitHub con una ronda en curso

Deja que termine el proceso en curso antes de actualizar el fichero Python; compruébalo con `pgrep -af '[b]roken_links_checker.py'`. Si instalaste clonando GitHub, entra en `/opt/broken_links_checker` y ejecuta `git pull --ff-only` con el usuario que administra ese clon. No borres ni muevas `state.sqlite3`, sus ficheros `-wal` y `-shm`, `config.ini` o `smtp.env`. El siguiente arranque crea automáticamente la tabla `pending_links`; conserva los niveles y las rondas anteriores. Las páginas que ya tenían `retry_at` por un enlace incierto se leerán una última vez al vencer ese plazo y desde entonces el enlace se reintentará de forma independiente. Si quieres un límite distinto al predeterminado, añade `max_pending_links_per_run = 1000` bajo `[crawl]` en `config.ini`. La versión nueva escribe una línea de progreso cada 100 páginas intentadas.

El siguiente arranque también añade la columna `context` a la tabla `links` existente sin borrar el grafo. Los enlaces guardados antes de la actualización muestran `sin_datos` hasta que se vuelva a rastrear su página. Para cambiar las 30 agrupaciones mostradas en el correo, añade `max_inline_groups = 30` bajo `[mail]` en tu `config.ini`; el `max_inline_rows` anterior deja de utilizarse. Se adjuntan el CSV agrupado y el CSV completo con `contexto_html`.
