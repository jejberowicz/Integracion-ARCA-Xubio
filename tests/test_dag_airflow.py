"""El DAG tiene que parsear y tener la forma que se espera.

Airflow no es dependencia del proyecto (vive en .venv-airflow), así que estos
tests se saltean si se corren con el intérprete del proyecto. Cuando se corren
con el de Airflow, un DAG que no importa se detecta acá y no en producción a
las siete de la mañana.

    .venv-airflow/bin/python -m pytest tests/test_dag_airflow.py
"""
import pytest

# Se pide el submódulo y no "airflow" a secas: la carpeta airflow/ del
# proyecto alcanza para que el import del paquete vacío funcione y el skip
# nunca se dispare.
modelos = pytest.importorskip(
    "airflow.models", reason="Airflow vive en su propio virtualenv"
)

DagBag = modelos.DagBag

DAG_ID = "arca_xubio_sincronizacion"


@pytest.fixture(scope="module")
def dag():
    bolsa = DagBag(dag_folder="airflow/dags", include_examples=False)
    assert not bolsa.import_errors, bolsa.import_errors
    return bolsa.get_dag(DAG_ID)


def test_el_dag_existe(dag):
    assert dag is not None


def test_corre_todos_los_dias(dag):
    # El calendario por cliente vive en la base, no en el schedule: el DAG
    # corre a diario y pregunta a quién le toca.
    assert dag.schedule_interval == "0 7 * * *"
    assert dag.catchup is False


def test_una_sola_corrida_a_la_vez(dag):
    # Es el reemplazo del flock del script de cron.
    assert dag.max_active_runs == 1


def test_la_subida_a_xubio_no_se_reintenta(dag):
    # Un POST fallido no dice si el comprobante quedó creado igual.
    subir = dag.get_task("cliente.subir")
    assert subir.retries == 0


def test_la_descarga_si_se_reintenta(dag):
    # Solo lee de ARCA: reintentar no puede romper nada.
    assert dag.get_task("cliente.descargar").retries == 2


def test_el_resumen_corre_aunque_algo_falle(dag):
    assert dag.get_task("resumen").trigger_rule == "all_done"
