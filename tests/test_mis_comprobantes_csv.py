import datetime as dt
from decimal import Decimal

import pytest

from src.afip.mis_comprobantes import ErrorMisComprobantes, MisComprobantes

CSV_VENTAS = """\
Fecha de Emisión;Tipo de Comprobante;Punto de Venta;Número Desde;Número Hasta;Cód. Autorización;\
Tipo Doc. Receptor;Nro. Doc. Receptor;Denominación Receptor;Tipo Cambio;Moneda;\
Imp. Neto Gravado;Imp. Neto No Gravado;Imp. Op. Exentas;Otros Tributos;IVA;Imp. Total
2026-03-05;1 - Factura A;00003;00000125;00000125;74123456789012;80;30712345678;ACME S.A.;1,00;PES;\
10.000,00;0,00;0,00;0,00;2.100,00;12.100,00
2026-03-07;83 - Tique;00004;00000876;00000876;;99;0;Consumidor Final;1,00;PES;\
1.652,89;0,00;0,00;0,00;347,11;2.000,00
2026-03-09;3 - Nota de Crédito A;00003;00000012;00000012;74123456789099;80;30712345678;ACME S.A.;1,00;PES;\
1.000,00;0,00;0,00;0,00;210,00;1.210,00
"""


@pytest.fixture
def archivo_csv(tmp_path):
    ruta = tmp_path / "ventas.csv"
    ruta.write_text(CSV_VENTAS, encoding="utf-8")
    return ruta


def test_parsea_todas_las_filas(archivo_csv):
    comprobantes = list(MisComprobantes().importar_csv(archivo_csv))
    assert len(comprobantes) == 3


def test_campos_de_la_factura_a(archivo_csv):
    factura = list(MisComprobantes().importar_csv(archivo_csv))[0]
    assert factura.tipo_cbte == 1
    assert factura.pto_vta == 3
    assert factura.nro == 125
    assert factura.fecha == dt.date(2026, 3, 5)
    assert factura.imp_total == Decimal("12100.00")
    assert factura.imp_neto == Decimal("10000.00")
    assert factura.imp_iva == Decimal("2100.00")
    assert factura.doc_nro == "30712345678"
    assert factura.receptor_nombre == "ACME S.A."
    assert factura.cae == "74123456789012"


def test_alicuotas_quedan_marcadas_como_inferidas(archivo_csv):
    factura = list(MisComprobantes().importar_csv(archivo_csv))[0]
    assert factura.alicuotas_inferidas is True
    assert [a.id for a in factura.alicuotas_iva] == [5]
    assert factura.alicuotas_iva[0].importe == Decimal("2100.00")


def test_tique_de_controlador_fiscal(archivo_csv):
    tique = list(MisComprobantes().importar_csv(archivo_csv))[1]
    assert tique.tipo_cbte == 83
    assert tique.imp_total == Decimal("2000.00")
    assert tique.cae is None


def test_filtra_por_rango_de_fechas(archivo_csv):
    comprobantes = list(
        MisComprobantes().importar_csv(
            archivo_csv, desde=dt.date(2026, 3, 6), hasta=dt.date(2026, 3, 8)
        )
    )
    assert [c.nro for c in comprobantes] == [876]


def test_csv_con_columnas_faltantes_falla_claro(tmp_path):
    ruta = tmp_path / "roto.csv"
    ruta.write_text("Fecha;Otra cosa\n2026-01-01;x\n", encoding="utf-8")
    with pytest.raises(ErrorMisComprobantes, match="no tiene las columnas esperadas"):
        list(MisComprobantes().importar_csv(ruta))
