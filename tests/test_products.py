"""
Tests de los endpoints CRUD de productos (app/products.py) contra la app
FastAPI real vía TestClient. La API de Mercado Libre se mockea siempre
(unittest.mock, nunca red real); la autenticación es real (login vía
/auth/login para obtener el token, get_current_user no se mockea).

Los fixtures client/auth_headers y el helper _make_ml_response viven en
tests/conftest.py (compartidos con tests/test_scheduler.py).
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from tests.conftest import create_user
from tests.conftest import make_ml_response as _make_ml_response


def _login_as(client, db_session, username, password="Otra-Clave-Valida1"):  # gitleaks:allow -- password sintética de fixture de test
    create_user(db_session, username=username, password=password)
    response = client.post("/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


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


@patch("app.url_resolver.requests.head")
@patch("app.url_resolver.socket.getaddrinfo")
@patch("app.ml_client.requests.get")
def test_agregar_producto_via_meli_la_resuelve_y_crea(mock_get, mock_getaddrinfo, mock_head, client, auth_headers):
    import socket

    destino_final = "https://articulo.mercadolibre.cl/MLC-200000123-audifonos"

    def _fake_getaddrinfo(host, *args, **kwargs):
        ip = {"meli.la": "93.184.216.10", "articulo.mercadolibre.cl": "93.184.216.20"}[host]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    mock_getaddrinfo.side_effect = _fake_getaddrinfo

    redirect_response = MagicMock(status_code=302, headers={"Location": destino_final}, is_redirect=True)
    final_response = MagicMock(status_code=200, headers={}, is_redirect=False)
    mock_head.side_effect = [redirect_response, final_response]

    mock_get.return_value = _make_ml_response(200, {"price": 4990, "currency_id": "CLP", "title": "Audífonos"})

    response = client.post("/products", json={"url": "https://meli.la/1BP7AeP"}, headers=auth_headers)

    assert response.status_code == 201
    body = response.json()
    assert body["item_id"] == "MLC200000123"
    assert body["url"] == destino_final
    assert Decimal(str(body["precio_actual"])) == Decimal("4990")


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
def test_dos_usuarios_distintos_pueden_agregar_el_mismo_item_id(mock_get, client, auth_headers, db_session):
    mock_get.return_value = _make_ml_response(200, {"price": 1000, "currency_id": "CLP", "title": "Producto"})
    url = "https://articulo.mercadolibre.cl/MLC-200000099-producto-compartido"

    primero = client.post("/products", json={"url": url}, headers=auth_headers)
    assert primero.status_code == 201

    otro_headers = _login_as(client, db_session, "usuario_compartido")
    segundo = client.post("/products", json={"url": url}, headers=otro_headers)

    assert segundo.status_code == 201
    assert primero.json()["item_id"] == segundo.json()["item_id"] == "MLC200000099"
    assert primero.json()["id"] != segundo.json()["id"]  # dos filas independientes


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


@patch("app.ml_client.requests.get")
def test_usuario_no_puede_ver_escanear_ni_borrar_producto_de_otro_404(mock_get, client, auth_headers, db_session):
    mock_get.return_value = _make_ml_response(200, {"price": 1000, "currency_id": "CLP", "title": "Producto de A"})
    crear = client.post(
        "/products",
        json={"url": "https://articulo.mercadolibre.cl/MLC-600000011-producto-a"},
        headers=auth_headers,
    )
    assert crear.status_code == 201
    product_id = crear.json()["id"]

    otro_headers = _login_as(client, db_session, "otro_usuario")

    # No aparece en el listado del otro usuario.
    listado_otro = client.get("/products", headers=otro_headers).json()
    assert all(item["id"] != product_id for item in listado_otro)

    # Scan del producto ajeno -> 404 (no 403: no se revela que existe).
    scan = client.post(f"/products/{product_id}/scan", headers=otro_headers)
    assert scan.status_code == 404
    mock_get.assert_called_once()  # solo la llamada de la creación original, el scan ajeno no llegó a la API

    # Delete del producto ajeno -> 404 (no 403).
    borrar = client.delete(f"/products/{product_id}", headers=otro_headers)
    assert borrar.status_code == 404

    # El producto sigue existiendo, intacto, para su dueño real.
    listado_dueno = client.get("/products", headers=auth_headers).json()
    assert any(item["id"] == product_id for item in listado_dueno)


@patch("app.ml_client.requests.get")
def test_integrity_error_no_relacionada_a_duplicado_responde_500(mock_get, client, auth_headers, db_session):
    # Simula una IntegrityError que NO es por el UNIQUE de item_id (p.ej.
    # una FK inválida u otra constraint) forzando que commit() falle,
    # sin que exista ningún producto previo con ese item_id.
    with patch.object(
        db_session,
        "commit",
        side_effect=IntegrityError("INSERT INTO products ...", {}, Exception("constraint no relacionada")),
    ):
        response = client.post(
            "/products",
            json={"url": "https://articulo.mercadolibre.cl/MLC-999999998-x"},
            headers=auth_headers,
        )

    assert response.status_code == 500
    assert response.json()["detail"] == "No se pudo crear el producto en este momento."
    mock_get.assert_not_called()  # nunca se llegó a consultar el precio


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
