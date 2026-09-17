"""DAG diario ARCA -> Xubio: una rama por cliente.

Reemplaza al cron (`scripts/sincronizar.sh`). Lo que aporta sobre el shell:

- una tarea por cliente en vez de un único proceso, así una credencial vencida
  no se lleva puestos a los otros diecinueve;
- la descarga se reintenta sola, la subida **no** (un POST a Xubio que falla no
  se puede reintentar sin arriesgar un duplicado);
- lo que necesita una persona —captcha, 2FA, un trámite pendiente en ARCA— se
  marca como fallo definitivo y no se reintenta hasta mañana;
- la bitácora, los reintentos y el "ya hay una corrida andando" los pone
  Airflow, en vez del `flock` y el `find -mtime +60` del script.

El calendario de cada cliente sigue viviendo en la base
(`clientes_afip.dia_sincronizacion`): el DAG corre todos los días y pregunta a
quién le toca. Ponerlo en el `schedule` obligaría a un DAG por cliente.

**El DAG no importa nada del proyecto.** Airflow trae sus propias versiones de
SQLAlchemy y compañía, y el proyecto usa Playwright y SQLAlchemy 2.x; mezclar
los dos entornos es pedir un conflicto de dependencias. Todo se invoca por
subprocess contra el virtualenv del proyecto.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import timedelta
from pathlib import Path

import pendulum
from airflow.decorators import dag, task, task_group
from airflow.exceptions import AirflowFailException
from airflow.models.param import Param

log = logging.getLogger(__name__)

#: Raíz del proyecto y su intérprete. Se pueden pisar por entorno para correr
#: el DAG desde otra máquina o contra otra copia.
RAIZ = Path(os.getenv("CARGA_DATOS_ARCA_HOME", Path(__file__).resolve().parents[2]))
PYTHON = os.getenv("CARGA_DATOS_ARCA_PYTHON", str(RAIZ / ".venv" / "bin" / "python"))

TZ = pendulum.timezone("America/Argentina/Buenos_Aires")

#: Código de salida con el que `main.py sync` avisa que reintentar no sirve.
SALIDA_REQUIERE_ATENCION = 2

#: Una corrida de scraping con navegador puede ser lenta, pero no eterna.
TIMEOUT = timedelta(minutes=45)


def _correr(argumentos: list[str]) -> str:
    """Corre `main.py <argumentos>` en el virtualenv del proyecto.

    Devuelve el stdout. Traduce el código de salida a la distinción que le
    importa a Airflow: lo que se reintenta y lo que no.
    """
    comando = [PYTHON, "main.py", *argumentos]
    log.info("Ejecutando: %s (en %s)", " ".join(comando), RAIZ)

    proceso = subprocess.run(
        comando,
        cwd=RAIZ,
        capture_output=True,
        text=True,
        timeout=TIMEOUT.total_seconds(),
    )

    if proceso.stdout:
        log.info("--- salida ---\n%s", proceso.stdout.strip())
    if proceso.stderr:
        log.info("--- log ---\n%s", proceso.stderr.strip())

    if proceso.returncode == SALIDA_REQUIERE_ATENCION:
        # AirflowFailException corta los reintentos: nadie va a resolver un
        # captcha porque el scheduler pruebe de nuevo en quince minutos.
        raise AirflowFailException(
            "Requiere que alguien resuelva algo a mano (captcha, 2FA o un "
            "trámite en ARCA). No se reintenta; mirá la salida de arriba."
        )
    if proceso.returncode != 0:
        raise RuntimeError(f"main.py {' '.join(argumentos)} terminó con estado {proceso.returncode}")

    return proceso.stdout


def _rango(params: dict) -> list[str]:
    """Traduce los params del DAG a las opciones --desde/--hasta."""
    opciones = []
    for nombre in ("desde", "hasta"):
        if params.get(nombre):
            opciones += [f"--{nombre}", params[nombre]]
    return opciones


@dag(
    dag_id="arca_xubio_sincronizacion",
    description="Ventas de ARCA hacia Xubio, un cliente por rama",
    schedule="0 7 * * *",
    start_date=pendulum.datetime(2026, 1, 1, tz=TZ),
    catchup=False,
    # Una sola corrida a la vez: es el reemplazo del flock del script viejo.
    max_active_runs=1,
    # Y de a un cliente por vez: el scraping abre un navegador y la base es
    # SQLite. Si algún día se pasa a Postgres, esto se puede subir.
    max_active_tasks=1,
    default_args={
        "owner": "estudio",
        "depends_on_past": False,
        "execution_timeout": TIMEOUT,
    },
    params={
        "desde": Param(None, type=["null", "string"], format="date", title="Desde (opcional)"),
        "hasta": Param(None, type=["null", "string"], format="date", title="Hasta (opcional)"),
        "cuits": Param(
            [],
            type="array",
            title="CUIT puntuales",
            description="Vacío = los que hoy corresponda según su calendario.",
        ),
        "igualmente": Param(
            False,
            type="boolean",
            title="Ignorar el calendario",
            description="Sincronizar a todos los activos aunque hoy no les toque.",
        ),
    },
    tags=["arca", "xubio", "contable"],
    doc_md=__doc__,
)
def sincronizacion_arca_xubio():

    @task
    def clientes_de_hoy(**contexto) -> list[str]:
        """A quién le toca hoy. La respuesta la da la base, no el schedule."""
        params = contexto["params"]
        if params.get("cuits"):
            elegidos = [str(c).strip() for c in params["cuits"] if str(c).strip()]
            log.info("Corrida puntual sobre %s", elegidos)
            return elegidos

        argumentos = ["clientes-hoy", "--json"]
        if params.get("igualmente"):
            argumentos.append("--igualmente")

        salida = _correr(argumentos)
        # El comando imprime el JSON en una sola línea; el resto es ruido.
        cuits = json.loads(salida.strip().splitlines()[-1])
        log.info("Hoy le toca a %s cliente(s): %s", len(cuits), cuits or "ninguno")
        return cuits

    @task_group(group_id="cliente")
    def sincronizar(cuit: str):
        """Las dos mitades del flujo, con políticas de reintento distintas."""

        @task(retries=2, retry_delay=timedelta(minutes=15))
        def descargar(cuit: str, **contexto) -> str:
            """ARCA -> base. Solo lectura, así que reintentar es gratis."""
            return _correr(
                ["sync", "--cuit", cuit, "--solo-descargar", "--igualmente"]
                + _rango(contexto["params"])
            )

        @task(retries=0)
        def subir(cuit: str, **contexto) -> str:
            """Base -> Xubio. Sin reintentos automáticos.

            Un POST que falla no dice si el comprobante quedó creado igual.
            Lo pendiente se retoma en la corrida siguiente, que antes de subir
            vuelve a preguntarle a Xubio si el número ya existe.
            """
            return _correr(
                ["sync", "--cuit", cuit, "--solo-subir", "--igualmente"]
                + _rango(contexto["params"])
            )

        descargar(cuit) >> subir(cuit)

    @task(trigger_rule="all_done", retries=0)
    def resumen() -> None:
        """Qué quedó sin entrar a Xubio, con corrida buena o mala."""
        _correr(["pendientes"])

    clientes = clientes_de_hoy()
    ramas = sincronizar.expand(cuit=clientes)
    ramas >> resumen()


sincronizacion_arca_xubio()
