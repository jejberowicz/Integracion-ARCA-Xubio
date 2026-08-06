"""Configuración global leída del entorno (.env)."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

load_dotenv(ROOT / ".env")


def _bool(valor: str | None, default: bool = False) -> bool:
    if valor is None:
        return default
    return valor.strip().lower() in ("1", "true", "yes", "si", "sí")


# --- Base de datos ---
DATABASE_URL = os.getenv("DATABASE_URL") or (
    "postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}".format(
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        name=os.getenv("DB_NAME", "gestion_contable"),
    )
)

SQL_ECHO = _bool(os.getenv("SQL_ECHO"))

# --- Cifrado ---
FERNET_KEY = os.getenv("FERNET_KEY", "")

# --- AFIP / ARCA ---
AFIP_AMBIENTE = os.getenv("AFIP_AMBIENTE", "homologacion").strip().lower()
BASE_CERTS_PATH = Path(os.getenv("BASE_CERTS_PATH", ROOT / ".certs")).expanduser()

WSAA_WSDL = {
    "homologacion": "https://wsaahomo.afip.gov.ar/ws/services/LoginCms?wsdl",
    "produccion": "https://wsaa.afip.gov.ar/ws/services/LoginCms?wsdl",
}

WSFEV1_WSDL = {
    "homologacion": "https://wswhomo.afip.gov.ar/wsfev1/service.asmx?WSDL",
    "produccion": "https://servicios1.afip.gov.ar/wsfev1/service.asmx?WSDL",
}

# Margen de seguridad para renovar el Ticket de Acceso antes de que expire.
TA_MARGEN_MINUTOS = int(os.getenv("TA_MARGEN_MINUTOS", "10"))

# --- Xubio ---
XUBIO_API_BASE = os.getenv("XUBIO_API_BASE", "https://xubio.com/API/1.1").rstrip("/")
XUBIO_TOKEN_URL = os.getenv("XUBIO_TOKEN_URL", "https://xubio.com/API/1.1/TokenEndpoint")
XUBIO_TIMEOUT = int(os.getenv("XUBIO_TIMEOUT", "60"))

# --- Descargas ---
DOWNLOADS_PATH = Path(os.getenv("DOWNLOADS_PATH", ROOT / "data" / "descargas")).expanduser()


def validar_ambiente(ambiente: str | None = None) -> str:
    amb = (ambiente or AFIP_AMBIENTE).strip().lower()
    if amb not in WSAA_WSDL:
        raise ValueError(
            f"Ambiente AFIP inválido: {amb!r}. Usar 'homologacion' o 'produccion'."
        )
    return amb
