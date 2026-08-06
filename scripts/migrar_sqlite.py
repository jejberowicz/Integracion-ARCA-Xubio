"""Migra los clientes de la base SQLite original a PostgreSQL.

Uso:
    python scripts/migrar_sqlite.py [ruta_sqlite]

Los secretos de Xubio que estaban en claro se cifran al pasarlos.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.database.connection import crear_tablas, get_session  # noqa: E402
from src.database.models import Ambiente, Cliente, FuenteVentas  # noqa: E402
from src.security import crypto  # noqa: E402

ORIGEN_POR_DEFECTO = Path("data/empresa_contable.db")


def migrar(origen: Path) -> None:
    if not origen.exists():
        print(f"No existe {origen}: nada que migrar.")
        return

    crear_tablas()
    conexion = sqlite3.connect(origen)
    conexion.row_factory = sqlite3.Row
    filas = conexion.execute("SELECT * FROM clientes_afip").fetchall()
    conexion.close()

    migrados, omitidos = 0, 0
    with get_session() as session:
        for fila in filas:
            cuit = str(fila["cuit"]).replace("-", "").strip()
            if session.query(Cliente).filter_by(cuit=cuit).first():
                omitidos += 1
                continue

            tiene_certificados = bool(fila["key_path"] and fila["crt_path"])
            session.add(
                Cliente(
                    cuit=cuit,
                    razon_social=fila["razon_social"],
                    # La base vieja no distinguía fuentes: todo lo que tenga
                    # certificado arranca en wsfe y se ajusta a mano si hace falta.
                    fuente_ventas=(
                        FuenteVentas.WSFE if tiene_certificados else FuenteVentas.MIS_COMPROBANTES
                    ),
                    ambiente=Ambiente.PRODUCCION,
                    key_path=fila["key_path"],
                    crt_path=fila["crt_path"],
                    xubio_client_id=fila["xubio_client_id"],
                    xubio_secret_id=crypto.cifrar(fila["xubio_secret_id"]),
                    last_sync=fila["last_sync"],
                    activo=bool(fila["activo"]),
                    config_xubio={},
                )
            )
            migrados += 1

    print(f"Migrados: {migrados}. Omitidos (ya existían): {omitidos}.")
    print(
        "Revisá la fuente_ventas de cada cliente: los que no tenían certificado "
        "quedaron en 'mis_comprobantes' y necesitan Clave Fiscal cargada."
    )


if __name__ == "__main__":
    migrar(Path(sys.argv[1]) if len(sys.argv) > 1 else ORIGEN_POR_DEFECTO)
