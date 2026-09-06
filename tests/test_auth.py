"""
Tests del endpoint de autenticación (app/auth.py) contra la app FastAPI
real vía TestClient. No se toca ninguna base de datos ni servicio externo
real: SECRET_KEY/APP_USERNAME/APP_PASSWORD_HASH de prueba vienen de
tests/conftest.py, y el fixture autouse _reset_auth_security_state limpia
el rate limiter (slowapi) y el bloqueo progresivo entre cada test.
"""

from datetime import timedelta

import jwt
import pytest
from fastapi.testclient import TestClient

from app.auth import ALGORITHM, GENERIC_AUTH_ERROR_DETAIL, SECRET_KEY, _create_token, limiter
from app.main import app
from tests.conftest import TEST_PASSWORD, TEST_USERNAME


@pytest.fixture()
def client():
    """
    Cliente nuevo por test: evita que las cookies de un test contaminen otro.
    base_url en https porque la cookie de refresh se emite con Secure=True
    (correcto para producción); TestClient usa http:// por defecto y un
    cliente respetuoso del flag Secure jamás reenviaría la cookie sobre
    http en el siguiente request.
    """
    return TestClient(app, base_url="https://testserver")


def _decode(token: str) -> dict:
    return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])


def test_login_correcto_devuelve_access_token_y_cookie_de_refresh(client):
    response = client.post(
        "/auth/login",
        json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"

    payload = _decode(body["access_token"])
    assert payload["sub"] == TEST_USERNAME
    assert payload["type"] == "access"

    set_cookie_header = response.headers.get("set-cookie", "").lower()
    assert "refresh_token=" in set_cookie_header
    assert "httponly" in set_cookie_header
    assert "secure" in set_cookie_header
    assert "samesite=strict" in set_cookie_header


def test_login_clave_incorrecta_mensaje_generico(client):
    response = client.post(
        "/auth/login",
        json={"username": TEST_USERNAME, "password": "clave-incorrecta"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == GENERIC_AUTH_ERROR_DETAIL


def test_login_usuario_incorrecto_mismo_mensaje_generico(client):
    response = client.post(
        "/auth/login",
        json={"username": "usuario-que-no-existe", "password": TEST_PASSWORD},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == GENERIC_AUTH_ERROR_DETAIL


def test_refresh_valido_devuelve_nuevo_access_token(client):
    login_response = client.post(
        "/auth/login",
        json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
    )
    assert login_response.status_code == 200

    refresh_response = client.post("/auth/refresh")

    assert refresh_response.status_code == 200
    payload = _decode(refresh_response.json()["access_token"])
    assert payload["sub"] == TEST_USERNAME
    assert payload["type"] == "access"


def test_refresh_exitoso_rota_el_jti(client):
    login_response = client.post(
        "/auth/login",
        json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
    )
    old_refresh_token = login_response.cookies["refresh_token"]
    old_jti = _decode(old_refresh_token)["jti"]

    refresh_response = client.post("/auth/refresh")
    assert refresh_response.status_code == 200

    new_refresh_token = refresh_response.cookies["refresh_token"]
    new_jti = _decode(new_refresh_token)["jti"]

    assert new_jti != old_jti


def test_reusar_refresh_token_ya_rotado_devuelve_401_y_loguea_warning(client, caplog):
    login_response = client.post(
        "/auth/login",
        json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
    )
    old_refresh_token = login_response.cookies["refresh_token"]

    primer_refresh = client.post("/auth/refresh")
    assert primer_refresh.status_code == 200  # rota correctamente

    # Se reusa el refresh token viejo, ya invalidado por la rotación anterior.
    client.cookies.set("refresh_token", old_refresh_token)

    with caplog.at_level("WARNING"):
        reuso_response = client.post("/auth/refresh")

    assert reuso_response.status_code == 401
    assert reuso_response.json()["detail"] == GENERIC_AUTH_ERROR_DETAIL
    assert any("reuso" in record.message.lower() for record in caplog.records)


def test_logout_invalida_la_sesion_y_borra_la_cookie(client):
    login_response = client.post(
        "/auth/login",
        json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
    )
    refresh_token_antes_de_logout = login_response.cookies["refresh_token"]

    logout_response = client.post("/auth/logout")
    assert logout_response.status_code == 200

    set_cookie_header = logout_response.headers.get("set-cookie", "").lower()
    assert "refresh_token=" in set_cookie_header
    assert "max-age=0" in set_cookie_header

    # El token de antes del logout ya no debe servir para refrescar.
    client.cookies.set("refresh_token", refresh_token_antes_de_logout)
    refresh_after_logout = client.post("/auth/refresh")
    assert refresh_after_logout.status_code == 401
    assert refresh_after_logout.json()["detail"] == GENERIC_AUTH_ERROR_DETAIL


def test_refresh_con_token_expirado_devuelve_401_generico(client):
    expired_refresh_token = _create_token(
        subject=TEST_USERNAME,
        token_type="refresh",
        expires_delta=timedelta(seconds=-1),
    )

    client.cookies.set("refresh_token", expired_refresh_token)
    response = client.post("/auth/refresh")

    assert response.status_code == 401
    assert response.json()["detail"] == GENERIC_AUTH_ERROR_DETAIL


def test_rate_limit_se_activa_en_el_sexto_intento(client):
    for _ in range(5):
        response = client.post(
            "/auth/login",
            json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
        )
        assert response.status_code == 200

    sexto = client.post(
        "/auth/login",
        json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
    )
    assert sexto.status_code == 429


def test_bloqueo_progresivo_se_activa_en_el_quinto_fallo(client):
    # Se resetea el rate limiter entre cada llamada para aislar el bloqueo
    # progresivo del límite de slowapi (ambos usan 5 como umbral, pero son
    # mecanismos independientes: este test prueba que el bloqueo por
    # fallos actúa aunque el rate limiter no haya disparado).
    for _ in range(5):
        response = client.post(
            "/auth/login",
            json={"username": TEST_USERNAME, "password": "clave-incorrecta"},
        )
        assert response.status_code == 401
        assert response.json()["detail"] == GENERIC_AUTH_ERROR_DETAIL
        limiter.reset()

    # Aunque ahora se manden credenciales CORRECTAS, la IP debe seguir
    # bloqueada por los 5 fallos previos.
    bloqueado = client.post(
        "/auth/login",
        json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
    )
    assert bloqueado.status_code == 429
