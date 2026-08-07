"""El CUIT se acepta escrito como venga, pero se guarda siempre normalizado."""
import click
import pytest

from main import _cuit_opcion


@pytest.mark.parametrize(
    "escrito",
    ["30-71234567-1", "30712345671", "30.712.345.671", " 30 71234567 1 "],
)
def test_se_normaliza_a_once_digitos(escrito):
    assert _cuit_opcion(escrito) == "30712345671"


def test_rechaza_digito_verificador_incorrecto():
    with pytest.raises(click.ClickException, match="dígito verificador"):
        _cuit_opcion("30-71234567-9")


def test_rechaza_cuit_incompleto():
    with pytest.raises(click.ClickException):
        _cuit_opcion("3071234567")


def test_rechaza_texto():
    with pytest.raises(click.ClickException):
        _cuit_opcion("no-soy-un-cuit")
