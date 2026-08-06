"""Contrato común a todas las fuentes de ventas.

Cada cliente declara en ``clientes_afip.fuente_ventas`` de dónde salen sus datos.
El orquestador no sabe cuál es: pide comprobantes y recibe siempre el mismo
:class:`ComprobanteNormalizado`.
"""
from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from src.database.models import Cliente


@dataclass(slots=True)
class AlicuotaIVA:
    id: int
    base_imp: Decimal
    importe: Decimal

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "base_imp": str(self.base_imp),
            "importe": str(self.importe),
        }


@dataclass(slots=True)
class ComprobanteNormalizado:
    """Comprobante de venta con la misma forma venga de donde venga.

    Los importes son siempre positivos; el signo lo determina el tipo de
    comprobante (ver :func:`src.afip.tablas.signo_comprobante`).
    """

    tipo_cbte: int
    pto_vta: int
    nro: int
    fecha: dt.date

    imp_total: Decimal = Decimal("0")
    imp_neto: Decimal = Decimal("0")
    imp_iva: Decimal = Decimal("0")
    imp_op_ex: Decimal = Decimal("0")
    imp_tot_conc: Decimal = Decimal("0")
    imp_trib: Decimal = Decimal("0")

    doc_tipo: int | None = None
    doc_nro: str | None = None
    receptor_nombre: str | None = None

    moneda: str = "PES"
    cotizacion: Decimal = Decimal("1")
    concepto: int | None = None

    cae: str | None = None
    fecha_vto_cae: dt.date | None = None

    alicuotas_iva: list[AlicuotaIVA] = field(default_factory=list)
    alicuotas_inferidas: bool = False

    fuente: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def clave_natural(self) -> tuple[int, int, int]:
        return (self.tipo_cbte, self.pto_vta, self.nro)

    def __str__(self) -> str:
        return f"{self.tipo_cbte}-{self.pto_vta:05d}-{self.nro:08d} ({self.fecha})"


class FuenteVentas(ABC):
    """Interfaz que implementa cada fuente de datos de ventas."""

    #: valor guardado en ``clientes_afip.fuente_ventas``
    nombre: str = ""

    @abstractmethod
    def obtener_ventas(
        self,
        cliente: "Cliente",
        desde: dt.date,
        hasta: dt.date,
    ) -> Iterable[ComprobanteNormalizado]:
        """Devuelve los comprobantes de venta emitidos en el rango [desde, hasta]."""

    def validar_cliente(self, cliente: "Cliente") -> None:
        """Falla temprano si al cliente le faltan credenciales para esta fuente."""


_REGISTRO: dict[str, type[FuenteVentas]] = {}


def registrar(clase: type[FuenteVentas]) -> type[FuenteVentas]:
    _REGISTRO[clase.nombre] = clase
    return clase


def obtener_fuente(nombre: str) -> FuenteVentas:
    """Instancia la fuente configurada para un cliente."""
    # Importes locales para que el registro se pueble sin ciclos de import.
    from src.afip import mis_comprobantes, wsfe  # noqa: F401

    if nombre not in _REGISTRO:
        raise ValueError(
            f"Fuente de ventas desconocida: {nombre!r}. "
            f"Disponibles: {sorted(_REGISTRO)}"
        )
    return _REGISTRO[nombre]()
