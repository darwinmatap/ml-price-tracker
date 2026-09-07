"""
Middleware que agrega cabeceras de seguridad HTTP a toda respuesta.

Separado de app/main.py para mantenerlo testeable de forma aislada y para
que la política quede documentada en un solo lugar.

Content-Security-Policy: la app sirve exclusivamente su propio CSS/JS
desde /static (ver templates/base.html) — no hay scripts inline, estilos
inline, ni recursos de terceros/CDNs. Por eso la política puede ser
estricta (default-src 'self', sin excepciones) sin romper nada. Si en el
futuro se agrega un recurso externo (fuente, CDN, etc.), esta política
debe actualizarse explícitamente para permitirlo — nunca aflojarla
"por si acaso" de antemano.
"""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self'; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "form-action 'self'; "
    "base-uri 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'none'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)

        # HSTS: sin efecto real hasta estar detrás de HTTPS de verdad en
        # producción, pero debe estar presente desde ya para no depender
        # de acordarse de agregarla en el momento del deploy.
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Redundante con frame-ancestors 'none' de la CSP, pero se deja
        # explícita para navegadores viejos que no soportan CSP.
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY

        return response
