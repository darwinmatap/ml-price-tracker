"""
Conexión OAuth 2.0 de la app con Mercado Libre (flujo authorization code).

Esta NO es una autenticación por-usuario: es la credencial única que la
app usa para consultar la API de Mercado Libre autenticada (ver
MeliOAuthToken en app/models.py, y get_valid_meli_access_token en
app/ml_client.py, que la usa y la renueva). Por eso conectar la app
(GET /admin/meli/connect) es una acción de administración, protegida con
require_admin — y el callback (GET /oauth/mercadolibre/callback) NO puede
requerir un JWT nuestro: Mercado Libre redirige el navegador del admin
directo a esa URL, sin ningún header nuestro.

GET /admin/meli/connect devuelve la URL de autorización como JSON (no un
RedirectResponse): al estar protegido con Bearer token, un navegador no
tiene forma de adjuntar ese header si se navega directo a la URL — el
front (ver static/js/admin.js) llama al endpoint vía fetch con el header,
y recién con la URL ya en la respuesta hace la navegación real
(window.location.href) hacia Mercado Libre.

Política de "state" (protección CSRF/replay del flujo OAuth):
- Se genera con secrets.token_urlsafe (aleatoriedad criptográfica) al
  entrar a /admin/meli/connect, y se guarda en memoria (dict + Lock,
  mismo patrón que el bloqueo progresivo de login en app/auth.py) con una
  expiración de 10 minutos.
- El callback lo invalida INMEDIATAMENTE al consumirlo (se hace pop en el
  mismo paso en que se valida, exista o no, haya expirado o no): de un
  solo uso, así ni un reintento del mismo link ni un replay posterior lo
  vuelven a aceptar.

Política de logging: NUNCA se loguea ni se expone en una respuesta HTTP el
client_secret, el access_token ni el refresh_token completos. Si hace
falta loguear algo para debug, se trunca a los primeros 8 caracteres
(suficiente para correlacionar sin poder reconstruir el secreto).
"""

import logging
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

import requests
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.database import get_db
from app.models import MeliOAuthToken, User

logger = logging.getLogger(__name__)

MELI_CLIENT_ID = os.environ.get("MELI_CLIENT_ID")
MELI_CLIENT_SECRET = os.environ.get("MELI_CLIENT_SECRET")
MELI_REDIRECT_URI = os.environ.get("MELI_REDIRECT_URI")
if not MELI_CLIENT_ID or not MELI_CLIENT_SECRET or not MELI_REDIRECT_URI:
    raise RuntimeError(
        "MELI_CLIENT_ID, MELI_CLIENT_SECRET y MELI_REDIRECT_URI deben estar "
        "definidas como variables de entorno (ver .env.example) antes de "
        "iniciar la aplicación."
    )

MELI_AUTHORIZATION_URL = "https://auth.mercadolibre.cl/authorization"
MELI_TOKEN_URL = "https://api.mercadolibre.com/oauth/token"  # nosec B105 # es una URL, no un secreto
REQUEST_TIMEOUT_SECONDS = 10

STATE_EXPIRATION = timedelta(minutes=10)

_state_lock = threading.Lock()
_pending_states: dict = {}  # state -> datetime de expiración


def _truncate_secret(value: str) -> str:
    """Trunca un secreto a 8 caracteres para poder loguearlo sin exponerlo completo (intencional)."""
    return value[:8] + "..."


def _generate_state() -> str:
    state = secrets.token_urlsafe(32)
    with _state_lock:
        _pending_states[state] = datetime.now(timezone.utc) + STATE_EXPIRATION
    return state


def _consume_state(state: str) -> bool:
    """
    Devuelve True si `state` fue generado por nosotros y no expiró. Lo
    invalida (pop) en el mismo paso, siempre — de un solo uso, para que
    ni un reintento ni un replay posterior lo vuelvan a aceptar.
    """
    with _state_lock:
        expires_at = _pending_states.pop(state, None)
    return expires_at is not None and datetime.now(timezone.utc) < expires_at


def reset_meli_oauth_state_for_tests() -> None:
    """Solo para uso en tests: limpia los states pendientes en memoria entre casos."""
    with _state_lock:
        _pending_states.clear()


class MeliTokenExchangeError(Exception):
    """El intercambio de code por tokens con Mercado Libre falló (red, timeout o error HTTP)."""


