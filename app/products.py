"""
Endpoints CRUD de productos monitoreados.

Todos los endpoints de este router requieren autenticación (ver
dependencies=[Depends(get_current_user)] en la definición del router más
abajo) — se reutiliza la dependencia ya existente en app/auth.py, no se
reimplementa validación de tokens aquí.

Política de errores: ningún endpoint debe filtrar detalles internos
(tracebacks, mensajes crudos de SQLAlchemy) al cliente. Los errores
esperables (dominio no permitido, item_id duplicado, producto no
encontrado, fallo al consultar la API externa) se traducen a HTTPException
con un mensaje claro y un código HTTP apropiado.
"""

import logging
from datetime import datetime
from decimal import Decimal
from typing import List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.ml_client import fetch_and_store_price
from app.models import PriceCheck, Product
from app.utils import extract_product_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/products", tags=["products"], dependencies=[Depends(get_current_user)])

# --- Allowlist de dominios ---
#
# Solo se aceptan URLs de dominios de Mercado Libre reconocidos. Nunca se
# procesa una URL de un dominio fuera de esta lista, para evitar que el
# endpoint se use como proxy/SSRF hacia hosts arbitrarios.
#
# NOTA DE SEGURIDAD: la comparación es SIEMPRE sobre el hostname parseado
# por urlparse(url).hostname (nunca un "in"/substring sobre la URL
# completa), y es exacta o de sufijo con límite de punto explícito
# (host == dominio or host.endswith("." + dominio)). Un simple
# host.startswith("articulo.mercadolibre.") NO es seguro: no valida qué
# viene después del prefijo, así que "articulo.mercadolibre.atacante.com"
# lo pasaría (dominio real: atacante.com). Por eso solo se listan dominios
# completos aquí — para agregar un país nuevo, se agrega su dominio
# completo a este set, nunca un prefijo abierto.
ALLOWED_DOMAINS = {
    "mercadolibre.cl",
    "mercadolibre.com.ar",
    "mercadolibre.com.mx",
    "mercadolibre.com.co",
    "mercadolibre.com.pe",
    "mercadolibre.com.uy",
}


def _is_allowed_domain(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    return any(host == domain or host.endswith("." + domain) for domain in ALLOWED_DOMAINS)


# --- Esquemas ---


class ProductCreateRequest(BaseModel):
    # Una URL de Mercado Libre nunca debería ser tan corta como para ser
    # inválida de entrada (min_length) ni exceder un largo razonable
    # (max_length) — cortamos entradas absurdas antes de tocar regex/red.
    url: str = Field(min_length=10, max_length=500)


class ProductCreateResponse(BaseModel):
    id: int
    item_id: str
    url: str
    title: Optional[str] = None
    precio_actual: Optional[Decimal] = None
    moneda: Optional[str] = None
    precio_inicial_obtenido: bool
    advertencia: Optional[str] = None


class ProductListItem(BaseModel):
    id: int
    item_id: str
    url: str
    title: Optional[str] = None
    precio_actual: Optional[Decimal] = None
    precio_anterior: Optional[Decimal] = None
    moneda: Optional[str] = None
    fecha_ultima_revision: Optional[datetime] = None


class PriceCheckOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    price: Decimal
    currency: str
    checked_at: datetime


class ScanAllResult(BaseModel):
    total: int
    exitosos: int
    fallidos: int
    item_ids_fallidos: List[str]


# --- Endpoints ---


@router.post("", response_model=ProductCreateResponse, status_code=201)
def create_product(payload: ProductCreateRequest, db: Session = Depends(get_db)):
    if not _is_allowed_domain(payload.url):
        raise HTTPException(
            status_code=422,
            detail=(
                "La URL debe pertenecer a un dominio de Mercado Libre reconocido "
                "(ej. articulo.mercadolibre.cl, mercadolibre.com.ar)."
            ),
        )

    try:
        item_id = extract_product_id(payload.url)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="No se pudo determinar el ID del producto a partir de la URL.",
        )

    existing_product = db.execute(select(Product).where(Product.item_id == item_id)).scalar_one_or_none()
    if existing_product is not None:
        raise HTTPException(status_code=409, detail=f"El producto {item_id} ya está siendo monitoreado.")

    product = Product(item_id=item_id, url=payload.url)
    db.add(product)
    try:
        db.commit()
    except IntegrityError:
        # Red de seguridad ante condiciones de carrera: el chequeo de
        # arriba no es atómico con este insert. El constraint UNIQUE de
        # la base es la fuente de verdad real.
        db.rollback()
        raise HTTPException(status_code=409, detail=f"El producto {item_id} ya está siendo monitoreado.")
    db.refresh(product)

    try:
        price_check = fetch_and_store_price(db, product)
    except Exception:
        # fetch_and_store_price ya maneja sus propios errores de red
        # devolviendo None; esto solo cubre un fallo inesperado (p.ej. de
        # base de datos) durante ese paso. El producto ya quedó creado y
        # comiteado antes de este bloque, así que no hay nada que
        # revertir: solo se informa que no se pudo obtener el precio.
        logger.exception(
            "Error inesperado obteniendo el precio inicial",
            extra={"item_id": product.item_id},
        )
        db.rollback()
        price_check = None

    return ProductCreateResponse(
        id=product.id,
        item_id=product.item_id,
        url=product.url,
        title=product.title,
        precio_actual=price_check.price if price_check else None,
        moneda=price_check.currency if price_check else None,
        precio_inicial_obtenido=price_check is not None,
        advertencia=(
            None
            if price_check is not None
            else "No se pudo obtener el precio inicial del producto en este momento."
        ),
    )


