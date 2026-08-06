"""Modelo de datos (SQLAlchemy 2.0)."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON


class Base(DeclarativeBase):
    pass


# JSONB en Postgres, JSON genérico en cualquier otro motor (tests con SQLite).
JSONType = JSON().with_variant(JSONB(), "postgresql")


# --- Vocabulario ---

class FuenteVentas:
    """De dónde se bajan las ventas de un cliente."""

    WSFE = "wsfe"                        # Comprobantes en Línea / WSFEv1 (certificado)
    MIS_COMPROBANTES = "mis_comprobantes"  # Portal ARCA con Clave Fiscal (CSV)

    TODAS = (WSFE, MIS_COMPROBANTES)


class EstadoSync:
    PENDIENTE = "pendiente"
    SUBIDO = "subido"
    ERROR = "error"
    IGNORADO = "ignorado"  # excluido a mano de la sincronización

    TODOS = (PENDIENTE, SUBIDO, ERROR, IGNORADO)


class Ambiente:
    HOMOLOGACION = "homologacion"
    PRODUCCION = "produccion"

    TODOS = (HOMOLOGACION, PRODUCCION)


# --- Tablas ---

class Cliente(Base):
    """Una empresa/CUIT que sincronizamos entre ARCA y Xubio."""

    __tablename__ = "clientes_afip"

    id: Mapped[int] = mapped_column(primary_key=True)
    razon_social: Mapped[str] = mapped_column(String(200), nullable=False)
    cuit: Mapped[str] = mapped_column(String(11), nullable=False, unique=True)

    fuente_ventas: Mapped[str] = mapped_column(
        String(30), nullable=False, default=FuenteVentas.WSFE
    )
    ambiente: Mapped[str] = mapped_column(
        String(20), nullable=False, default=Ambiente.PRODUCCION
    )

    # Credenciales AFIP por certificado (fuente 'wsfe')
    key_path: Mapped[str | None] = mapped_column(String(500))
    crt_path: Mapped[str | None] = mapped_column(String(500))

    # Credenciales Clave Fiscal (fuente 'mis_comprobantes'). La password va cifrada.
    clave_fiscal_usuario: Mapped[str | None] = mapped_column(String(50))
    clave_fiscal_password: Mapped[str | None] = mapped_column(Text)

    # Credenciales Xubio propias de este cliente. El secret va cifrado.
    xubio_client_id: Mapped[str | None] = mapped_column(String(200))
    xubio_secret_id: Mapped[str | None] = mapped_column(Text)

    # IDs de maestros de Xubio que hacen falta para armar el comprobante:
    # {"producto_default_id": ..., "punto_venta_ids": {"1": ...},
    #  "cliente_consumidor_final_id": ...}
    config_xubio: Mapped[dict | None] = mapped_column(JSONType, default=dict)

    last_sync: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    creado_en: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    actualizado_en: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    comprobantes: Mapped[list["Comprobante"]] = relationship(
        back_populates="cliente", cascade="all, delete-orphan"
    )
    tickets: Mapped[list["TicketAcceso"]] = relationship(
        back_populates="cliente", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            f"fuente_ventas IN {FuenteVentas.TODAS}", name="ck_cliente_fuente_ventas"
        ),
        CheckConstraint(f"ambiente IN {Ambiente.TODOS}", name="ck_cliente_ambiente"),
        # Coherencia: cada fuente exige sus propias credenciales.
        CheckConstraint(
            "(fuente_ventas <> 'wsfe') OR "
            "(key_path IS NOT NULL AND crt_path IS NOT NULL)",
            name="ck_cliente_wsfe_requiere_certificados",
        ),
        CheckConstraint(
            "(fuente_ventas <> 'mis_comprobantes') OR "
            "(clave_fiscal_usuario IS NOT NULL AND clave_fiscal_password IS NOT NULL)",
            name="ck_cliente_mc_requiere_clave_fiscal",
        ),
    )

    def __repr__(self) -> str:
        return f"<Cliente {self.cuit} {self.razon_social!r} via {self.fuente_ventas}>"


class Comprobante(Base):
    """Comprobante de venta normalizado, sea cual sea la fuente de origen."""

    __tablename__ = "comprobantes_venta"

    id: Mapped[int] = mapped_column(primary_key=True)
    cliente_id: Mapped[int] = mapped_column(
        ForeignKey("clientes_afip.id", ondelete="CASCADE"), nullable=False
    )

    # Identidad del comprobante (clave natural)
    tipo_cbte: Mapped[int] = mapped_column(Integer, nullable=False)
    pto_vta: Mapped[int] = mapped_column(Integer, nullable=False)
    nro: Mapped[int] = mapped_column(Integer, nullable=False)

    fecha: Mapped[dt.date] = mapped_column(nullable=False)

    # Receptor
    doc_tipo: Mapped[int | None] = mapped_column(Integer)
    doc_nro: Mapped[str | None] = mapped_column(String(20))
    receptor_nombre: Mapped[str | None] = mapped_column(String(200))

    # Importes (siempre positivos; el signo lo define el tipo de comprobante)
    imp_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=0)
    imp_neto: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=0)
    imp_iva: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=0)
    imp_op_ex: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=0)
    imp_tot_conc: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=0)
    imp_trib: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=0)

    moneda: Mapped[str] = mapped_column(String(3), nullable=False, default="PES")
    cotizacion: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=1)

    concepto: Mapped[int | None] = mapped_column(Integer)  # 1 prod, 2 serv, 3 ambos
    cae: Mapped[str | None] = mapped_column(String(20))
    fecha_vto_cae: Mapped[dt.date | None] = mapped_column()

    # [{"id": 5, "base_imp": "1000.00", "importe": "210.00"}, ...]
    # Para Mis Comprobantes viene inferido, no informado por AFIP.
    alicuotas_iva: Mapped[list | None] = mapped_column(JSONType)
    alicuotas_inferidas: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    fuente: Mapped[str] = mapped_column(String(30), nullable=False)
    raw: Mapped[dict | None] = mapped_column(JSONType)  # payload original, para auditoría

    # Estado frente a Xubio
    estado_sync: Mapped[str] = mapped_column(
        String(20), nullable=False, default=EstadoSync.PENDIENTE
    )
    xubio_id: Mapped[str | None] = mapped_column(String(100))
    intentos_sync: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_sync: Mapped[str | None] = mapped_column(Text)
    fecha_sync: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    creado_en: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    actualizado_en: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    cliente: Mapped[Cliente] = relationship(back_populates="comprobantes")

    __table_args__ = (
        # Idempotencia: correr la sincronización dos veces no puede duplicar nada.
        UniqueConstraint(
            "cliente_id", "tipo_cbte", "pto_vta", "nro", name="uq_comprobante_natural"
        ),
        CheckConstraint(f"estado_sync IN {EstadoSync.TODOS}", name="ck_cbte_estado_sync"),
        Index("ix_cbte_pendientes", "cliente_id", "estado_sync"),
        Index("ix_cbte_fecha", "cliente_id", "fecha"),
    )

    @property
    def signo(self) -> int:
        from src.afip.tablas import signo_comprobante

        return signo_comprobante(self.tipo_cbte)

    def __repr__(self) -> str:
        return f"<Comprobante {self.tipo_cbte}-{self.pto_vta:05d}-{self.nro:08d}>"


class TicketAcceso(Base):
    """Ticket de Acceso (TA) de WSAA cacheado.

    AFIP entrega un TA con 12 horas de validez y rechaza pedidos repetidos dentro
    de esa ventana, así que hay que guardarlo por CUIT + servicio + ambiente.
    """

    __tablename__ = "tickets_acceso"

    id: Mapped[int] = mapped_column(primary_key=True)
    cliente_id: Mapped[int] = mapped_column(
        ForeignKey("clientes_afip.id", ondelete="CASCADE"), nullable=False
    )
    servicio: Mapped[str] = mapped_column(String(30), nullable=False)  # p.ej. 'wsfe'
    ambiente: Mapped[str] = mapped_column(String(20), nullable=False)

    token: Mapped[str] = mapped_column(Text, nullable=False)
    sign: Mapped[str] = mapped_column(Text, nullable=False)

    generado_en: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expira_en: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    cliente: Mapped[Cliente] = relationship(back_populates="tickets")

    __table_args__ = (
        UniqueConstraint("cliente_id", "servicio", "ambiente", name="uq_ta_cliente_servicio"),
    )


class LogSync(Base):
    """Bitácora de cada corrida. Auditoría contable: qué se bajó y qué se subió."""

    __tablename__ = "log_sync"

    id: Mapped[int] = mapped_column(primary_key=True)
    cliente_id: Mapped[int | None] = mapped_column(
        ForeignKey("clientes_afip.id", ondelete="SET NULL")
    )
    inicio: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    fin: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    fuente: Mapped[str | None] = mapped_column(String(30))
    desde: Mapped[dt.date | None] = mapped_column()
    hasta: Mapped[dt.date | None] = mapped_column()

    descargados: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    nuevos: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    subidos: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fallidos: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    detalle: Mapped[str | None] = mapped_column(Text)
