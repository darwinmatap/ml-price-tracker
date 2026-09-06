import os

from passlib.context import CryptContext

# app.database exige DATABASE_URL y app.auth exige SECRET_KEY al
# importarse (fail-fast en producción). Se define un valor de prueba
# aquí, ANTES de importar cualquier módulo de la app, para que la sola
# importación no rompa los tests. Nunca se usan credenciales reales.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret-key-solo-para-tests-no-usar-en-produccion")

TEST_USERNAME = "usuario_test"
TEST_PASSWORD = "S3cur3-Test-Password!"
TEST_NOMBRE = "Usuario de Prueba"
TEST_ADMIN_USERNAME = "admin_test"
TEST_ADMIN_PASSWORD = "Adm1n-Test-Password!"
TEST_ADMIN_NOMBRE = "Admin de Prueba"

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

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
from app.models import User, UserRole  # noqa: E402


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


def create_user(
    db_session,
    username=TEST_USERNAME,
    password=TEST_PASSWORD,
    nombre="Usuario de Prueba",
    role=UserRole.USER,
    is_active=True,
    debe_cambiar_password=False,
):
    """Crea y comitea un User de prueba directamente vía el ORM."""
    user = User(
        username=username,
        nombre=nombre,
        password_hash=_pwd_context.hash(password),
        role=role,
        is_active=is_active,
        debe_cambiar_password=debe_cambiar_password,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def test_user(db_session):
    """Usuario normal (role=user) ya persistido, listo para loguearse."""
    return create_user(
        db_session,
        username=TEST_USERNAME,
        password=TEST_PASSWORD,
        nombre=TEST_NOMBRE,
        role=UserRole.USER,
    )


@pytest.fixture()
def test_admin(db_session):
    """Usuario admin ya persistido, listo para loguearse."""
    return create_user(
        db_session,
        username=TEST_ADMIN_USERNAME,
        password=TEST_ADMIN_PASSWORD,
        nombre=TEST_ADMIN_NOMBRE,
        role=UserRole.ADMIN,
    )


@pytest.fixture()
def auth_headers(client, test_user):
    response = client.post(
        "/auth/login",
        json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
    )
    assert response.status_code == 200
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def admin_auth_headers(client, test_admin):
    response = client.post(
        "/auth/login",
        json={"username": TEST_ADMIN_USERNAME, "password": TEST_ADMIN_PASSWORD},
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
