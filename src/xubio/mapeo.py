"""Traducción del vocabulario AFIP al de Xubio.

Todo lo que dependa del esquema exacto de la API de Xubio vive acá, para que
ajustarlo sea tocar un solo archivo.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from src.afip.tablas import (
    NOTAS_DE_CREDITO,
    TIPOS_COMPROBANTE,
    es_consumidor_final,
    letra_comprobante,
    porcentaje_iva,
)
from src.database.models import Comprobante

# Nombre del tipo de comprobante tal como Xubio lo espera.
# Xubio distingue por documento y letra; las notas de crédito son su propio tipo,
# por eso los importes se mandan siempre positivos.
TIPO_XUBIO = {
    1: "FACTURA_A",
    2: "NOTA_DEBITO_A",
    3: "NOTA_CREDITO_A",
    6: "FACTURA_B",
    7: "NOTA_DEBITO_B",
    8: "NOTA_CREDITO_B",
    11: "FACTURA_C",
    12: "NOTA_DEBITO_C",
    13: "NOTA_CREDITO_C",
    19: "FACTURA_E",
    20: "NOTA_DEBITO_E",
    21: "NOTA_CREDITO_E",
    51: "FACTURA_M",
    52: "NOTA_DEBITO_M",
    53: "NOTA_CREDITO_M",
    # Tiques de controlador fiscal: contablemente equivalen a la factura de su letra.
    81: "FACTURA_A",
    82: "FACTURA_B",
    83: "FACTURA_B",
    109: "FACTURA_C",
    111: "FACTURA_C",
    112: "NOTA_CREDITO_A",
    113: "NOTA_CREDITO_B",
    114: "NOTA_CREDITO_C",
    115: "NOTA_DEBITO_A",
    116: "NOTA_DEBITO_B",
    117: "NOTA_DEBITO_C",
    118: "FACTURA_M",
    119: "NOTA_CREDITO_M",
    120: "NOTA_DEBITO_M",
    201: "FACTURA_A",
    202: "NOTA_DEBITO_A",
    203: "NOTA_CREDITO_A",
    206: "FACTURA_B",
    207: "NOTA_DEBITO_B",
    208: "NOTA_CREDITO_B",
    211: "FACTURA_C",
    212: "NOTA_DEBITO_C",
    213: "NOTA_CREDITO_C",
}

MONEDA_XUBIO = {
    "PES": "ARS",
    "DOL": "USD",
    "060": "EUR",
}


class ErrorMapeo(RuntimeError):
    pass


def tipo_xubio(tipo_cbte: int) -> str:
    if tipo_cbte not in TIPO_XUBIO:
        raise ErrorMapeo(
            f"No hay equivalencia en Xubio para el comprobante AFIP tipo "
            f"{tipo_cbte} ({TIPOS_COMPROBANTE.get(tipo_cbte, 'desconocido')}). "
            "Agregala en src/xubio/mapeo.py::TIPO_XUBIO."
        )
    return TIPO_XUBIO[tipo_cbte]


def moneda_xubio(moneda_afip: str) -> str:
    return MONEDA_XUBIO.get(moneda_afip, moneda_afip)


def es_nota_credito(tipo_cbte: int) -> bool:
    return tipo_cbte in NOTAS_DE_CREDITO


def numero_formateado(cbte: Comprobante) -> str:
    return f"{cbte.pto_vta:05d}-{cbte.nro:08d}"


def resolver_producto(
    alicuota_id: int | str,
    producto_default,
    productos_por_alicuota: dict | None,
) -> Any:
    """Elige el producto de Xubio que corresponde a una alícuota de IVA.

    En Xubio la tasa de IVA es un atributo del producto, así que un producto
    "Servicio al 21%" no sirve para una línea al 10,5 % ni para una exenta. Por
    eso se puede mapear un producto por alícuota; el default queda como
    fallback para las que no estén mapeadas.

    Las claves se buscan por Id de AFIP ("5") y también por porcentaje ("21",
    "10.5"), que es como uno piensa el mapeo al configurarlo.
    """
    if not productos_por_alicuota:
        return producto_default

    mapa = {str(k).strip(): v for k, v in productos_por_alicuota.items()}

    if str(alicuota_id) in mapa:
        return mapa[str(alicuota_id)]

    try:
        porcentaje = porcentaje_iva(int(alicuota_id))
    except (KeyError, ValueError):
        return producto_default

    # "21" y "21.0" y "21.00" tienen que encontrar la misma entrada.
    for clave, valor in mapa.items():
        try:
            if Decimal(clave) == porcentaje:
                return valor
        except (ArithmeticError, ValueError):
            continue

    return producto_default


def _linea(producto_id, descripcion: str, importe: Decimal, iva: float) -> dict:
    return {
        "producto": {"ID": producto_id},
        "descripcion": descripcion,
        "cantidad": 1,
        "precio": float(importe),
        "iva": iva,
        "importe": float(importe),
    }


def _items_desde_alicuotas(
    cbte: Comprobante,
    producto_id,
    productos_por_alicuota: dict | None = None,
) -> list[dict]:
    """Una línea por alícuota de IVA, más las de no gravado y exento.

    Se factura contra un producto/servicio genérico: ni AFIP ni Mis Comprobantes
    devuelven el detalle de líneas del comprobante.
    """
    items: list[dict] = []
    descripcion = f"{TIPOS_COMPROBANTE.get(cbte.tipo_cbte, 'Comprobante')} {numero_formateado(cbte)}"

    for alicuota in cbte.alicuotas_iva or []:
        alic_id = int(alicuota["id"])
        items.append(
            _linea(
                resolver_producto(alic_id, producto_id, productos_por_alicuota),
                descripcion,
                Decimal(str(alicuota["base_imp"])),
                float(porcentaje_iva(alic_id)),
            )
        )

    # No gravado y exento van sin IVA: se usa el producto mapeado al 0 %.
    producto_sin_iva = resolver_producto(3, producto_id, productos_por_alicuota)

    if cbte.imp_tot_conc and cbte.imp_tot_conc != 0:
        items.append(
            _linea(producto_sin_iva, f"{descripcion} - No gravado", cbte.imp_tot_conc, 0)
        )

    if cbte.imp_op_ex and cbte.imp_op_ex != 0:
        items.append(
            _linea(producto_sin_iva, f"{descripcion} - Exento", cbte.imp_op_ex, 0)
        )

    if not items:
        # Comprobante sin desglose utilizable: se manda el neto como una línea.
        items.append(
            _linea(producto_sin_iva, descripcion, cbte.imp_neto or cbte.imp_total, 0)
        )

    return items


def armar_payload(
    cbte: Comprobante,
    cliente_xubio_id,
    producto_id,
    punto_venta_id=None,
    productos_por_alicuota: dict | None = None,
) -> dict:
    """Construye el cuerpo del ``comprobanteVentaBean`` para la API de Xubio."""
    if producto_id is None and not productos_por_alicuota:
        raise ErrorMapeo(
            "Falta 'producto_default_id' en config_xubio del cliente: Xubio exige "
            "un producto/servicio existente en cada línea del comprobante."
        )

    payload = {
        "tipo": tipo_xubio(cbte.tipo_cbte),
        "letra": letra_comprobante(cbte.tipo_cbte),
        "cliente": {"ID": cliente_xubio_id},
        "fecha": cbte.fecha.isoformat(),
        "fechaVto": (cbte.fecha_vto_cae or cbte.fecha).isoformat(),
        "puntoVenta": cbte.pto_vta,
        "numeroDocumento": numero_formateado(cbte),
        "moneda": moneda_xubio(cbte.moneda),
        "cotizacion": float(cbte.cotizacion or 1),
        "importeTotal": float(cbte.imp_total),
        "transaccionProductoItems": _items_desde_alicuotas(
            cbte, producto_id, productos_por_alicuota
        ),
        "descripcion": (
            f"Importado de ARCA ({cbte.fuente})"
            + (f" - CAE {cbte.cae}" if cbte.cae else "")
        ),
        # Referencia cruzada para poder rastrear el comprobante desde Xubio.
        "numeroReferencia": f"{cbte.tipo_cbte}-{numero_formateado(cbte)}",
    }

    if punto_venta_id is not None:
        payload["puntoVentaId"] = punto_venta_id

    return payload


def datos_cliente_xubio(cbte: Comprobante) -> dict:
    """Datos mínimos para dar de alta al receptor en Xubio si no existe."""
    if es_consumidor_final(cbte.doc_tipo, cbte.doc_nro):
        return {
            "nombre": cbte.receptor_nombre or "Consumidor Final",
            "CUIT": None,
            "categoriaFiscal": "CONSUMIDOR_FINAL",
        }
    return {
        "nombre": cbte.receptor_nombre or f"CUIT {cbte.doc_nro}",
        "CUIT": cbte.doc_nro,
        "identificacionTributaria": cbte.doc_nro,
    }
