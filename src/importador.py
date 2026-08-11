"""Lectura de planillas de clientes (.xlsx o .csv).

Los encabezados se buscan por nombre normalizado, así que no importa el orden
de las columnas, ni las mayúsculas, ni las tildes.
"""
from __future__ import annotations

import csv
import re
import unicodedata
from pathlib import Path
from typing import Any


class ErrorPlanilla(RuntimeError):
    pass


def slug(texto: str) -> str:
    sin_tildes = "".join(
        c for c in unicodedata.normalize("NFD", str(texto))
        if unicodedata.category(c) != "Mn"
    )
    return re.sub(r"[^a-z0-9]+", "_", sin_tildes.lower()).strip("_")


# Campo interno -> encabezados aceptados en la planilla.
COLUMNAS = {
    "razon_social": ("razon_social", "razonsocial", "nombre", "cliente", "empresa"),
    "cuit": ("cuit", "cuil", "documento"),
    "fuente": ("fuente", "fuente_ventas", "origen", "metodo"),
    "fuente_compras": ("fuente_compras", "compras"),
    "ambiente": ("ambiente",),
    "key_path": ("key_path", "key", "clave_privada"),
    "crt_path": ("crt_path", "crt", "certificado"),
    "clave_fiscal_usuario": ("clave_fiscal_usuario", "usuario_clave_fiscal", "usuario"),
    "clave_fiscal_password": ("clave_fiscal_password", "clave_fiscal", "password"),
    "xubio_client_id": ("xubio_client_id", "client_id", "xubio_client"),
    "xubio_secret": ("xubio_secret", "xubio_secret_id", "secret", "secret_id"),
    "producto_xubio_id": ("producto_xubio_id", "producto_id", "producto"),
}

OBLIGATORIAS = ("razon_social", "cuit")


def _mapear(encabezados: list[Any]) -> dict[str, int]:
    slugs = [slug(h or "") for h in encabezados]
    mapa: dict[str, int] = {}
    for campo, alias in COLUMNAS.items():
        for a in alias:
            if a in slugs:
                mapa[campo] = slugs.index(a)
                break
    faltantes = [c for c in OBLIGATORIAS if c not in mapa]
    if faltantes:
        raise ErrorPlanilla(
            f"A la planilla le faltan columnas obligatorias: {faltantes}. "
            f"Encabezados encontrados: {[h for h in encabezados if h]}"
        )
    return mapa


def _filas_xlsx(ruta: Path) -> list[list[Any]]:
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover
        raise ErrorPlanilla(
            "Falta openpyxl para leer archivos .xlsx: pip install openpyxl"
        ) from exc

    hoja = openpyxl.load_workbook(ruta, data_only=True).active
    return [list(f) for f in hoja.iter_rows(values_only=True)]


def _filas_csv(ruta: Path) -> list[list[Any]]:
    contenido = ruta.read_text(encoding="utf-8-sig", errors="replace")
    lineas = contenido.splitlines()
    encabezado = next((l for l in lineas if l.strip()), "")
    sep = max((";", ",", "\t"), key=encabezado.count)
    return [list(f) for f in csv.reader(lineas, delimiter=sep)]


def leer_planilla(ruta: Path | str) -> list[dict[str, Any]]:
    """Devuelve una lista de dicts, uno por fila, con los campos reconocidos."""
    ruta = Path(ruta)
    if not ruta.exists():
        raise ErrorPlanilla(f"No existe el archivo: {ruta}")

    filas = _filas_xlsx(ruta) if ruta.suffix.lower() in (".xlsx", ".xlsm") else _filas_csv(ruta)
    filas = [f for f in filas if any(c not in (None, "") for c in f)]
    if not filas:
        raise ErrorPlanilla(f"La planilla está vacía: {ruta}")

    mapa = _mapear(filas[0])

    registros = []
    for fila in filas[1:]:
        # Todas las claves están siempre presentes, aunque la columna no exista:
        # así quien consume la planilla no tiene que adivinar cuáles vinieron.
        registro: dict[str, Any] = dict.fromkeys(COLUMNAS)
        for campo, idx in mapa.items():
            valor = fila[idx] if idx < len(fila) else None
            if isinstance(valor, str):
                valor = valor.strip() or None
            registro[campo] = valor
        if registro.get("razon_social") and registro.get("cuit"):
            registros.append(registro)
    return registros
