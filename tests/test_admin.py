"""
Tests de los endpoints administrativos (app/admin.py) contra la app
FastAPI real vía TestClient. Todos requieren role="admin"
(Depends(require_admin), reutiliza get_current_user — no se mockea).
"""

from decimal import Decimal
from unittest.mock import patch

from app.models import UserRole
from tests.conftest import TEST_USERNAME, create_user
from tests.conftest import make_ml_response as _make_ml_response


def test_usuario_normal_no_puede_acceder_a_admin_403(client, auth_headers):
    response = client.get("/admin/users", headers=auth_headers)

    assert response.status_code == 403


def test_sin_token_admin_devuelve_401_no_403(client):
    # Sin autenticación, el fallo debe ser 401 (no autenticado), no 403
    # (autenticado pero sin permiso) — son cosas distintas.
    response = client.get("/admin/users")
    assert response.status_code == 401


def test_post_admin_users_crea_usuario_con_debe_cambiar_password_true(client, admin_auth_headers):
    response = client.post(
        "/admin/users",
        json={"username": "nuevo_usuario", "nombre": "Usuario Nuevo", "password": "Password-Temporal1"},
        headers=admin_auth_headers,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["username"] == "nuevo_usuario"
    assert body["nombre"] == "Usuario Nuevo"
    assert body["role"] == "user"
    assert body["is_active"] is True
    assert body["debe_cambiar_password"] is True
    assert "password_hash" not in body
    assert "password" not in body


def test_post_admin_users_username_duplicado_409(client, admin_auth_headers, test_user):
    response = client.post(
        "/admin/users",
        json={"username": TEST_USERNAME, "nombre": "Alguien", "password": "Password-Temporal1"},
        headers=admin_auth_headers,
    )

    assert response.status_code == 409


def test_get_admin_users_no_expone_password_hash(client, admin_auth_headers, test_user):
    response = client.get("/admin/users", headers=admin_auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert len(body) >= 2  # el admin y test_user
    for user in body:
        assert "password_hash" not in user
        assert "password" not in user


def test_patch_deactivate_usuario(client, admin_auth_headers, test_user):
    response = client.patch(f"/admin/users/{test_user.id}/deactivate", headers=admin_auth_headers)

    assert response.status_code == 200
    assert response.json()["is_active"] is False


def test_no_se_puede_desactivar_al_unico_admin_activo(client, admin_auth_headers, test_admin, db_session):
    response = client.patch(f"/admin/users/{test_admin.id}/deactivate", headers=admin_auth_headers)

    assert response.status_code == 409

    db_session.refresh(test_admin)
    assert test_admin.is_active is True


def test_con_dos_admins_activos_se_puede_desactivar_uno(client, admin_auth_headers, test_admin, db_session):
    segundo_admin = create_user(db_session, username="admin_dos", password="Adm1n-Dos-Valida1", role=UserRole.ADMIN)  # gitleaks:allow

    response = client.patch(f"/admin/users/{segundo_admin.id}/deactivate", headers=admin_auth_headers)

    assert response.status_code == 200
    assert response.json()["is_active"] is False

    db_session.refresh(test_admin)
    assert test_admin.is_active is True  # el otro admin no se ve afectado


def test_patch_deactivate_usuario_inexistente_404(client, admin_auth_headers):
    response = client.patch("/admin/users/999999/deactivate", headers=admin_auth_headers)

    assert response.status_code == 404


@patch("app.ml_client.requests.get")
def test_get_admin_products_trae_productos_de_mas_de_un_usuario_etiquetados(
    mock_get, client, admin_auth_headers, auth_headers, db_session
):
    mock_get.return_value = _make_ml_response(200, {"price": 1000, "currency_id": "CLP", "title": "Producto A"})
    crear_a = client.post(
        "/products",
        json={"url": "https://articulo.mercadolibre.cl/MLC-700000020-producto-a"},
        headers=auth_headers,
    )
    assert crear_a.status_code == 201

    otro = create_user(db_session, username="segundo_usuario", password="Otra-Clave-Valida2")  # gitleaks:allow
    otro_login = client.post(
        "/auth/login",
        json={"username": "segundo_usuario", "password": "Otra-Clave-Valida2"},  # gitleaks:allow
    )
    otro_headers = {"Authorization": f"Bearer {otro_login.json()['access_token']}"}

    mock_get.return_value = _make_ml_response(200, {"price": 2000, "currency_id": "ARS", "title": "Producto B"})
    crear_b = client.post(
        "/products",
        json={"url": "https://articulo.mercadolibre.cl/MLC-700000021-producto-b"},
        headers=otro_headers,
    )
    assert crear_b.status_code == 201

    response = client.get("/admin/products", headers=admin_auth_headers)
    assert response.status_code == 200
    body = response.json()

    item_a = next(i for i in body if i["id"] == crear_a.json()["id"])
    item_b = next(i for i in body if i["id"] == crear_b.json()["id"])

    assert item_a["username"] == TEST_USERNAME
    assert item_b["username"] == otro.username
    assert Decimal(str(item_a["precio_actual"])) == Decimal("1000")
    assert Decimal(str(item_b["precio_actual"])) == Decimal("2000")
