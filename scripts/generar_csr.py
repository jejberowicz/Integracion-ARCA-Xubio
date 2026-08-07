"""Genera la clave privada y el CSR de cada cliente de una planilla.

Los archivos quedan como ``<CUIT>.key`` y ``<CUIT>.csr`` dentro de
BASE_CERTS_PATH, así el alta puede deducir las rutas sola.

Uso:
    python scripts/generar_csr.py .certs/razones_sociales.xlsx --dry-run
    python scripts/generar_csr.py .certs/razones_sociales.xlsx
    python scripts/generar_csr.py .certs/razones_sociales.xlsx --cuit 30712345671
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config  # noqa: E402
from src.importador import leer_planilla  # noqa: E402
from src.utils.helpers import cuit_valido, normalizar_cuit  # noqa: E402

CN = "carga-datos"


def subject(razon_social: str, cuit: str) -> str:
    # AFIP valida O contra la razón social y serialNumber contra el CUIT.
    # Las barras romperían el -subj de openssl.
    limpia = razon_social.replace("/", "-").strip()
    return f"/C=AR/O={limpia}/CN={CN}/serialNumber=CUIT {cuit}"


def generar(razon_social: str, cuit: str, destino: Path, dry_run: bool) -> str:
    key = destino / f"{cuit}.key"
    csr = destino / f"{cuit}.csr"

    if key.exists() or csr.exists():
        # Nunca se pisa una clave existente: si ya hay un certificado emitido
        # contra ella, regenerarla lo invalida.
        return f"OMITIDO (ya existe {key.name})"

    if dry_run:
        return f"generaría {key.name} + {csr.name}  [{subject(razon_social, cuit)}]"

    subprocess.run(
        ["openssl", "genrsa", "-out", str(key), "2048"],
        check=True, capture_output=True,
    )
    key.chmod(0o600)
    subprocess.run(
        ["openssl", "req", "-new", "-key", str(key),
         "-subj", subject(razon_social, cuit), "-out", str(csr)],
        check=True, capture_output=True,
    )
    return f"OK  {key.name} + {csr.name}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("planilla")
    parser.add_argument("--cuit", help="Generar solo el de este CUIT.")
    parser.add_argument("--dry-run", action="store_true", help="Mostrar sin generar.")
    args = parser.parse_args()

    destino = config.BASE_CERTS_PATH
    destino.mkdir(parents=True, exist_ok=True)

    filtro = normalizar_cuit(args.cuit) if args.cuit else None
    errores = 0

    for registro in leer_planilla(args.planilla):
        cuit = normalizar_cuit(registro["cuit"])
        razon = str(registro["razon_social"])

        if filtro and cuit != filtro:
            continue
        if not cuit_valido(cuit):
            print(f"!! {cuit}  {razon[:40]:<40} CUIT inválido, se saltea")
            errores += 1
            continue

        print(f"   {cuit}  {razon[:40]:<40} {generar(razon, cuit, destino, args.dry_run)}")

    if args.dry_run:
        print("\n(dry-run: no se generó nada)")
    else:
        print(f"\nListo. Los .csr están en {destino}/ y se suben a ARCA uno por uno.")
        print("Las .key NO se suben a ningún lado.")
    return 1 if errores else 0


if __name__ == "__main__":
    raise SystemExit(main())
