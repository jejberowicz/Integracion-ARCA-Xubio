"""Prueba la firma del TRA sin tocar la red, con un certificado autofirmado."""
import base64
import datetime as dt
from xml.etree import ElementTree as ET

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from src.afip.wsaa import _parsear_ta, firmar_tra, generar_tra


@pytest.fixture
def certificado(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nombre = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    ahora = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(nombre)
        .issuer_name(nombre)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(ahora - dt.timedelta(days=1))
        .not_valid_after(ahora + dt.timedelta(days=30))
        .sign(key, hashes.SHA256())
    )

    key_path = tmp_path / "test.key"
    crt_path = tmp_path / "test.crt"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    crt_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key_path, crt_path


def test_el_tra_tiene_la_estructura_que_pide_wsaa():
    root = ET.fromstring(generar_tra("wsfe"))
    assert root.tag == "loginTicketRequest"
    assert root.findtext("service") == "wsfe"
    assert root.findtext("header/uniqueId")

    generacion = dt.datetime.fromisoformat(root.findtext("header/generationTime"))
    expiracion = dt.datetime.fromisoformat(root.findtext("header/expirationTime"))
    assert generacion < expiracion
    # AFIP acota la validez a 12 horas.
    assert expiracion - generacion <= dt.timedelta(hours=12, minutes=15)


def test_el_uniqueid_cambia_entre_pedidos():
    ids = {ET.fromstring(generar_tra("wsfe")).findtext("header/uniqueId") for _ in range(20)}
    assert len(ids) > 1


def test_la_firma_cms_produce_base64_valido(certificado):
    key_path, crt_path = certificado
    firmado = firmar_tra(generar_tra("wsfe"), key_path, crt_path)

    crudo = base64.b64decode(firmado, validate=True)
    assert len(crudo) > 500
    # El CMS lleva el contenido adentro (no detached): WSAA lo necesita así.
    assert b"loginTicketRequest" in crudo


def test_parseo_del_ticket_de_acceso():
    xml = """<loginTicketResponse version="1.0">
      <header>
        <expirationTime>2026-03-05T22:00:00-03:00</expirationTime>
      </header>
      <credentials><token>TOKEN123</token><sign>SIGN456</sign></credentials>
    </loginTicketResponse>"""

    token, sign, expira_en = _parsear_ta(xml)
    assert (token, sign) == ("TOKEN123", "SIGN456")
    assert expira_en.tzinfo is dt.timezone.utc
    assert expira_en == dt.datetime(2026, 3, 6, 1, 0, tzinfo=dt.timezone.utc)


def test_ticket_incompleto_falla(certificado):
    from src.afip.wsaa import ErrorWSAA

    with pytest.raises(ErrorWSAA, match="incompleta"):
        _parsear_ta("<loginTicketResponse><header/></loginTicketResponse>")
