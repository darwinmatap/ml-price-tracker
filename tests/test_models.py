"""
Tests de los modelos ORM (User, Product, PriceCheck) contra una base
SQLite en memoria, aislada por test. No tocan la base de datos real de
la app.
"""

from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import PriceCheck, Product, User, UserRole

# La fixture db_session vive en tests/conftest.py (compartida con test_ml_client.py).


def _make_user(db_session, username="usuario-de-prueba"):
    user = User(username=username, nombre="Usuario de Prueba", password_hash="hash-de-prueba", role=UserRole.USER)
    db_session.add(user)
    db_session.commit()
    return user


def test_no_permite_item_id_duplicado(db_session):
    user = _make_user(db_session)

    db_session.add(Product(item_id="MLC123456789", url="https://articulo.mercadolibre.cl/1", user=user))
    db_session.commit()

    db_session.add(Product(item_id="MLC123456789", url="https://articulo.mercadolibre.cl/2", user=user))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_borrar_product_borra_price_checks_en_cascada(db_session):
    user = _make_user(db_session)

    product = Product(item_id="MLC999999999", url="https://articulo.mercadolibre.cl/3", user=user)
    db_session.add(product)
    db_session.commit()

    db_session.add_all(
        [
            PriceCheck(product_id=product.id, price=Decimal("19990.50"), currency="CLP"),
            PriceCheck(product_id=product.id, price=Decimal("18990.00"), currency="CLP"),
        ]
    )
    db_session.commit()
    assert db_session.query(PriceCheck).count() == 2

    db_session.delete(product)
    db_session.commit()

    assert db_session.query(PriceCheck).count() == 0


def test_price_check_requiere_product_existente(db_session):
    db_session.add(PriceCheck(product_id=999999, price=Decimal("1000"), currency="CLP"))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_no_permite_username_duplicado(db_session):
    db_session.add(User(username="admin", nombre="Admin Uno", password_hash="hash1", role=UserRole.ADMIN))
    db_session.commit()

    db_session.add(User(username="admin", nombre="Admin Dos", password_hash="hash2", role=UserRole.USER))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_product_requiere_user_id(db_session):
    # Sin user=... ni user_id=..., la columna NOT NULL debe rechazar el insert.
    db_session.add(Product(item_id="MLC000000001", url="https://articulo.mercadolibre.cl/x"))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_borrar_user_borra_en_cascada_products_y_sus_price_checks(db_session):
    user = _make_user(db_session)

    producto = Product(item_id="MLC000000002", url="https://articulo.mercadolibre.cl/y", user=user)
    db_session.add(producto)
    db_session.commit()

    db_session.add(PriceCheck(product_id=producto.id, price=Decimal("1000"), currency="CLP"))
    db_session.commit()

    assert db_session.query(Product).count() == 1
    assert db_session.query(PriceCheck).count() == 1

    db_session.delete(user)
    db_session.commit()

    # Cascada de dos niveles: al borrar el User se borra su Product, y al
    # borrarse el Product se borran sus PriceCheck.
    assert db_session.query(Product).count() == 0
    assert db_session.query(PriceCheck).count() == 0
