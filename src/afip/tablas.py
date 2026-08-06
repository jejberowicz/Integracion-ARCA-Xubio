"""Tablas de referencia de AFIP/ARCA.

Fuente: FEParamGetTiposCbte / FEParamGetTiposIva de WSFEv1. Se hardcodean las de
uso corriente para no depender de una llamada extra en cada corrida.
"""
from __future__ import annotations

from decimal import Decimal

# --- Tipos de comprobante ---

TIPOS_COMPROBANTE: dict[int, str] = {
    1: "Factura A",
    2: "Nota de Débito A",
    3: "Nota de Crédito A",
    4: "Recibo A",
    6: "Factura B",
    7: "Nota de Débito B",
    8: "Nota de Crédito B",
    9: "Recibo B",
    11: "Factura C",
    12: "Nota de Débito C",
    13: "Nota de Crédito C",
    15: "Recibo C",
    19: "Factura de Exportación E",
    20: "Nota de Débito por Operaciones con el Exterior",
    21: "Nota de Crédito por Operaciones con el Exterior",
    51: "Factura M",
    52: "Nota de Débito M",
    53: "Nota de Crédito M",
    54: "Recibo M",
    81: "Tique Factura A",
    82: "Tique Factura B",
    83: "Tique",
    109: "Tique C",
    110: "Tique Nota de Crédito",
    111: "Tique Factura C",
    112: "Tique Nota de Crédito A",
    113: "Tique Nota de Crédito B",
    114: "Tique Nota de Crédito C",
    115: "Tique Nota de Débito A",
    116: "Tique Nota de Débito B",
    117: "Tique Nota de Débito C",
    118: "Tique Factura M",
    119: "Tique Nota de Crédito M",
    120: "Tique Nota de Débito M",
    201: "Factura de Crédito Electrónica MiPyME A",
    202: "Nota de Débito Electrónica MiPyME A",
    203: "Nota de Crédito Electrónica MiPyME A",
    206: "Factura de Crédito Electrónica MiPyME B",
    207: "Nota de Débito Electrónica MiPyME B",
    208: "Nota de Crédito Electrónica MiPyME B",
    211: "Factura de Crédito Electrónica MiPyME C",
    212: "Nota de Débito Electrónica MiPyME C",
    213: "Nota de Crédito Electrónica MiPyME C",
}

# Notas de crédito: restan en el libro IVA ventas.
NOTAS_DE_CREDITO = frozenset({3, 8, 13, 21, 53, 110, 112, 113, 114, 119, 203, 208, 213})

# Tipos que representan ventas y que por lo tanto se sincronizan.
TIPOS_VENTA_HABITUALES = tuple(sorted(TIPOS_COMPROBANTE))


def signo_comprobante(tipo_cbte: int) -> int:
    """+1 para facturas/débitos, -1 para notas de crédito."""
    return -1 if tipo_cbte in NOTAS_DE_CREDITO else 1

def nombre_comprobante(tipo_cbte: int) -> str:
    return TIPOS_COMPROBANTE.get(tipo_cbte, f"Comprobante tipo {tipo_cbte}")


def letra_comprobante(tipo_cbte: int) -> str | None:
    """Letra fiscal (A/B/C/M/E) del comprobante, si aplica."""
    nombre = TIPOS_COMPROBANTE.get(tipo_cbte, "")
    if not nombre:
        return None
    ultima = nombre.rsplit(" ", 1)[-1]
    return ultima if ultima in ("A", "B", "C", "M", "E") else None


# --- Alícuotas de IVA (Id de AFIP -> porcentaje) ---

ALICUOTAS_IVA: dict[int, Decimal] = {
    3: Decimal("0"),
    4: Decimal("10.5"),
    5: Decimal("21"),
    6: Decimal("27"),
    8: Decimal("5"),
    9: Decimal("2.5"),
}

# Orden de preferencia al inferir la alícuota desde un total (las más usadas primero).
ALICUOTAS_PROBABLES = (5, 4, 6, 8, 9, 3)


def porcentaje_iva(alicuota_id: int) -> Decimal:
    if alicuota_id not in ALICUOTAS_IVA:
        raise KeyError(f"Alícuota de IVA desconocida: {alicuota_id}")
    return ALICUOTAS_IVA[alicuota_id]


# --- Tipos de documento del receptor ---

TIPOS_DOC = {
    80: "CUIT",
    86: "CUIL",
    87: "CDI",
    89: "LE",
    90: "LC",
    91: "CI Extranjera",
    94: "Pasaporte",
    96: "DNI",
    99: "Consumidor Final",
}


def es_consumidor_final(doc_tipo: int | None, doc_nro: str | None) -> bool:
    return doc_tipo == 99 or not doc_nro or str(doc_nro).strip() in ("", "0")
