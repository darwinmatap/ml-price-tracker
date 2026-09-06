import re
from urllib.parse import urlparse

# --- Allowlist de dominios de Mercado Libre ---
#
# Solo se aceptan URLs de estos dominios. Nunca se procesa una URL de un
# dominio fuera de esta lista, para evitar que la app se use como
# proxy/SSRF hacia hosts arbitrarios. Compartida entre app/products.py y
# app/url_resolver.py — no duplicar este set en ningún otro lado.
#
# NOTA DE SEGURIDAD: la comparación es SIEMPRE sobre el hostname parseado
# por urlparse(url).hostname (nunca un "in"/substring sobre la URL
# completa), y es exacta o de sufijo con límite de punto explícito
# (host == dominio or host.endswith("." + dominio)). Un simple
# host.startswith("articulo.mercadolibre.") NO es seguro: no valida qué
# viene después del prefijo, así que "articulo.mercadolibre.atacante.com"
# lo pasaría (dominio real: atacante.com). Por eso solo se listan dominios
# completos aquí — para agregar un país nuevo, se agrega su dominio
# completo a este set, nunca un prefijo abierto.
ALLOWED_DOMAINS = {
    "mercadolibre.cl",
    "mercadolibre.com.ar",
    "mercadolibre.com.mx",
    "mercadolibre.com.co",
    "mercadolibre.com.pe",
    "mercadolibre.com.uy",
}


def is_allowed_domain(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    return any(host == domain or host.endswith("." + domain) for domain in ALLOWED_DOMAINS)


def extract_product_id(url: str) -> str:
    """
    Extrae el ID de un producto a partir de la URL de una publicación
    de Mercado Libre y lo devuelve en el formato esperado por la API
    (prefijo de país en mayúsculas seguido de los dígitos, sin guion).

    Funciona con URLs de distintos países (Chile, Argentina, México, etc.)
    y con IDs que vengan con o sin guion después del prefijo.

    Args:
        url: URL completa de una publicación de Mercado Libre.
            Ej: "https://articulo.mercadolibre.cl/MLC-123456789-notebook-lenovo-_JM"

    Returns:
        El ID del producto en formato "MLC123456789".

    Raises:
        ValueError: si la URL no contiene un ID de producto válido.
    """
    path = urlparse(url).path
    match = re.search(r"([A-Za-z]{2,4})-?(\d{5,})", path)

    if not match:
        raise ValueError(f"No se pudo extraer un ID de producto válido de la URL: {url}")

    prefix, number = match.groups()
    return f"{prefix.upper()}{number}"
