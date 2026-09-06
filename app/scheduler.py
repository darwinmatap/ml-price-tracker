"""
Escaneo automático periódico de precios (APScheduler).

run_scan_all() contiene la lógica de "escanear todos los productos" y es
la ÚNICA implementación: la reutilizan tanto el endpoint
POST /products/scan-all (app/products.py) como el job programado de este
módulo. No se duplica esta lógica en ningún otro lugar.

Política de sesiones de base de datos:
- El job programado (_scan_all_job) abre su PROPIA sesión por ejecución
  (SessionLocal()), nunca reutiliza una sesión compartida con requests
  HTTP — evita que una corrida en background y un request concurrente
  se pisen sobre el mismo estado de transacción. La sesión se cierra
  siempre en un finally, incluso si la ejecución falla.

Política de errores del job:
- Si run_scan_all lanza una excepción no controlada (ej. la base de datos
  no responde), _scan_all_job la captura, la loguea con severidad alta,
  y NO la deja propagarse. Una excepción sin capturar en un job de
  APScheduler no tumba el proceso de FastAPI, pero si no se maneja acá
  explícitamente se pierde el contexto (item_id, motivo) y, según la
  configuración del scheduler, puede afectar la programación de la
  siguiente corrida — mejor capturarla y seguir.
"""

import logging
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.ml_client import fetch_and_store_price
from app.models import Product

logger = logging.getLogger(__name__)

SCAN_ALL_JOB_ID = "scan_all_products_hourly"


def run_scan_all(db_session: Session, user_id: Optional[int] = None) -> dict:
    """
    Escanea productos y guarda su precio actual vía fetch_and_store_price.
    Sigue procesando el resto aunque uno falle.

    user_id=None (uso del job programado): escanea los productos de TODOS
    los usuarios. user_id=<id> (uso del endpoint POST /products/scan-all):
    escanea solo los productos de ese usuario.

    Devuelve un resumen:
    {"total": int, "exitosos": int, "fallidos": int, "item_ids_fallidos": [str, ...]}
    """
    query = select(Product).order_by(Product.id)
    if user_id is not None:
        query = query.where(Product.user_id == user_id)
    products = db_session.execute(query).scalars().all()

    exitosos = 0
    fallidos_item_ids: list = []

    for product in products:
        try:
            price_check = fetch_and_store_price(db_session, product)
        except Exception:
            # No se aborta el batch por un producto que falle: se loguea,
            # se limpia el estado de la sesión (una excepción a mitad de
            # un commit deja la transacción inválida para el resto de
            # queries) y se sigue con el siguiente producto.
            logger.exception(
                "Error inesperado escaneando producto durante scan-all",
                extra={"item_id": product.item_id},
            )
            db_session.rollback()
            price_check = None

        if price_check is not None:
            exitosos += 1
        else:
            fallidos_item_ids.append(product.item_id)

    return {
        "total": len(products),
        "exitosos": exitosos,
        "fallidos": len(fallidos_item_ids),
        "item_ids_fallidos": fallidos_item_ids,
    }


def _scan_all_job() -> None:
    """Wrapper invocado por el scheduler: sesión propia, cierre garantizado, sin excepciones que escapen."""
    db_session = SessionLocal()
    try:
        summary = run_scan_all(db_session)
        logger.info("Escaneo automático programado completado", extra=summary)
    except Exception:
        logger.error(
            "Fallo no controlado durante el escaneo automático programado",
            exc_info=True,
            extra={"severity": "high"},
        )
    finally:
        db_session.close()


def create_scheduler() -> BackgroundScheduler:
    """
    Crea (sin iniciar) un BackgroundScheduler con el job de escaneo cada
    hora. max_instances=1 evita que dos corridas se solapen si una se
    demora más de una hora; coalesce=True hace que, si el proceso estuvo
    caído y se perdieron ejecuciones, al volver se corra solo una vez
    (la más reciente pendiente) en vez de todas las perdidas de golpe.
    """
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        _scan_all_job,
        trigger="interval",
        hours=1,
        id=SCAN_ALL_JOB_ID,
        max_instances=1,
        coalesce=True,
    )
    return scheduler
