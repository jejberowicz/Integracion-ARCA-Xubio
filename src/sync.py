"""Orquestador: ARCA -> base de datos -> Xubio.

El flujo es el mismo para todos los clientes; lo único que cambia es la fuente
de ventas, que se resuelve por configuración (``cliente.fuente_ventas``).
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.afip.base import ComprobanteNormalizado, obtener_fuente
from src.database.models import Cliente, Comprobante, EstadoSync, LogSync
from src.xubio.client import ErrorXubio, XubioClient

log = logging.getLogger(__name__)

MAX_INTENTOS_SUBIDA = 5


@dataclass
class Resultado:
    cuit: str = ""
    fuente: str = ""
    descargados: int = 0
    nuevos: int = 0
    subidos: int = 0
    fallidos: int = 0
    #: Ya estaban en Xubio (cargados a mano o por otro sistema).
    ya_estaban: int = 0
    #: No se pudo confirmar si están; se dejan sin tocar.
    sin_certeza: int = 0
    errores: list[str] = field(default_factory=list)
    #: True cuando el fallo lo tiene que resolver una persona (captcha, 2FA,
    #: un trámite pendiente en ARCA). En una corrida por cron es la diferencia
    #: entre "reintentar mañana" y "andá a hacer algo".
    requiere_atencion: bool = False

    @property
    def ok(self) -> bool:
        return not self.errores and self.fallidos == 0

    def resumen(self) -> str:
        partes = [
            f"{self.descargados} descargados",
            f"{self.nuevos} nuevos",
            f"{self.subidos} subidos",
        ]
        if self.ya_estaban:
            partes.append(f"{self.ya_estaban} ya estaban")
        if self.sin_certeza:
            partes.append(f"{self.sin_certeza} sin confirmar")
        partes.append(f"{self.fallidos} fallidos")
        return f"{self.cuit} [{self.fuente}]: " + ", ".join(partes)


def clientes_activos(session: Session, cuit: str | None = None) -> list[Cliente]:
    consulta = select(Cliente).where(Cliente.activo.is_(True)).order_by(Cliente.razon_social)
    if cuit:
        consulta = consulta.where(Cliente.cuit == cuit)
    return list(session.scalars(consulta))


# --- Paso 1: descargar y persistir ---

def _persistir(session: Session, cliente: Cliente, cbte: ComprobanteNormalizado) -> bool:
    """Guarda el comprobante si no estaba. Devuelve True si es nuevo.

    La unicidad natural (cliente, tipo, punto de venta, número) hace que volver a
    correr la sincronización sobre un período ya procesado no duplique nada.
    """
    existente = session.scalar(
        select(Comprobante).where(
            Comprobante.cliente_id == cliente.id,
            Comprobante.tipo_cbte == cbte.tipo_cbte,
            Comprobante.pto_vta == cbte.pto_vta,
            Comprobante.nro == cbte.nro,
        )
    )
    if existente is not None:
        return False

    session.add(
        Comprobante(
            cliente_id=cliente.id,
            tipo_cbte=cbte.tipo_cbte,
            pto_vta=cbte.pto_vta,
            nro=cbte.nro,
            fecha=cbte.fecha,
            doc_tipo=cbte.doc_tipo,
            doc_nro=cbte.doc_nro,
            receptor_nombre=cbte.receptor_nombre,
            imp_total=cbte.imp_total,
            imp_neto=cbte.imp_neto,
            imp_iva=cbte.imp_iva,
            imp_op_ex=cbte.imp_op_ex,
            imp_tot_conc=cbte.imp_tot_conc,
            imp_trib=cbte.imp_trib,
            moneda=cbte.moneda,
            cotizacion=cbte.cotizacion,
            concepto=cbte.concepto,
            cae=cbte.cae,
            fecha_vto_cae=cbte.fecha_vto_cae,
            alicuotas_iva=[a.to_dict() for a in cbte.alicuotas_iva],
            alicuotas_inferidas=cbte.alicuotas_inferidas,
            fuente=cbte.fuente,
            raw=cbte.raw,
            estado_sync=EstadoSync.PENDIENTE,
        )
    )
    return True


def descargar(
    session: Session,
    cliente: Cliente,
    desde: dt.date,
    hasta: dt.date,
    resultado: Resultado,
) -> None:
    fuente = obtener_fuente(cliente.fuente_ventas)
    log.info(
        "Descargando ventas de %s (%s) desde %s hasta %s",
        cliente.cuit, cliente.fuente_ventas, desde, hasta,
    )

    for cbte in fuente.obtener_ventas(cliente, desde, hasta):
        resultado.descargados += 1
        if _persistir(session, cliente, cbte):
            resultado.nuevos += 1

    session.flush()


# --- Paso 2: subir a Xubio ---

def pendientes(session: Session, cliente: Cliente) -> list[Comprobante]:
    return list(
        session.scalars(
            select(Comprobante)
            .where(
                Comprobante.cliente_id == cliente.id,
                Comprobante.estado_sync.in_((EstadoSync.PENDIENTE, EstadoSync.ERROR)),
                Comprobante.intentos_sync < MAX_INTENTOS_SUBIDA,
            )
            .order_by(Comprobante.fecha, Comprobante.pto_vta, Comprobante.nro)
        )
    )


def subir(session: Session, cliente: Cliente, resultado: Resultado) -> None:
    a_subir = pendientes(session, cliente)
    if not a_subir:
        log.info("%s: no hay comprobantes pendientes de subir", cliente.cuit)
        return

    xubio = XubioClient(cliente)
    log.info("%s: subiendo %s comprobantes a Xubio", cliente.cuit, len(a_subir))

    for cbte in a_subir:
        cbte.intentos_sync += 1
        try:
            # Xubio puede tener el comprobante cargado a mano o desde otro
            # sistema. Nuestra clave única no lo sabe, así que hay que
            # preguntarle a Xubio antes de crear nada.
            estado, xubio_id = xubio.existente_en_xubio(cbte)

            if estado == "existe":
                cbte.xubio_id = str(xubio_id)
                cbte.estado_sync = EstadoSync.SUBIDO
                cbte.error_sync = None
                cbte.fecha_sync = dt.datetime.now(dt.timezone.utc)
                resultado.ya_estaban += 1
                log.info("%s ya estaba en Xubio, no se duplica", cbte)
                continue

            if estado == "sin_certeza":
                # Más viejo que lo que la API devuelve: ante la duda, no se
                # sube. Duplicar en la contabilidad de un cliente es peor que
                # dejarlo pendiente.
                cbte.estado_sync = EstadoSync.ERROR
                cbte.error_sync = (
                    "No se pudo confirmar si ya está en Xubio (es anterior a los "
                    "comprobantes que devuelve la API). Revisalo a mano y, si "
                    "corresponde, marcalo como ignorado."
                )
                resultado.sin_certeza += 1
                continue

            cbte.xubio_id = xubio.subir_comprobante(cbte)
            cbte.estado_sync = EstadoSync.SUBIDO
            cbte.error_sync = None
            cbte.fecha_sync = dt.datetime.now(dt.timezone.utc)
            resultado.subidos += 1
        except ErrorXubio as exc:
            cbte.estado_sync = EstadoSync.ERROR
            cbte.error_sync = str(exc)[:2000]
            resultado.fallidos += 1
            log.error("Falló la subida de %s: %s", cbte, exc)
        finally:
            # Se persiste comprobante por comprobante: si la corrida se corta,
            # no se repite lo que ya entró en Xubio.
            session.flush()


# --- Corrida completa ---

def sincronizar_cliente(
    session: Session,
    cliente: Cliente,
    desde: dt.date,
    hasta: dt.date,
    solo_descargar: bool = False,
    solo_subir: bool = False,
) -> Resultado:
    resultado = Resultado(cuit=cliente.cuit, fuente=cliente.fuente_ventas)
    bitacora = LogSync(
        cliente_id=cliente.id,
        fuente=cliente.fuente_ventas,
        desde=desde,
        hasta=hasta,
    )
    session.add(bitacora)
    session.flush()

    try:
        if not solo_subir:
            descargar(session, cliente, desde, hasta, resultado)
        if not solo_descargar:
            subir(session, cliente, resultado)

        if not solo_descargar and resultado.ok:
            cliente.last_sync = dt.datetime.now(dt.timezone.utc)
    except Exception as exc:
        resultado.errores.append(f"{type(exc).__name__}: {exc}")
        resultado.requiere_atencion = _necesita_una_persona(exc)
        log.exception("Error sincronizando %s", cliente.cuit)
    finally:
        bitacora.fin = dt.datetime.now(dt.timezone.utc)
        bitacora.descargados = resultado.descargados
        bitacora.nuevos = resultado.nuevos
        bitacora.subidos = resultado.subidos
        bitacora.fallidos = resultado.fallidos
        bitacora.ok = resultado.ok
        bitacora.detalle = "\n".join(resultado.errores) or None
        session.flush()

    return resultado


def _necesita_una_persona(exc: Exception) -> bool:
    """Distingue lo que se arregla solo de lo que hay que ir a resolver."""
    from src.afip.mis_comprobantes import LoginAsistidoRequerido

    if isinstance(exc, LoginAsistidoRequerido):
        return True
    # Trámites pendientes en ARCA: reintentar no los va a arreglar.
    texto = str(exc).lower()
    return any(
        marca in texto
        for marca in (
            "computador no autorizado",
            "no se encuentra habilitado a usar en el presente ws",
            "certificado expirado",
            "faltan las credenciales",
        )
    )


def rango_por_defecto(cliente: Cliente, dias_atras: int = 31) -> tuple[dt.date, dt.date]:
    """Desde el último sync (con solape de un día) hasta hoy."""
    hoy = dt.date.today()
    if cliente.last_sync:
        desde = cliente.last_sync.date() - dt.timedelta(days=1)
    else:
        desde = hoy - dt.timedelta(days=dias_atras)
    return desde, hoy
