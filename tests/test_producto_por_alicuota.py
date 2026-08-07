"""En Xubio la tasa de IVA es un atributo del producto, así que cada alícuota
necesita su propio producto."""
from decimal import Decimal

from src.xubio.mapeo import armar_payload, resolver_producto
from tests.test_mapeo_xubio import hacer_comprobante

MAPA = {"21": "P21", "10.5": "P105", "0": "P0"}


def test_sin_mapa_usa_siempre_el_default():
    assert resolver_producto(5, "DEFAULT", None) == "DEFAULT"
    assert resolver_producto(4, "DEFAULT", {}) == "DEFAULT"


def test_resuelve_por_porcentaje():
    assert resolver_producto(5, "DEFAULT", MAPA) == "P21"
    assert resolver_producto(4, "DEFAULT", MAPA) == "P105"
    assert resolver_producto(3, "DEFAULT", MAPA) == "P0"


def test_resuelve_por_id_de_afip():
    assert resolver_producto(5, "DEFAULT", {"5": "PorId"}) == "PorId"


def test_tolera_como_este_escrito_el_porcentaje():
    assert resolver_producto(5, "DEFAULT", {"21.00": "P21"}) == "P21"
    assert resolver_producto(4, "DEFAULT", {" 10.5 ": "P105"}) == "P105"


def test_alicuota_no_mapeada_cae_en_el_default():
    # El 27 % no está en el mapa.
    assert resolver_producto(6, "DEFAULT", MAPA) == "DEFAULT"


def test_cada_linea_va_contra_su_producto():
    cbte = hacer_comprobante(
        imp_neto=Decimal("2000.00"),
        imp_iva=Decimal("315.00"),
        imp_total=Decimal("2315.00"),
        alicuotas_iva=[
            {"id": 5, "base_imp": "1000.00", "importe": "210.00"},
            {"id": 4, "base_imp": "1000.00", "importe": "105.00"},
        ],
    )
    items = armar_payload(
        cbte, cliente_xubio_id=1, producto_id="DEFAULT", productos_por_alicuota=MAPA
    )["transaccionProductoItems"]

    porproducto = {i["producto"]["ID"]: i["iva"] for i in items}
    assert porproducto == {"P21": 21.0, "P105": 10.5}


def test_no_gravado_y_exento_usan_el_producto_de_cero():
    cbte = hacer_comprobante(
        imp_tot_conc=Decimal("500.00"), imp_op_ex=Decimal("300.00")
    )
    items = armar_payload(
        cbte, cliente_xubio_id=1, producto_id="DEFAULT", productos_por_alicuota=MAPA
    )["transaccionProductoItems"]

    sin_iva = [i for i in items if i["iva"] == 0]
    assert len(sin_iva) == 2
    assert all(i["producto"]["ID"] == "P0" for i in sin_iva)


def test_el_mapa_solo_alcanza_sin_default():
    # Si están todas las alícuotas mapeadas, no hace falta producto default.
    cbte = hacer_comprobante()
    items = armar_payload(
        cbte, cliente_xubio_id=1, producto_id=None, productos_por_alicuota=MAPA
    )["transaccionProductoItems"]
    assert items[0]["producto"]["ID"] == "P21"
