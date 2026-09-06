"""
Tests de app/url_resolver.py (resolución segura de links acortados de
meli.la). Salvo dos excepciones explícitas y controladas (el test de
pinning con servidor HTTPS real, que habla por loopback consigo mismo —
nunca con meli.la ni con Internet), nunca se hacen requests reales: se
mockean tanto socket.getaddrinfo (resolución DNS) como requests.head
(los saltos HTTP).

Convención de mocks en este archivo:
- mock_getaddrinfo: dict hostname -> ip pública/privada a devolver.
- mock_head: side_effect con la secuencia de respuestas HTTP por salto.
"""

import socket
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import MagicMock, patch

import pytest
import requests

from app.url_resolver import (
    MAX_HOPS,
    GENERIC_RESOLVE_ERROR,
    ShortLinkResolutionError,
    _pin_dns_resolution,
    resolve_meli_short_link,
)


def _addrinfo(ip: str):
    """Simula el formato de retorno real de socket.getaddrinfo para una sola IP."""
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    sockaddr = (ip, 0, 0, 0) if family == socket.AF_INET6 else (ip, 0)
    return [(family, socket.SOCK_STREAM, 6, "", sockaddr)]


def _make_getaddrinfo_mock(host_to_ip: dict):
    def _fake_getaddrinfo(host, *args, **kwargs):
        if host not in host_to_ip:
            raise AssertionError(f"getaddrinfo llamado con un host no esperado en el test: {host}")
        return _addrinfo(host_to_ip[host])

    return _fake_getaddrinfo


def _make_response(status_code=200, location=None):
    response = MagicMock()
    response.status_code = status_code
    response.headers = {"Location": location} if location else {}
    response.is_redirect = status_code in (301, 302, 303, 307, 308) and location is not None
    return response


@patch("app.url_resolver.requests.head")
@patch("app.url_resolver.socket.getaddrinfo")
def test_meli_la_redirige_en_un_salto_a_producto_valido(mock_getaddrinfo, mock_head):
    mock_getaddrinfo.side_effect = _make_getaddrinfo_mock(
        {
            "meli.la": "93.184.216.10",
            "articulo.mercadolibre.cl": "93.184.216.20",
        }
    )
    destino_final = "https://articulo.mercadolibre.cl/MLC-123456789-notebook-lenovo"
    mock_head.side_effect = [
        _make_response(302, location=destino_final),
        _make_response(200),
    ]

    resultado = resolve_meli_short_link("https://meli.la/1BP7AeP")

    assert resultado == destino_final
    assert mock_head.call_count == 2


@patch("app.url_resolver.requests.head")
@patch("app.url_resolver.socket.getaddrinfo")
def test_meli_la_cadena_de_redirecciones_validas(mock_getaddrinfo, mock_head):
    mock_getaddrinfo.side_effect = _make_getaddrinfo_mock(
        {
            "meli.la": "93.184.216.10",
            "redirect1.example.com": "93.184.216.11",
            "redirect2.example.com": "93.184.216.12",
            "articulo.mercadolibre.com.ar": "93.184.216.21",
        }
    )
    destino_final = "https://articulo.mercadolibre.com.ar/MLA-987654321-celular"
    mock_head.side_effect = [
        _make_response(302, location="https://redirect1.example.com/a"),
        _make_response(302, location="https://redirect2.example.com/b"),
        _make_response(302, location=destino_final),
        _make_response(200),
    ]

    resultado = resolve_meli_short_link("https://meli.la/1BP7AeP")

    assert resultado == destino_final
    assert mock_head.call_count == 4


@patch("app.url_resolver.requests.head")
@patch("app.url_resolver.socket.getaddrinfo")
def test_meli_la_mas_de_5_saltos_rechazado(mock_getaddrinfo, mock_head):
    hosts = {"meli.la": "93.184.216.10"}
    respuestas = []
    for i in range(8):
        host = f"redirect{i}.example.com"
        hosts[host] = f"93.184.216.{50 + i}"
        siguiente = f"https://redirect{i + 1}.example.com/x"
        respuestas.append(_make_response(302, location=siguiente))
    mock_getaddrinfo.side_effect = _make_getaddrinfo_mock(hosts)
    mock_head.side_effect = respuestas

    with pytest.raises(ShortLinkResolutionError) as exc_info:
        resolve_meli_short_link("https://meli.la/1BP7AeP")

    assert str(exc_info.value) == GENERIC_RESOLVE_ERROR
    assert mock_head.call_count == MAX_HOPS


@patch("app.url_resolver.requests.head")
@patch("app.url_resolver.socket.getaddrinfo")
def test_meli_la_redirige_a_localhost_rechazado(mock_getaddrinfo, mock_head):
    mock_getaddrinfo.side_effect = _make_getaddrinfo_mock(
        {
            "meli.la": "93.184.216.10",
            "localhost": "127.0.0.1",
        }
    )
    mock_head.side_effect = [_make_response(302, location="http://localhost/admin")]

    with pytest.raises(ShortLinkResolutionError) as exc_info:
        resolve_meli_short_link("https://meli.la/1BP7AeP")

    assert str(exc_info.value) == GENERIC_RESOLVE_ERROR
    # El segundo salto (localhost) se rechaza por IP ANTES de hacerle un
    # request HTTP: solo debió llamarse requests.head una vez (meli.la).
    assert mock_head.call_count == 1


