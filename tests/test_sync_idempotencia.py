"""La sincronización tiene que poder correrse dos veces sin duplicar nada."""
import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.afip.base import AlicuotaIVA, ComprobanteNormalizado
from src.database.models import Base, Cliente, Comprobante, EstadoSync, FuenteVentas
from src.sync import _persistir


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def cliente(session):
    c = Cliente(
        cuit="30712345678",
        razon_social="Test SA",
        fuente_ventas=FuenteVentas.WSFE,
        key_path="test.key",
        crt_path="test.crt",
    )
    session.add(c)
    session.flush()
    return c


def comprobante(nro=1):
    return ComprobanteNormalizado(
        tipo_cbte=1,
        pto_vta=3,
        nro=nro,
        fecha=dt.date(2026, 3, 5),
        imp_total=Decimal("12100.00"),
        imp_neto=Decimal("10000.00"),
        imp_iva=Decimal("2100.00"),
        alicuotas_iva=[AlicuotaIVA(5, Decimal("10000.00"), Decimal("2100.00"))],
        fuente="wsfe",
    )


def test_primera_carga_lo_inserta(session, cliente):
    assert _persistir(session, cliente, comprobante()) is True
    session.flush()
    assert session.query(Comprobante).count() == 1


def test_segunda_carga_no_duplica(session, cliente):
    _persistir(session, cliente, comprobante())
    session.flush()
    assert _persistir(session, cliente, comprobante()) is False
    session.flush()
    assert session.query(Comprobante).count() == 1


def test_numeros_distintos_conviven(session, cliente):
    for nro in (1, 2, 3):
        _persistir(session, cliente, comprobante(nro))
    session.flush()
    assert session.query(Comprobante).count() == 3


def test_arranca_pendiente_y_guarda_las_alicuotas(session, cliente):
    _persistir(session, cliente, comprobante())
    session.flush()
    guardado = session.query(Comprobante).one()
    assert guardado.estado_sync == EstadoSync.PENDIENTE
    assert guardado.xubio_id is None
    assert guardado.alicuotas_iva == [
        {"id": 5, "base_imp": "10000.00", "importe": "2100.00"}
    ]
