"""Fuente 'wsfe': Comprobantes en Línea / WSFEv1 (con certificado digital).

Es la fuente preferida para clientes que emiten toda su facturación electrónica
por AFIP: trae el desglose completo (neto por alícuota, IVA discriminado,
tributos, receptor, CAE).

WSFEv1 no expone "dame los comprobantes del mes". La única forma de recorrerlos
es, por cada punto de venta y tipo de comprobante, pedir el último autorizado
(``FECompUltimoAutorizado``) y bajar de a uno hacia atrás con
``FECompConsultar`` hasta salirse del rango de fechas pedido.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import time
from decimal import Decimal
from typing import Iterator

from src import config
from src.afip.base import (
    AlicuotaIVA,
    ComprobanteNormalizado,
    FuenteVentas,
    registrar,
)
from src.afip.wsaa import Credenciales, obtener_credenciales
from src.database.models import Cliente

log = logging.getLogger(__name__)

# Tipos que se consultan por defecto. Ampliar si el cliente emite otros
# (exportación, MiPyME, tiques fiscales...).
TIPOS_POR_DEFECTO = (1, 2, 3, 6, 7, 8, 11, 12, 13, 51, 52, 53)

# Pausa entre llamadas para no chocar contra el rate limit de AFIP.
PAUSA_ENTRE_LLAMADAS = float(os.getenv("WSFE_PAUSA", "0.15"))


class ErrorWSFE(RuntimeError):
    pass


def _fecha_afip(valor: str | int | None) -> dt.date | None:
    """Convierte 'YYYYMMDD' al date de Python."""
    if not valor:
        return None
    texto = str(valor).strip()
    if len(texto) != 8:
        return None
    return dt.date(int(texto[:4]), int(texto[4:6]), int(texto[6:]))


def _dec(valor) -> Decimal:
    if valor is None:
        return Decimal("0")
    return Decimal(str(valor))


@registrar
class ComprobantesEnLinea(FuenteVentas):
    nombre = "wsfe"

    def __init__(self, tipos: tuple[int, ...] = TIPOS_POR_DEFECTO):
        self.tipos = tipos
        self._soap = None

    # --- infraestructura SOAP ---

    def _cliente_soap(self, ambiente: str):
        if self._soap is None:
            from zeep import Client

            self._soap = Client(wsdl=config.WSFEV1_WSDL[ambiente])
        return self._soap

    @staticmethod
    def _auth(creds: Credenciales) -> dict:
        return {"Token": creds.token, "Sign": creds.sign, "Cuit": int(creds.cuit)}

    @staticmethod
    def _chequear_errores(respuesta, contexto: str) -> None:
        errores = getattr(respuesta, "Errors", None)
        if errores and getattr(errores, "Err", None):
            detalle = "; ".join(f"[{e.Code}] {e.Msg}" for e in errores.Err)
            raise ErrorWSFE(f"{contexto}: {detalle}")

    # --- API pública ---

    def validar_cliente(self, cliente: Cliente) -> None:
        if not (cliente.key_path and cliente.crt_path):
            raise ErrorWSFE(
                f"{cliente.cuit}: la fuente 'wsfe' requiere key_path y crt_path."
            )

    def obtener_ventas(
        self,
        cliente: Cliente,
        desde: dt.date,
        hasta: dt.date,
    ) -> Iterator[ComprobanteNormalizado]:
        from src.database.connection import get_session

        self.validar_cliente(cliente)
        ambiente = config.validar_ambiente(cliente.ambiente)

        # El TA se pide en su propia sesión corta: queda cacheado en la DB.
        with get_session() as session:
            cliente_db = session.merge(cliente)
            creds = obtener_credenciales(session, cliente_db, servicio="wsfe")

        soap = self._cliente_soap(ambiente)
        auth = self._auth(creds)

        for pto_vta in self.puntos_de_venta(soap, auth):
            for tipo in self.tipos:
                yield from self._recorrer(soap, auth, pto_vta, tipo, desde, hasta)

    # --- internos ---

    def puntos_de_venta(self, soap, auth: dict) -> list[int]:
        """Puntos de venta habilitados y no bloqueados del contribuyente."""
        respuesta = soap.service.FEParamGetPtosVenta(Auth=auth)
        self._chequear_errores(respuesta, "FEParamGetPtosVenta")
        resultado = getattr(respuesta, "ResultGet", None)
        if not resultado or not getattr(resultado, "PtoVenta", None):
            log.warning("El CUIT %s no tiene puntos de venta habilitados", auth["Cuit"])
            return []
        return [
            int(p.Nro)
            for p in resultado.PtoVenta
            if str(getattr(p, "Bloqueado", "N")).upper() != "S"
        ]

    def _ultimo_autorizado(self, soap, auth: dict, pto_vta: int, tipo: int) -> int:
        respuesta = soap.service.FECompUltimoAutorizado(
            Auth=auth, PtoVta=pto_vta, CbteTipo=tipo
        )
        errores = getattr(respuesta, "Errors", None)
        if errores and getattr(errores, "Err", None):
            # 602 = "no existen comprobantes para el punto de venta y tipo".
            codigos = {int(e.Code) for e in errores.Err}
            if codigos <= {602}:
                return 0
            self._chequear_errores(respuesta, f"FECompUltimoAutorizado {pto_vta}/{tipo}")
        return int(respuesta.CbteNro or 0)

    def _recorrer(
        self,
        soap,
        auth: dict,
        pto_vta: int,
        tipo: int,
        desde: dt.date,
        hasta: dt.date,
    ) -> Iterator[ComprobanteNormalizado]:
        ultimo = self._ultimo_autorizado(soap, auth, pto_vta, tipo)
        if ultimo == 0:
            return

        log.debug("PtoVta %s tipo %s: último autorizado %s", pto_vta, tipo, ultimo)

        for nro in range(ultimo, 0, -1):
            time.sleep(PAUSA_ENTRE_LLAMADAS)
            detalle = self._consultar(soap, auth, pto_vta, tipo, nro)
            if detalle is None:
                continue

            fecha = _fecha_afip(detalle.CbteFch)
            if fecha is None:
                continue
            if fecha < desde:
                # Los comprobantes se numeran en orden cronológico: ya nos pasamos.
                break
            if fecha > hasta:
                continue

            yield self._normalizar(detalle, pto_vta, tipo, nro, fecha)

    def _consultar(self, soap, auth: dict, pto_vta: int, tipo: int, nro: int):
        respuesta = soap.service.FECompConsultar(
            Auth=auth,
            FeCompConsReq={"CbteTipo": tipo, "CbteNro": nro, "PtoVta": pto_vta},
        )
        errores = getattr(respuesta, "Errors", None)
        if errores and getattr(errores, "Err", None):
            codigos = {int(e.Code) for e in errores.Err}
            if 602 in codigos:  # comprobante inexistente (hueco en la numeración)
                return None
            self._chequear_errores(respuesta, f"FECompConsultar {pto_vta}-{nro}")
        return getattr(respuesta, "ResultGet", None)

    def _normalizar(
        self, d, pto_vta: int, tipo: int, nro: int, fecha: dt.date
    ) -> ComprobanteNormalizado:
        alicuotas: list[AlicuotaIVA] = []
        iva = getattr(d, "Iva", None)
        if iva and getattr(iva, "AlicIva", None):
            alicuotas = [
                AlicuotaIVA(
                    id=int(a.Id),
                    base_imp=_dec(a.BaseImp),
                    importe=_dec(a.Importe),
                )
                for a in iva.AlicIva
            ]

        doc_nro = getattr(d, "DocNro", None)

        return ComprobanteNormalizado(
            tipo_cbte=tipo,
            pto_vta=pto_vta,
            nro=nro,
            fecha=fecha,
            imp_total=_dec(getattr(d, "ImpTotal", 0)),
            imp_neto=_dec(getattr(d, "ImpNeto", 0)),
            imp_iva=_dec(getattr(d, "ImpIVA", 0)),
            imp_op_ex=_dec(getattr(d, "ImpOpEx", 0)),
            imp_tot_conc=_dec(getattr(d, "ImpTotConc", 0)),
            imp_trib=_dec(getattr(d, "ImpTrib", 0)),
            doc_tipo=int(getattr(d, "DocTipo", 0) or 0) or None,
            doc_nro=str(doc_nro) if doc_nro else None,
            receptor_nombre=None,  # WSFEv1 no devuelve la razón social del receptor
            moneda=getattr(d, "MonId", "PES") or "PES",
            cotizacion=_dec(getattr(d, "MonCotiz", 1)) or Decimal("1"),
            concepto=int(getattr(d, "Concepto", 0) or 0) or None,
            cae=getattr(d, "CodAutorizacion", None),
            fecha_vto_cae=_fecha_afip(getattr(d, "FchVto", None)),
            alicuotas_iva=alicuotas,
            alicuotas_inferidas=False,
            fuente=self.nombre,
            raw=_a_dict(d),
        )


def _a_dict(obj) -> dict:
    """Serializa el objeto zeep a un dict JSON-able para auditoría."""
    try:
        from zeep.helpers import serialize_object

        return _limpiar(serialize_object(obj))
    except Exception:  # pragma: no cover - solo afecta la auditoría
        return {}


def _limpiar(valor):
    if isinstance(valor, dict):
        return {k: _limpiar(v) for k, v in valor.items()}
    if isinstance(valor, (list, tuple)):
        return [_limpiar(v) for v in valor]
    if isinstance(valor, (dt.date, dt.datetime)):
        return valor.isoformat()
    if isinstance(valor, Decimal):
        return str(valor)
    return valor
