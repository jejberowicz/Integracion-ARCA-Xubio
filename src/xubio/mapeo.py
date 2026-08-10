"""Traducción del vocabulario AFIP al de Xubio.

Todo lo que dependa del esquema exacto de la API de Xubio vive acá, para que
ajustarlo sea tocar un solo archivo.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from src.utils.helpers import formatear_cuit
from src.afip.tablas import (
    NOTAS_DE_CREDITO,
    TIPOS_COMPROBANTE,
    es_consumidor_final,
    letra_comprobante,
    porcentaje_iva,
)
from src.database.models import Comprobante

# Tipo de documento en Xubio. Es un entero, y la letra fiscal NO va acá: viaja
# como prefijo de numeroDocumento ("C-00002-00001201"). Verificado contra datos
# reales de la API: 1 = Factura, 3 = Nota de Crédito.
FACTURA, NOTA_DEBITO, NOTA_CREDITO = 1, 2, 3

TIPO_XUBIO = {
    1: FACTURA, 2: NOTA_DEBITO, 3: NOTA_CREDITO,          # A
    6: FACTURA, 7: NOTA_DEBITO, 8: NOTA_CREDITO,          # B
    11: FACTURA, 12: NOTA_DEBITO, 13: NOTA_CREDITO,       # C
    19: FACTURA, 20: NOTA_DEBITO, 21: NOTA_CREDITO,       # E (exportación)
    51: FACTURA, 52: NOTA_DEBITO, 53: NOTA_CREDITO,       # M
    # Tiques de controlador fiscal: contablemente son la factura de su letra.
    81: FACTURA, 82: FACTURA, 83: FACTURA,
    109: FACTURA, 111: FACTURA, 118: FACTURA,
    112: NOTA_CREDITO, 113: NOTA_CREDITO, 114: NOTA_CREDITO, 119: NOTA_CREDITO,
    115: NOTA_DEBITO, 116: NOTA_DEBITO, 117: NOTA_DEBITO, 120: NOTA_DEBITO,
    # Factura de Crédito Electrónica MiPyME
    201: FACTURA, 202: NOTA_DEBITO, 203: NOTA_CREDITO,
    206: FACTURA, 207: NOTA_DEBITO, 208: NOTA_CREDITO,
    211: FACTURA, 212: NOTA_DEBITO, 213: NOTA_CREDITO,
}

# IDs de moneda de Xubio (de /monedaBean). Son negativos y fijos por cuenta.
MONEDA_XUBIO = {
    "PES": -2,   # PESOS_ARGENTINOS
    "DOL": -3,   # DOLARES
    "060": -4,   # EUROS
}
MONEDA_POR_DEFECTO = -2

# 1 = contado. Xubio lo exige; no lo informa ni AFIP ni Mis Comprobantes.
CONDICION_DE_PAGO_CONTADO = 1


class ErrorMapeo(RuntimeError):
    pass


def tipo_xubio(tipo_cbte: int) -> int:
    if tipo_cbte not in TIPO_XUBIO:
        raise ErrorMapeo(
            f"No hay equivalencia en Xubio para el comprobante AFIP tipo "
            f"{tipo_cbte} ({TIPOS_COMPROBANTE.get(tipo_cbte, 'desconocido')}). "
            "Agregala en src/xubio/mapeo.py::TIPO_XUBIO."
        )
    return TIPO_XUBIO[tipo_cbte]


def moneda_xubio(moneda_afip: str) -> int:
    return MONEDA_XUBIO.get(moneda_afip, MONEDA_POR_DEFECTO)


def es_nota_credito(tipo_cbte: int) -> bool:
    return tipo_cbte in NOTAS_DE_CREDITO


def numero_formateado(cbte: Comprobante) -> str:
    """Como lo espera Xubio: 'C-00002-00001201', con la letra fiscal adelante."""
    letra = letra_comprobante(cbte.tipo_cbte) or "C"
    return f"{letra}-{cbte.pto_vta:05d}-{cbte.nro:08d}"


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


def _cotizacion(valor) -> float | int:
    """Xubio valida la cotización de la moneda principal contra el entero 1.

    Mandar 1.0 hace que rechace el comprobante con "La cotización para moneda
    principal no puede ser distinta de 1", así que los valores enteros van sin
    parte decimal.
    """
    numero = Decimal(str(valor if valor is not None else 1))
    return int(numero) if numero == numero.to_integral_value() else float(numero)


def _id_numerico(valor):
    """Los IDs de Xubio son enteros; la configuración los guarda como texto."""
    if isinstance(valor, str) and valor.lstrip("-").isdigit():
        return int(valor)
    return valor


def _linea(producto_id, descripcion: str, neto: Decimal, iva: Decimal) -> dict:
    """Renglón de ``transaccionProductoItems``, con los campos que usa Xubio.

    Ojo con ``iva``: es el **importe en pesos**, no el porcentaje. Verificado
    contra comprobantes reales, donde la tasa siempre da ``iva / importe``
    (360000 de neto llevan 75600 de IVA, que es el 21 %).

    ``montoExento`` va en cero incluso en los renglones exentos: en los datos
    reales nunca aparece con valor.
    """
    base = float(neto)
    return {
        "producto": {"ID": _id_numerico(producto_id)},
        "descripcion": descripcion,
        "cantidad": 1.0,
        "precio": base,
        "importe": base,
        "iva": float(iva),
        "total": base + float(iva),
        "montoExento": 0.0,
        "porcentajeDescuento": 0.0,
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
                Decimal(str(alicuota["importe"])),
            )
        )

    # No gravado y exento van sin IVA: se usa el producto mapeado al 0 %.
    producto_sin_iva = resolver_producto(3, producto_id, productos_por_alicuota)
    cero = Decimal("0")

    if cbte.imp_tot_conc and cbte.imp_tot_conc != 0:
        items.append(
            _linea(producto_sin_iva, f"{descripcion} - No gravado", cbte.imp_tot_conc, cero)
        )

    if cbte.imp_op_ex and cbte.imp_op_ex != 0:
        items.append(
            _linea(producto_sin_iva, f"{descripcion} - Exento", cbte.imp_op_ex, cero)
        )

    if not items:
        # Comprobante sin desglose utilizable: se manda el neto como una línea.
        items.append(
            _linea(producto_sin_iva, descripcion, cbte.imp_neto or cbte.imp_total, cero)
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

    items = _items_desde_alicuotas(cbte, producto_id, productos_por_alicuota)

    # Xubio referencia sus maestros como objetos {"ID": n}, no como enteros
    # sueltos, y los importes van con estos nombres exactos (ojo con
    # 'importetotal', todo en minúscula).
    payload = {
        "tipo": tipo_xubio(cbte.tipo_cbte),
        "numeroDocumento": numero_formateado(cbte),
        "cliente": {"ID": cliente_xubio_id},
        "fecha": cbte.fecha.isoformat(),
        "fechaVto": (cbte.fecha_vto_cae or cbte.fecha).isoformat(),
        "moneda": {"ID": moneda_xubio(cbte.moneda)},
        "cotizacion": _cotizacion(cbte.cotizacion),
        "importeGravado": float(cbte.imp_neto),
        "importeImpuestos": float(cbte.imp_iva + cbte.imp_trib),
        "importetotal": float(cbte.imp_total),
        "condicionDePago": CONDICION_DE_PAGO_CONTADO,
        "transaccionProductoItems": items,
    }

    if punto_venta_id is not None:
        payload["puntoVenta"] = {"ID": punto_venta_id}

    return payload


# Maestros de Xubio verificados leyendo un cliente real de la API.
IDENTIFICACION_CUIT = 9
CATEGORIA_CONSUMIDOR_FINAL = 6
CATEGORIA_EXENTO = 5

# Xubio recorta los nombres de cliente a 30 caracteres.
LARGO_NOMBRE_CLIENTE = 30


def datos_cliente_xubio(cbte: Comprobante) -> dict:
    """Datos para dar de alta al receptor en Xubio si no existe.

    El CUIT viaja formateado con guiones, que es como lo devuelve la API.
    """
    if es_consumidor_final(cbte.doc_tipo, cbte.doc_nro):
        nombre = (cbte.receptor_nombre or "Consumidor Final")[:LARGO_NOMBRE_CLIENTE]
        return {
            "nombre": nombre,
            "razonSocial": nombre,
            "categoriaFiscal": {"ID": CATEGORIA_CONSUMIDOR_FINAL},
        }

    nombre = (cbte.receptor_nombre or f"CUIT {cbte.doc_nro}")[:LARGO_NOMBRE_CLIENTE]
    return {
        "nombre": nombre,
        "razonSocial": nombre,
        "CUIT": formatear_cuit(cbte.doc_nro),
        "identificacionTributaria": {"ID": IDENTIFICACION_CUIT},
    }
