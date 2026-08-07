import pytest

from src.importador import ErrorPlanilla, leer_planilla


def escribir(tmp_path, contenido, nombre="clientes.csv"):
    ruta = tmp_path / nombre
    ruta.write_text(contenido, encoding="utf-8")
    return ruta


def test_lee_las_columnas_minimas(tmp_path):
    ruta = escribir(tmp_path, "RAZON SOCIAL;CUIT\nACME SA;30712345671\n")
    registros = leer_planilla(ruta)
    assert len(registros) == 1
    assert registros[0]["razon_social"] == "ACME SA"
    assert registros[0]["cuit"] == "30712345671"


def test_no_importa_el_orden_ni_las_mayusculas_ni_las_tildes(tmp_path):
    ruta = escribir(tmp_path, "Cuit,Razón Social,Fuente\n30712345671,ACME SA,wsfe\n")
    registro = leer_planilla(ruta)[0]
    assert registro["razon_social"] == "ACME SA"
    assert registro["fuente"] == "wsfe"


def test_detecta_separador_punto_y_coma(tmp_path):
    ruta = escribir(tmp_path, "RAZON SOCIAL;CUIT\nUNO, DOS Y TRES SA;30712345671\n")
    assert leer_planilla(ruta)[0]["razon_social"] == "UNO, DOS Y TRES SA"


def test_columnas_opcionales_quedan_en_none(tmp_path):
    ruta = escribir(tmp_path, "RAZON SOCIAL;CUIT\nACME SA;30712345671\n")
    registro = leer_planilla(ruta)[0]
    assert registro["fuente"] is None
    assert registro["xubio_client_id"] is None


def test_saltea_filas_vacias(tmp_path):
    ruta = escribir(
        tmp_path,
        "RAZON SOCIAL;CUIT\nACME SA;30712345671\n;\n\nOTRA SA;27123456780\n",
    )
    assert len(leer_planilla(ruta)) == 2


def test_falta_columna_obligatoria(tmp_path):
    ruta = escribir(tmp_path, "NOMBRE;TELEFONO\nACME;123\n")
    with pytest.raises(ErrorPlanilla, match="columnas obligatorias"):
        leer_planilla(ruta)


def test_archivo_inexistente(tmp_path):
    with pytest.raises(ErrorPlanilla, match="No existe"):
        leer_planilla(tmp_path / "no_esta.csv")


def test_lee_xlsx(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    ruta = tmp_path / "clientes.xlsx"
    wb = openpyxl.Workbook()
    hoja = wb.active
    hoja.append(["RAZON SOCIAL", "CUIT", "FUENTE"])
    hoja.append(["ACME SA", 30712345671, "wsfe"])
    hoja.append([None, None, None])
    wb.save(ruta)

    registros = leer_planilla(ruta)
    assert len(registros) == 1
    # openpyxl devuelve el CUIT como entero: se conserva el valor crudo.
    assert str(registros[0]["cuit"]) == "30712345671"
