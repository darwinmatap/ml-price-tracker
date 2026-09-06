"""
Tests de los modelos ORM (Product, PriceCheck) contra una base SQLite
en memoria, aislada por test. No tocan la base de datos real de la app.
"""

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import PriceCheck, Product


@pytest.fixture()
def db_session():
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


def test_no_permite_item_id_duplicado(db_session):
    db_session.add(Product(item_id="MLC123456789", url="https://articulo.mercadolibre.cl/1"))
    db_session.commit()

    db_session.add(Product(item_id="MLC123456789", url="https://articulo.mercadolibre.cl/2"))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_borrar_product_borra_price_checks_en_cascada(db_session):
    product = Product(item_id="MLC999999999", url="https://articulo.mercadolibre.cl/3")
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
