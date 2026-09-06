"""
Tests de los endpoints CRUD de productos (app/products.py) contra la app
FastAPI real vía TestClient. La API de Mercado Libre se mockea siempre
(unittest.mock, nunca red real); la autenticación es real (login vía
/auth/login para obtener el token, get_current_user no se mockea).

get_db se sobreescribe para que la app use la misma sesión SQLite en
memoria aislada por test (fixture db_session de tests/conftest.py) en vez
del engine singleton de la app real — así cada test parte de una base
vacía y no hay fugas de datos entre tests.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import requests
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from tests.conftest import TEST_PASSWORD, TEST_USERNAME


@pytest.fixture()
def client(db_session):
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


def _make_ml_response(status_code=200, json_data=None):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_data or {}
    response.headers = {}
    if status_code >= 400:
        response.raise_for_status.side_effect = requests.exceptions.HTTPError(f"{status_code} error")
    else:
        response.raise_for_status.return_value = None
    return response


@patch("app.ml_client.requests.get")
def test_agregar_producto_valido_201_con_primer_precio(mock_get, client, auth_headers):
    mock_get.return_value = _make_ml_response(
        200, {"price": 19990, "currency_id": "CLP", "title": "Notebook Lenovo"}
    )

    response = client.post(
        "/products",
        json={"url": "https://articulo.mercadolibre.cl/MLC-100000001-notebook-lenovo"},
        headers=auth_headers,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["item_id"] == "MLC100000001"
    assert body["precio_inicial_obtenido"] is True
    assert body["advertencia"] is None
    assert Decimal(str(body["precio_actual"])) == Decimal("19990")
    assert body["moneda"] == "CLP"
    assert body["title"] == "Notebook Lenovo"


@patch("app.ml_client.requests.get")
def test_agregar_producto_dominio_fuera_de_allowlist_422(mock_get, client, auth_headers):
    response = client.post(
        "/products",
        json={"url": "https://www.amazon.com/producto/123"},
        headers=auth_headers,
    )

    assert response.status_code == 422
    mock_get.assert_not_called()


@pytest.mark.parametrize(
    "url_maliciosa",
    [
        "https://mercadolibre.cl.atacante.com/MLC123456789",
        "https://evilmercadolibre.cl/MLC123456789",
        "https://articulo.mercadolibre.atacante.com/MLC123456789",
        "http://atacante.com/?url=mercadolibre.cl",
        "https://mercadolibre.cl@atacante.com/MLC123456789",
    ],
)
@patch("app.ml_client.requests.get")
def test_agregar_producto_bypass_de_allowlist_rechazado_422(mock_get, client, auth_headers, url_maliciosa):
    response = client.post("/products", json={"url": url_maliciosa}, headers=auth_headers)

    assert response.status_code == 422
    mock_get.assert_not_called()


@patch("app.ml_client.requests.get")
def test_agregar_producto_item_id_duplicado_409(mock_get, client, auth_headers):
    mock_get.return_value = _make_ml_response(200, {"price": 1000, "currency_id": "CLP", "title": "Producto"})
    url = "https://articulo.mercadolibre.cl/MLC-200000002-producto"

    primero = client.post("/products", json={"url": url}, headers=auth_headers)
    assert primero.status_code == 201

    segundo = client.post("/products", json={"url": url}, headers=auth_headers)
    assert segundo.status_code == 409


@patch("app.ml_client.requests.get")
def test_listar_productos_precio_actual_y_anterior_tras_dos_escaneos(mock_get, client, auth_headers):
    mock_get.return_value = _make_ml_response(200, {"price": 1000, "currency_id": "CLP", "title": "Producto"})
    crear = client.post(
        "/products",
        json={"url": "https://articulo.mercadolibre.cl/MLC-300000003-producto"},
        headers=auth_headers,
    )
    assert crear.status_code == 201
    product_id = crear.json()["id"]

    mock_get.return_value = _make_ml_response(200, {"price": 900, "currency_id": "CLP", "title": "Producto"})
    scan = client.post(f"/products/{product_id}/scan", headers=auth_headers)
    assert scan.status_code == 200

    listado = client.get("/products", headers=auth_headers)
    assert listado.status_code == 200
    item = next(i for i in listado.json() if i["id"] == product_id)

    assert Decimal(str(item["precio_actual"])) == Decimal("900")
    assert Decimal(str(item["precio_anterior"])) == Decimal("1000")
    assert item["moneda"] == "CLP"
    assert item["fecha_ultima_revision"] is not None


def test_scan_producto_inexistente_404(client, auth_headers):
    response = client.post("/products/999999/scan", headers=auth_headers)
    assert response.status_code == 404


@patch("app.ml_client.requests.get")
def test_scan_all_un_producto_falla_otro_funciona(mock_get, client, auth_headers):
    mock_get.return_value = _make_ml_response(200, {"price": 500, "currency_id": "CLP", "title": "A"})
    crear_a = client.post(
        "/products",
        json={"url": "https://articulo.mercadolibre.cl/MLC-400000004-producto-a"},
        headers=auth_headers,
    )
    assert crear_a.status_code == 201

    mock_get.return_value = _make_ml_response(200, {"price": 700, "currency_id": "CLP", "title": "B"})
    crear_b = client.post(
        "/products",
        json={"url": "https://articulo.mercadolibre.cl/MLC-400000005-producto-b"},
        headers=auth_headers,
    )
    assert crear_b.status_code == 201

    id_a, id_b = crear_a.json()["id"], crear_b.json()["id"]

    # scan-all itera por Product.id ascendente (orden de creación: A, B).
    # A responde con éxito, B responde 404 (producto eliminado en la API).
    respuesta_ok = _make_ml_response(200, {"price": 555, "currency_id": "CLP", "title": "A"})
    respuesta_404 = _make_ml_response(404)
    mock_get.side_effect = [respuesta_ok, respuesta_404]

    resumen = client.post("/products/scan-all", headers=auth_headers)
    assert resumen.status_code == 200
    body = resumen.json()
    assert body["total"] == 2
    assert body["exitosos"] == 1
    assert body["fallidos"] == 1
    assert crear_b.json()["item_id"] in body["item_ids_fallidos"]

    listado = client.get("/products", headers=auth_headers).json()
    item_a = next(i for i in listado if i["id"] == id_a)
    item_b = next(i for i in listado if i["id"] == id_b)

    assert Decimal(str(item_a["precio_actual"])) == Decimal("555")  # se actualizó pese al fallo de B
    assert Decimal(str(item_b["precio_actual"])) == Decimal("700")  # quedó con el precio de creación


@patch("app.ml_client.requests.get")
def test_eliminar_producto_204_y_luego_404(mock_get, client, auth_headers):
    mock_get.return_value = _make_ml_response(200, {"price": 100, "currency_id": "CLP", "title": "C"})
    crear = client.post(
        "/products",
        json={"url": "https://articulo.mercadolibre.cl/MLC-500000006-producto-c"},
        headers=auth_headers,
    )
    assert crear.status_code == 201
    product_id = crear.json()["id"]

    eliminar = client.delete(f"/products/{product_id}", headers=auth_headers)
    assert eliminar.status_code == 204

    listado = client.get("/products", headers=auth_headers).json()
    assert all(item["id"] != product_id for item in listado)


def test_eliminar_producto_inexistente_404(client, auth_headers):
    response = client.delete("/products/999999", headers=auth_headers)
    assert response.status_code == 404


@pytest.mark.parametrize(
    "method,path,json_body",
    [
        ("get", "/products", None),
        ("post", "/products", {"url": "https://articulo.mercadolibre.cl/MLC-999999999-x"}),
        ("post", "/products/1/scan", None),
        ("post", "/products/scan-all", None),
        ("delete", "/products/1", None),
    ],
)
def test_endpoints_sin_token_devuelven_401(client, method, path, json_body):
    response = client.request(method, path, json=json_body)
    assert response.status_code == 401
