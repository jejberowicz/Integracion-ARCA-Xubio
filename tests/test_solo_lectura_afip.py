"""El programa lee de AFIP; no emite comprobantes.

Asociar el servicio de Facturación Electrónica habilita a hacer las dos cosas,
porque AFIP no ofrece una variante de solo lectura de WSFEv1. La garantía de
que nunca vamos a emitir tiene que estar de este lado, y este test la fija:
si alguien agrega una llamada de emisión, falla.
"""
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"

# Métodos de WSFEv1 que crean o modifican algo en AFIP.
METODOS_DE_ESCRITURA = (
    "FECAESolicitar",            # solicita CAE: emite el comprobante
    "FECAEARegInformativo",      # informa comprobantes ya emitidos
    "FEDummy",                   # inofensivo, pero no lo usamos
    "FECAEASolicitar",
    "FECAEASinMovimientoInformar",
    "FERegXReqSolicitar",
)

# Los únicos métodos de consulta que el programa tiene permitido usar.
METODOS_PERMITIDOS = {
    "FECompConsultar",
    "FECompUltimoAutorizado",
    "FEParamGetPtosVenta",
    "FEParamGetTiposCbte",
    "FEParamGetTiposIva",
    "loginCms",
}


# Solo invocaciones reales: '.service.Metodo('. Sin el paréntesis matchearía
# 'service.asmx' de la URL del WSDL.
PATRON_LLAMADA = re.compile(r"\.service\.([A-Za-z][A-Za-z0-9_]*)\s*\(")


def archivos_python() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


@pytest.mark.parametrize("metodo", METODOS_DE_ESCRITURA)
def test_no_se_invoca_ningun_metodo_de_emision(metodo):
    culpables = [f for f in archivos_python() if metodo in f.read_text(encoding="utf-8")]
    assert not culpables, (
        f"{metodo} aparece en {[str(f.relative_to(SRC)) for f in culpables]}. "
        "El programa no debe emitir comprobantes en AFIP."
    )


def test_todas_las_llamadas_soap_son_de_consulta():
    invocados = set()
    for archivo in archivos_python():
        invocados |= set(
            PATRON_LLAMADA.findall(archivo.read_text(encoding="utf-8"))
        )

    no_declarados = invocados - METODOS_PERMITIDOS
    assert not no_declarados, (
        f"Métodos SOAP nuevos sin revisar: {sorted(no_declarados)}. "
        "Si son de consulta, agregalos a METODOS_PERMITIDOS."
    )


def test_la_lista_de_permitidos_no_esta_vacia_por_error():
    # Si el regex dejara de encontrar llamadas, el test de arriba pasaría
    # trivialmente y la garantía sería falsa.
    invocados = set()
    for archivo in archivos_python():
        invocados |= set(
            PATRON_LLAMADA.findall(archivo.read_text(encoding="utf-8"))
        )
    assert "FECompConsultar" in invocados
