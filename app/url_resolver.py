"""
Resolución segura de links acortados de meli.la.

Mercado Libre genera links cortos (https://meli.la/xxxxxxx) al compartir
un producto desde su app. No contienen el item_id en el texto — solo se
puede obtener siguiendo la redirección real hasta el link largo
(articulo.mercadolibre.xx/...).

Seguir una redirección controlada por un tercero hacia una URL arbitraria
es un vector clásico de SSRF: un link acortado podría apuntar a
localhost, a la IP de metadata de nube (169.254.169.254), o a cualquier
servicio interno de la red donde corre este servidor. Por eso, en CADA
salto (incluido el primero, hacia meli.la mismo) se resuelve el hostname
a IP y se rechaza si es privada, loopback, link-local, reservada o
multicast — ANTES de hacer cualquier request hacia esa IP.

Protección contra DNS rebinding (TOCTOU): validar la IP resuelta no basta
si el HEAD posterior vuelve a resolver el hostname por su cuenta — un
atacante con su propio DNS (TTL=0) puede devolver una IP pública en la
validación y una IP privada distinta en esa segunda resolución. Por eso
la conexión real se PINNEA a la IP exacta ya validada (ver
_pin_dns_resolution): se reemplaza socket.getaddrinfo de forma acotada,
solo durante ese HEAD puntual, para que la única respuesta posible sea la
IP que ya inspeccionamos. La verificación TLS (SNI + hostname del
certificado) sigue usando el hostname de la URL, no la IP — urllib3/ssl
nunca leen la IP para eso, así que el pin es transparente para HTTPS real
(ver test con servidor HTTPS real en tests/test_url_resolver.py).

Política de errores: el mensaje que ve el usuario final es siempre el
genérico GENERIC_RESOLVE_ERROR. El detalle específico (qué salto falló,
a qué IP resolvió, etc.) solo va al log interno — nunca se expone, para
no darle a un atacante información sobre qué validación exacta lo bloqueó.
"""

import contextlib
import ipaddress
import logging
import socket
from urllib.parse import urljoin, urlparse

import requests

from app.utils import is_allowed_domain

logger = logging.getLogger(__name__)

MELI_SHORT_LINK_HOSTNAME = "meli.la"
MAX_HOPS = 5
REQUEST_TIMEOUT_SECONDS = 5

GENERIC_RESOLVE_ERROR = "No se pudo procesar el link."


class ShortLinkResolutionError(Exception):
    """
    Error al resolver un link acortado. El mensaje de esta excepción es
    SIEMPRE el genérico apto para mostrar al usuario final — el detalle
    real ya quedó logueado internamente antes de lanzarla.
    """


def _validate_hostname_is_public(hostname: str, *, hop: int) -> set:
    """
    Lanza ShortLinkResolutionError si `hostname` no resuelve
    exclusivamente a IPs públicas. Si pasa la validación, devuelve el
    conjunto de IPs resueltas (para poder pinnear la conexión a una de
    ellas más adelante).

    family=socket.AF_UNSPEC (explícito, aunque es el default) para que
    esto consulte tanto registros IPv4 como IPv6 — un hostname que solo
    resolviera a una IP interna vía IPv6 (ej. ::1) no debe colarse por
    revisar solo IPv4.
    """
    try:
        addrinfo = socket.getaddrinfo(hostname, None, family=socket.AF_UNSPEC)
    except socket.gaierror as exc:
        logger.warning(
            "No se pudo resolver el hostname de un salto del link acortado",
            extra={"hop": hop, "hostname": hostname, "error": str(exc)},
        )
        raise ShortLinkResolutionError(GENERIC_RESOLVE_ERROR) from exc

    resolved_ips = {info[4][0] for info in addrinfo}
    for ip_str in resolved_ips:
        ip = ipaddress.ip_address(ip_str)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            logger.warning(
                "Salto de link acortado rechazado: el hostname resuelve a una IP no pública (posible SSRF)",
                extra={"hop": hop, "hostname": hostname, "ip": ip_str, "severity": "high"},
            )
            raise ShortLinkResolutionError(GENERIC_RESOLVE_ERROR)

    return resolved_ips