def _exchange_code_for_tokens(code: str) -> dict:
    """
    POST a MELI_TOKEN_URL con grant_type=authorization_code. Lanza
    MeliTokenExchangeError ante cualquier fallo (el detalle real queda
    solo en el log interno, nunca en la excepción vista más arriba).
    """
    payload = {
        "grant_type": "authorization_code",
        "client_id": MELI_CLIENT_ID,
        "client_secret": MELI_CLIENT_SECRET,
        "code": code,
        "redirect_uri": MELI_REDIRECT_URI,
    }
    try:
        response = requests.post(MELI_TOKEN_URL, data=payload, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        return response.json()
    except (requests.exceptions.RequestException, ValueError) as exc:
        raise MeliTokenExchangeError("Error intercambiando el code por tokens con Mercado Libre") from exc


def _save_tokens(db: Session, token_data: dict) -> None:
    expires_in = token_data.get("expires_in", 0)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    token_row = db.execute(select(MeliOAuthToken)).scalar_one_or_none()
    if token_row is None:
        token_row = MeliOAuthToken()

    token_row.access_token = token_data["access_token"]
    token_row.refresh_token = token_data["refresh_token"]
    token_row.expires_at = expires_at

    db.add(token_row)
    db.commit()

    logger.info(
        "Tokens de Mercado Libre guardados",
        extra={
            # Truncado a propósito: nunca se loguea el token completo.
            "access_token_preview": _truncate_secret(token_row.access_token),
            "expires_at": expires_at.isoformat(),
        },
    )


class ConnectResponse(BaseModel):
    authorization_url: str


class StatusResponse(BaseModel):
    connected: bool
    updated_at: Optional[datetime] = None


router = APIRouter(tags=["meli-oauth"])

_SUCCESS_HTML = """<!doctype html>
<html lang="es">
<head><meta charset="utf-8"><title>Conexion con Mercado Libre</title></head>
<body><p>Conexión con Mercado Libre exitosa. Ya puedes cerrar esta pestaña.</p></body>
</html>"""

_ERROR_HTML = """<!doctype html>
<html lang="es">
<head><meta charset="utf-8"><title>Error de conexion</title></head>
<body><p>No se pudo completar la conexión con Mercado Libre. Intenta nuevamente desde el panel de administrador.</p></body>
</html>"""


@router.get("/admin/meli/connect", response_model=ConnectResponse)
def connect_mercadolibre(current_user: User = Depends(require_admin)):
    """
    Inicia el flujo de authorization code de Mercado Libre: genera un
    state de un solo uso y devuelve la URL de autorización como JSON (no
    un RedirectResponse — ver docstring del módulo). El front navega el
    navegador completo a esa URL una vez que la recibe.
    """
    state = _generate_state()
    query = urlencode(
        {
            "response_type": "code",
            "client_id": MELI_CLIENT_ID,
            "redirect_uri": MELI_REDIRECT_URI,
            "state": state,
        }
    )
    return ConnectResponse(authorization_url=f"{MELI_AUTHORIZATION_URL}?{query}")


@router.get("/admin/meli/status", response_model=StatusResponse)
def meli_status(current_user: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Si hay una conexión OAuth guardada (y desde cuándo), sin exponer el token en ningún momento."""
    token_row = db.execute(select(MeliOAuthToken)).scalar_one_or_none()
    if token_row is None:
        return StatusResponse(connected=False)
    return StatusResponse(connected=True, updated_at=token_row.updated_at)


@router.get("/oauth/mercadolibre/callback", response_class=HTMLResponse)
def mercadolibre_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    Mercado Libre redirige acá tras la autorización del admin. Sin
    Depends(get_current_user): esta request la dispara el navegador
    siguiendo la redirección de Mercado Libre, nunca lleva nuestro JWT.
    """
    if not state or not _consume_state(state):
        logger.warning("Callback de Mercado Libre rechazado: state ausente, inválido o expirado")
        raise HTTPException(
            status_code=400,
            detail=(
                "El enlace de conexión con Mercado Libre expiró o no es válido. "
                "Inicia el proceso nuevamente desde el panel de administrador."
            ),
        )

    if not code:
        logger.warning("Callback de Mercado Libre sin parámetro 'code'")
        raise HTTPException(status_code=400, detail="Mercado Libre no envió un código de autorización.")

    try:
        token_data = _exchange_code_for_tokens(code)
        _save_tokens(db, token_data)
    except MeliTokenExchangeError as exc:
        logger.error("Fallo al intercambiar el code de Mercado Libre por tokens", extra={"error": str(exc)})
        return HTMLResponse(_ERROR_HTML, status_code=502)

    return HTMLResponse(_SUCCESS_HTML)
