"""
Autenticación para la app de un solo usuario.

No hay tabla de usuarios ni registro público: las credenciales válidas
viven exclusivamente en variables de entorno (APP_USERNAME,
APP_PASSWORD_HASH, SECRET_KEY — ver .env.example). Nunca hardcodeadas.

Política de respuesta ante fallos de autenticación:
- Usuario incorrecto, contraseña incorrecta, IP bloqueada o rate limit
  superado deben ser indistinguibles para el cliente en cuanto al
  mensaje ("Credenciales inválidas"). No revelar cuál validación falló.

Política de logging:
- Se loguea IP, timestamp (automático del logger) y username intentado
  en cada fallo. NUNCA se loguea la contraseña ingresada, ni siquiera en
  un intento fallido.
"""

import logging
import os
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.context import CryptContext
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address

logger = logging.getLogger(__name__)

# --- Configuración desde variables de entorno (fail-fast si falta algo) ---

APP_USERNAME = os.environ.get("APP_USERNAME")
APP_PASSWORD_HASH = os.environ.get("APP_PASSWORD_HASH")
SECRET_KEY = os.environ.get("SECRET_KEY")

for _var_name, _var_value in (
    ("APP_USERNAME", APP_USERNAME),
    ("APP_PASSWORD_HASH", APP_PASSWORD_HASH),
    ("SECRET_KEY", SECRET_KEY),
):
    if not _var_value:
        raise RuntimeError(
            f"{_var_name} no está definida. Configúrala como variable de entorno "
            "(ver .env.example) antes de iniciar la aplicación."
        )

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 15
REFRESH_TOKEN_EXPIRE_DAYS = 7

GENERIC_AUTH_ERROR_DETAIL = "Credenciales inválidas"

# --- Rate limiting (slowapi) ---

limiter = Limiter(key_func=get_remote_address)

# --- Bloqueo progresivo por intentos fallidos ---
#
# NOTA DE ESCALABILIDAD: este estado vive en un diccionario en memoria del
# proceso. En un escenario multi-instancia (varios workers/procesos/
# contenedores detrás de un balanceador) esto debería vivir en un store
# compartido como Redis, porque cada instancia tendría su propio contador
# y el bloqueo dejaría de ser consistente entre ellas. Para esta app
# (un solo proceso, un solo usuario posible) mantenerlo en memoria es
# aceptable y evita sumar una dependencia de infraestructura en esta etapa.
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_DURATION = timedelta(minutes=5)

_lockout_lock = threading.Lock()
_failed_attempts: dict = defaultdict(int)
_locked_until: dict = {}


def _is_locked_out(ip: str) -> bool:
    with _lockout_lock:
        locked_until = _locked_until.get(ip)
        if locked_until is None:
            return False
        if datetime.now(timezone.utc) >= locked_until:
            _locked_until.pop(ip, None)
            _failed_attempts.pop(ip, None)
            return False
        return True


def _register_failed_attempt(ip: str, attempted_username: str) -> None:
    with _lockout_lock:
        _failed_attempts[ip] += 1
        count = _failed_attempts[ip]

    logger.warning(
        "Intento de login fallido",
        extra={"ip": ip, "attempted_username": attempted_username, "failed_count": count},
    )

    if count >= MAX_FAILED_ATTEMPTS:
        with _lockout_lock:
            _locked_until[ip] = datetime.now(timezone.utc) + LOCKOUT_DURATION
        logger.warning(
            "IP bloqueada temporalmente por múltiples intentos fallidos",
            extra={"ip": ip, "locked_minutes": LOCKOUT_DURATION.total_seconds() / 60},
        )


def _reset_failed_attempts(ip: str) -> None:
    with _lockout_lock:
        _failed_attempts.pop(ip, None)
        _locked_until.pop(ip, None)


# --- Rotación de refresh tokens con detección de reuso ---
#
# NOTA DE ESCALABILIDAD: igual que el bloqueo progresivo de arriba, este
# estado vive en un diccionario en memoria del proceso (dict + Lock). En
# un escenario multi-instancia esto debería vivir en un store compartido
# como Redis, para que la rotación/revocación sea consistente entre
# instancias. Para esta app (un solo proceso, un solo usuario posible)
# mantenerlo en memoria es aceptable.
_session_lock = threading.Lock()
_valid_refresh_jti: dict = {}  # username -> jti del refresh token vigente


def _set_valid_refresh_jti(username: str, jti: str) -> None:
    with _session_lock:
        _valid_refresh_jti[username] = jti


def _get_valid_refresh_jti(username: str) -> Optional[str]:
    with _session_lock:
        return _valid_refresh_jti.get(username)


def _invalidate_refresh_jti(username: str) -> None:
    with _session_lock:
        _valid_refresh_jti.pop(username, None)


def reset_login_security_state_for_tests() -> None:
    """
    Solo para uso en tests: limpia el estado de bloqueo y de sesiones
    (jti vigente) en memoria entre casos, para que no queden datos de un
    test contaminando el siguiente. No usar desde código de producción.
    """
    with _lockout_lock:
        _failed_attempts.clear()
        _locked_until.clear()
    with _session_lock:
        _valid_refresh_jti.clear()


# --- Verificación de credenciales ---


