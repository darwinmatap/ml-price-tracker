"""
Cliente HTTP para la API pública de Mercado Libre.

Toda persistencia se hace vía el ORM (ver app/models.py) — nunca SQL crudo.

Política de logging:
- Nunca se loguea product.url completa (puede no corresponder a lo que el
  usuario pegó, o filtrar detalles que no queremos en logs). Solo se
  loguea product.item_id, que es el identificador ya normalizado.
- Tampoco se loguea ni se expone nunca el access_token o refresh_token de
  Mercado Libre completos. Si hace falta loguear algo para debug, se
  trunca a los primeros 8 caracteres (intencional, ver _truncate_secret).
"""

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import MeliOAuthToken, PriceCheck, Product

logger = logging.getLogger(__name__)

ML_API_BASE_URL = "https://api.mercadolibre.com"
REQUEST_TIMEOUT_SECONDS = 10
MAX_RETRIES = 1
DEFAULT_RETRY_AFTER_SECONDS = 5
MAX_RETRY_AFTER_SECONDS = 30

MELI_TOKEN_URL = "https://api.mercadolibre.com/oauth/token"  # nosec B105 # es una URL, no un secreto
MELI_CLIENT_ID = os.environ.get("MELI_CLIENT_ID")
MELI_CLIENT_SECRET = os.environ.get("MELI_CLIENT_SECRET")
if not MELI_CLIENT_ID or not MELI_CLIENT_SECRET:
    raise RuntimeError(
        "MELI_CLIENT_ID y MELI_CLIENT_SECRET deben estar definidas como "
        "variables de entorno (ver .env.example) antes de iniciar la aplicación."
    )

# Margen de seguridad: si al access_token le quedan menos de 5 minutos de
# vida, se renueva de una vez en vez de arriesgarse a que expire a mitad
# de la consulta siguiente.
TOKEN_EXPIRY_SAFETY_MARGIN = timedelta(minutes=5)


class MeliNotConnectedError(Exception):
    """
    La app todavía no tiene ninguna fila de MeliOAuthToken guardada (nunca
    se completó el flujo de GET /admin/meli/connect). Es un fallo
    controlado: el caller debe manejarlo como cualquier otro fallo al
    consultar Mercado Libre, no dejar que se propague como un crash.
    """


def _truncate_secret(value: str) -> str:
    """Trunca un secreto a 8 caracteres para poder loguearlo sin exponerlo completo (intencional)."""
    return value[:8] + "..."


def _as_aware_utc(value: datetime) -> datetime:
    """
    Normaliza a tz-aware UTC. SQLite (usado en tests) no preserva tzinfo
    al leer una columna DateTime(timezone=True) — devuelve un datetime
    naive aunque el valor almacenado siempre haya sido UTC (ver _utcnow
    en app/models.py) — así que un naive leído de la base se interpreta
    como UTC en vez de comparar naive contra aware y romper.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _refresh_meli_token(db_session: Session, token_row: MeliOAuthToken) -> MeliOAuthToken:
    payload = {
        "grant_type": "refresh_token",
        "client_id": MELI_CLIENT_ID,
        "client_secret": MELI_CLIENT_SECRET,
        "refresh_token": token_row.refresh_token,
    }
    response = requests.post(MELI_TOKEN_URL, data=payload, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    token_data = response.json()

    token_row.access_token = token_data["access_token"]
    # Mercado Libre puede devolver un refresh_token nuevo: hay que guardar
    # ese y dejar de usar el viejo. Si no viene uno nuevo, se mantiene el actual.
    token_row.refresh_token = token_data.get("refresh_token", token_row.refresh_token)
    token_row.expires_at = datetime.now(timezone.utc) + timedelta(seconds=token_data.get("expires_in", 0))

    db_session.add(token_row)
    db_session.commit()
    db_session.refresh(token_row)

    logger.info(
        "Token de Mercado Libre renovado automáticamente",
        extra={"access_token_preview": _truncate_secret(token_row.access_token)},
    )
    return token_row


def get_valid_meli_access_token(db_session: Session) -> str:
    """
    Devuelve un access_token vigente para consultar la API de Mercado
    Libre, renovándolo automáticamente si ya expiró o le queda menos de
    TOKEN_EXPIRY_SAFETY_MARGIN de vida.

    Raises:
        MeliNotConnectedError: si la app todavía no está conectada (ver
            arriba). El caller debe tratarlo como un fallo controlado.
    """
    token_row = db_session.execute(select(MeliOAuthToken)).scalar_one_or_none()
    if token_row is None:
        raise MeliNotConnectedError(
            "La app todavía no está conectada a Mercado Libre. Un administrador debe "
            "completar el flujo en GET /admin/meli/connect."
        )

    if _as_aware_utc(token_row.expires_at) - datetime.now(timezone.utc) <= TOKEN_EXPIRY_SAFETY_MARGIN:
        token_row = _refresh_meli_token(db_session, token_row)

    return token_row.access_token


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


def _fetch_item_json(item_id: str, access_token: str) -> Optional[dict]:
    """
    Consulta GET /items/{item_id}. Devuelve el JSON de la respuesta o None
    si el item no existe, o si falló de forma no recuperable.

    Presupuesto de reintentos: máximo 1 reintento en total para esta
    llamada (ya sea por 429 o por timeout/error de conexión), para evitar
    loops de reintento indefinidos.
    """
    attempt = 0
    url = f"{ML_API_BASE_URL}/items/{item_id}"
    headers = {"Authorization": f"Bearer {access_token}"}

    while True:
        try:
            response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
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
    válido (404, rate limit agotado, error de red, respuesta sin precio,
    o la app todavía no está conectada a Mercado Libre / falló la
    renovación del token).
    """
    try:
        access_token = get_valid_meli_access_token(db_session)
    except MeliNotConnectedError as exc:
        logger.warning(
            "No se pudo consultar el precio: la app no está conectada a Mercado Libre",
            extra={"item_id": product.item_id, "error": str(exc)},
        )
        return None
    except requests.exceptions.RequestException as exc:
        logger.error(
            "No se pudo renovar el token de Mercado Libre",
            extra={"item_id": product.item_id, "error": str(exc)},
        )
        return None

    item = _fetch_item_json(product.item_id, access_token)
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
