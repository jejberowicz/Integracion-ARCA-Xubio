"""Renovación del access_token de Xubio (dura 3600 s)."""
import datetime as dt

import pytest
import requests

from src.database.models import Cliente
from src.security import crypto
from src.xubio.client import XubioClient, _token_muerto


@pytest.fixture(autouse=True)
def clave_de_prueba(monkeypatch):
    from src import config

    monkeypatch.setattr(config, "FERNET_KEY", crypto.generar_clave())


@pytest.fixture
def cliente():
    return Cliente(
        cuit="30712345671",
        razon_social="ACME SA",
        xubio_client_id="cid",
        xubio_secret_id=crypto.cifrar("sec"),
        config_xubio={},
    )


def respuesta(status: int, cuerpo: bytes) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = cuerpo
    r.headers["Content-Type"] = "application/json"
    return r


def test_detecta_el_token_muerto_de_la_documentacion():
    cuerpo = b'{"error":"invalid_token","error_description":"token died"}'
    # Sin importar el codigo de estado con que venga.
    assert _token_muerto(respuesta(200, cuerpo))
    assert _token_muerto(respuesta(401, cuerpo))


def test_no_confunde_una_respuesta_normal():
    assert not _token_muerto(respuesta(200, b'[{"cliente_id":1,"nombre":"X"}]'))
    assert not _token_muerto(respuesta(200, b""))
    assert not _token_muerto(respuesta(500, b"<html>Internal Server Error</html>"))


def test_no_confunde_otro_error_de_oauth():
    assert not _token_muerto(respuesta(400, b'{"error":"invalid_request"}'))


def test_el_token_se_reusa_mientras_este_vigente(cliente, monkeypatch):
    llamadas = []

    def falso_post(url, **kw):
        llamadas.append(url)
        return respuesta(200, b'{"access_token":"T1","expires_in":"3600"}')

    xubio = XubioClient(cliente)
    monkeypatch.setattr(xubio.sesion, "post", falso_post)

    assert xubio.autenticar() == "T1"
    assert xubio.autenticar() == "T1"
    assert len(llamadas) == 1  # el segundo sale del cache


def test_se_renueva_un_minuto_antes_de_vencer(cliente, monkeypatch):
    monkeypatch.setattr(
        XubioClient,
        "autenticar",
        XubioClient.autenticar,
    )
    xubio = XubioClient(cliente)
    monkeypatch.setattr(
        xubio.sesion,
        "post",
        lambda url, **kw: respuesta(200, b'{"access_token":"T1","expires_in":"3600"}'),
    )
    xubio.autenticar()

    restante = xubio._token_expira - dt.datetime.now(dt.timezone.utc)
    # 3600 menos el margen de 60 segundos.
    assert dt.timedelta(seconds=3530) < restante <= dt.timedelta(seconds=3540)


def test_expires_in_como_texto_no_rompe(cliente, monkeypatch):
    # La documentación lo devuelve entre comillas: "expires_in":"3600".
    xubio = XubioClient(cliente)
    monkeypatch.setattr(
        xubio.sesion,
        "post",
        lambda url, **kw: respuesta(200, b'{"access_token":"T1","expires_in":"3600"}'),
    )
    assert xubio.autenticar() == "T1"


# --- Errores de negocio disfrazados de 401 ---

def test_extrae_el_motivo_del_rechazo():
    from src.xubio.client import _motivo_funcional

    cuerpo = (
        b'{"description":"La cotizacion para moneda principal no puede ser '
        b'distinta de 1","codeResponse":400,"stackTrace":[{"methodName":"post"}]}'
    )
    # Xubio lo manda con 401 aunque sea un error de validación.
    motivo = _motivo_funcional(respuesta(401, cuerpo))
    assert motivo and "cotizacion para moneda principal" in motivo
    assert "400" in motivo


def test_una_respuesta_normal_no_es_un_rechazo():
    from src.xubio.client import _motivo_funcional

    assert _motivo_funcional(respuesta(200, b'[{"cliente_id":1}]')) is None
    assert _motivo_funcional(respuesta(401, b'{"error":"invalid_token"}')) is None


def test_el_token_muerto_no_se_confunde_con_un_rechazo():
    from src.xubio.client import _motivo_funcional, _token_muerto

    cuerpo = b'{"error":"invalid_token","error_description":"token died"}'
    assert _token_muerto(respuesta(401, cuerpo))
    assert _motivo_funcional(respuesta(401, cuerpo)) is None
