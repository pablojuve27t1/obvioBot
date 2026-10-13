"""
polymarket_dns.py -- resuelve los hosts de polymarket.com contra DNS publico
(Google/Cloudflare) en lugar del resolver del sistema.

Algunos ISPs bloquean a nivel DNS los dominios de mercados de prediccion por
regulacion local, aunque la IP en si no este bloqueada -- una vez resuelta,
la conexion HTTPS funciona normal. Este modulo parchea `socket.getaddrinfo`
(usado internamente por `requests`/`urllib3`) solo para hosts *.polymarket.com;
cualquier otro host sigue resolviendo con el DNS normal del sistema.

Se importa una sola vez, antes de hacer cualquier request, y el parche queda
activo para el resto del proceso (mismo enfoque que src/net/polymarketDns.js
del bot hermano en Node).
"""
import socket
import time
import logging

import dns.resolver

log = logging.getLogger("polymarket_dns")

_orig_getaddrinfo = socket.getaddrinfo
_resolver = dns.resolver.Resolver(configure=False)
_resolver.nameservers = ["8.8.8.8", "1.1.1.1"]
_resolver.timeout = 3
_resolver.lifetime = 3

_CACHE_TTL = 300  # segundos
_cache = {}  # host -> (ip, expires_at)

_PATCHED = False


def _is_polymarket_host(host: str) -> bool:
    return host == "polymarket.com" or host.endswith(".polymarket.com")


def _resolve_public(host: str):
    cached = _cache.get(host)
    now = time.time()
    if cached and cached[1] > now:
        return cached[0]
    try:
        answer = _resolver.resolve(host, "A")
        ip = answer[0].to_text()
        _cache[host] = (ip, now + _CACHE_TTL)
        return ip
    except Exception as e:
        log.warning(f"no se pudo resolver {host} via DNS publico: {e}")
        return None


def _patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if isinstance(host, str) and _is_polymarket_host(host):
        ip = _resolve_public(host)
        if ip:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
    return _orig_getaddrinfo(host, port, family, type, proto, flags)


def install():
    """Activa el parche (idempotente)."""
    global _PATCHED
    if _PATCHED:
        return
    socket.getaddrinfo = _patched_getaddrinfo
    _PATCHED = True
    log.info("polymarket_dns: resolviendo *.polymarket.com via 8.8.8.8 / 1.1.1.1")


install()
