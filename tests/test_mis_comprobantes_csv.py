"""Parser del CSV de Mis Comprobantes.

El encabezado y los datos son los que devuelve el portal de verdad, copiados de
una descarga real. El archivo llega dentro de un ZIP.
"""
import datetime as dt
import io
import zipfile
from decimal import Decimal

import pytest

from src.afip.mis_comprobantes import ErrorMisComprobantes, MisComprobantes

ENCABEZADO = (
    '"Fecha de Emisión";"Tipo de Comprobante";"Punto de Venta";"Número Desde";'
    '"Número Hasta";"Cód. Autorización";"Tipo Doc. Receptor";"Nro. Doc. Receptor";'
    '"Denominación Receptor";"Tipo Cambio";"Moneda";"Imp. Neto Gravado IVA 0%";'
    '"IVA 2,5%";"Imp. Neto Gravado IVA 2,5%";"IVA 5%";"Imp. Neto Gravado IVA 5%";'
    '"IVA 10,5%";"Imp. Neto Gravado IVA 10,5%";"IVA 21%";"Imp. Neto Gravado IVA 21%";'
    '"IVA 27%";"Imp. Neto Gravado IVA 27%";"Imp. Neto Gravado Total";'
    '"Imp. Neto No Gravado";"Imp. Op. Exentas";"Otros Tributos";"Total IVA";"Imp. Total"'
)

# Factura C de un exento: sin IVA que discriminar, todo en el total.
FACTURA_C = (
    "2026-06-03;11;1;747;747;74123456789012;80;30719999995;"
    "CLIENTE RECEPTOR S.A.;1,00;$;;;;;;;;;;;;"
    "0,00;0,00;0,00;0,00;0,00;12000000,00"
)

# Factura A con IVA al 21 %.
FACTURA_A = (
    "2026-06-10;1;3;125;125;74123456789012;80;30712345671;ACME SA;1,00;$;"
    ";;;;;;;2100,00;10000,00;;;"
    "10000,00;0,00;0,00;0,00;2100,00;12100,00"
)

# Comprobante con dos alícuotas: 21 % y 10,5 %.
FACTURA_MIXTA = (
    "2026-06-11;1;3;126;126;74123456789013;80;30712345671;ACME SA;1,00;$;"
    ";;;;;105,00;1000,00;210,00;1000,00;;;"
    "2000,00;0,00;0,00;0,00;315,00;2315,00"
)


def escribir(tmp_path, filas, comprimido=False):
    texto = "\n".join([ENCABEZADO, *filas]) + "\n"
    ruta = tmp_path / ("ventas.zip" if comprimido else "ventas.csv")
    if comprimido:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as z:
            z.writestr("comprobantes_consulta_csv_emitidos_1_30712345671.csv", texto)
        ruta.write_bytes(buffer.getvalue())
    else:
        ruta.write_text(texto, encoding="utf-8")
    return ruta


def test_lee_el_encabezado_real_del_portal(tmp_path):
    ruta = escribir(tmp_path, [FACTURA_C, FACTURA_A])
    assert len(list(MisComprobantes().importar_csv(ruta))) == 2


def test_lee_el_csv_dentro_de_un_zip(tmp_path):
    # El portal exporta un .zip aunque el navegador lo guarde con otro nombre.
    ruta = escribir(tmp_path, [FACTURA_C], comprimido=True)
    comprobantes = list(MisComprobantes().importar_csv(ruta))
    assert len(comprobantes) == 1
    assert comprobantes[0].imp_total == Decimal("12000000.00")


def test_campos_de_la_factura_c(tmp_path):
    c = list(MisComprobantes().importar_csv(escribir(tmp_path, [FACTURA_C])))[0]
    assert c.tipo_cbte == 11
    assert c.pto_vta == 1
    assert c.nro == 747
    assert c.fecha == dt.date(2026, 6, 3)
    assert c.imp_total == Decimal("12000000.00")
    assert c.doc_nro == "30719999995"
    assert c.receptor_nombre == "CLIENTE RECEPTOR S.A."
    assert c.cae == "74123456789012"
    # El portal escribe la moneda como símbolo.
    assert c.moneda == "PES"


def test_en_factura_c_no_hay_iva_que_discriminar(tmp_path):
    c = list(MisComprobantes().importar_csv(escribir(tmp_path, [FACTURA_C])))[0]
    assert c.alicuotas_iva == []
    # No es falta de datos: el CSV trae las columnas y están vacías.
    assert c.alicuotas_inferidas is False


def test_toma_la_alicuota_del_csv_sin_inferir(tmp_path):
    c = list(MisComprobantes().importar_csv(escribir(tmp_path, [FACTURA_A])))[0]
    assert c.alicuotas_inferidas is False
    assert len(c.alicuotas_iva) == 1
    assert c.alicuotas_iva[0].id == 5  # 21 %
    assert c.alicuotas_iva[0].base_imp == Decimal("10000.00")
    assert c.alicuotas_iva[0].importe == Decimal("2100.00")


def test_dos_alicuotas_en_el_mismo_comprobante(tmp_path):
    c = list(MisComprobantes().importar_csv(escribir(tmp_path, [FACTURA_MIXTA])))[0]
    assert sorted(a.id for a in c.alicuotas_iva) == [4, 5]  # 10,5 % y 21 %
    assert sum(a.base_imp for a in c.alicuotas_iva) == Decimal("2000.00")
    assert sum(a.importe for a in c.alicuotas_iva) == Decimal("315.00")


def test_no_confunde_el_neto_total_con_el_del_cero_por_ciento(tmp_path):
    # 'Imp. Neto Gravado Total' e 'Imp. Neto Gravado IVA 0%' empiezan igual.
    c = list(MisComprobantes().importar_csv(escribir(tmp_path, [FACTURA_A])))[0]
    assert c.imp_neto == Decimal("10000.00")


def test_no_confunde_el_total_de_iva_con_la_alicuota_del_21(tmp_path):
    # 'Total IVA' contra 'IVA 21%'.
    c = list(MisComprobantes().importar_csv(escribir(tmp_path, [FACTURA_MIXTA])))[0]
    assert c.imp_iva == Decimal("315.00")


def test_filtra_por_rango_de_fechas(tmp_path):
    ruta = escribir(tmp_path, [FACTURA_C, FACTURA_A, FACTURA_MIXTA])
    comprobantes = list(
        MisComprobantes().importar_csv(
            ruta, desde=dt.date(2026, 6, 10), hasta=dt.date(2026, 6, 10)
        )
    )
    assert [c.nro for c in comprobantes] == [125]


def test_csv_con_columnas_faltantes_falla_claro(tmp_path):
    ruta = tmp_path / "roto.csv"
    ruta.write_text("Fecha;Otra cosa\n2026-01-01;x\n", encoding="utf-8")
    with pytest.raises(ErrorMisComprobantes, match="no tiene las columnas esperadas"):
        list(MisComprobantes().importar_csv(ruta))
