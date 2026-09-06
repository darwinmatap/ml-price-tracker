import re
from urllib.parse import urlparse


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
