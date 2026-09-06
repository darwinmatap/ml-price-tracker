import os

# app.database exige DATABASE_URL al importarse (fail-fast en producción).
# Para que la sola importación no rompa los tests que no tocan la DB real,
# se define un valor por defecto aquí, antes de que se importe cualquier
# módulo de la app. Los tests igual usan su propio motor SQLite en
# memoria (ver fixture db_session más abajo); esto nunca toca una base real.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest  # noqa: E402
from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.database import Base  # noqa: E402


@pytest.fixture()
def db_session():
    """Sesión de ORM contra una base SQLite en memoria, aislada por test."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # SQLite no aplica FOREIGN KEY por defecto; se activa explícitamente
    # para que las constraints declaradas en los modelos se validen de
    # verdad en los tests.
    @event.listens_for(engine, "connect")
    def _enable_sqlite_fk(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
