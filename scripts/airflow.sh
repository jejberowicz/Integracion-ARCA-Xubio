#!/usr/bin/env bash
# Airflow para este proyecto: instalación, arranque y prueba.
#
#   scripts/airflow.sh instalar   # crea .venv-airflow y la base de Airflow
#   scripts/airflow.sh iniciar    # scheduler + web en http://localhost:8080
#   scripts/airflow.sh probar     # corre el DAG entero, sin servidor
#
# Airflow vive en su propio virtualenv: sus dependencias chocan con las del
# proyecto (ver requirements-airflow.txt). El DAG llama al proyecto por
# subprocess, así que los dos entornos nunca se cruzan.
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$RAIZ"

VENV="$RAIZ/.venv-airflow"
VERSION="2.10.5"
PY_MINOR="$("$RAIZ/.venv/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
CONSTRAINTS="https://raw.githubusercontent.com/apache/airflow/constraints-${VERSION}/constraints-${PY_MINOR}.txt"

export AIRFLOW_HOME="$RAIZ/airflow"
export AIRFLOW__CORE__DAGS_FOLDER="$RAIZ/airflow/dags"
export AIRFLOW__CORE__LOAD_EXAMPLES=False
# El scraping de ARCA abre un navegador y la base del proyecto es SQLite: de a
# una tarea por vez. Con Postgres esto se puede subir.
export AIRFLOW__CORE__PARALLELISM=1
export AIRFLOW__CORE__EXECUTOR=LocalExecutor
# Para que el DAG encuentre el proyecto aunque Airflow corra desde otro lado.
export CARGA_DATOS_ARCA_HOME="$RAIZ"
export CARGA_DATOS_ARCA_PYTHON="$RAIZ/.venv/bin/python"

instalar() {
    if [[ ! -d "$VENV" ]]; then
        "$RAIZ/.venv/bin/python" -m venv "$VENV"
    fi
    "$VENV/bin/pip" install --upgrade pip
    "$VENV/bin/pip" install -r "$RAIZ/requirements-airflow.txt" --constraint "$CONSTRAINTS"
    "$VENV/bin/airflow" db migrate
    echo
    echo "Listo. 'scripts/airflow.sh iniciar' levanta scheduler y web."
}

verificar_instalado() {
    if [[ ! -x "$VENV/bin/airflow" ]]; then
        echo "Falta instalar Airflow: scripts/airflow.sh instalar" >&2
        exit 1
    fi
}

case "${1:-}" in
    instalar)
        instalar
        ;;
    iniciar)
        verificar_instalado
        # standalone levanta scheduler + web y crea el usuario admin la
        # primera vez (la contraseña queda en airflow/simple_auth_manager_passwords.json
        # o airflow/standalone_admin_password.txt según la versión).
        exec "$VENV/bin/airflow" standalone
        ;;
    scheduler)
        verificar_instalado
        exec "$VENV/bin/airflow" scheduler
        ;;
    probar)
        verificar_instalado
        # Corre el DAG completo en este proceso, sin scheduler ni servidor.
        # Toca ARCA y Xubio de verdad: es la corrida real, no un simulacro.
        exec "$VENV/bin/airflow" dags test arca_xubio_sincronizacion "$(date +%Y-%m-%d)"
        ;;
    *)
        sed -n '2,8p' "$0"
        exit 1
        ;;
esac
