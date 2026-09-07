"""
Tests de app.ml_client contra respuestas mockeadas de la API de Mercado
Libre (unittest.mock). Nunca se llama a la API real, y la persistencia
usa la fixture db_session (SQLite en memoria, ver tests/conftest.py).
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import requests

from app.ml_client import MeliNotConnectedError, fetch_and_store_price, get_valid_meli_access_token
from app.models import MeliOAuthToken, PriceCheck, Product, User, UserRole


def _make_response(status_code=200, json_data=None, headers=None):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_data or {}
    response.headers = headers or {}
    if status_code >= 400:
        response.raise_for_status.side_effect = requests.exceptions.HTTPError(f"{status_code} error")
    else:
        response.raise_for_status.return_value = None
    return response


def _make_meli_token(db_session, access_token="test-access-token-12345678", hours_to_expire=1):
    """
    Ajusta la fila de MeliOAuthToken vigente que ya sembró por defecto el
    fixture db_session (ver tests/conftest.py) — o la crea, si algún test
    la borró explícitamente para simular el escenario "no conectado".
    """
    token = db_session.query(MeliOAuthToken).first()
    if token is None:
        token = MeliOAuthToken()
    token.access_token = access_token
    token.refresh_token = "test-refresh-token-12345678"
    token.expires_at = datetime.now(timezone.utc) + timedelta(hours=hours_to_expire)
    db_session.add(token)
    db_session.commit()
    return token


def _clear_meli_token(db_session):
    """Borra cualquier fila de MeliOAuthToken, para simular que la app nunca se conectó."""
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


def _make_product(db_session, item_id="MLC123456789", title=None):
    # Product.user_id es NOT NULL: se crea un usuario dueño mínimo para
    # satisfacer la FK, aunque estos tests no ejercitan nada relacionado
    # con usuarios.
    user = User(
        username=f"user-{item_id}",
        nombre="Usuario de Prueba",
        password_hash="unused-hash-solo-para-satisfacer-not-null",
        role=UserRole.USER,
    )
    db_session.add(user)
    db_session.commit()

    product = Product(
        item_id=item_id,
        url=f"https://articulo.mercadolibre.cl/{item_id}",
        title=title,
        user=user,
    )
    db_session.add(product)
    db_session.commit()
    return product


@patch("app.ml_client.requests.get")
def test_precio_normal(mock_get, db_session):
    product = _make_product(db_session, title=None)
    _make_meli_token(db_session, access_token="token-vigente-12345678")
    mock_get.return_value = _make_response(
        200,
        {"price": 19990, "currency_id": "CLP", "title": "Notebook Lenovo"},
    )

    result = fetch_and_store_price(db_session, product)

    assert result is not None
    assert result.price == Decimal("19990")
    assert result.currency == "CLP"
    assert product.title == "Notebook Lenovo"  # se completó porque estaba vacío
    assert db_session.query(PriceCheck).count() == 1
    mock_get.assert_called_once()
    # El header Authorization lleva el access_token vigente.
    called_headers = mock_get.call_args.kwargs["headers"]
    assert called_headers["Authorization"] == "Bearer token-vigente-12345678"


@patch("app.ml_client.requests.get")
def test_precio_via_variacion(mock_get, db_session):
    product = _make_product(db_session, title="Ya tiene título")
    _make_meli_token(db_session)
    mock_get.return_value = _make_response(
        200,
        {
            "price": None,
            "currency_id": "ARS",
            "title": "Zapatillas Running",
            "variations": [{"id": 1, "price": 45999}, {"id": 2, "price": 47999}],
        },
    )

    result = fetch_and_store_price(db_session, product)

    assert result is not None
    assert result.price == Decimal("45999")  # primera variación
    assert result.currency == "ARS"
    assert product.title == "Ya tiene título"  # no se pisa un título existente


@patch("app.ml_client.requests.get")
def test_404_no_crea_price_check(mock_get, db_session, caplog):
    product = _make_product(db_session)
    _make_meli_token(db_session)
    mock_get.return_value = _make_response(404)

    with caplog.at_level("WARNING"):
        result = fetch_and_store_price(db_session, product)

    assert result is None
    assert db_session.query(PriceCheck).count() == 0
    assert any("404" in record.message or "existe" in record.message for record in caplog.records)
    mock_get.assert_called_once()


@patch("app.ml_client.time.sleep")
@patch("app.ml_client.requests.get")
def test_429_reintenta_y_tiene_exito(mock_get, mock_sleep, db_session):
    product = _make_product(db_session)
    _make_meli_token(db_session)
    respuesta_429 = _make_response(429, headers={"Retry-After": "2"})
    respuesta_ok = _make_response(200, {"price": 5000, "currency_id": "MXN", "title": "Audífonos"})
    mock_get.side_effect = [respuesta_429, respuesta_ok]

    result = fetch_and_store_price(db_session, product)

    assert result is not None
    assert result.price == Decimal("5000")
    assert result.currency == "MXN"
    assert mock_get.call_count == 2
    mock_sleep.assert_called_once_with(2)


@patch("app.ml_client.requests.get")
def test_timeout_reintenta_una_vez_y_desiste_sin_crash(mock_get, db_session, caplog):
    product = _make_product(db_session)
    _make_meli_token(db_session)
    mock_get.side_effect = requests.exceptions.Timeout("tiempo de espera agotado")

    with caplog.at_level("WARNING"):
        result = fetch_and_store_price(db_session, product)

    assert result is None
    assert db_session.query(PriceCheck).count() == 0
    # 1 intento inicial + 1 reintento = 2 llamadas, nunca más.
    assert mock_get.call_count == 2


@patch("app.ml_client.time.sleep")
@patch("app.ml_client.requests.get")
def test_retry_after_mayor_a_30_se_limita_a_30(mock_get, mock_sleep, db_session):
    product = _make_product(db_session)
    _make_meli_token(db_session)
    respuesta_429 = _make_response(429, headers={"Retry-After": "9999"})
    respuesta_ok = _make_response(200, {"price": 1000, "currency_id": "CLP", "title": "Cargador"})
    mock_get.side_effect = [respuesta_429, respuesta_ok]

    result = fetch_and_store_price(db_session, product)

    assert result is not None
    mock_sleep.assert_called_once_with(30)  # nunca los 9999 que pedía el header


@pytest.mark.parametrize("retry_after_header", ["-5", "abc"])
@patch("app.ml_client.time.sleep")
@patch("app.ml_client.requests.get")
def test_retry_after_corrupto_usa_default_de_5s(mock_get, mock_sleep, db_session, retry_after_header):
    product = _make_product(db_session)
    _make_meli_token(db_session)
    respuesta_429 = _make_response(429, headers={"Retry-After": retry_after_header})
    respuesta_ok = _make_response(200, {"price": 1000, "currency_id": "CLP", "title": "Cargador"})
    mock_get.side_effect = [respuesta_429, respuesta_ok]

    result = fetch_and_store_price(db_session, product)

    assert result is not None
    mock_sleep.assert_called_once_with(5)  # default, nunca 0 ni un crash por parsear el header


@patch("app.ml_client.requests.get")
def test_precio_no_determinable_no_crea_price_check(mock_get, db_session, caplog):
    product = _make_product(db_session)
    _make_meli_token(db_session)
    mock_get.return_value = _make_response(
        200,
        {"price": None, "currency_id": "CLP", "title": "Producto sin precio", "variations": []},
    )

    with caplog.at_level("WARNING"):
        result = fetch_and_store_price(db_session, product)

    assert result is None
    assert db_session.query(PriceCheck).count() == 0
    assert any("precio" in record.message.lower() for record in caplog.records)


# --- get_valid_meli_access_token / renovación automática ---


def test_get_valid_meli_access_token_sin_fila_lanza_not_connected(db_session):
    _clear_meli_token(db_session)

    with pytest.raises(MeliNotConnectedError):
        get_valid_meli_access_token(db_session)


def test_get_valid_meli_access_token_vigente_no_renueva(db_session):
    _make_meli_token(db_session, access_token="token-aun-vigente", hours_to_expire=1)

    with patch("app.ml_client.requests.post") as mock_post:
        token = get_valid_meli_access_token(db_session)

    assert token == "token-aun-vigente"
    mock_post.assert_not_called()


@patch("app.ml_client.requests.post")
def test_get_valid_meli_access_token_expirado_renueva_automaticamente(mock_post, db_session):
    original = _make_meli_token(db_session, access_token="token-viejo-expirado", hours_to_expire=-1)
    mock_post.return_value = _make_response(
        200,
        {"access_token": "token-nuevo-renovado", "refresh_token": "refresh-nuevo", "expires_in": 21600},
    )

    token = get_valid_meli_access_token(db_session)

    assert token == "token-nuevo-renovado"
    mock_post.assert_called_once()
    called_payload = mock_post.call_args.kwargs["data"]
    assert called_payload["grant_type"] == "refresh_token"
    assert called_payload["refresh_token"] == "test-refresh-token-12345678"

    db_session.refresh(original)
    assert original.access_token == "token-nuevo-renovado"
    assert original.refresh_token == "refresh-nuevo"  # se guardó el refresh_token nuevo, no se reusó el viejo
    # SQLite (usado en tests) no preserva tzinfo al leer de vuelta una
    # columna DateTime(timezone=True); el valor guardado siempre fue UTC.
    assert original.expires_at.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)


@patch("app.ml_client.requests.post")
def test_get_valid_meli_access_token_dentro_del_margen_de_seguridad_renueva(mock_post, db_session):
    # A 2 minutos de expirar: dentro del margen de seguridad de 5 minutos, debe renovar.
    token_row = _make_meli_token(db_session, access_token="token-por-vencer")
    token_row.expires_at = datetime.now(timezone.utc) + timedelta(minutes=2)
    db_session.add(token_row)
    db_session.commit()

    mock_post.return_value = _make_response(
        200,
        {"access_token": "token-renovado-preventivo", "refresh_token": "refresh-nuevo", "expires_in": 21600},
    )

    token = get_valid_meli_access_token(db_session)

    assert token == "token-renovado-preventivo"
    mock_post.assert_called_once()


@patch("app.ml_client.requests.post")
def test_get_valid_meli_access_token_sin_refresh_token_nuevo_mantiene_el_viejo(mock_post, db_session):
    original = _make_meli_token(db_session, hours_to_expire=-1)
    mock_post.return_value = _make_response(
        200,
        {"access_token": "token-nuevo", "expires_in": 21600},  # sin refresh_token en la respuesta
    )

    get_valid_meli_access_token(db_session)

    db_session.refresh(original)
    assert original.refresh_token == "test-refresh-token-12345678"  # se mantuvo el que ya había


# --- fetch_and_store_price sin conexión OAuth ---


@patch("app.ml_client.requests.get")
def test_fetch_and_store_price_sin_token_no_crashea(mock_get, db_session, caplog):
    """Sin ninguna fila de MeliOAuthToken (app nunca conectada), debe fallar controladamente."""
    product = _make_product(db_session)
    _clear_meli_token(db_session)

    with caplog.at_level("WARNING"):
        result = fetch_and_store_price(db_session, product)

    assert result is None
    assert db_session.query(PriceCheck).count() == 0
    mock_get.assert_not_called()
    assert any("no está conectada" in record.message.lower() for record in caplog.records)


@patch("app.ml_client.requests.post")
def test_fetch_and_store_price_fallo_de_red_renovando_token_no_crashea(mock_post, db_session, caplog):
    product = _make_product(db_session)
    _make_meli_token(db_session, hours_to_expire=-1)  # expirado -> intenta renovar
    mock_post.side_effect = requests.exceptions.ConnectionError("boom")

    with caplog.at_level("ERROR"):
        result = fetch_and_store_price(db_session, product)

    assert result is None
    assert db_session.query(PriceCheck).count() == 0


# --- Ningún test verifica el token completo en un log ---


@patch("app.ml_client.requests.post")
def test_renovacion_de_token_nunca_loguea_el_token_completo(mock_post, db_session, caplog):
    _make_meli_token(db_session, hours_to_expire=-1)
    nuevo_access_token = "APP_USR-token-completo-secreto-1234567890"
    mock_post.return_value = _make_response(
        200,
        {"access_token": nuevo_access_token, "refresh_token": "refresh-nuevo-secreto", "expires_in": 21600},
    )

    with caplog.at_level("INFO"):
        get_valid_meli_access_token(db_session)

    logged_values = _all_logged_strings(caplog.records)
    assert not any(nuevo_access_token in value for value in logged_values)
    assert not any("refresh-nuevo-secreto" in value for value in logged_values)
    assert any(nuevo_access_token[:8] in value for value in logged_values)  # el prefijo truncado sí se permite
