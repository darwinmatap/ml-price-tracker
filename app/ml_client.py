"""
Cliente HTTP para la API pública de Mercado Libre.

Toda persistencia se hace vía el ORM (ver app/models.py) — nunca SQL crudo.

Política de logging:
- Nunca se loguea product.url completa (puede no corresponder a lo que el
  usuario pegó, o filtrar detalles que no queremos en logs). Solo se
  loguea product.item_id, que es el identificador ya normalizado.
"""

import logging
import time
from decimal import Decimal
from typing import Optional

import requests
from sqlalchemy.orm import Session

from app.models import PriceCheck, Product

logger = logging.getLogger(__name__)

ML_API_BASE_URL = "https://api.mercadolibre.com"
REQUEST_TIMEOUT_SECONDS = 10
MAX_RETRIES = 1
DEFAULT_RETRY_AFTER_SECONDS = 5
MAX_RETRY_AFTER_SECONDS = 30


def _parse_retry_after(header_value: Optional[str]) -> int:
    """
    Interpreta el header Retry-After sin confiar ciegamente en un tercero:
    - Valor no numérico o corrupto -> DEFAULT_RETRY_AFTER_SECONDS.
    - Valor negativo -> también se considera corrupto -> default.
    - Cualquier valor válido se limita a MAX_RETRY_AFTER_SECONDS, para que
      la API externa no pueda dictar una espera arbitrariamente larga.
    """
    if header_value is None:
        return DEFAULT_RETRY_AFTER_SECONDS
    try:
        seconds = int(header_value)
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER_SECONDS
    if seconds < 0:
        return DEFAULT_RETRY_AFTER_SECONDS
    return min(seconds, MAX_RETRY_AFTER_SECONDS)


def _fetch_item_json(item_id: str) -> Optional[dict]:
    """
    Consulta GET /items/{item_id}. Devuelve el JSON de la respuesta o None
    si el item no existe, o si falló de forma no recuperable.

    Presupuesto de reintentos: máximo 1 reintento en total para esta
    llamada (ya sea por 429 o por timeout/error de conexión), para evitar
    loops de reintento indefinidos.
    """
    attempt = 0
    url = f"{ML_API_BASE_URL}/items/{item_id}"

    while True:
        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            if attempt >= MAX_RETRIES:
                logger.error(
                    "Error de conexión/timeout consultando Mercado Libre, sin más reintentos",
                    extra={"item_id": item_id, "error": str(exc)},
                )
                return None
            logger.warning(
                "Error de conexión/timeout consultando Mercado Libre, reintentando",
                extra={"item_id": item_id, "error": str(exc)},
            )
            attempt += 1
            continue
        except requests.exceptions.RequestException as exc:
            logger.error(
                "Error inesperado de requests consultando Mercado Libre",
                extra={"item_id": item_id, "error": str(exc)},
            )
            return None

        if response.status_code == 404:
            logger.warning(
                "El producto ya no existe o fue eliminado en Mercado Libre (404)",
                extra={"item_id": item_id},
            )
            return None

        if response.status_code == 429:
            if attempt >= MAX_RETRIES:
                logger.warning(
                    "Rate limit (429) tras agotar los reintentos, se descarta esta consulta",
                    extra={"item_id": item_id},
                )
                return None
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            logger.warning(
                "Rate limit (429), reintentando tras esperar",
                extra={"item_id": item_id, "retry_after_seconds": retry_after},
            )
            time.sleep(retry_after)
            attempt += 1
            continue

        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            logger.error(
                "Respuesta HTTP inesperada consultando Mercado Libre",
                extra={"item_id": item_id, "status_code": response.status_code, "error": str(exc)},
            )
            return None

        return response.json()


def _extract_price(item: dict) -> Optional[Decimal]:
    price = item.get("price")
    if price is not None:
        return Decimal(str(price))

    # Algunos productos con variaciones (talla, color, etc.) traen
    # "price" nulo a nivel raíz y el precio real vive por variación.
    # Por ahora tomamos la primera variación como aproximación del precio
    # del producto; si a futuro se necesita trackear precio por variación
    # individual, este es el punto a extender (guardar una fila por
    # variación en vez de solo variations[0]).
    variations = item.get("variations") or []
    if variations:
        variation_price = variations[0].get("price")
        if variation_price is not None:
            return Decimal(str(variation_price))

    return None


def fetch_and_store_price(db_session: Session, product: Product) -> Optional[PriceCheck]:
    """
    Consulta el precio actual de `product` en Mercado Libre y, si lo
    obtiene, lo guarda como un nuevo PriceCheck vía el ORM. Si product.title
    está vacío, lo completa con el título que venga en la respuesta.

    Devuelve el PriceCheck creado, o None si no se pudo obtener un precio
    válido (404, rate limit agotado, error de red, o respuesta sin precio).
    """
    item = _fetch_item_json(product.item_id)
    if item is None:
        return None

    price = _extract_price(item)
    if price is None:
        logger.warning(
            "La respuesta de Mercado Libre no trae un precio utilizable",
            extra={"item_id": product.item_id},
        )
        return None

    currency = item.get("currency_id")
    if not currency:
        logger.warning(
            "La respuesta de Mercado Libre no trae currency_id, se descarta la consulta",
            extra={"item_id": product.item_id},
        )
        return None

    if not product.title:
        title = item.get("title")
        if title:
            product.title = title

    price_check = PriceCheck(product=product, price=price, currency=currency)
    db_session.add(product)
    db_session.add(price_check)
    db_session.commit()
    db_session.refresh(price_check)

    logger.info(
        "Precio registrado",
        extra={"item_id": product.item_id, "price": str(price), "currency": currency},
    )
    return price_check
