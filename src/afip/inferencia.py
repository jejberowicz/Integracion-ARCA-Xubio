"""Inferencia de alícuotas de IVA.

Mis Comprobantes exporta el neto gravado y el IVA total, pero **no** el desglose
por alícuota que sí trae WSFEv1. Xubio necesita ese desglose, así que hay que
reconstruirlo a partir de los totales.

Todo lo que sale de acá viene marcado con ``inferidas=True`` para que quede
asentado en la auditoría que no es un dato informado por AFIP.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from src.afip.base import AlicuotaIVA
from src.afip.tablas import ALICUOTAS_IVA, ALICUOTAS_PROBABLES

# Tolerancia al comparar la alícuota efectiva contra las oficiales, en puntos
# porcentuales. Cubre los redondeos por línea que arrastra el total.
TOLERANCIA_PP = Decimal("0.30")

# Diferencia máxima admitida al recomponer el IVA de una combinación, en pesos.
TOLERANCIA_PESOS = Decimal("0.50")

CENTAVO = Decimal("0.01")


def _r(valor: Decimal) -> Decimal:
    return valor.quantize(CENTAVO, rounding=ROUND_HALF_UP)


def alicuota_por_porcentaje(porcentaje: Decimal) -> int | None:
    """Devuelve el Id de AFIP de la alícuota más cercana, si entra en tolerancia."""
    mejor_id, mejor_dif = None, None
    for alic_id in ALICUOTAS_PROBABLES:
        dif = abs(ALICUOTAS_IVA[alic_id] - porcentaje)
        if mejor_dif is None or dif < mejor_dif:
            mejor_id, mejor_dif = alic_id, dif
    if mejor_dif is not None and mejor_dif <= TOLERANCIA_PP:
        return mejor_id
    return None


def inferir_alicuotas(neto_gravado: Decimal, iva: Decimal) -> list[AlicuotaIVA]:
    """Reconstruye el desglose de IVA desde neto gravado + IVA total.

    Estrategia:

    1. Si no hay neto gravado, no hay nada que desglosar.
    2. Si la alícuota efectiva coincide con una oficial, se usa esa (caso normal).
    3. Si no, se prueba partir el neto entre dos alícuotas oficiales (el caso
       típico es 21 % + 10,5 %) resolviendo el sistema de dos ecuaciones.
    4. Si nada cierra, se devuelve una sola alícuota con el porcentaje efectivo
       más cercano; el importe se respeta tal cual vino.
    """
    neto_gravado = Decimal(neto_gravado)
    iva = Decimal(iva)

    if neto_gravado <= 0:
        return []

    if iva == 0:
        return [AlicuotaIVA(id=3, base_imp=_r(neto_gravado), importe=Decimal("0.00"))]

    efectiva = (iva / neto_gravado) * 100

    alic_id = alicuota_por_porcentaje(efectiva)
    if alic_id is not None:
        return [AlicuotaIVA(id=alic_id, base_imp=_r(neto_gravado), importe=_r(iva))]

    combinacion = _partir_en_dos(neto_gravado, iva)
    if combinacion:
        return combinacion

    # Último recurso: una sola alícuota, la más cercana, sin tocar los importes.
    cercana = min(
        ALICUOTAS_PROBABLES, key=lambda i: abs(ALICUOTAS_IVA[i] - efectiva)
    )
    return [AlicuotaIVA(id=cercana, base_imp=_r(neto_gravado), importe=_r(iva))]


def _partir_en_dos(neto: Decimal, iva: Decimal) -> list[AlicuotaIVA] | None:
    """Busca dos alícuotas oficiales cuyas bases sumen ``neto`` y su IVA, ``iva``.

    Con b1 + b2 = neto y t1*b1 + t2*b2 = iva, queda
    b1 = (iva - t2*neto) / (t1 - t2).
    """
    candidatas = [i for i in ALICUOTAS_PROBABLES if ALICUOTAS_IVA[i] > 0]

    for idx, id_alta in enumerate(candidatas):
        for id_baja in candidatas[idx + 1:]:
            t1 = ALICUOTAS_IVA[id_alta] / 100
            t2 = ALICUOTAS_IVA[id_baja] / 100
            if t1 == t2:
                continue

            b1 = (iva - t2 * neto) / (t1 - t2)
            b2 = neto - b1

            if b1 <= 0 or b2 <= 0:
                continue

            recompuesto = t1 * b1 + t2 * b2
            if abs(recompuesto - iva) > TOLERANCIA_PESOS:
                continue

            b1_r, b2_r = _r(b1), _r(neto - _r(b1))
            iva1 = _r(t1 * b1_r)
            # El segundo importe absorbe el redondeo para que la suma dé exacta.
            iva2 = _r(iva) - iva1
            return [
                AlicuotaIVA(id=id_alta, base_imp=b1_r, importe=iva1),
                AlicuotaIVA(id=id_baja, base_imp=b2_r, importe=iva2),
            ]
    return None
