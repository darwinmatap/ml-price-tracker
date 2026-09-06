import os

from passlib.context import CryptContext

# Los módulos de la app exigen estas variables al importarse (fail-fast en
# producción, ver app/database.py y app/auth.py). Se definen valores de
# prueba aquí, ANTES de importar cualquier módulo de la app, para que la
# sola importación no rompa los tests. Nunca se usan credenciales reales.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

TEST_USERNAME = "admin"
TEST_PASSWORD = "S3cur3-Test-Password!"
_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

os.environ.setdefault("APP_USERNAME", TEST_USERNAME)
os.environ.setdefault("APP_PASSWORD_HASH", _pwd_context.hash(TEST_PASSWORD))
os.environ.setdefault("SECRET_KEY", "test-secret-key-solo-para-tests-no-usar-en-produccion")

import pytest  # noqa: E402
from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.auth import limiter, reset_login_security_state_for_tests  # noqa: E402
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


@pytest.fixture(autouse=True)
def _reset_auth_security_state():
    """
    Limpia entre cada test el estado de rate limiting (slowapi) y de
    bloqueo progresivo (app.auth), para que no queden contadores de un
    test contaminando el siguiente.
    """
    limiter.reset()
    reset_login_security_state_for_tests()
    yield
    limiter.reset()
    reset_login_security_state_for_tests()
