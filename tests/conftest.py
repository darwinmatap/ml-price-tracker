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

from unittest.mock import MagicMock  # noqa: E402

import pytest  # noqa: E402
import requests  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.auth import limiter, reset_login_security_state_for_tests  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402


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


@pytest.fixture()
def client(db_session):
    """
    TestClient contra la app real, con get_db sobreescrito para usar la
    sesión SQLite en memoria aislada de este test (en vez del engine
    singleton de la app) — así cada test parte de una base vacía.
    """

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    test_client = TestClient(app, base_url="https://testserver")
    try:
        yield test_client
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture()
def auth_headers(client):
    response = client.post(
        "/auth/login",
        json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
    )
    assert response.status_code == 200
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def make_ml_response(status_code=200, json_data=None):
    """Mockea una respuesta de requests.get contra la API de Mercado Libre."""
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_data or {}
    response.headers = {}
    if status_code >= 400:
        response.raise_for_status.side_effect = requests.exceptions.HTTPError(f"{status_code} error")
    else:
        response.raise_for_status.return_value = None
    return response
