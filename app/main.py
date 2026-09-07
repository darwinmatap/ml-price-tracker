import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.admin import router as admin_router
from app.auth import limiter, router as auth_router
from app.meli_oauth import router as meli_oauth_router
from app.products import router as products_router
from app.scheduler import create_scheduler
from app.security_headers import SecurityHeadersMiddleware
from app.views import router as views_router

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

logger = logging.getLogger(__name__)

# Orígenes permitidos para CORS. En dev, los dos puertos locales que ya
# usamos (HTTP plano y HTTPS con certificado autofirmado). En producción
# se sobreescribe con el dominio real vía la variable de entorno — nunca
# debe quedar en "*" (permitiría que cualquier sitio haga requests
# autenticadas contra la API).
_DEFAULT_ALLOWED_ORIGINS = "http://localhost:8000,https://localhost:8443"


def _get_allowed_origins() -> list[str]:
    raw = os.environ.get("ALLOWED_ORIGINS", _DEFAULT_ALLOWED_ORIGINS)
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    if "*" in origins:
        raise RuntimeError(
            "ALLOWED_ORIGINS no puede ser '*'. Especifica los orígenes exactos permitidos "
            "(separados por coma) como variable de entorno."
        )
    return origins


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = create_scheduler()
    scheduler.start()
    app.state.scheduler = scheduler
    logger.info("Scheduler de escaneo automático iniciado")
    try:
        yield
    finally:
        scheduler.shutdown()
        logger.info("Scheduler de escaneo automático detenido")


app = FastAPI(title="ML Price Tracker", lifespan=lifespan)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Orden de middleware: Starlette aplica el último agregado como el más
# externo. Queremos SecurityHeadersMiddleware envolviendo todo (para que
# hasta una respuesta de CORS rechazada o un 429 de rate limit lleven las
# cabeceras), luego CORS, luego rate limiting, luego las rutas.
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_get_allowed_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)
app.add_middleware(SecurityHeadersMiddleware)

app.include_router(auth_router)
app.include_router(products_router)
app.include_router(admin_router)
app.include_router(meli_oauth_router)
app.include_router(views_router)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/health")
def health_check():
    return {"status": "ok"}
