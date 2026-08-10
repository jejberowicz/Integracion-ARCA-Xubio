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


# --- No duplicar lo que ya está en Xubio ---

class XubioFalso:
    """Stub del cliente: responde qué comprobantes ya están cargados."""

    def __init__(self, existentes=None, mas_antiguo=None):
        self.existentes = existentes or {}
        self.mas_antiguo = mas_antiguo
        self.subidos = []

    def existente_en_xubio(self, cbte):
        from src.xubio.mapeo import numero_formateado

        numero = numero_formateado(cbte)
        if numero in self.existentes:
            return "existe", self.existentes[numero]
        if self.mas_antiguo and cbte.fecha < self.mas_antiguo:
            return "sin_certeza", None
        return "no_existe", None

    def subir_comprobante(self, cbte):
        self.subidos.append(cbte)
        return "nuevo-123"


@pytest.fixture
def guardado(session, cliente):
    from src.sync import _persistir

    _persistir(session, cliente, comprobante(nro=747))
    session.flush()
    return session.query(Comprobante).one()


def test_no_sube_lo_que_ya_esta_en_xubio(session, cliente, guardado, monkeypatch):
    from src import sync

    falso = XubioFalso(existentes={"A-00003-00000747": "9000002"})
    monkeypatch.setattr(sync, "XubioClient", lambda c: falso)

    resultado = sync.Resultado()
    sync.subir(session, cliente, resultado)

    # Lo importante: no se creó nada en la contabilidad del cliente.
    assert falso.subidos == []
    assert resultado.ya_estaban == 1
    assert resultado.subidos == 0
    assert guardado.estado_sync == EstadoSync.SUBIDO
    assert guardado.xubio_id == "9000002"


def test_sube_lo_que_no_esta(session, cliente, guardado, monkeypatch):
    from src import sync

    falso = XubioFalso()
    monkeypatch.setattr(sync, "XubioClient", lambda c: falso)

    resultado = sync.Resultado()
    sync.subir(session, cliente, resultado)

    assert len(falso.subidos) == 1
    assert resultado.subidos == 1
    assert guardado.xubio_id == "nuevo-123"


def test_ante_la_duda_no_sube(session, cliente, guardado, monkeypatch):
    from src import sync

    # El comprobante es anterior a lo que la API de Xubio devuelve, así que no
    # se puede saber si ya está. Duplicar es peor que dejarlo pendiente.
    falso = XubioFalso(mas_antiguo=dt.date(2026, 12, 1))
    monkeypatch.setattr(sync, "XubioClient", lambda c: falso)

    resultado = sync.Resultado()
    sync.subir(session, cliente, resultado)

    assert falso.subidos == []
    assert resultado.sin_certeza == 1
    assert guardado.estado_sync == EstadoSync.ERROR
    assert "No se pudo confirmar" in guardado.error_sync
