"""El código de salida es lo único que Airflow mira para decidir si reintenta.

1 se reintenta (la red, un 500 de Xubio); 2 no, porque hay que ir a resolver
algo a mano. Confundirlos significa o reintentar en vano un captcha, o dejar de
reintentar algo que se arreglaba solo.
"""
from main import SALIDA_FALLA, SALIDA_OK, SALIDA_REQUIERE_ATENCION, _codigo_de_salida
from src.sync import Resultado


def ok():
    return Resultado(cuit="30712345671", fuente="mis_comprobantes")


def falla():
    r = ok()
    r.errores.append("ConnectionError: se cayó la red")
    return r


def requiere_atencion():
    r = ok()
    r.errores.append("Computador no autorizado a acceder al servicio")
    r.requiere_atencion = True
    return r


def test_todo_bien():
    assert _codigo_de_salida([ok(), ok()]) == SALIDA_OK


def test_sin_clientes_es_exito():
    assert _codigo_de_salida([]) == SALIDA_OK


def test_falla_transitoria():
    assert _codigo_de_salida([ok(), falla()]) == SALIDA_FALLA


def test_lo_que_necesita_una_persona_no_se_reintenta():
    assert _codigo_de_salida([ok(), requiere_atencion()]) == SALIDA_REQUIERE_ATENCION


def test_una_transitoria_alcanza_para_reintentar():
    # Si hay algo recuperable en la corrida, conviene volver a intentar aunque
    # otro cliente esté trabado esperando a una persona.
    assert _codigo_de_salida([requiere_atencion(), falla()]) == SALIDA_FALLA


def test_comprobante_fallido_sin_excepcion_tambien_cuenta():
    r = ok()
    r.fallidos = 1
    assert _codigo_de_salida([r]) == SALIDA_FALLA
