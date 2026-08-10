"""El diagnóstico del scraping está pensado para compartirse.

Se corre sobre un portal fiscal con la sesión iniciada, así que no puede
arrastrar credenciales ni datos del contribuyente.
"""
from src.afip.mis_comprobantes import _inventario, _url_sin_sesion


class ElementoFalso:
    def __init__(self, atributos: dict, texto: str = ""):
        self.atributos = atributos
        self.texto = texto

    def get_attribute(self, nombre):
        return self.atributos.get(nombre)

    def inner_text(self):
        return self.texto


class PaginaFalsa:
    def __init__(self, por_selector: dict):
        self.por_selector = por_selector

    def query_selector_all(self, selector):
        return self.por_selector.get(selector, [])


def test_no_expone_el_valor_de_los_campos():
    pagina = PaginaFalsa({
        "input": [ElementoFalso({"id": "F1:username", "type": "number", "value": "20111111119"})]
    })
    salida = _inventario(pagina)

    assert "F1:username" in salida        # el selector sirve
    assert "20111111119" not in salida    # el CUIT no
    assert "<con contenido>" in salida


def test_marca_las_contrasenas_sin_leerlas():
    pagina = PaginaFalsa({
        "input": [ElementoFalso({"id": "F1:password", "type": "password", "value": "secreta"})]
    })
    salida = _inventario(pagina)

    assert "secreta" not in salida
    assert "<contraseña>" in salida


def test_conserva_lo_que_sirve_para_los_selectores():
    pagina = PaginaFalsa({
        "button": [ElementoFalso({"id": "btnExportar", "class": "btn primary"}, "Exportar CSV")]
    })
    salida = _inventario(pagina)

    assert "btnExportar" in salida
    assert "Exportar CSV" in salida


def test_la_url_no_lleva_el_id_de_sesion():
    sucia = "https://auth.afip.gob.ar/portal.xhtml;jsessionid=ABC123DEF456.auth3"
    assert "ABC123DEF456" not in _url_sin_sesion(sucia)
    assert "auth.afip.gob.ar/portal.xhtml" in _url_sin_sesion(sucia)


def test_la_url_no_lleva_tokens():
    sucia = "https://x.afip.gob.ar/a?token=PEGAJOSO&otro=1"
    limpia = _url_sin_sesion(sucia)
    assert "PEGAJOSO" not in limpia
    assert "otro=1" in limpia
