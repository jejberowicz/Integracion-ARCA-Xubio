"""Cifrado simétrico de los secretos que se guardan en la base de datos.

Las credenciales de Xubio y la clave fiscal de ARCA nunca se guardan en claro.
La clave maestra vive en la variable de entorno ``FERNET_KEY``.
"""
from __future__ import annotations

import sys

from cryptography.fernet import Fernet, InvalidToken

from src import config


class ClaveNoConfigurada(RuntimeError):
    pass


def _fernet() -> Fernet:
    if not config.FERNET_KEY:
        raise ClaveNoConfigurada(
            "Falta FERNET_KEY en el .env. "
            "Generala con: python -m src.security.crypto generar-clave"
        )
    try:
        return Fernet(config.FERNET_KEY.encode())
    except (ValueError, TypeError) as exc:
        raise ClaveNoConfigurada(f"FERNET_KEY inválida: {exc}") from exc


def cifrar(valor: str | None) -> str | None:
    """Cifra un secreto. ``None`` y cadena vacía pasan sin tocar."""
    if not valor:
        return valor
    return _fernet().encrypt(valor.encode()).decode()


def descifrar(valor: str | None) -> str | None:
    """Descifra un secreto guardado con :func:`cifrar`."""
    if not valor:
        return valor
    try:
        return _fernet().decrypt(valor.encode()).decode()
    except InvalidToken as exc:
        raise ClaveNoConfigurada(
            "No se pudo descifrar el secreto: la FERNET_KEY no es la que se usó "
            "para guardarlo."
        ) from exc


def generar_clave() -> str:
    return Fernet.generate_key().decode()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "generar-clave":
        print(generar_clave())
    else:
        print("Uso: python -m src.security.crypto generar-clave")
