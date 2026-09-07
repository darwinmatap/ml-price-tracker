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

import enum
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Enum, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import relationship

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    USER = "user"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String, unique=True, nullable=False, index=True)
    nombre = Column(String, nullable=False)
    password_hash = Column(String, nullable=False)
    role = Column(
        Enum(UserRole, name="user_role", values_callable=lambda enum_cls: [e.value for e in enum_cls]),
        nullable=False,
        default=UserRole.USER,
    )
    is_active = Column(Boolean, nullable=False, default=True)
    debe_cambiar_password = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    products = relationship(
        "Product",
        back_populates="user",
        cascade="all, delete-orphan",
    )


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        # Dos usuarios distintos pueden monitorear el mismo producto de
        # Mercado Libre de forma independiente (cada uno con su propia
        # fila); lo que NO puede pasar es que el mismo usuario duplique
        # su propio item_id. Por eso el UNIQUE es compuesto, no solo
        # sobre item_id.
        UniqueConstraint("user_id", "item_id", name="uq_products_user_id_item_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    item_id = Column(String, nullable=False)
    url = Column(String, nullable=False)
    title = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    user = relationship("User", back_populates="products")
    price_checks = relationship(
        "PriceCheck",
        back_populates="product",
        cascade="all, delete-orphan",
    )


class MeliOAuthToken(Base):
    """
    Conexión OAuth 2.0 de la app con Mercado Libre (no es por-usuario):
    existe una sola fila, la credencial que la app usa para consultar la
    API de Mercado Libre autenticada. Se crea/actualiza desde
    app/meli_oauth.py al completar el flujo de authorization code, y se
    renueva automáticamente desde app/ml_client.py cuando expires_at está
    por vencer.
    """

    __tablename__ = "meli_oauth_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    access_token = Column(String, nullable=False)
    refresh_token = Column(String, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


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
