"""
Vistas HTML server-side (Jinja2). Solo sirven el marcado; toda la lógica
(login, verificación de sesión, cambio de contraseña, listado y gestión
de productos) vive en el JS del cliente llamando a la API ya existente
(app/auth.py, app/products.py) — estas rutas no hacen ninguna llamada a
la base de datos ni verifican autenticación por sí mismas.
"""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["views"])


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html")


@router.get("/cambiar-password", response_class=HTMLResponse)
def cambiar_password_page(request: Request):
    return templates.TemplateResponse(request, "cambiar-password.html")


@router.get("/productos", response_class=HTMLResponse)
def productos_page(request: Request):
    return templates.TemplateResponse(request, "productos.html")
