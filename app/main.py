import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.auth import limiter, router as auth_router
from app.products import router as products_router
from app.scheduler import create_scheduler

logger = logging.getLogger(__name__)


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
app.add_middleware(SlowAPIMiddleware)

app.include_router(auth_router)
app.include_router(products_router)


@app.get("/health")
def health_check():
    return {"status": "ok"}
