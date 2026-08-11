"""La planilla completa datos sobre clientes ya cargados sin pisar lo que hay."""
import pytest

from main import _actualizar_cliente, _detectar_corrupcion_excel, _validar_coherencia
from src.database.models import Cliente, FuenteCompras, FuenteVentas
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


def test_una_empresa_no_completa_sola_el_usuario_del_portal():
    # 30... es persona jurídica: su CUIT no sirve para entrar al portal, así
    # que no se puede adivinar el usuario.
    cliente = hacer_cliente(cuit="30712345671")
    _actualizar_cliente(
        cliente,
        {"fuente": "mis_comprobantes", "clave_fiscal_password": "clave"},
        FuenteVentas.MIS_COMPROBANTES,
        dry_run=False,
    )
    assert cliente.clave_fiscal_usuario is None


def test_una_persona_fisica_si_usa_su_propio_cuit():
    cliente = hacer_cliente(cuit="20123456786")
    cambios = _actualizar_cliente(
        cliente,
        {"fuente": "mis_comprobantes", "clave_fiscal_password": "clave"},
        FuenteVentas.MIS_COMPROBANTES,
        dry_run=False,
    )
    assert "clave_fiscal_usuario" in cambios
    assert cliente.clave_fiscal_usuario == "20123456786"


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
    cliente = hacer_cliente(
        cuit="20123456786", clave_fiscal_usuario="20123456786",
        clave_fiscal_password="cifrado",
    )
    assert _validar_coherencia(cliente, {}, "mis_comprobantes") is None


def test_coherencia_exige_el_usuario_cuando_el_cliente_es_empresa():
    cliente = hacer_cliente(cuit="30712345671", clave_fiscal_password="cifrado")
    problema = _validar_coherencia(cliente, {}, "mis_comprobantes")
    assert problema and "persona física que la representa" in problema


def test_coherencia_rechaza_un_usuario_que_es_empresa():
    cliente = hacer_cliente(
        cuit="20123456786", clave_fiscal_usuario="30712345671",
        clave_fiscal_password="cifrado",
    )
    problema = _validar_coherencia(cliente, {}, "mis_comprobantes")
    assert problema and "solo acepta el de una persona física" in problema


def test_coherencia_wsfe_siempre_pasa():
    assert _validar_coherencia(None, {}, "wsfe") is None


# --- Corrupción de identificadores largos por Excel ---


@pytest.mark.parametrize(
    "valor",
    ["5,51249281159823E+98", "5.51249281159823E+98", "1E+15", "-2,5e-3"],
)
def test_detecta_notacion_cientifica(valor):
    problema = _detectar_corrupcion_excel({"xubio_client_id": valor})
    assert problema and "Excel" in problema


@pytest.mark.parametrize(
    "valor",
    [
        # El client_id real: 99 dígitos, intacto.
        "5512498477298239634139608489261886125696986490506726439001659622508524670159847725512417400899284",
        "101",
        "abc123",
        "J*RgDcx49g_oym5+V6Ef1aKrls",
        None,
    ],
)
def test_no_marca_valores_sanos(valor):
    assert _detectar_corrupcion_excel({"xubio_client_id": valor}) is None


def test_revisa_todas_las_columnas_sensibles():
    assert _detectar_corrupcion_excel({"xubio_secret": "1,23E+20"})
    assert _detectar_corrupcion_excel({"producto_xubio_id": "1,23E+20"})


# --- Quién carga las compras ---

def test_sin_compras_las_traemos_nosotros():
    from main import sugerir_fuente_compras

    fuente, motivo = sugerir_fuente_compras({"cantidad": 0})
    assert fuente == FuenteCompras.MIS_COMPROBANTES
    assert "nadie las está trayendo" in motivo


def test_compras_recientes_las_carga_xubio():
    import datetime as dt

    from main import sugerir_fuente_compras

    # Si llegan hasta hace poco, el módulo de Portal IVA las está trayendo y
    # cargarlas nosotros las duplicaría.
    fuente, _ = sugerir_fuente_compras(
        {"cantidad": 500, "hasta": "2026-08-01"}, hoy=dt.date(2026, 8, 11)
    )
    assert fuente == FuenteCompras.PORTAL_IVA_XUBIO


def test_compras_viejas_quedan_para_nosotros():
    import datetime as dt

    from main import sugerir_fuente_compras

    fuente, motivo = sugerir_fuente_compras(
        {"cantidad": 500, "hasta": "2025-01-01"}, hoy=dt.date(2026, 8, 11)
    )
    assert fuente == FuenteCompras.MIS_COMPROBANTES
    assert "dejaron de cargar" in motivo


def test_el_limite_son_sesenta_dias():
    import datetime as dt

    from main import DIAS_COMPRAS_RECIENTES, sugerir_fuente_compras

    hoy = dt.date(2026, 8, 11)
    justo = hoy - dt.timedelta(days=DIAS_COMPRAS_RECIENTES)
    pasado = hoy - dt.timedelta(days=DIAS_COMPRAS_RECIENTES + 1)

    assert sugerir_fuente_compras({"cantidad": 1, "hasta": justo.isoformat()}, hoy)[0] \
        == FuenteCompras.PORTAL_IVA_XUBIO
    assert sugerir_fuente_compras({"cantidad": 1, "hasta": pasado.isoformat()}, hoy)[0] \
        == FuenteCompras.MIS_COMPROBANTES


def test_fecha_ilegible_no_decide_sola():
    from main import sugerir_fuente_compras

    fuente, motivo = sugerir_fuente_compras({"cantidad": 10, "hasta": "no-es-fecha"})
    assert fuente == FuenteCompras.NINGUNA
    assert "revisalo a mano" in motivo
