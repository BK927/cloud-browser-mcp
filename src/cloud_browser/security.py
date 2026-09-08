import http.client
import ipaddress
import re
import socket
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from .models import BrowserError

DNS_CHECK_METHOD = "CB-DNS-CHECK"
DNS_POLICY_HEADER = "X-Cloud-Browser-DNS-Policy"
DNS_POLICY_VERSION = "public-v1"


def public_document_csp(callback: str | None = None) -> str:
    """Only pass an already validated, exact OAuth callback from the server.

    CSP cannot enforce query equality (or redirect-path equality); OAuth still
    checks the full registered URI. Encode delimiters to prevent header/directive
    injection, and never add a wildcard or a scheme-wide source.
    """
    targets = "'self'"
    if callback is not None:
        parsed = urlsplit(callback)
        source = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        targets += " " + quote(source, safe=":/%[]")
    return f"default-src 'none'; form-action {targets}; frame-ancestors 'none'; base-uri 'none'"


SENSITIVE = re.compile(
    r"password|passwd|one.?time|otp|auth.?code|credit.?card|card.?number|cc-number|cc-csc|secret|api.?key|access.?token|refresh.?token|security.?answer",
    re.I,
)
TOKEN = re.compile(
    r"(?:Bearer\s+\S+|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{15,})"
)
SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|otp|authorization|cookie|api[_ -]?key|access[_ -]?token|refresh[_ -]?token|secret)\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;<>]+)"
)


def redact(text: str) -> str:
    return SECRET_ASSIGNMENT.sub(
        lambda m: m.group(1) + "=[REDACTED]", TOKEN.sub("[REDACTED]", text)
    )


def redact_tree(value):
    """Scrub nested observation metadata as well as top-level strings."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_tree(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_tree(item) for key, item in value.items()}
    return value


def safe_url(url: str) -> str:
    try:
        p = urlsplit(url)
        host = p.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if p.port:
            host += f":{p.port}"
        # Preserve ordinary navigation/search parameters, not arbitrary opaque
        # server parameters or authentication material. Never preserve userinfo.
        public_keys = {
            "q",
            "query",
            "search",
            "search_query",
            "p",
            "page",
            "sort",
            "order",
            "filter",
            "id",
            "v",
            "t",
            "lang",
            "tab",
            "view",
            "category",
            "tag",
        }

        def public_value(key, value):
            if (
                key.casefold() not in public_keys
                or len(value) > 1000
                or TOKEN.search(value)
                or SECRET_ASSIGNMENT.search(value)
                or re.search(r"[A-Za-z0-9_-]{40,}", value)
            ):
                return "[REDACTED]"
            return value

        query = urlencode(
            [(redact(k), public_value(k, v)) for k, v in parse_qsl(p.query, keep_blank_values=True)]
        )
        fragment = p.fragment
        if (
            len(fragment) > 1000
            or re.search(
                r"token|auth|session|password|secret|(?:^|[?&])code=|[A-Za-z0-9_-]{40,}",
                fragment,
                re.I,
            )
            or TOKEN.search(fragment)
        ):
            fragment = ""
        elif "?" in fragment:
            anchor, values = fragment.split("?", 1)
            fragment = (
                anchor
                + "?"
                + urlencode(
                    [(k, public_value(k, v)) for k, v in parse_qsl(values, keep_blank_values=True)]
                )
            )
        elif "=" in fragment:
            fragment = ""
        return urlunsplit((p.scheme, host, redact(p.path), query, fragment))
    except ValueError:
        return "[REDACTED URL]"


def _public_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        # Reject translation/tunneling ranges (NAT64, IPv4-mapped, 6to4,
        # Teredo) that can conceal an otherwise denied IPv4 destination.
        return ip in ipaddress.ip_network("2000::/3") and not ip.sixtofour and not ip.teredo
    return True


def public_addresses(host: str, port: int) -> list[str]:
    addresses = sorted({r[4][0] for r in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})

    if not addresses or any(not _public_ip(ip) for ip in addresses):
        raise ValueError("Destination is not globally routable")
    return addresses


def _proxy_dns_check(host: str, port: int, proxy: str):
    """DNS-only preflight at the operator-owned egress; never contact the site.

    This is not a reusable authorization: actual proxy requests resolve, validate
    every result, and pin a numeric destination independently on every connection.
    """
    connection = None
    try:
        endpoint = urlsplit(proxy)
        if (
            endpoint.scheme != "http"
            or not endpoint.hostname
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.path not in ("", "/")
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError("Unsupported egress validation endpoint")
        hostname = host.encode("idna").decode("ascii")
        authority = f"[{hostname}]:{port}" if ":" in hostname else f"{hostname}:{port}"
        connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port or 80, timeout=5)
        connection.request(
            DNS_CHECK_METHOD,
            authority,
            headers={"Host": authority, "Connection": "close", "Content-Length": "0"},
        )
        result = connection.getresponse()
        if result.getheader(DNS_POLICY_HEADER) != DNS_POLICY_VERSION:
            raise ValueError("Egress does not provide the required DNS policy")
        status = result.status
        if status not in (204, 403):
            raise ValueError("Egress validation unavailable")
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise BrowserError(
            "EGRESS_UNAVAILABLE",
            "Required egress DNS validation is unavailable; no navigation performed",
        ) from exc
    finally:
        if connection is not None:
            connection.close()
    if status == 403:
        raise ValueError("Egress rejected the destination")


def validate_url(url: str, *, dns_proxy: str | None = None):
    try:
        p = urlsplit(url)
        if (
            p.scheme not in ("http", "https")
            or not p.hostname
            or p.username is not None
            or p.password is not None
        ):
            raise ValueError("Only HTTP(S) URLs without credentials are supported")
        if p.port not in (None, 80, 443):
            raise ValueError("Only ports 80 and 443 are supported")
        try:
            literal = ipaddress.ip_address(p.hostname)
        except ValueError:
            literal = None
        if literal is not None and not _public_ip(str(literal)):
            raise ValueError("Private or unsupported IP literal")
        port = p.port or (443 if p.scheme == "https" else 80)
        if dns_proxy:
            _proxy_dns_check(p.hostname, port, dns_proxy)
        else:
            public_addresses(p.hostname, port)
    except (ValueError, OSError) as exc:
        raise BrowserError(
            "INVALID_URL", "URL is unsupported, unresolved or targets a private network"
        ) from exc


def origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"
