from decimal import Decimal

import pytest

from src.afip.inferencia import inferir_alicuotas


def total(alicuotas):
    return sum(a.importe for a in alicuotas)


def base(alicuotas):
    return sum(a.base_imp for a in alicuotas)


def test_iva_21_limpio():
    alicuotas = inferir_alicuotas(Decimal("1000.00"), Decimal("210.00"))
    assert len(alicuotas) == 1
    assert alicuotas[0].id == 5
    assert alicuotas[0].base_imp == Decimal("1000.00")
    assert alicuotas[0].importe == Decimal("210.00")


def test_iva_10_5():
    alicuotas = inferir_alicuotas(Decimal("2000.00"), Decimal("210.00"))
    assert [a.id for a in alicuotas] == [4]


def test_iva_27():
    alicuotas = inferir_alicuotas(Decimal("500.00"), Decimal("135.00"))
    assert [a.id for a in alicuotas] == [6]


def test_sin_iva_es_alicuota_cero():
    alicuotas = inferir_alicuotas(Decimal("800.00"), Decimal("0"))
    assert [a.id for a in alicuotas] == [3]
    assert alicuotas[0].importe == Decimal("0.00")


def test_neto_cero_no_genera_alicuotas():
    assert inferir_alicuotas(Decimal("0"), Decimal("0")) == []


def test_tolera_redondeo_por_linea():
    # 21 % sobre 1000 pero con dos centavos de arrastre.
    alicuotas = inferir_alicuotas(Decimal("1000.00"), Decimal("210.02"))
    assert [a.id for a in alicuotas] == [5]


def test_mezcla_21_y_10_5_se_parte_en_dos():
    # 1000 al 21 % (210) + 1000 al 10,5 % (105) = neto 2000, IVA 315.
    alicuotas = inferir_alicuotas(Decimal("2000.00"), Decimal("315.00"))
    assert sorted(a.id for a in alicuotas) == [4, 5]
    assert base(alicuotas) == Decimal("2000.00")
    assert total(alicuotas) == Decimal("315.00")


@pytest.mark.parametrize(
    "neto,iva",
    [
        (Decimal("1234.56"), Decimal("259.26")),
        (Decimal("87654.32"), Decimal("18407.41")),
        (Decimal("15000.00"), Decimal("1575.00")),
    ],
)
def test_las_bases_siempre_suman_el_neto(neto, iva):
    alicuotas = inferir_alicuotas(neto, iva)
    assert base(alicuotas) == neto
    assert total(alicuotas) == iva
