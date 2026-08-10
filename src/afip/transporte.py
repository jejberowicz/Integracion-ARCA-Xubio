"""Transporte HTTPS para los servidores de AFIP.

Los servidores de AFIP negocian Diffie-Hellman con una clave de 1024 bits.
OpenSSL 3, que es el que traen las distribuciones actuales, la rechaza por
debajo de su nivel de seguridad por defecto y la conexión ni siquiera se
establece::

    [SSL: DH_KEY_TOO_SMALL] dh key too small

La única forma de hablar con AFIP es bajar el nivel de seguridad a SECLEVEL=1.
Se hace en una sesión propia que se usa **solo** contra los hosts de AFIP, para
no debilitar el resto de las conexiones del proceso (Xubio, en particular, va
por una sesión aparte con la configuración normal).
"""
from __future__ import annotations

import ssl

import certifi
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

# DEFAULT mantiene la lista de cifrados habitual; lo que cambia es el nivel de
# seguridad, que es lo que descarta la clave DH corta.
CIFRADOS_AFIP = "DEFAULT@SECLEVEL=1"


class AdaptadorAFIP(HTTPAdapter):
    """Adaptador HTTPS que tolera la clave DH corta de AFIP."""

    def _contexto(self) -> ssl.SSLContext:
        contexto = create_urllib3_context()
        contexto.set_ciphers(CIFRADOS_AFIP)
        # Al construir el contexto a mano hay que cargar las CA raíz
        # explícitamente: sin esto la cadena de AFIP no valida y el error que
        # aparece ("self-signed certificate in certificate chain") apunta al
        # lugar equivocado.
        contexto.load_verify_locations(cafile=certifi.where())
        contexto.load_default_certs()
        # La verificación del servidor se mantiene: lo único que se relaja es
        # el tamaño mínimo aceptado de la clave DH.
        contexto.check_hostname = True
        contexto.verify_mode = ssl.CERT_REQUIRED
        return contexto

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self._contexto()
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["ssl_context"] = self._contexto()
        return super().proxy_manager_for(*args, **kwargs)


def sesion_afip() -> requests.Session:
    """Sesión HTTP para usar contra WSAA y WSFEv1."""
    sesion = requests.Session()
    sesion.mount("https://", AdaptadorAFIP())
    return sesion


def transporte_zeep():
    """Transporte de zeep que usa la sesión de AFIP (también para bajar el WSDL)."""
    from zeep.transports import Transport

    return Transport(session=sesion_afip(), timeout=60, operation_timeout=60)