def _verify_credentials(username: str, password: str) -> bool:
    """
    Siempre ejecuta la verificación bcrypt contra el hash real, exista o
    no coincidencia de username, para no filtrar por timing si el
    username ingresado es válido o no.
    """
    username_ok = username == APP_USERNAME
    password_ok = pwd_context.verify(password, APP_PASSWORD_HASH)
    return username_ok and password_ok


# --- Tokens JWT ---


def _create_token(
    subject: str,
    token_type: str,
    expires_delta: timedelta,
    jti: Optional[str] = None,
) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "type": token_type,
        "iat": now,
        "exp": now + expires_delta,
    }
    if jti is not None:
        payload["jti"] = jti
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def create_access_token(subject: str) -> str:
    return _create_token(subject, "access", timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))


def create_refresh_token(subject: str) -> str:
    """
    Emite un refresh token con un jti nuevo y lo registra como el único
    jti vigente para `subject` (invalida cualquier refresh token anterior
    de ese usuario, incluido el de otra sesión activa).
    """
    jti = str(uuid.uuid4())
    token = _create_token(subject, "refresh", timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS), jti=jti)
    _set_valid_refresh_jti(subject, jti)
    return token


def _decode_token(token: str, expected_type: str) -> dict:
    """
    Decodifica y valida firma + expiración. Lanza jwt.PyJWTError (o
    subclase) si el token es inválido, expiró, o no es del tipo esperado
    (evita que un refresh token robado se use como access token o
    viceversa).
    """
    payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    if payload.get("type") != expected_type:
        raise jwt.InvalidTokenError("Tipo de token incorrecto")
    return payload


# --- Dependency reusable para endpoints protegidos ---

_bearer_scheme = HTTPBearer(auto_error=True)


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
) -> str:
    try:
        payload = _decode_token(credentials.credentials, expected_type="access")
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=401,
            detail=GENERIC_AUTH_ERROR_DETAIL,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return payload["sub"]


# --- Esquemas ---


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


# --- Router ---

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_refresh_cookie(response: Response, refresh_token: str) -> None:
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=int(timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS).total_seconds()),
        path="/auth",
    )


@router.post("/login", response_model=TokenResponse)
@limiter.limit("5/minute")
def login(request: Request, response: Response, credentials: LoginRequest):
    client_ip = get_remote_address(request)

    if _is_locked_out(client_ip):
        logger.warning("Login rechazado: IP con bloqueo activo", extra={"ip": client_ip})
        raise HTTPException(status_code=429, detail=GENERIC_AUTH_ERROR_DETAIL)

    if not _verify_credentials(credentials.username, credentials.password):
        _register_failed_attempt(client_ip, credentials.username)
        raise HTTPException(status_code=401, detail=GENERIC_AUTH_ERROR_DETAIL)

    _reset_failed_attempts(client_ip)

    access_token = create_access_token(subject=credentials.username)
    refresh_token = create_refresh_token(subject=credentials.username)
    _set_refresh_cookie(response, refresh_token)

    return TokenResponse(access_token=access_token)


@router.post("/refresh", response_model=TokenResponse)
def refresh(request: Request, response: Response):
    refresh_token = request.cookies.get("refresh_token")
    if not refresh_token:
        raise HTTPException(status_code=401, detail=GENERIC_AUTH_ERROR_DETAIL)

    try:
        payload = _decode_token(refresh_token, expected_type="refresh")
    except jwt.PyJWTError:
        logger.warning(
            "Refresh token inválido o expirado",
            extra={"ip": get_remote_address(request)},
        )
        raise HTTPException(status_code=401, detail=GENERIC_AUTH_ERROR_DETAIL)

    username = payload["sub"]
    token_jti = payload.get("jti")
    current_valid_jti = _get_valid_refresh_jti(username)

    if not token_jti or current_valid_jti is None or token_jti != current_valid_jti:
        # El jti presentado no es el vigente: o ya fue rotado antes (reuso
        # de un refresh token viejo) o nunca fue emitido por este flujo.
        # Se trata como señal de posible robo: se invalida la sesión
        # completa para forzar un login limpio, sin importar si el
        # atacante o el usuario legítimo hace la siguiente request.
        _invalidate_refresh_jti(username)
        logger.warning(
            "Posible reuso de refresh token detectado",
            extra={
                "ip": get_remote_address(request),
                "username": username,
                "severity": "high",
            },
        )
        raise HTTPException(status_code=401, detail=GENERIC_AUTH_ERROR_DETAIL)

    # Rotación: nuevo access + nuevo refresh token, reemplazando el jti vigente.
    new_access_token = create_access_token(subject=username)
    new_refresh_token = create_refresh_token(subject=username)
    _set_refresh_cookie(response, new_refresh_token)

    return TokenResponse(access_token=new_access_token)


@router.post("/logout")
def logout(request: Request, response: Response):
    refresh_token = request.cookies.get("refresh_token")
    if refresh_token:
        try:
            payload = _decode_token(refresh_token, expected_type="refresh")
            _invalidate_refresh_jti(payload["sub"])
        except jwt.PyJWTError:
            pass  # token ya inválido o expirado: nada que invalidar

    response.delete_cookie(
        key="refresh_token",
        path="/auth",
        httponly=True,
        secure=True,
        samesite="strict",
    )
    return {"detail": "Sesión cerrada"}
