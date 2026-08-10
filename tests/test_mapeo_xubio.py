import datetime as dt
from decimal import Decimal

import pytest

from src.database.models import Comprobante
from src.xubio.mapeo import ErrorMapeo, armar_payload, datos_cliente_xubio


def hacer_comprobante(**kwargs) -> Comprobante:
    base = dict(
        tipo_cbte=1,
        pto_vta=3,
        nro=125,
        fecha=dt.date(2026, 3, 5),
        imp_total=Decimal("12100.00"),
        imp_neto=Decimal("10000.00"),
        imp_iva=Decimal("2100.00"),
        imp_op_ex=Decimal("0"),
        imp_tot_conc=Decimal("0"),
        imp_trib=Decimal("0"),
        moneda="PES",
        cotizacion=Decimal("1"),
        doc_tipo=80,
        doc_nro="30712345671",
        receptor_nombre="ACME S.A.",
        alicuotas_iva=[{"id": 5, "base_imp": "10000.00", "importe": "2100.00"}],
        fuente="wsfe",
        cae="74123456789012",
    )
    base.update(kwargs)
    return Comprobante(**base)


def test_payload_de_factura_a():
    payload = armar_payload(hacer_comprobante(), cliente_xubio_id=42, producto_id=777)

    assert payload["tipo"] == 1
    assert payload["cliente"] == {"ID": 42}
    assert payload["fecha"] == "2026-03-05"
    # La letra fiscal va como prefijo del número, no en un campo aparte.
    assert payload["numeroDocumento"] == "A-00003-00000125"
    assert payload["moneda"] == {"ID": -2}
    assert payload["importetotal"] == 12100.0
    assert payload["importeGravado"] == 10000.0
    assert payload["importeImpuestos"] == 2100.0


def test_una_linea_por_alicuota():
    cbte = hacer_comprobante(
        imp_neto=Decimal("2000.00"),
        imp_iva=Decimal("315.00"),
        imp_total=Decimal("2315.00"),
        alicuotas_iva=[
            {"id": 5, "base_imp": "1000.00", "importe": "210.00"},
            {"id": 4, "base_imp": "1000.00", "importe": "105.00"},
        ],
    )
    items = armar_payload(cbte, cliente_xubio_id=1, producto_id=777)["transaccionProductoItems"]

    assert len(items) == 2
    # 'iva' es el importe en pesos, no el porcentaje.
    assert {i["iva"] for i in items} == {210.0, 105.0}
    assert sum(i["importe"] for i in items) == 2000.0
    assert sum(i["iva"] for i in items) == 315.0


def test_no_gravado_y_exento_van_como_lineas_aparte():
    cbte = hacer_comprobante(
        imp_tot_conc=Decimal("500.00"),
        imp_op_ex=Decimal("300.00"),
    )
    items = armar_payload(cbte, cliente_xubio_id=1, producto_id=777)["transaccionProductoItems"]

    assert len(items) == 3
    descripciones = " ".join(i["descripcion"] for i in items)
    assert "No gravado" in descripciones and "Exento" in descripciones
    assert all(i["iva"] == 0 for i in items if "gravado" in i["descripcion"].lower()
               or "Exento" in i["descripcion"])


def test_nota_de_credito_usa_su_propio_tipo_con_importes_positivos():
    payload = armar_payload(
        hacer_comprobante(tipo_cbte=3), cliente_xubio_id=1, producto_id=777
    )
    assert payload["tipo"] == 3
    assert payload["numeroDocumento"].startswith("A-")
    assert payload["importetotal"] > 0


def test_falta_producto_falla_con_mensaje_util():
    with pytest.raises(ErrorMapeo, match="producto_default_id"):
        armar_payload(hacer_comprobante(), cliente_xubio_id=1, producto_id=None)


def test_receptor_consumidor_final():
    datos = datos_cliente_xubio(
        hacer_comprobante(doc_tipo=99, doc_nro="0", receptor_nombre=None)
    )
    assert datos["categoriaFiscal"] == {"ID": 6}
    assert "CUIT" not in datos


def test_receptor_con_cuit():
    datos = datos_cliente_xubio(hacer_comprobante())
    # Xubio guarda el CUIT formateado con guiones.
    assert datos["CUIT"] == "30-71234567-1"
    assert datos["identificacionTributaria"] == {"ID": 9}
    assert datos["nombre"] == "ACME S.A."


def test_el_nombre_se_recorta_a_lo_que_acepta_xubio():
    # Xubio corta los nombres de cliente en 30 caracteres.
    largo = "COOPERATIVA DE TRABAJO Y SERVICIOS DEL SUR LIMITADA"
    datos = datos_cliente_xubio(hacer_comprobante(receptor_nombre=largo))
    assert datos["nombre"] == largo[:30]
    assert len(datos["nombre"]) == 30


def test_el_producto_va_como_numero():
    # config_xubio guarda los IDs como texto; Xubio los quiere enteros.
    items = armar_payload(
        hacer_comprobante(), cliente_xubio_id=1, producto_id="1000001"
    )["transaccionProductoItems"]
    assert items[0]["producto"]["ID"] == 1000001


def test_la_cotizacion_uno_va_como_entero():
    # Con 1.0 Xubio rechaza: "La cotización para moneda principal no puede ser
    # distinta de 1".
    payload = armar_payload(hacer_comprobante(), cliente_xubio_id=1, producto_id=1)
    assert payload["cotizacion"] == 1
    assert isinstance(payload["cotizacion"], int)


def test_una_cotizacion_real_conserva_los_decimales():
    from decimal import Decimal as D

    payload = armar_payload(
        hacer_comprobante(moneda="DOL", cotizacion=D("1350.75")),
        cliente_xubio_id=1, producto_id=1,
    )
    assert payload["cotizacion"] == 1350.75
    assert payload["moneda"] == {"ID": -3}
