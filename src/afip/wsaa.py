"""WSAA: obtención y cacheo del Ticket de Acceso (TA) de AFIP/ARCA.

El TA dura 12 horas y AFIP rechaza un pedido nuevo mientras haya uno vigente
("El CEE ya posee un TA valido para el acceso al WSN solicitado"), así que se
cachea en la tabla ``tickets_acceso`` por cliente + servicio + ambiente.
"""
from __future__ import annotations

import base64
import datetime as dt
import logging
import random
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509 import load_pem_x509_certificate
from sqlalchemy import select
from sqlalchemy.orm import Session

from src import config
from src.database.models import Cliente, TicketAcceso

log = logging.getLogger(__name__)

# AFIP trabaja en hora argentina.
TZ_AR = dt.timezone(dt.timedelta(hours=-3))

# Duración que pedimos para el TA (AFIP acota a 12 h).
DURACION_TRA = dt.timedelta(hours=12)


class ErrorWSAA(RuntimeError):
    pass


@dataclass(slots=True)
class Credenciales:
    token: str
    sign: str
    cuit: str
    expira_en: dt.datetime

    @property
    def vigente(self) -> bool:
        margen = dt.timedelta(minutes=config.TA_MARGEN_MINUTOS)
        return dt.datetime.now(dt.timezone.utc) < self.expira_en - margen


def _resolver_ruta(ruta: str) -> Path:
    """Resuelve rutas de certificados relativas a ``BASE_CERTS_PATH``."""
    p = Path(ruta).expanduser()
    if not p.is_absolute():
        p = config.BASE_CERTS_PATH / p
    if not p.exists():
        raise ErrorWSAA(f"No se encontró el archivo de certificado: {p}")
    return p


def generar_tra(servicio: str) -> bytes:
    """Arma el Ticket de Requerimiento de Acceso (XML sin firmar)."""
    ahora = dt.datetime.now(TZ_AR)
    root = ET.Element("loginTicketRequest", version="1.0")
    header = ET.SubElement(root, "header")
    # uniqueId debe variar entre pedidos; con segundos + random alcanza.
    ET.SubElement(header, "uniqueId").text = str(
        int(ahora.timestamp()) % 1_000_000 + random.randint(0, 1000)
    )
    ET.SubElement(header, "generationTime").text = (ahora - dt.timedelta(minutes=10)).isoformat()
    ET.SubElement(header, "expirationTime").text = (ahora + DURACION_TRA).isoformat()
    ET.SubElement(root, "service").text = servicio
    return ET.tostring(root, encoding="UTF-8", xml_declaration=True)


def firmar_tra(tra: bytes, key_path: Path, crt_path: Path) -> str:
    """Firma el TRA en CMS/PKCS#7 y lo devuelve en base64, como espera WSAA."""
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    cert = load_pem_x509_certificate(crt_path.read_bytes())

    cms = (
        pkcs7.PKCS7SignatureBuilder()
        .set_data(tra)
        .add_signer(cert, key, hashes.SHA256())
        # Binary evita que se canonicalicen los saltos de línea del XML.
        .sign(serialization.Encoding.DER, [pkcs7.PKCS7Options.Binary])
    )
    return base64.b64encode(cms).decode()


def _parsear_ta(xml: str) -> tuple[str, str, dt.datetime]:
    root = ET.fromstring(xml)
    token = root.findtext(".//credentials/token")
    sign = root.findtext(".//credentials/sign")
    expiracion = root.findtext(".//header/expirationTime")
    if not (token and sign and expiracion):
        raise ErrorWSAA(f"Respuesta de WSAA incompleta: {xml[:400]}")
    expira_en = dt.datetime.fromisoformat(expiracion)
    if expira_en.tzinfo is None:
        expira_en = expira_en.replace(tzinfo=TZ_AR)
    return token, sign, expira_en.astimezone(dt.timezone.utc)


def _pedir_ta_a_afip(cms_b64: str, ambiente: str) -> str:
    from zeep import Client
    from zeep.exceptions import Fault

    wsdl = config.WSAA_WSDL[ambiente]
    try:
        cliente_soap = Client(wsdl=wsdl)
        return cliente_soap.service.loginCms(in0=cms_b64)
    except Fault as exc:
        raise ErrorWSAA(f"WSAA rechazó el pedido: {exc.message}") from exc


def obtener_credenciales(
    session: Session,
    cliente: Cliente,
    servicio: str = "wsfe",
    forzar: bool = False,
) -> Credenciales:
    """Devuelve un TA vigente: lo lee del cache o lo pide a WSAA."""
    ambiente = config.validar_ambiente(cliente.ambiente)

    if not (cliente.key_path and cliente.crt_path):
        raise ErrorWSAA(
            f"El cliente {cliente.cuit} no tiene certificado configurado "
            "(key_path/crt_path). ¿Debería usar la fuente 'mis_comprobantes'?"
        )

    ta = session.scalar(
        select(TicketAcceso).where(
            TicketAcceso.cliente_id == cliente.id,
            TicketAcceso.servicio == servicio,
            TicketAcceso.ambiente == ambiente,
        )
    )

    if ta and not forzar:
        creds = Credenciales(ta.token, ta.sign, cliente.cuit, _aware(ta.expira_en))
        if creds.vigente:
            log.debug("TA en cache para %s/%s (expira %s)", cliente.cuit, servicio, creds.expira_en)
            return creds

    log.info("Pidiendo TA nuevo a WSAA (%s) para %s / %s", ambiente, cliente.cuit, servicio)
    tra = generar_tra(servicio)
    cms = firmar_tra(tra, _resolver_ruta(cliente.key_path), _resolver_ruta(cliente.crt_path))
    respuesta = _pedir_ta_a_afip(cms, ambiente)
    token, sign, expira_en = _parsear_ta(respuesta)

    if ta is None:
        ta = TicketAcceso(cliente_id=cliente.id, servicio=servicio, ambiente=ambiente)
        session.add(ta)
    ta.token = token
    ta.sign = sign
    ta.generado_en = dt.datetime.now(dt.timezone.utc)
    ta.expira_en = expira_en
    session.flush()

    return Credenciales(token, sign, cliente.cuit, expira_en)


def _aware(valor: dt.datetime) -> dt.datetime:
    """Normaliza a UTC (SQLite devuelve datetimes naive)."""
    return valor if valor.tzinfo else valor.replace(tzinfo=dt.timezone.utc)
