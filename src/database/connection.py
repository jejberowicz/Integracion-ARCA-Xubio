"""Conexión a la base de datos."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src import config
from src.database.models import Base

engine = create_engine(
    config.DATABASE_URL,
    echo=config.SQL_ECHO,
    pool_pre_ping=True,
    future=True,
)

log = logging.getLogger(__name__)

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def get_session() -> Iterator[Session]:
    """Sesión transaccional: commit al salir bien, rollback si algo explota."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def crear_tablas() -> None:
    """Crea el esquema y agrega las columnas que falten."""
    Base.metadata.create_all(engine)
    _agregar_columnas_faltantes()


def _agregar_columnas_faltantes() -> None:
    """Agrega columnas nuevas a tablas que ya existen.

    ``create_all`` no modifica tablas existentes, y el proyecto todavía no usa
    Alembic. Agregar una columna con su default alcanza para los cambios que
    hubo hasta ahora; cualquier cosa más compleja va a necesitar migraciones
    de verdad.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    with engine.begin() as conexion:
        for tabla in Base.metadata.sorted_tables:
            if tabla.name not in inspector.get_table_names():
                continue
            existentes = {c["name"] for c in inspector.get_columns(tabla.name)}
            for columna in tabla.columns:
                if columna.name in existentes:
                    continue
                tipo = columna.type.compile(engine.dialect)
                sql = f"ALTER TABLE {tabla.name} ADD COLUMN {columna.name} {tipo}"
                if columna.default is not None and columna.default.is_scalar:
                    valor = columna.default.arg
                    sql += f" NOT NULL DEFAULT '{valor}'" if isinstance(valor, str) else ""
                conexion.execute(text(sql))
                log.info("Columna agregada: %s.%s", tabla.name, columna.name)
