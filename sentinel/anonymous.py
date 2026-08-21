"""Anonymous client identity for unauthenticated rate limiting (Phase 19).

Unauthenticated endpoints (login, signup, password reset) have no JWT ``sub``
to key on, so Sentinel mints a server-issued anonymous client id, delivers it
as an HMAC-signed expiring cookie, and derives the request's client IP from
``request.client`` — the socket peer as resolved by the ASGI server's
trusted-proxy layer. The library never reads ``X-Forwarded-For`` or any other
client-supplied forwarding header (SEC-ANON-01): raw header values are the one
thing a client fully controls.

Cookie format: ``<client_id>.<exp_epoch>.<hmac_hex>`` where ``client_id`` is 32
hex chars (16 CSRNG bytes) and the MAC is
``HMAC-SHA256(secret, "sentinel-anon:v1:" + client_id + ":" + exp)``.
``anonymous_cookie_secret`` is a dedicated config secret (OWASP device-cookie
guidance: separate keys per token type) and is required iff any anonymous
policy exists. Parsing is all-or-nothing: tampered, malformed, or expired
cookies yield ``None`` and the request is treated as cookie-less (IP-only
bucket).

Raw identifiers never reach Redis keys, logs, or metrics: bucket identities
are the strings ``anon:cookie:{client_id}`` / ``anon:ip:{ip}`` and are
sha256-hashed by the key builder (invariant #4's hygiene, extended to
anonymous identity).
"""

import hashlib
import hmac
import ipaddress
import logging
import os
import string
import time

_LOGGER = logging.getLogger("sentinel")
_ANON_COOKIE_SCHEME = "sentinel-anon:v1"
_CLIENT_ID_HEX_LENGTH = 32


def mint_client_id() -> str:
    """Return a fresh 32-hex-char opaque client id (16 CSRNG bytes)."""
    return os.urandom(16).hex()


def sign_cookie(client_id: str, exp_epoch: int, secret: str) -> str:
    """Return ``<client_id>.<exp>.<mac>`` signed with the anonymous secret."""
    message = f"{_ANON_COOKIE_SCHEME}:{client_id}:{exp_epoch}".encode()
    mac = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"{client_id}.{exp_epoch}.{mac}"


def parse_cookie(
    value: str | None,
    *,
    secret: str,
    now: int,
) -> str | None:
    """Verify an anonymous client cookie and return its client id.

    ``None`` for missing, malformed, tampered, or expired cookies — the caller
    treats that as "no cookie" and mints a fresh one. ``now`` is epoch seconds.
    """
    if value is None or not value.strip():
        return None
    parts = value.split(".")
    if len(parts) != 3:
        return None
    client_id, exp, mac = parts
    if not (
        len(client_id) == _CLIENT_ID_HEX_LENGTH and all(c in string.hexdigits for c in client_id)
    ):
        return None
    if not exp.isdigit():
        return None
    try:
        exp_epoch = int(exp)
    except ValueError:
        return None
    expected = sign_cookie(client_id, exp_epoch, secret)
    if not hmac.compare_digest(mac, expected.split(".")[2]):
        return None
    if exp_epoch <= now:
        return None
    return client_id


def mint_cookie(secret: str, ttl_seconds: int, now: int | None = None) -> tuple[str, str]:
    """Mint a new client id and its signed cookie value.

    Returns ``(client_id, cookie_value)``; ``now`` is epoch seconds (defaults
    to the wall clock).
    """
    if now is None:
        now = int(time.time())
    client_id = mint_client_id()
    return client_id, sign_cookie(client_id, now + ttl_seconds, secret)


def is_private_ip(ip: str) -> bool:
    """True for private, loopback, and link-local addresses.

    A private/loopback peer on an anonymous policy usually means the host is
    not resolving forwarding headers (proxy-collapse: all traffic shares one
    bucket) — over-blocking, never quota bypass, but worth a warning.
    """
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


def client_ip(host: str | None) -> str:
    """Normalize the client IP for identity keying.

    ``host`` comes from ``request.client.host`` — the ASGI-server-resolved
    peer, never a raw header. Missing or non-IP peers collapse to a single
    ``unknown`` identity (fail-safe over-blocking) with a WARNING.
    """
    if host is None or not host.strip():
        _LOGGER.warning("anonymous rate limiting has no client ip; collapsing to the shared bucket")
        return "unknown"
    try:
        ipaddress.ip_address(host)
    except ValueError:
        _LOGGER.warning(
            "anonymous rate limiting received a non-IP client peer; collapsing to the shared bucket"
        )
        return "unknown"
    if is_private_ip(host):
        _LOGGER.warning(
            "anonymous rate limiting sees a private/loopback client ip; "
            "verify trusted-proxy configuration (uvicorn --proxy-headers --forwarded-allow-ips)"
        )
    return host


def anonymous_cookie_identity(client_id: str) -> str:
    return f"anon:cookie:{client_id}"


def anonymous_ip_identity(ip: str) -> str:
    return f"anon:ip:{ip}"


def hash_identity(identity: str) -> str:
    """sha256-hex of an identity string — raw identities never reach keys or logs."""
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()
