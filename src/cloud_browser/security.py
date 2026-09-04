import ipaddress
import re
import socket
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import BrowserError

SENSITIVE = re.compile(
    r"password|passwd|one.?time|otp|auth.?code|credit.?card|card.?number|cc-number|cc-csc|secret|api.?key|access.?token|refresh.?token|security.?answer",
    re.I,
)
TOKEN = re.compile(
    r"(?:Bearer\s+\S+|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{15,})"
)


def redact(text: str) -> str:
    return TOKEN.sub("[REDACTED]", text)


def safe_url(url: str) -> str:
    try:
        p = urlsplit(url)
        host = p.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if p.port:
            host += f":{p.port}"
        # Query names remain useful; values and fragments are never emitted.
        query = urlencode(
            [(k, "[REDACTED]") for k, _ in parse_qsl(p.query, keep_blank_values=True)]
        )
        return urlunsplit((p.scheme, host, redact(p.path), query, ""))
    except ValueError:
        return "[REDACTED URL]"


def public_addresses(host: str, port: int) -> list[str]:
    addresses = sorted({r[4][0] for r in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})

    def allowed(value):
        ip = ipaddress.ip_address(value)
        if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            return False
        if isinstance(ip, ipaddress.IPv6Address):
            # Reject translation/tunneling ranges (NAT64, IPv4-mapped, 6to4,
            # Teredo) that can conceal an otherwise denied IPv4 destination.
            return ip in ipaddress.ip_network("2000::/3") and not ip.sixtofour and not ip.teredo
        return True

    if not addresses or any(not allowed(ip) for ip in addresses):
        raise ValueError("Destination is not globally routable")
    return addresses


def validate_url(url: str):
    try:
        p = urlsplit(url)
        if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
            raise ValueError("Only HTTP(S) URLs without credentials are supported")
        if p.port not in (None, 80, 443):
            raise ValueError("Only ports 80 and 443 are supported")
        public_addresses(p.hostname, p.port or (443 if p.scheme == "https" else 80))
    except (ValueError, OSError) as exc:
        raise BrowserError(
            "INVALID_URL", "URL is unsupported, unresolved or targets a private network"
        ) from exc


def origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"