def _ranked_price_checks_subquery():
    """
    Subquery con row_number() particionado por producto y ordenado por
    fecha descendente: rn=1 es el PriceCheck más reciente, rn=2 el
    anterior. Se usa dos veces (alias distintos) en list_products para
    traer precio_actual y precio_anterior en una sola query, sin N+1.
    """
    return select(
        PriceCheck.product_id,
        PriceCheck.price,
        PriceCheck.currency,
        PriceCheck.checked_at,
        func.row_number()
        .over(partition_by=PriceCheck.product_id, order_by=PriceCheck.checked_at.desc())
        .label("rn"),
    ).subquery()


@router.get("", response_model=List[ProductListItem])
def list_products(db: Session = Depends(get_db)):
    latest = _ranked_price_checks_subquery()
    previous = _ranked_price_checks_subquery()

    query = (
        select(
            Product,
            latest.c.price,
            latest.c.currency,
            latest.c.checked_at,
            previous.c.price,
        )
        .outerjoin(latest, (latest.c.product_id == Product.id) & (latest.c.rn == 1))
        .outerjoin(previous, (previous.c.product_id == Product.id) & (previous.c.rn == 2))
        .order_by(Product.id)
    )

    rows = db.execute(query).all()

    return [
        ProductListItem(
            id=product.id,
            item_id=product.item_id,
            url=product.url,
            title=product.title,
            precio_actual=precio_actual,
            precio_anterior=precio_anterior,
            moneda=moneda,
            fecha_ultima_revision=fecha_ultima_revision,
        )
        for product, precio_actual, moneda, fecha_ultima_revision, precio_anterior in rows
    ]


@router.post("/scan-all", response_model=ScanAllResult)
def scan_all_products(db: Session = Depends(get_db)):
    products = db.execute(select(Product).order_by(Product.id)).scalars().all()

    exitosos = 0
    fallidos_item_ids: List[str] = []

    for product in products:
        try:
            price_check = fetch_and_store_price(db, product)
        except Exception:
            # No se aborta el batch por un producto que falle: se loguea,
            # se limpia el estado de la sesión (una excepción a mitad de
            # un commit deja la transacción inválida para el resto de
            # queries) y se sigue con el siguiente producto.
            logger.exception(
                "Error inesperado escaneando producto durante scan-all",
                extra={"item_id": product.item_id},
            )
            db.rollback()
            price_check = None

        if price_check is not None:
            exitosos += 1
        else:
            fallidos_item_ids.append(product.item_id)

    return ScanAllResult(
        total=len(products),
        exitosos=exitosos,
        fallidos=len(fallidos_item_ids),
        item_ids_fallidos=fallidos_item_ids,
    )


@router.post("/{product_id}/scan", response_model=PriceCheckOut)
def scan_product(product_id: int, db: Session = Depends(get_db)):
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Producto no encontrado.")

    try:
        price_check = fetch_and_store_price(db, product)
    except Exception:
        logger.exception(
            "Error inesperado escaneando producto",
            extra={"item_id": product.item_id},
        )
        db.rollback()
        price_check = None

    if price_check is None:
        raise HTTPException(
            status_code=502,
            detail="No se pudo obtener el precio actualizado del producto en este momento.",
        )

    return price_check


@router.delete("/{product_id}", status_code=204)
def delete_product(product_id: int, db: Session = Depends(get_db)):
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Producto no encontrado.")
    db.delete(product)
    db.commit()