@patch("app.url_resolver.requests.head")
@patch("app.url_resolver.socket.getaddrinfo")
def test_meli_la_redirige_a_ip_literal_localhost_rechazado(mock_getaddrinfo, mock_head):
    mock_getaddrinfo.side_effect = _make_getaddrinfo_mock(
        {
            "meli.la": "93.184.216.10",
            "127.0.0.1": "127.0.0.1",
        }
    )
    mock_head.side_effect = [_make_response(302, location="http://127.0.0.1/admin")]

    with pytest.raises(ShortLinkResolutionError) as exc_info:
        resolve_meli_short_link("https://meli.la/1BP7AeP")

    assert str(exc_info.value) == GENERIC_RESOLVE_ERROR
    assert mock_head.call_count == 1


@patch("app.url_resolver.requests.head")
@patch("app.url_resolver.socket.getaddrinfo")
def test_meli_la_redirige_a_metadata_de_nube_rechazado(mock_getaddrinfo, mock_head):
    mock_getaddrinfo.side_effect = _make_getaddrinfo_mock(
        {
            "meli.la": "93.184.216.10",
            "169.254.169.254": "169.254.169.254",
        }
    )
    mock_head.side_effect = [
        _make_response(302, location="http://169.254.169.254/latest/meta-data/")
    ]

    with pytest.raises(ShortLinkResolutionError) as exc_info:
        resolve_meli_short_link("https://meli.la/1BP7AeP")

    assert str(exc_info.value) == GENERIC_RESOLVE_ERROR
    assert mock_head.call_count == 1


@patch("app.url_resolver.requests.head")
@patch("app.url_resolver.socket.getaddrinfo")
def test_meli_la_redirige_a_ip_privada_rechazado(mock_getaddrinfo, mock_head):
    mock_getaddrinfo.side_effect = _make_getaddrinfo_mock(
        {
            "meli.la": "93.184.216.10",
            "192.168.1.1": "192.168.1.1",
        }
    )
    mock_head.side_effect = [_make_response(302, location="http://192.168.1.1/router-admin")]

    with pytest.raises(ShortLinkResolutionError) as exc_info:
        resolve_meli_short_link("https://meli.la/1BP7AeP")

    assert str(exc_info.value) == GENERIC_RESOLVE_ERROR
    assert mock_head.call_count == 1


@patch("app.url_resolver.requests.head")
@patch("app.url_resolver.socket.getaddrinfo")
def test_meli_la_redirige_a_dominio_no_ml_rechazado(mock_getaddrinfo, mock_head):
    mock_getaddrinfo.side_effect = _make_getaddrinfo_mock(
        {
            "meli.la": "93.184.216.10",
            "atacante.com": "93.184.216.99",
        }
    )
    mock_head.side_effect = [
        _make_response(302, location="https://atacante.com/pagina-de-phishing"),
        _make_response(200),
    ]

    with pytest.raises(ShortLinkResolutionError) as exc_info:
        resolve_meli_short_link("https://meli.la/1BP7AeP")

    assert str(exc_info.value) == GENERIC_RESOLVE_ERROR
    # Llegó a consultar atacante.com (IP pública, sin problema de SSRF);
    # lo que lo rechaza es el chequeo de dominio final, no la IP.
    assert mock_head.call_count == 2


@patch("app.url_resolver.requests.head")
@patch("app.url_resolver.socket.getaddrinfo")
def test_meli_la_timeout_de_red_no_crashea(mock_getaddrinfo, mock_head):
    mock_getaddrinfo.side_effect = _make_getaddrinfo_mock({"meli.la": "93.184.216.10"})
    mock_head.side_effect = requests.exceptions.Timeout("tiempo de espera agotado")

    with pytest.raises(ShortLinkResolutionError) as exc_info:
        resolve_meli_short_link("https://meli.la/1BP7AeP")

    assert str(exc_info.value) == GENERIC_RESOLVE_ERROR


@patch("app.url_resolver.requests.head")
@patch("app.url_resolver.socket.getaddrinfo")
def test_meli_la_acortador_devuelve_error_http_no_crashea(mock_getaddrinfo, mock_head):
    mock_getaddrinfo.side_effect = _make_getaddrinfo_mock({"meli.la": "93.184.216.10"})
    mock_head.side_effect = [_make_response(500)]

    with pytest.raises(ShortLinkResolutionError) as exc_info:
        resolve_meli_short_link("https://meli.la/1BP7AeP")

    assert str(exc_info.value) == GENERIC_RESOLVE_ERROR
    assert mock_head.call_count == 1


