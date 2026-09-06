import pytest

from app.utils import extract_product_id


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "https://articulo.mercadolibre.cl/MLC-123456789-notebook-lenovo-_JM",
            "MLC123456789",
        ),
        (
            "https://articulo.mercadolibre.com.ar/MLA-987654321-celular-samsung-_JM",
            "MLA987654321",
        ),
        (
            "https://articulo.mercadolibre.com.mx/MLM555444333-audifonos-bluetooth",
            "MLM555444333",
        ),
    ],
)
def test_extract_product_id_valid(url, expected):
    assert extract_product_id(url) == expected


def test_extract_product_id_invalid_url_raises_value_error():
    with pytest.raises(ValueError):
        extract_product_id("https://www.mercadolibre.cl/ofertas")
