"""
Verifica que SecurityHeadersMiddleware agregue las cabeceras de
seguridad esperadas a toda respuesta, y que la configuración de
ALLOWED_ORIGINS nunca acepte "*" (ver app/main.py:_get_allowed_origins).
"""
import pytest

from app.main import _get_allowed_origins


def test_get_health_incluye_cabeceras_de_seguridad(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.headers["Strict-Transport-Security"] == "max-age=63072000; includeSubDomains"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"

    csp = response.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "style-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp


def test_paginas_html_tambien_llevan_las_cabeceras(client):
    """Las cabeceras deben aplicar a toda respuesta, no solo a la API JSON."""
    response = client.get("/login")

    assert response.status_code == 200
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "Content-Security-Policy" in response.headers


def test_allowed_origins_nunca_acepta_wildcard(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://ejemplo.com,*")

    with pytest.raises(RuntimeError, match="no puede ser '\\*'"):
        _get_allowed_origins()


def test_allowed_origins_default_no_incluye_wildcard(monkeypatch):
    monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)

    origins = _get_allowed_origins()

    assert "*" not in origins
    assert "http://localhost:8000" in origins
    assert "https://localhost:8443" in origins


def test_allowed_origins_lee_variable_de_entorno(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://app.ejemplo.com, https://otro.ejemplo.com ")

    origins = _get_allowed_origins()

    assert origins == ["https://app.ejemplo.com", "https://otro.ejemplo.com"]
