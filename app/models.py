"""
Modelos de datos de la aplicación (SQLAlchemy ORM).

POLÍTICA DE ACCESO A DATOS — léase antes de tocar este código:
Todo acceso a la base de datos debe hacerse vía el ORM de SQLAlchemy
(Session.query, sqlalchemy.select, relaciones declaradas, etc.).
Está PROHIBIDO construir SQL a mano con f-strings, %-formatting o
concatenación de strings: eso abre la puerta a inyección SQL. Si en algún
caso excepcional se necesita SQL crudo, debe pasar por sqlalchemy.text()
con parámetros bindeados (nunca interpolando valores directamente en el
string), y requiere revisión explícita de seguridad antes de mergear.

El esquema se versiona con Alembic (ver alembic/). No usar Base.metadata.create_all()
para crear o modificar tablas fuera de las migraciones.
"""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import relationship

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, autoincrement=True)
    item_id = Column(String, unique=True, nullable=False, index=True)
    url = Column(String, nullable=False)
    title = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    price_checks = relationship(
        "PriceCheck",
        back_populates="product",
        cascade="all, delete-orphan",
    )


class PriceCheck(Base):
    __tablename__ = "price_checks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    product_id = Column(
        Integer,
        ForeignKey("products.id", ondelete="CASCADE"),
        nullable=False,
    )
    price = Column(Numeric(12, 2), nullable=False)
    currency = Column(String, nullable=False)
    checked_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        index=True,
    )

    product = relationship("Product", back_populates="price_checks")
