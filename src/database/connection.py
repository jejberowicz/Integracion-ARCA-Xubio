"""Conexión a la base de datos."""
from __future__ import annotations

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
    """Crea el esquema. Para cambios posteriores, usar Alembic."""
    Base.metadata.create_all(engine)
