#!/usr/bin/env bash
set -euo pipefail

# Run from the extracted project directory: sudo ./install_linux.sh [--enable-cron]
app_dir=/opt/broken_links_checker
service_user=analytics-svc
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
mode=${1:-install}

if [[ $EUID -ne 0 ]]; then
    echo 'Ejecuta este instalador con sudo.' >&2
    exit 1
fi
if [[ $mode != install && $mode != --enable-cron ]]; then
    echo 'Uso: sudo ./install_linux.sh [--enable-cron]' >&2
    exit 2
fi

if [[ $mode == install ]]; then
    command -v python3 >/dev/null || { echo 'Falta python3.' >&2; exit 1; }
    command -v runuser >/dev/null || { echo 'Falta runuser (util-linux).' >&2; exit 1; }
    if ! id -u "$service_user" >/dev/null 2>&1; then
        echo "No existe el usuario $service_user en este servidor." >&2
        exit 1
    fi
    service_group=$(id -gn "$service_user")
    install -d -o "$service_user" -g "$service_group" -m 0750 "$app_dir" "$app_dir/reports"
    for item in broken_links_checker.py config.ini.example smtp.env.example requirements.txt; do
        install -o "$service_user" -g "$service_group" -m 0644 "$script_dir/$item" "$app_dir/$item"
    done
    if [[ ! -e $app_dir/config.ini ]]; then
        install -o "$service_user" -g "$service_group" -m 0640 \
            "$script_dir/config.ini.example" "$app_dir/config.ini"
    fi
    if [[ ! -e $app_dir/smtp.env ]]; then
        install -o "$service_user" -g "$service_group" -m 0600 \
            "$script_dir/smtp.env.example" "$app_dir/smtp.env"
    fi
    if [[ ! -x $app_dir/venv/bin/python ]]; then
        runuser -u "$service_user" -- python3 -m venv "$app_dir/venv"
    fi
    runuser -u "$service_user" -- "$app_dir/venv/bin/python" -m pip install -r "$app_dir/requirements.txt"
    echo "Instalado en $app_dir sin modificar /opt/Analytics ni sobrescribir config.ini o smtp.env."
    echo 'Configura SMTP y destinatarios, prueba una ejecución pequeña y luego activa cron.'
    echo 'Para activar cron: sudo ./install_linux.sh --enable-cron'
    exit 0
fi

if [[ ! -x $app_dir/venv/bin/python || ! -f $app_dir/config.ini || ! -f $app_dir/smtp.env ]]; then
    echo 'Primero ejecuta sudo ./install_linux.sh y configura los archivos.' >&2
    exit 1
fi
if [[ ! -d /etc/cron.d ]]; then
    echo 'No existe /etc/cron.d; instala cron antes de activarlo.' >&2
    exit 1
fi
python3 - "$app_dir/config.ini" "$app_dir/smtp.env" <<'PY'
import configparser
import pathlib
import sys

cfg = configparser.ConfigParser(interpolation=None)
cfg.read(sys.argv[1])
env = pathlib.Path(sys.argv[2]).read_text()
if not cfg.getboolean('mail', 'enabled', fallback=False):
    raise SystemExit('Activa mail.enabled = yes antes de habilitar cron.')
if cfg.get('mail', 'smtp_server_address', fallback='') == 'smtp.example.org':
    raise SystemExit('Configura el servidor SMTP real.')
if cfg.get('mail', 'recipients', fallback='') == 'webmaster@example.org':
    raise SystemExit('Configura los destinatarios reales.')
if 'sustituir-por-la-contraseña-real' in env or 'NO_ES_UNA_CONTRASENA_REAL' in env:
    raise SystemExit('Sustituye las credenciales de ejemplo en smtp.env.')
PY

cron_file=/etc/cron.d/upv-broken-links-checker
cat >"$cron_file" <<'CRON'
# Se interpreta en la zona horaria del servidor. Ejecutar una vez al día a las 03:00.
SHELL=/bin/sh
0 3 * * * analytics-svc . /opt/broken_links_checker/smtp.env && cd /opt/broken_links_checker && /opt/broken_links_checker/venv/bin/python broken_links_checker.py --config config.ini >> /opt/broken_links_checker/reports/cron.log 2>&1
CRON
chmod 0644 "$cron_file"
echo "Cron activado: $cron_file (03:00, zona horaria del servidor)."
