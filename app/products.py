"""
Endpoints CRUD de productos monitoreados.

Todos los endpoints de este router requieren autenticación (ver
dependencies=[Depends(get_current_user)] en la definición del router más
abajo) — se reutiliza la dependencia ya existente en app/auth.py, no se
reimplementa validación de tokens aquí.

Aislamiento multi-tenant: cada producto pertenece a un user_id. Todo
endpoint que lea, escanee o borre un producto por id filtra explícitamente
por Product.user_id == current_user.id — nunca se confía solo en el id
de la URL. Si el id pertenece a otro usuario, la respuesta es 404 (no
403): admitir "existe pero no es tuyo" ya sería filtrar información sobre
datos de otro usuario.

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
from app.models import PriceCheck, Product, User
from app.scheduler import run_scan_all
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
    # No incluye user_id: el dueño del producto es SIEMPRE el usuario
    # autenticado, nunca algo que el cliente pueda elegir en el body.
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
def create_product(
    payload: ProductCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
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

    # El UNIQUE es compuesto (user_id, item_id): dos usuarios distintos
    # pueden monitorear el mismo producto de Mercado Libre de forma
    # independiente, cada uno con su propia fila. Lo único que no puede
    # pasar es que ESTE usuario duplique su propio item_id.
    existing_product = db.execute(
        select(Product).where(Product.user_id == current_user.id, Product.item_id == item_id)
    ).scalar_one_or_none()
    if existing_product is not None:
        raise HTTPException(status_code=409, detail=f"El producto {item_id} ya está siendo monitoreado.")

    product = Product(item_id=item_id, url=payload.url, user_id=current_user.id)
    db.add(product)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # No asumir que la IntegrityError fue el UNIQUE de (user_id,
        # item_id): se reconsulta para confirmar la causa real en vez de
        # adivinar a partir del texto de la excepción (que varía entre
        # Postgres y SQLite, y es información interna que no queremos
        # parsear).
        conflicto = db.execute(
            select(Product).where(Product.user_id == current_user.id, Product.item_id == item_id)
        ).scalar_one_or_none()
        if conflicto is not None:
            raise HTTPException(status_code=409, detail=f"El producto {item_id} ya está siendo monitoreado.")

        # Cualquier otro motivo (FK inválida, columna NOT NULL sin
        # completar, etc.) es un fallo real del servidor: se loguea con
        # detalle y se responde genérico, sin exponerlo al cliente.
        logger.exception(
            "IntegrityError inesperada creando producto (no fue un item_id duplicado)",
            extra={"item_id": item_id, "user_id": current_user.id},
        )
        raise HTTPException(status_code=500, detail="No se pudo crear el producto en este momento.")
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


def ranked_price_checks_subquery():
    """
    Subquery con row_number() particionado por producto y ordenado por
    fecha descendente: rn=1 es el PriceCheck más reciente, rn=2 el
    anterior. Se usa dos veces (alias distintos) para traer precio_actual
    y precio_anterior en una sola query, sin N+1. La reutiliza también
    app/admin.py para su listado global de productos.
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
def list_products(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    latest = ranked_price_checks_subquery()
    previous = ranked_price_checks_subquery()

    query = (
        select(
            Product,
            latest.c.price,
            latest.c.currency,
            latest.c.checked_at,
            previous.c.price,
        )
        .where(Product.user_id == current_user.id)
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
def scan_all_products(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    # La lógica vive en app/scheduler.py (run_scan_all): la comparte este
    # endpoint y el job programado, no se duplica. Acá se acota a los
    # productos del usuario autenticado; el job programado (sin user_id)
    # escanea los de todos.
    summary = run_scan_all(db, user_id=current_user.id)
    return ScanAllResult(**summary)


@router.post("/{product_id}/scan", response_model=PriceCheckOut)
def scan_product(product_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    product = db.execute(
        select(Product).where(Product.id == product_id, Product.user_id == current_user.id)
    ).scalar_one_or_none()
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
def delete_product(product_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    product = db.execute(
        select(Product).where(Product.id == product_id, Product.user_id == current_user.id)
    ).scalar_one_or_none()
    if product is None:
        raise HTTPException(status_code=404, detail="Producto no encontrado.")
    db.delete(product)
    db.commit()
