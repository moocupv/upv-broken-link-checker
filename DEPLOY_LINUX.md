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

Para completar antes los contextos antiguos, espera a que `pgrep -af '[b]roken_links_checker.py'` no muestre ningún proceso y ejecuta como `analytics-svc`:

```bash
cd /opt/broken_links_checker
nohup venv/bin/python broken_links_checker.py --config config.ini --backfill-context >> reports/backfill.log 2>&1 < /dev/null &
```

No necesita `smtp.env`: no envía correo. El estado se conserva para poder repetir la orden cuando haya más de `max_context_backfill_pages = 1000` páginas o fallos temporales. Consulta el resultado en `reports/backfill.log` y `reports/contexto_pendiente_*.csv`. No lo incluyas en cron.

Para elegir el separador de todos los CSV, añade a `config.ini` la sección `[report]` con `csv_format = es` (`;`) o `csv_format = en` (`,`). Si falta, se usa `es` por defecto. No hace falta modificar las credenciales ni la base SQLite.

Tras actualizar a la versión que solo rastrea páginas UPV, el siguiente arranque elimina del grafo antiguo las páginas de otros dominios y sus enlaces salientes. Conserva los enlaces que parten de páginas UPV hacia sitios externos y los sigue comprobando por estado HTTP. Espera a que termine el proceso actual antes de hacer `git pull` y lanzar la versión nueva; no borres `state.sqlite3`.

Para enviar informes específicos, añade bajo `[mail]` de `config.ini` una línea como `subdominios = alumni.upv.es:alumni@example.org, cfp.upv.es:cfp@example.org`. Sustituye las direcciones por destinatarios reales. El informe de las demás páginas sigue llegando a `recipients`; la selección se hace por el host de la página de origen. Si no hay rutas, se conserva un solo correo. La línea se valida al arrancar.

Para probar otra raíz sin tocar la base principal, cuando no haya un rastreo activo ejecuta:

```bash
cd /opt/broken_links_checker
. ./smtp.env
venv/bin/python broken_links_checker.py --config config.ini --start-url https://etsit.upv.es/
```

El comando crea su propio fichero SQLite e informes bajo `reports/`, y comparte el bloqueo con el rastreo principal: si hay una ejecución activa, se omite y debes volver a lanzarlo cuando termine. Conserva las restricciones y los destinatarios del `config.ini`. La opción no queda programada en cron a menos que añadas una entrada específica. Si cambias la raíz en el INI sin usar la opción, define otra ruta `paths.database` y `paths.reports` para evitar mezclar las distancias de dos raíces.
