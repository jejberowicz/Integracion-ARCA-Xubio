"""La planilla completa datos sobre clientes ya cargados sin pisar lo que hay."""
import pytest

from main import _actualizar_cliente, _validar_coherencia
from src.database.models import Cliente, FuenteVentas
from src.security import crypto


@pytest.fixture(autouse=True)
def clave_de_prueba(monkeypatch):
    from src import config

    monkeypatch.setattr(config, "FERNET_KEY", crypto.generar_clave())


def hacer_cliente(**kwargs) -> Cliente:
    base = dict(
        cuit="30712345671",
        razon_social="ACME SA",
        fuente_ventas=FuenteVentas.WSFE,
        key_path="30712345671.key",
        crt_path="30712345671.crt",
        config_xubio={},
    )
    base.update(kwargs)
    return Cliente(**base)


def test_completa_los_campos_vacios():
    cliente = hacer_cliente()
    cambios = _actualizar_cliente(
        cliente,
        {"xubio_client_id": "cid", "xubio_secret": "sec", "producto_xubio_id": "101"},
        FuenteVentas.WSFE,
        dry_run=False,
    )
    assert set(cambios) == {"xubio_client_id", "xubio_secret", "producto_xubio_id"}
    assert cliente.xubio_client_id == "cid"
    assert cliente.config_xubio["producto_default_id"] == "101"


def test_el_secret_queda_cifrado():
    cliente = hacer_cliente()
    _actualizar_cliente(cliente, {"xubio_secret": "sec"}, FuenteVentas.WSFE, dry_run=False)
    assert cliente.xubio_secret_id != "sec"
    assert crypto.descifrar(cliente.xubio_secret_id) == "sec"


def test_las_celdas_vacias_no_borran():
    cliente = hacer_cliente(xubio_client_id="ya-estaba")
    cambios = _actualizar_cliente(
        cliente, {"xubio_client_id": None, "xubio_secret": ""}, FuenteVentas.WSFE, False
    )
    assert cambios == []
    assert cliente.xubio_client_id == "ya-estaba"


def test_es_idempotente_incluso_con_secretos():
    cliente = hacer_cliente()
    registro = {"xubio_client_id": "cid", "xubio_secret": "sec"}
    _actualizar_cliente(cliente, registro, FuenteVentas.WSFE, dry_run=False)
    # La segunda pasada no debe reportar cambios: los secretos se comparan
    # descifrados, no reescritos a ciegas.
    assert _actualizar_cliente(cliente, registro, FuenteVentas.WSFE, dry_run=False) == []


def test_detecta_el_secret_cambiado():
    cliente = hacer_cliente()
    _actualizar_cliente(cliente, {"xubio_secret": "viejo"}, FuenteVentas.WSFE, False)
    cambios = _actualizar_cliente(cliente, {"xubio_secret": "nuevo"}, FuenteVentas.WSFE, False)
    assert cambios == ["xubio_secret"]
    assert crypto.descifrar(cliente.xubio_secret_id) == "nuevo"


def test_dry_run_no_toca_nada():
    cliente = hacer_cliente()
    cambios = _actualizar_cliente(
        cliente, {"xubio_client_id": "cid"}, FuenteVentas.WSFE, dry_run=True
    )
    assert cambios == ["xubio_client_id"]
    assert cliente.xubio_client_id is None


def test_cambiar_a_mis_comprobantes_completa_el_usuario():
    cliente = hacer_cliente()
    cambios = _actualizar_cliente(
        cliente,
        {"fuente": "mis_comprobantes", "clave_fiscal_password": "clave"},
        FuenteVentas.MIS_COMPROBANTES,
        dry_run=False,
    )
    # El usuario de Clave Fiscal es el CUIT: sin esto choca contra el CHECK.
    assert "clave_fiscal_usuario" in cambios
    assert cliente.clave_fiscal_usuario == "30712345671"


def test_cambiar_a_wsfe_completa_las_rutas_de_certificados():
    cliente = hacer_cliente(
        fuente_ventas=FuenteVentas.MIS_COMPROBANTES, key_path=None, crt_path=None
    )
    _actualizar_cliente(cliente, {"fuente": "wsfe"}, FuenteVentas.WSFE, dry_run=False)
    assert cliente.key_path == "30712345671.key"
    assert cliente.crt_path == "30712345671.crt"


def test_coherencia_rechaza_mis_comprobantes_sin_clave():
    cliente = hacer_cliente()
    problema = _validar_coherencia(cliente, {"fuente": "mis_comprobantes"}, "mis_comprobantes")
    assert problema and "clave_fiscal_password" in problema


def test_coherencia_acepta_si_la_clave_ya_estaba_cargada():
    cliente = hacer_cliente(clave_fiscal_password="cifrado")
    assert _validar_coherencia(cliente, {}, "mis_comprobantes") is None


def test_coherencia_wsfe_siempre_pasa():
    assert _validar_coherencia(None, {}, "wsfe") is None
