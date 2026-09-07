"""
Tests del flujo OAuth con Mercado Libre (app/meli_oauth.py) contra la app
FastAPI real vía TestClient. Toda llamada HTTP a Mercado Libre se mockea
(unittest.mock) — nunca se llama a la API real. El fixture autouse
_reset_auth_security_state (conftest) ya limpia los states pendientes en
memoria entre tests (reset_meli_oauth_state_for_tests).
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from app.meli_oauth import MELI_AUTHORIZATION_URL, _generate_state, _pending_states
from app.models import MeliOAuthToken


def _clear_meli_tokens(db_session):
    """
    db_session (ver tests/conftest.py) siembra por defecto una fila de
    MeliOAuthToken "ya conectada" para que la mayoría de la suite no
    dependa del flujo OAuth. Estos tests SÍ ejercitan ese flujo, así que
    parten de un estado "nunca conectado" explícito.
    """
    db_session.query(MeliOAuthToken).delete()
    db_session.commit()


def _all_logged_strings(records):
    """
    Todos los strings visibles de una lista de LogRecord: el mensaje ya
    formateado (getMessage) y cualquier valor de sus campos "extra"
    (logger.info(..., extra={...}) los deja como atributos del record, no
    dentro del mensaje — por eso no basta con revisar solo getMessage()).
    """
    values = []
    for record in records:
        values.append(record.getMessage())
        for value in vars(record).values():
            if isinstance(value, str):
                values.append(value)
    return values


def _make_token_exchange_response(status_code=200, json_data=None):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_data or {}
    if status_code >= 400:
        import requests

        response.raise_for_status.side_effect = requests.exceptions.HTTPError(f"{status_code} error")
    else:
        response.raise_for_status.return_value = None
    return response


# --- GET /admin/meli/connect ---


def test_connect_sin_autenticacion_devuelve_401(client: TestClient):
    response = client.get("/admin/meli/connect")
    assert response.status_code == 401


def test_connect_usuario_no_admin_devuelve_403(client: TestClient, auth_headers):
    response = client.get("/admin/meli/connect", headers=auth_headers)
    assert response.status_code == 403


def test_connect_admin_devuelve_json_con_url_de_autorizacion_y_state(client: TestClient, admin_auth_headers):
    """
    /admin/meli/connect está protegido con Bearer token, así que un
    navegador no puede navegar directo a la URL (no hay forma de
    adjuntar el header ahí) — por eso devuelve la URL como JSON en vez
    de un RedirectResponse; el front (static/js/admin.js) hace la
    navegación real recién con esa URL en mano.
    """
    response = client.get("/admin/meli/connect", headers=admin_auth_headers)

    assert response.status_code == 200
    body = response.json()
    authorization_url = body["authorization_url"]
    assert authorization_url.startswith(MELI_AUTHORIZATION_URL)

    params = parse_qs(urlparse(authorization_url).query)
    assert params["response_type"] == ["code"]
    assert "client_id" in params
    assert "redirect_uri" in params
    assert len(params["state"][0]) > 20  # token_urlsafe(32) es largo

    # El state generado quedó registrado como pendiente (de un solo uso).
    assert params["state"][0] in _pending_states


# --- GET /admin/meli/status ---


def test_status_sin_autenticacion_devuelve_401(client: TestClient):
    response = client.get("/admin/meli/status")
    assert response.status_code == 401


def test_status_usuario_no_admin_devuelve_403(client: TestClient, auth_headers):
    response = client.get("/admin/meli/status", headers=auth_headers)
    assert response.status_code == 403


def test_status_sin_conexion_devuelve_connected_false(client: TestClient, admin_auth_headers, db_session):
    _clear_meli_tokens(db_session)

    response = client.get("/admin/meli/status", headers=admin_auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body == {"connected": False, "updated_at": None}


def test_status_con_conexion_devuelve_connected_true_y_updated_at(client: TestClient, admin_auth_headers, db_session):
    # db_session (ver conftest) ya siembra por defecto una fila conectada.
    response = client.get("/admin/meli/status", headers=admin_auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is True
    assert body["updated_at"] is not None
    # Nunca se expone el token, ni completo ni truncado, en esta respuesta.
    assert "access_token" not in body
    assert "refresh_token" not in body


# --- GET /oauth/mercadolibre/callback ---


@patch("app.meli_oauth.requests.post")
def test_callback_flujo_completo_exitoso(mock_post, client: TestClient, db_session):
    state = _generate_state()
    mock_post.return_value = _make_token_exchange_response(
        200,
        {"access_token": "APP_USR-abcdefgh12345", "refresh_token": "TG-refreshtoken12345", "expires_in": 21600},
    )

    response = client.get(
        "/oauth/mercadolibre/callback",
        params={"code": "TG-somecode", "state": state},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert "exitosa" in response.text.lower()

    token_row = db_session.query(MeliOAuthToken).one()
    assert token_row.access_token == "APP_USR-abcdefgh12345"
    assert token_row.refresh_token == "TG-refreshtoken12345"
    # SQLite (usado en tests) no preserva tzinfo al leer de vuelta una
    # columna DateTime(timezone=True); el valor guardado siempre fue UTC.
    assert token_row.expires_at.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)

    # El state ya no puede reusarse (de un solo uso).
    assert state not in _pending_states


@patch("app.meli_oauth.requests.post")
def test_callback_actualiza_fila_existente_en_vez_de_duplicar(mock_post, client: TestClient, db_session):
    _clear_meli_tokens(db_session)
    existing = MeliOAuthToken(
        access_token="token-viejo",
        refresh_token="refresh-viejo",
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    db_session.add(existing)
    db_session.commit()

    state = _generate_state()
    mock_post.return_value = _make_token_exchange_response(
        200,
        {"access_token": "token-nuevo", "refresh_token": "refresh-nuevo", "expires_in": 21600},
    )

    response = client.get(
        "/oauth/mercadolibre/callback",
        params={"code": "TG-somecode", "state": state},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert db_session.query(MeliOAuthToken).count() == 1
    token_row = db_session.query(MeliOAuthToken).one()
    assert token_row.access_token == "token-nuevo"
    assert token_row.refresh_token == "refresh-nuevo"


def test_callback_state_invalido_devuelve_400_sin_procesar(client: TestClient, db_session):
    _clear_meli_tokens(db_session)

    response = client.get(
        "/oauth/mercadolibre/callback",
        params={"code": "TG-somecode", "state": "state-que-nunca-generamos"},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert db_session.query(MeliOAuthToken).count() == 0


def test_callback_state_expirado_devuelve_400(client: TestClient, db_session):
    _clear_meli_tokens(db_session)
    state = _generate_state()
    # Se fuerza la expiración manipulando directamente el diccionario en
    # memoria (mismo patrón que usa auth.py para simular bloqueo vencido).
    _pending_states[state] = datetime.now(timezone.utc) - timedelta(seconds=1)

    response = client.get(
        "/oauth/mercadolibre/callback",
        params={"code": "TG-somecode", "state": state},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert db_session.query(MeliOAuthToken).count() == 0
    # Un state expirado también queda invalidado tras el intento (un solo uso).
    assert state not in _pending_states


@patch("app.meli_oauth.requests.post")
def test_callback_state_no_se_puede_reusar_replay(mock_post, client: TestClient, db_session):
    state = _generate_state()
    mock_post.return_value = _make_token_exchange_response(
        200,
        {"access_token": "token-1", "refresh_token": "refresh-1", "expires_in": 21600},
    )

    first = client.get(
        "/oauth/mercadolibre/callback",
        params={"code": "TG-somecode", "state": state},
        follow_redirects=False,
    )
    assert first.status_code == 200

    second = client.get(
        "/oauth/mercadolibre/callback",
        params={"code": "TG-otro-code", "state": state},
        follow_redirects=False,
    )
    assert second.status_code == 400
    # Sigue habiendo una sola fila (la del primer intento), el replay no la tocó.
    assert db_session.query(MeliOAuthToken).count() == 1


def test_callback_sin_code_devuelve_400(client: TestClient, db_session):
    _clear_meli_tokens(db_session)
    state = _generate_state()

    response = client.get(
        "/oauth/mercadolibre/callback",
        params={"state": state},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert db_session.query(MeliOAuthToken).count() == 0


@patch("app.meli_oauth.requests.post")
def test_callback_intercambio_fallido_no_expone_client_secret(mock_post, client: TestClient, db_session, caplog):
    import requests

    _clear_meli_tokens(db_session)
    state = _generate_state()
    mock_post.side_effect = requests.exceptions.ConnectionError("boom")

    with caplog.at_level("ERROR"):
        response = client.get(
            "/oauth/mercadolibre/callback",
            params={"code": "TG-somecode", "state": state},
            follow_redirects=False,
        )

    assert response.status_code == 502
    assert db_session.query(MeliOAuthToken).count() == 0

    from app.meli_oauth import MELI_CLIENT_SECRET

    assert MELI_CLIENT_SECRET not in response.text
    for record in caplog.records:
        assert MELI_CLIENT_SECRET not in record.getMessage()


@patch("app.meli_oauth.requests.post")
def test_callback_nunca_loguea_tokens_completos(mock_post, client: TestClient, db_session, caplog):
    access_token = "APP_USR-secreto-completo-1234567890"
    refresh_token = "TG-secreto-refresh-completo-1234567890"
    state = _generate_state()
    mock_post.return_value = _make_token_exchange_response(
        200,
        {"access_token": access_token, "refresh_token": refresh_token, "expires_in": 21600},
    )

    with caplog.at_level("INFO"):
        response = client.get(
            "/oauth/mercadolibre/callback",
            params={"code": "TG-somecode", "state": state},
            follow_redirects=False,
        )

    assert response.status_code == 200

    logged_values = _all_logged_strings(caplog.records)
    assert not any(access_token in value for value in logged_values)
    assert not any(refresh_token in value for value in logged_values)
    # Sí se permite el prefijo truncado (8 caracteres) usado para debug.
    assert any(access_token[:8] in value for value in logged_values)
