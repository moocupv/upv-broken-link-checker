# Auditoría incremental de enlaces UPV

La **portada es el nivel 0 y la única semilla**. Cada salto por un enlace HTTP(S), aunque cambie de dominio, añade un nivel. El mapa web solo entra en el rastreo si se puede alcanzar desde la portada, al nivel que corresponda. SQLite guarda el menor nivel conocido de cada página, sus enlaces, los resultados de comprobación y la ronda completada. `level_schedule = 0:1;1:1;2:1;3:4` programa rondas diarias para los niveles 0, 1 y 2, y una ronda cada cuatro días para el nivel 3. Una ronda nueva empieza cuando termina la anterior y ha transcurrido el intervalo desde su comienzo. Una página **se rastrea una sola vez por ronda de su nivel** aunque otros enlaces vuelvan a señalarla; los fallos inciertos se reintentan.

Cada ejecución revisa **hasta 10.000 páginas** por defecto, incluyendo las que descubra durante ese mismo lote, y entrega **un correo** con el CSV de incidencias vistas. El correo empieza con el resumen `Nivel | Día del rastreo | Número de enlaces rotos`, seguido del listado (primeras 200 filas por defecto; CSV completo adjunto). Un informe vacío solo indica que no se encontraron enlaces rotos en las páginas procesadas. Se comprueban los enlaces de las páginas de niveles 0 a 3, incluidos los que apuntan a nivel 4; las páginas de nivel 4 no se rastrean. Una página puede recibir una petición al comprobarla como enlace y otra al rastrearla; solo la segunda analiza sus enlaces. Los resultados válidos se reutilizan durante `link_ttl_days = 1`; los fallos inciertos se reintentan.

## Instalación en Linux

```bash
sudo mkdir -p /opt/broken_links_checker/reports
sudo cp broken_links_checker.py config.ini.example smtp.env.example requirements.txt /opt/broken_links_checker/
cd /opt/broken_links_checker
sudo cp config.ini.example config.ini
sudo python3 -m venv venv
sudo venv/bin/pip install -r requirements.txt
```

Editar `config.ini`: rutas, destinatarios, servidor SMTP, cuota y periodicidad. Si se actualiza una instalación previa, copiar las nuevas opciones `db_retention_days`, `report_timezone`, `level_schedule` y `mail.user_env`; las opciones antiguas `allowed_hosts`, `exclude_patterns` y `mail.smtp_user` ya no se usan. Activar `mail.enabled = yes` tras configurar el servidor. Crear `/opt/broken_links_checker/smtp.env` copiando `smtp.env.example` y sustituir **ambos** valores de ejemplo:

```bash
export UPV_SMTP_USER='cuenta-smtp@example.org'
export UPV_SMTP_PASSWORD='contraseña-real'
```

El propietario de `smtp.env` debe ser la cuenta que ejecuta cron; restringirlo con `chmod 600 /opt/broken_links_checker/smtp.env`. `config.ini` no contiene usuario ni contraseña; `from_address` puede quedar vacío para usar `UPV_SMTP_USER` como remitente. Usar una cuenta de servicio con acceso de escritura al directorio de informes y a la base de datos. Instalar en su crontab (`crontab -e`):

```cron
0 3 * * * . /opt/broken_links_checker/smtp.env && cd /opt/broken_links_checker && /opt/broken_links_checker/venv/bin/python broken_links_checker.py --config config.ini >> /opt/broken_links_checker/reports/cron.log 2>&1
```

El cron diario reparte la cobertura entre ejecuciones. `max_pages_per_run = 10000` y `max_http_requests_per_run = 50000` son **techos**, no promesas: el segundo cuenta intentos, redirecciones, reintentos y solicitudes a robots.txt. También se detiene si se agota la cola, si se alcanza `max_duration_hours = 20`, o si el servidor limita las peticiones. La página interrumpida por un límite conserva su estado pendiente y se reanuda en el siguiente cron. El lock evita solapamientos. Se siguen enlaces HTTP(S) a **cualquier dominio público**, con o sin parámetros, hasta el nivel configurado. Se comprueban los enlaces de todas las páginas procesadas, incluidos los destinos fuera del último nivel. Las extensiones binarias se comprueban como enlace y no se exploran como página. Los hosts locales o privados se omiten por defecto; `allow_private_hosts = yes` es solo para pruebas controladas.

Cada petición al mismo host se separa al menos `min_interval_seconds = 1.0` segundos, y las peticiones globales al menos `min_global_interval_seconds = 0.2`. Ante un 429 o 503 se respeta `Retry-After` (segundos o fecha HTTP); sin esa cabecera se aplica una espera exponencial corta. Si la espera supera `max_retry_wait_seconds = 30` o fallan tres intentos, ese host se pausa durante el resto de la ejecución; el cron siguiente lo vuelve a probar. Las páginas que no se pudieron revisar se reintentan al día siguiente. Los 429/503, otras respuestas 5xx, 401/403/408 y errores de red se cuentan como inciertos, no como enlaces rotos. El CSV incluye nivel, fecha, origen, destino, ancla y resultado. Un robots.txt con 4xx se trata como inexistente; con 5xx o error de red el host se omite temporalmente.

La tabla `pages` permite inspeccionar el tamaño descubierto por nivel (`SELECT level,count(*) FROM pages WHERE level IS NOT NULL GROUP BY level;`). La tabla `level_rounds` indica qué ronda está activa y cuándo puede empezar la siguiente. SQLite evita rastrear de nuevo una URL completada en su ronda aunque aparezca en distintas páginas de origen. Las páginas inaccesibles por errores temporales se reintentan al día siguiente sin marcar su ronda como completada. Los enlaces de páginas que desaparecen se sustituyen al volver a visitarlas. Al actualizar una base SQLite creada por la versión anterior, se conservan enlaces y comprobaciones, pero se reinician los niveles y las rondas: las distancias se recalculan desde la portada.

`db_retention_days = 30` elimina resultados de comprobación antiguos y páginas no visitadas desde hace ese plazo, junto con los enlaces que solo pertenecían a ellas. Conserva la cola activa y la portada para que un rastreo interrumpido pueda continuar. El valor debe ser al menos el mayor intervalo **efectivo** de `level_schedule`; de lo contrario la ejecución se detiene con un error de configuración. Si un nivel pide menos días que el anterior, el script utiliza los días del anterior y lo registra en el log. Los CSV generados en `reports` no están sujetos a esta retención de SQLite.

Las cifras de 2.500 páginas en nivel 2 y 24.000 en nivel 3 obtenidas desde el mapa web **no se trasladan directamente** a niveles definidos desde la portada, especialmente cuando se incluyen otros dominios. Tras las primeras rondas, usar los recuentos reales de `pages` para calibrar `max_pages_per_run`. El tope de 10.000 y el límite de 50.000 peticiones pueden alargar una ronda; el programa no fuerza la cadencia mediante más solicitudes. Los niveles limitan **qué páginas se rastrean**, no cuántos enlaces se comprueban en cada una. Una página cuyo contenido requiere JavaScript, autenticación o está excluida por robots no puede verificarse íntegramente con este rastreador HTTP.

El rastreador no ejecuta JavaScript de analítica, pero sus peticiones sí aparecen en logs, estadísticas de servidor y métricas de CDN. Excluir el `User-Agent` configurado (`UPV-BrokenLinks-Audit/1.0`) o la IP fija del cron en esas herramientas; el script no puede excluirse por sí solo de métricas generadas por el servidor.