@patch("app.url_resolver.requests.head")
@patch("app.url_resolver.socket.getaddrinfo")
def test_link_normal_no_meli_la_no_dispara_ningun_request_de_red(mock_getaddrinfo, mock_head):
    url_normal = "https://articulo.mercadolibre.cl/MLC-123456789-notebook-lenovo"

    resultado = resolve_meli_short_link(url_normal)

    assert resultado == url_normal
    mock_head.assert_not_called()
    mock_getaddrinfo.assert_not_called()


def test_pinning_previene_dns_rebinding_toctou():
    """
    Simula un ataque de DNS rebinding (TTL=0): un atacante que controla
    su propio DNS podría devolver una IP pública en la validación y, si
    algo volviera a resolver el hostname por su cuenta al conectar,
    una IP privada distinta en esa segunda resolución.

    Se instala primero un socket.getaddrinfo "real" simulado que SIEMPRE
    devuelve la IP privada del rebinding (representa lo que pasaría sin
    pinning). Encima de eso se activa _pin_dns_resolution con la IP
    pública ya validada. El test confirma que una resolución hecha
    DENTRO del pin (como la que haría urllib3 al conectar el socket)
    obtiene la IP pineada, nunca la del rebinding — es decir, que el
    ataque no tiene efecto.
    """
    hostname = "rebinding-attacker.example"
    ip_publica_validada = "93.184.216.50"
    ip_privada_del_rebinding = "10.0.0.5"

    def _dns_del_atacante_con_rebinding(host, *args, **kwargs):
        if host == hostname:
            return _addrinfo(ip_privada_del_rebinding)
        raise AssertionError(f"host inesperado en el test: {host}")

    with patch("socket.getaddrinfo", side_effect=_dns_del_atacante_con_rebinding):
        with _pin_dns_resolution(hostname, ip_publica_validada):
            # Esto simula exactamente lo que hace urllib3 internamente
            # al abrir el socket de la conexión: resuelve el hostname
            # justo antes de conectar.
            resultado = socket.getaddrinfo(hostname, 443)

        # Fuera del pin, la misma resolución vuelve a caer en el DNS
        # "del atacante" — confirma que el pin es acotado, no global.
        resultado_fuera_del_pin = socket.getaddrinfo(hostname, 443)

    ip_obtenida_con_pin = resultado[0][4][0]
    ip_obtenida_fuera_del_pin = resultado_fuera_del_pin[0][4][0]

    assert ip_obtenida_con_pin == ip_publica_validada
    assert ip_obtenida_con_pin != ip_privada_del_rebinding
    assert ip_obtenida_fuera_del_pin == ip_privada_del_rebinding


class _OKHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):  # silencia el log del servidor de prueba
        pass


@pytest.fixture()
def https_test_server(tmp_path):
    """
    Servidor HTTPS real en 127.0.0.1 (puerto efímero), con un
    certificado autofirmado emitido para "internal-test.invalid" — un
    hostname del TLD reservado .invalid (RFC 2606): jamás resuelve por
    DNS real, así que si el pinning fallara y algo intentara resolverlo
    de verdad, el test falla con un error de DNS en vez de arriesgarse a
    tocar la red real.
    """
    hostname = "internal-test.invalid"
    certfile = tmp_path / "cert.pem"
    keyfile = tmp_path / "key.pem"
    conf = tmp_path / "san.cnf"
    conf.write_text(
        f"""
[req]
distinguished_name = req_distinguished_name
x509_extensions = v3_req
prompt = no

[req_distinguished_name]
CN = {hostname}

[v3_req]
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names

[alt_names]
DNS.1 = {hostname}
"""
    )
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-nodes",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(keyfile),
            "-out",
            str(certfile),
            "-days",
            "1",
            "-config",
            str(conf),
        ],
        check=True,
        capture_output=True,
    )

    httpd = HTTPServer(("127.0.0.1", 0), _OKHandler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(certfile), keyfile=str(keyfile))
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    port = httpd.server_address[1]

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    try:
        yield {"hostname": hostname, "port": port, "certfile": str(certfile)}
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_pinning_preserva_verificacion_tls_contra_servidor_https_real(https_test_server):
    """
    Prueba con un servidor HTTPS REAL (no un MagicMock): si el pinning
    rompiera el SNI/hostname que se usa para verificar el certificado
    (por ejemplo, si conectara usando la IP en vez del hostname para
    ese propósito), esta llamada fallaría con un error de verificación
    de certificado, porque el certificado del servidor de prueba está
    emitido para "internal-test.invalid", no para "127.0.0.1".

    Que la respuesta llegue en 200 confirma dos cosas a la vez: que la
    conexión de socket sí fue pineada a 127.0.0.1 (ahí es donde escucha
    el servidor de prueba, no en la IP real — inexistente — de un host
    ".invalid"), y que la verificación TLS del certificado se siguió
    haciendo contra el hostname original.
    """
    hostname = https_test_server["hostname"]
    port = https_test_server["port"]
    certfile = https_test_server["certfile"]

    with _pin_dns_resolution(hostname, "127.0.0.1"):
        response = requests.head(f"https://{hostname}:{port}/", timeout=5, verify=certfile)

    assert response.status_code == 200
