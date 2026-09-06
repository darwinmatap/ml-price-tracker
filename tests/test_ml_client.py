"""
Tests de app.ml_client contra respuestas mockeadas de la API de Mercado
Libre (unittest.mock). Nunca se llama a la API real, y la persistencia
usa la fixture db_session (SQLite en memoria, ver tests/conftest.py).
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import requests

from app.ml_client import fetch_and_store_price
from app.models import PriceCheck, Product


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


def _make_product(db_session, item_id="MLC123456789", title=None):
    product = Product(item_id=item_id, url=f"https://articulo.mercadolibre.cl/{item_id}", title=title)
    db_session.add(product)
    db_session.commit()
    return product


@patch("app.ml_client.requests.get")
def test_precio_normal(mock_get, db_session):
    product = _make_product(db_session, title=None)
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


@patch("app.ml_client.requests.get")
def test_precio_via_variacion(mock_get, db_session):
    product = _make_product(db_session, title="Ya tiene título")
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
    respuesta_429 = _make_response(429, headers={"Retry-After": retry_after_header})
    respuesta_ok = _make_response(200, {"price": 1000, "currency_id": "CLP", "title": "Cargador"})
    mock_get.side_effect = [respuesta_429, respuesta_ok]

    result = fetch_and_store_price(db_session, product)

    assert result is not None
    mock_sleep.assert_called_once_with(5)  # default, nunca 0 ni un crash por parsear el header


@patch("app.ml_client.requests.get")
def test_precio_no_determinable_no_crea_price_check(mock_get, db_session, caplog):
    product = _make_product(db_session)
    mock_get.return_value = _make_response(
        200,
        {"price": None, "currency_id": "CLP", "title": "Producto sin precio", "variations": []},
    )

    with caplog.at_level("WARNING"):
        result = fetch_and_store_price(db_session, product)

    assert result is None
    assert db_session.query(PriceCheck).count() == 0
    assert any("precio" in record.message.lower() for record in caplog.records)
