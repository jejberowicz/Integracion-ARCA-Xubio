"""Utilidades varias."""
from __future__ import annotations

import re


def normalizar_cuit(valor: str | int | None) -> str | None:
    """Deja el CUIT en 11 dígitos, sin guiones ni espacios."""
    if valor is None:
        return None
    digitos = re.sub(r"\D", "", str(valor))
    return digitos or None


def cuit_valido(valor: str | int | None) -> bool:
    """Verifica el dígito verificador del CUIT (módulo 11)."""
    cuit = normalizar_cuit(valor)
    if not cuit or len(cuit) != 11:
        return False

    pesos = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)
    suma = sum(int(d) * p for d, p in zip(cuit[:10], pesos))
    resto = suma % 11
    verificador = {0: 0, 1: 9}.get(resto, 11 - resto)
    return verificador == int(cuit[10])


def formatear_cuit(valor: str | int | None) -> str | None:
    """20-12345678-9"""
    cuit = normalizar_cuit(valor)
    if not cuit or len(cuit) != 11:
        return cuit
    return f"{cuit[:2]}-{cuit[2:10]}-{cuit[10]}"


# Prefijos del CUIT: 20/23/24/27 identifican personas físicas, 30/33/34
# personas jurídicas. Importa porque la Clave Fiscal de ARCA es siempre de una
# persona física: el CUIT de una empresa no sirve como usuario del portal.
PREFIJOS_PERSONA_FISICA = ("20", "23", "24", "27")


def es_persona_fisica(valor: str | int | None) -> bool:
    cuit = normalizar_cuit(valor)
    return bool(cuit) and cuit[:2] in PREFIJOS_PERSONA_FISICA
