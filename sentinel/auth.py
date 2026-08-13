"""JWT bearer token verification for Sentinel (Phase 7).

Sentinel owns JWT verification: tokens must be signed with an allowlisted
HS* algorithm and carry both ``exp`` and ``sub`` (the tenant id). PyJWT is
passed the configured allowlist explicitly, so ``alg: none`` and algorithms
outside the allowlist are rejected by PyJWT itself.

PyJWT 2.13 raises ``InvalidSubjectError`` for non-string ``sub`` claims, which
maps to MALFORMED here; the defensive string check below keeps the
MISSING_TENANT_CLAIM contract stable regardless of PyJWT's behavior.
"""

import enum

import jwt


class AuthReason(enum.StrEnum):
    MISSING_TOKEN = "missing_token"
    INVALID_SIGNATURE = "invalid_signature"
    EXPIRED = "expired"
    UNSUPPORTED_ALGORITHM = "unsupported_algorithm"
    MISSING_TENANT_CLAIM = "missing_tenant_claim"
    MALFORMED = "malformed"


class AuthenticationError(Exception):
    """Raised when a bearer token cannot be authenticated."""

    def __init__(self, reason: AuthReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


def verify_bearer_token(
    token: str | None,
    *,
    secret: str,
    algorithms: frozenset[str],
) -> str:
    """Verify a bearer JWT and return its validated ``sub`` tenant id."""
    if token is None or not token.strip():
        raise AuthenticationError(AuthReason.MISSING_TOKEN)
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=sorted(algorithms),
            options={"require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError(AuthReason.EXPIRED) from exc
    except jwt.InvalidSignatureError as exc:
        raise AuthenticationError(AuthReason.INVALID_SIGNATURE) from exc
    except jwt.InvalidAlgorithmError as exc:
        raise AuthenticationError(AuthReason.UNSUPPORTED_ALGORITHM) from exc
    except jwt.MissingRequiredClaimError as exc:
        if exc.claim == "sub":
            raise AuthenticationError(AuthReason.MISSING_TENANT_CLAIM) from exc
        raise AuthenticationError(AuthReason.MALFORMED) from exc
    except jwt.DecodeError as exc:
        raise AuthenticationError(AuthReason.MALFORMED) from exc
    except jwt.PyJWTError as exc:
        # PyJWT's own base class: covers InvalidTokenError subclasses and the
        # InvalidKeyError family, which subclasses PyJWTError directly.
        raise AuthenticationError(AuthReason.MALFORMED) from exc
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub.strip():
        raise AuthenticationError(AuthReason.MISSING_TENANT_CLAIM)
    return sub