@contextlib.contextmanager
def _pin_dns_resolution(hostname: str, pinned_ip: str):
    """
    Reemplaza socket.getaddrinfo TEMPORALMENTE — solo dentro de este
    `with`, nunca de forma global ni persistente — para que resolver
    `hostname` devuelva EXCLUSIVAMENTE `pinned_ip`: la misma IP que ya
    validamos como pública un momento antes.

    Esto cierra la ventana de TOCTOU (DNS rebinding): sin este pin,
    requests/urllib3 resolverían `hostname` de nuevo al abrir el socket,
    y un DNS controlado por el atacante (TTL=0) podría devolver una IP
    distinta (privada) en esa segunda resolución, distinta de la que ya
    inspeccionamos.

    No afecta la verificación TLS: urllib3 arma el SNI y valida el
    certificado contra el HOSTNAME de la URL, nunca contra la IP — ese
    valor nunca pasa por socket.getaddrinfo, así que un certificado real
    emitido para `hostname` se sigue validando correctamente (ver test
    con servidor HTTPS real).

    Cualquier otro hostname resuelto durante la ventana (no debería
    ocurrir en este flujo, pero por seguridad) cae a la resolución real.
    """
    original_getaddrinfo = socket.getaddrinfo
    is_ipv6 = ":" in pinned_ip
    pinned_family = socket.AF_INET6 if is_ipv6 else socket.AF_INET

    def _pinned_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if host != hostname:
            return original_getaddrinfo(host, port, family, type, proto, flags)
        resolved_port = port or 0
        sockaddr = (pinned_ip, resolved_port, 0, 0) if is_ipv6 else (pinned_ip, resolved_port)
        return [(pinned_family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]

    socket.getaddrinfo = _pinned_getaddrinfo
    try:
        yield
    finally:
        socket.getaddrinfo = original_getaddrinfo


def _pinned_head_request(url: str, hostname: str, pinned_ip: str) -> requests.Response:
    """HEAD a `url`, forzando a nivel de socket la conexión a `pinned_ip` (ver _pin_dns_resolution)."""
    with _pin_dns_resolution(hostname, pinned_ip):
        return requests.head(url, timeout=REQUEST_TIMEOUT_SECONDS, allow_redirects=False)


def resolve_meli_short_link(url: str) -> str:
    """
    Sigue un link acortado de meli.la hasta su destino final, validando
    en cada salto que el hostname resuelva solo a IPs públicas, y
    pinneando la conexión real a esa IP exacta (protección contra DNS
    rebinding). Si el hostname de `url` no es exactamente "meli.la", la
    devuelve sin cambios y sin tocar la red — así se puede llamar
    incondicionalmente desde app/products.py sin duplicar ese chequeo.

    Devuelve la URL final, ya confirmada contra la misma allowlist de
    dominios de Mercado Libre que usa el resto de la app.

    Raises:
        ShortLinkResolutionError: con un mensaje genérico apto para el
            usuario final, ante cualquier fallo (timeout, error HTTP del
            acortador, más de MAX_HOPS saltos, un salto que resuelva a
            una IP no pública, o un destino final fuera de la allowlist).
            El detalle específico de cada caso queda en el log interno.
    """
    current_url = url
    if (urlparse(current_url).hostname or "").lower() != MELI_SHORT_LINK_HOSTNAME:
        return current_url

    for hop in range(MAX_HOPS):
        hostname = (urlparse(current_url).hostname or "").lower()
        if not hostname:
            logger.warning(
                "Salto de link acortado con hostname vacío o inválido",
                extra={"hop": hop, "url": current_url},
            )
            raise ShortLinkResolutionError(GENERIC_RESOLVE_ERROR)

        resolved_ips = _validate_hostname_is_public(hostname, hop=hop)
        # Determinístico (no aleatorio) para que el comportamiento sea
        # reproducible en tests y en producción.
        pinned_ip = sorted(resolved_ips)[0]

        try:
            response = _pinned_head_request(current_url, hostname, pinned_ip)
        except requests.exceptions.RequestException as exc:
            logger.warning(
                "Error de red siguiendo un link acortado",
                extra={"hop": hop, "url": current_url, "error": str(exc)},
            )
            raise ShortLinkResolutionError(GENERIC_RESOLVE_ERROR) from exc

        if response.is_redirect:
            location = response.headers.get("Location")
            if not location:
                logger.warning(
                    "Redirección sin header Location",
                    extra={"hop": hop, "url": current_url, "status_code": response.status_code},
                )
                raise ShortLinkResolutionError(GENERIC_RESOLVE_ERROR)
            current_url = urljoin(current_url, location)
            continue

        if response.status_code >= 400:
            logger.warning(
                "El acortador devolvió un error HTTP",
                extra={"hop": hop, "url": current_url, "status_code": response.status_code},
            )
            raise ShortLinkResolutionError(GENERIC_RESOLVE_ERROR)

        # No es redirección ni error: este es el destino final.
        if not is_allowed_domain(current_url):
            logger.warning(
                "Link acortado resuelto a un dominio fuera de la allowlist de Mercado Libre",
                extra={"hop": hop, "final_url": current_url},
            )
            raise ShortLinkResolutionError(GENERIC_RESOLVE_ERROR)

        return current_url

    logger.warning(
        "Link acortado excedió el máximo de saltos permitidos",
        extra={"max_hops": MAX_HOPS, "last_url": current_url},
    )
    raise ShortLinkResolutionError(GENERIC_RESOLVE_ERROR)
