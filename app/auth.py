"""
Autenticación multi-usuario respaldada por la tabla User (ver app/models.py).

No hay registro público: los usuarios los crea un admin vía
POST /admin/users (ver app/admin.py). Las credenciales se validan contra
User.password_hash (bcrypt); SECRET_KEY para firmar JWT sigue viniendo
exclusivamente de variable de entorno (ver .env.example). Nunca hardcodeada.

Política de respuesta ante fallos de autenticación:
- Usuario inexistente, usuario desactivado (is_active=False), o password
  incorrecta deben ser INDISTINGUIBLES para quien intenta el login: mismo
  código (401) y mismo mensaje genérico ("Credenciales inválidas"). Nunca
  revelar cuál validación falló — eso permitiría enumerar usuarios o
  saber si una cuenta fue desactivada.
- Excepción explícita: require_admin (usuario ya autenticado, sin rol
  suficiente) SÍ responde con un mensaje claro (403 Forbidden). No es una
  situación de enumeración: quien lo recibe ya demostró tener una cuenta
  válida, solo le falta el rol.

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
from pydantic import BaseModel, ConfigDict, Field
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User, UserRole

logger = logging.getLogger(__name__)

# --- Configuración desde variables de entorno (fail-fast si falta algo) ---

SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError(
        "SECRET_KEY no está definida. Configúrala como variable de entorno "
        "(ver .env.example) antes de iniciar la aplicación."
    )

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Hash "señuelo" contra el que se verifica cuando el username no existe,
# para que el costo de bcrypt sea el mismo exista o no la cuenta (evita
# filtrar por timing si un username es válido).
_DUMMY_PASSWORD_HASH = pwd_context.hash("dummy-password-solo-para-comparacion-de-tiempo-constante")

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
# (un solo proceso) mantenerlo en memoria es aceptable y evita sumar una
# dependencia de infraestructura en esta etapa.
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
# instancias. Para esta app (un solo proceso) mantenerlo en memoria es
# aceptable.
_session_lock = threading.Lock()
_valid_refresh_jti: dict = {}  # user_id -> jti del refresh token vigente


def _set_valid_refresh_jti(user_id: int, jti: str) -> None:
    with _session_lock:
        _valid_refresh_jti[user_id] = jti


def _get_valid_refresh_jti(user_id: int) -> Optional[str]:
    with _session_lock:
        return _valid_refresh_jti.get(user_id)


def _invalidate_refresh_jti(user_id: int) -> None:
    with _session_lock:
        _valid_refresh_jti.pop(user_id, None)


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


def _authenticate(db: Session, username: str, password: str) -> Optional[User]:
    """
    Busca al usuario por username y verifica la password. SIEMPRE ejecuta
    un verify bcrypt (contra el hash real si el usuario existe, o contra
    un hash señuelo si no) para no filtrar por timing si el username es
    válido.

    Devuelve el User solo si existe, está activo (is_active) y la
    password matchea. En cualquier otro caso devuelve None — sin
    distinguir el motivo, para que el caller responda siempre el mismo
    mensaje genérico.
    """
    user = db.execute(select(User).where(User.username == username)).scalar_one_or_none()

    if user is not None:
        password_ok = pwd_context.verify(password, user.password_hash)
    else:
        password_ok = pwd_context.verify(password, _DUMMY_PASSWORD_HASH)

    if user is None or not user.is_active or not password_ok:
        return None

    return user


# --- Tokens JWT ---


def _create_token(
    subject: str,
    token_type: str,
    expires_delta: timedelta,
    extra_claims: Optional[dict] = None,
    jti: Optional[str] = None,
) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "type": token_type,
        "iat": now,
        "exp": now + expires_delta,
    }
    if extra_claims:
        payload.update(extra_claims)
    if jti is not None:
        payload["jti"] = jti
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def create_access_token(user: User) -> str:
    return _create_token(
        subject=user.username,
        token_type="access",  # nosec B106 # discriminador de tipo de token ("access" vs "refresh"), no una credencial
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
        extra_claims={"user_id": user.id, "role": user.role.value},
    )


def create_refresh_token(user: User) -> str:
    """
    Emite un refresh token con un jti nuevo y lo registra como el único
    jti vigente para user.id (invalida cualquier refresh token anterior
    de ese usuario, incluido el de otra sesión activa).
    """
    jti = str(uuid.uuid4())
    token = _create_token(
        subject=user.username,
        token_type="refresh",  # nosec B106 # discriminador de tipo de token ("access" vs "refresh"), no una credencial
        expires_delta=timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
        extra_claims={"user_id": user.id, "role": user.role.value},
        jti=jti,
    )
    _set_valid_refresh_jti(user.id, jti)
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


# --- Dependencies reusables para endpoints protegidos ---

_bearer_scheme = HTTPBearer(auto_error=True)


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """
    Valida el access token y devuelve el User completo, consultado fresco
    en la base (no solo lo que diga el JWT) para que un usuario
    desactivado a mitad de sesión pierda el acceso de inmediato, no recién
    cuando su access token expire.
    """
    unauthorized = HTTPException(
        status_code=401,
        detail=GENERIC_AUTH_ERROR_DETAIL,
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = _decode_token(credentials.credentials, expected_type="access")
    except jwt.PyJWTError:
        raise unauthorized

    user_id = payload.get("user_id")
    user = db.get(User, user_id) if user_id is not None else None

    if user is None or not user.is_active:
        raise unauthorized

    return user


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    """
    Igual que get_current_user, pero además exige role == admin. A
    diferencia de los fallos de login, acá sí es correcto ser explícito
    (403 Forbidden con mensaje claro): quien la recibe ya está
    autenticado, no es una situación de enumeración de cuentas.
    """
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=403,
            detail="No tienes permisos de administrador para realizar esta acción.",
        )
    return current_user


# --- Esquemas ---


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserMeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    nombre: str
    role: UserRole
    debe_cambiar_password: bool


class ChangePasswordRequest(BaseModel):
    password_actual: str
    # Mínimo básico de longitud. Se puede endurecer después con reglas de
    # complejidad (mayúsculas, números, símbolos, etc.) si se requiere;
    # por ahora 8 caracteres es el piso razonable sin sobre-diseñar.
    password_nueva: str = Field(min_length=8, max_length=200)


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
def login(request: Request, response: Response, credentials: LoginRequest, db: Session = Depends(get_db)):
    client_ip = get_remote_address(request)

    if _is_locked_out(client_ip):
        logger.warning("Login rechazado: IP con bloqueo activo", extra={"ip": client_ip})
        raise HTTPException(status_code=429, detail=GENERIC_AUTH_ERROR_DETAIL)

    user = _authenticate(db, credentials.username, credentials.password)
    if user is None:
        _register_failed_attempt(client_ip, credentials.username)
        raise HTTPException(status_code=401, detail=GENERIC_AUTH_ERROR_DETAIL)

    _reset_failed_attempts(client_ip)

    access_token = create_access_token(user)
    refresh_token = create_refresh_token(user)
    _set_refresh_cookie(response, refresh_token)

    return TokenResponse(access_token=access_token)


@router.post("/refresh", response_model=TokenResponse)
def refresh(request: Request, response: Response, db: Session = Depends(get_db)):
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

    user_id = payload.get("user_id")
    token_jti = payload.get("jti")
    current_valid_jti = _get_valid_refresh_jti(user_id) if user_id is not None else None

    if user_id is None or not token_jti or current_valid_jti is None or token_jti != current_valid_jti:
        # El jti presentado no es el vigente: o ya fue rotado antes (reuso
        # de un refresh token viejo) o nunca fue emitido por este flujo.
        # Se trata como señal de posible robo: se invalida la sesión
        # completa para forzar un login limpio, sin importar si el
        # atacante o el usuario legítimo hace la siguiente request.
        if user_id is not None:
            _invalidate_refresh_jti(user_id)
        logger.warning(
            "Posible reuso de refresh token detectado",
            extra={
                "ip": get_remote_address(request),
                "user_id": user_id,
                "severity": "high",
            },
        )
        raise HTTPException(status_code=401, detail=GENERIC_AUTH_ERROR_DETAIL)

    user = db.get(User, user_id)
    if user is None or not user.is_active:
        _invalidate_refresh_jti(user_id)
        logger.warning(
            "Refresh rechazado: usuario inexistente o desactivado",
            extra={"ip": get_remote_address(request), "user_id": user_id},
        )
        raise HTTPException(status_code=401, detail=GENERIC_AUTH_ERROR_DETAIL)

    # Rotación: nuevo access + nuevo refresh token, reemplazando el jti vigente.
    new_access_token = create_access_token(user)
    new_refresh_token = create_refresh_token(user)
    _set_refresh_cookie(response, new_refresh_token)

    return TokenResponse(access_token=new_access_token)


@router.post("/logout")
def logout(request: Request, response: Response):
    refresh_token = request.cookies.get("refresh_token")
    if refresh_token:
        try:
            payload = _decode_token(refresh_token, expected_type="refresh")
            user_id = payload.get("user_id")
            if user_id is not None:
                _invalidate_refresh_jti(user_id)
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


@router.get("/me", response_model=UserMeResponse)
def me(current_user: User = Depends(get_current_user)):
    """Datos del usuario logueado, para que el frontend los consulte. Nunca incluye password_hash."""
    return current_user


@router.post("/change-password")
def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Cambia la password del usuario autenticado. Tener un access token
    válido no basta: hay que demostrar que se conoce la password actual
    (nunca se confía en la sola posesión del JWT para esta operación).
    """
    if not pwd_context.verify(payload.password_actual, current_user.password_hash):
        logger.warning(
            "Cambio de password rechazado: password actual incorrecta",
            extra={"ip": get_remote_address(request), "user_id": current_user.id},
        )
        raise HTTPException(status_code=401, detail=GENERIC_AUTH_ERROR_DETAIL)

    current_user.password_hash = pwd_context.hash(payload.password_nueva)
    current_user.debe_cambiar_password = False
    db.add(current_user)
    db.commit()

    # Invalida la sesión de refresh vigente: fuerza a loguearse de nuevo
    # con la clave nueva en cualquier otro dispositivo/pestaña donde
    # hubiera una sesión abierta.
    _invalidate_refresh_jti(current_user.id)

    logger.info(
        "Cambio de credenciales: password actualizada",
        extra={"user_id": current_user.id, "username": current_user.username},
    )

    return {"detail": "Password actualizada correctamente."}
