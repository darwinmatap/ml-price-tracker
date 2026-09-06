"""
Endpoints administrativos: gestión de usuarios y vista global de productos.

Todo este router requiere role == "admin" (Depends(require_admin), en
bloque a nivel de router — mismo patrón que app/products.py usa con
get_current_user). No hay registro público: solo un admin puede crear
usuarios nuevos.
"""

import logging
from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy import func, select

from app.auth import pwd_context, require_admin
from app.database import get_db
from app.models import Product, User, UserRole
from app.products import ranked_price_checks_subquery

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


# --- Esquemas ---


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=3, max_length=150)
    # Password TEMPORAL: el usuario queda con debe_cambiar_password=True.
    password: str = Field(min_length=8, max_length=200)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    role: UserRole
    is_active: bool
    debe_cambiar_password: bool
    created_at: datetime


class AdminProductListItem(BaseModel):
    id: int
    item_id: str
    url: str
    title: Optional[str] = None
    username: str
    precio_actual: Optional[Decimal] = None
    precio_anterior: Optional[Decimal] = None
    moneda: Optional[str] = None
    fecha_ultima_revision: Optional[datetime] = None


# --- Endpoints: usuarios ---


@router.post("/users", response_model=UserOut, status_code=201)
def create_user(payload: CreateUserRequest, db: Session = Depends(get_db)):
    existing = db.execute(select(User).where(User.username == payload.username)).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"El username '{payload.username}' ya existe.")

    user = User(
        username=payload.username,
        password_hash=pwd_context.hash(payload.password),
        role=UserRole.USER,
        is_active=True,
        debe_cambiar_password=True,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"El username '{payload.username}' ya existe.")
    db.refresh(user)

    return user


@router.get("/users", response_model=List[UserOut])
def list_users(db: Session = Depends(get_db)):
    users = db.execute(select(User).order_by(User.id)).scalars().all()
    return users


@router.patch("/users/{user_id}/deactivate", response_model=UserOut)
def deactivate_user(user_id: int, db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")

    if user.role == UserRole.ADMIN:
        active_admin_count = db.execute(
            select(func.count()).select_from(User).where(User.role == UserRole.ADMIN, User.is_active)
        ).scalar_one()
        if active_admin_count <= 1:
            raise HTTPException(
                status_code=409,
                detail="No se puede desactivar al único administrador activo.",
            )

    user.is_active = False
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


# --- Endpoints: vista global de productos ---


@router.get("/products", response_model=List[AdminProductListItem])
def list_all_products(db: Session = Depends(get_db)):
    latest = ranked_price_checks_subquery()
    previous = ranked_price_checks_subquery()

    query = (
        select(
            Product,
            User.username,
            latest.c.price,
            latest.c.currency,
            latest.c.checked_at,
            previous.c.price,
        )
        .join(User, User.id == Product.user_id)
        .outerjoin(latest, (latest.c.product_id == Product.id) & (latest.c.rn == 1))
        .outerjoin(previous, (previous.c.product_id == Product.id) & (previous.c.rn == 2))
        .order_by(Product.id)
    )

    rows = db.execute(query).all()

    return [
        AdminProductListItem(
            id=product.id,
            item_id=product.item_id,
            url=product.url,
            title=product.title,
            username=username,
            precio_actual=precio_actual,
            precio_anterior=precio_anterior,
            moneda=moneda,
            fecha_ultima_revision=fecha_ultima_revision,
        )
        for product, username, precio_actual, moneda, fecha_ultima_revision, precio_anterior in rows
    ]
