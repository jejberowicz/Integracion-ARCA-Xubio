import pytest

from src.afip.tablas import (
    es_consumidor_final,
    letra_comprobante,
    nombre_comprobante,
    signo_comprobante,
)
from src.utils.helpers import cuit_valido, formatear_cuit, normalizar_cuit
from src.xubio.mapeo import ErrorMapeo, tipo_xubio


@pytest.mark.parametrize("tipo", [3, 8, 13, 53, 203, 208, 213])
def test_notas_de_credito_restan(tipo):
    assert signo_comprobante(tipo) == -1


@pytest.mark.parametrize("tipo", [1, 2, 6, 7, 11, 12, 51])
def test_facturas_y_debitos_suman(tipo):
    assert signo_comprobante(tipo) == 1


def test_letra_del_comprobante():
    assert letra_comprobante(1) == "A"
    assert letra_comprobante(6) == "B"
    assert letra_comprobante(11) == "C"
    assert letra_comprobante(51) == "M"
    assert letra_comprobante(83) is None  # "Tique", sin letra


def test_nombre_para_tipo_desconocido():
    assert "999" in nombre_comprobante(999)


def test_consumidor_final():
    assert es_consumidor_final(99, "0")
    assert es_consumidor_final(80, None)
    assert not es_consumidor_final(80, "20123456789")


def test_tipo_xubio_conocido():
    # En Xubio el tipo es un entero y la letra viaja en numeroDocumento.
    assert tipo_xubio(1) == 1     # Factura A -> Factura
    assert tipo_xubio(8) == 3     # Nota de Crédito B -> Nota de Crédito
    assert tipo_xubio(12) == 2    # Nota de Débito C -> Nota de Débito
    # Los tiques de controlador fiscal son la factura de su misma letra.
    assert tipo_xubio(82) == 1


def test_tipo_xubio_desconocido_falla_explicito():
    with pytest.raises(ErrorMapeo, match="No hay equivalencia"):
        tipo_xubio(9999)


def test_cuit():
    assert normalizar_cuit("20-12345678-9") == "20123456789"
    assert cuit_valido("30-71234567-8") == cuit_valido("30712345678")
    assert not cuit_valido("20123456780")
    assert not cuit_valido("123")
    assert formatear_cuit("20123456789") == "20-12345678-9"
