"""JWT bearer token verification unit tests (Phase 7)."""

import time

import jwt
import pytest
from sentinel.auth import AuthenticationError, AuthReason, verify_bearer_token

SECRET = "test-secret-0123456789abcdef0123456789abcdef"
ALLOWLIST = frozenset({"HS256", "HS384", "HS512"})


def _encode(
    payload: dict[str, object], secret: str | None = SECRET, algorithm: str = "HS256"
) -> str:
    return jwt.encode(payload, secret, algorithm=algorithm)


def _future_exp() -> int:
    return int(time.time()) + 3_600


def test_valid_hs256_token_returns_sub() -> None:
    token = _encode({"sub": "tenant-a", "exp": _future_exp()})
    assert verify_bearer_token(token, secret=SECRET, algorithms=ALLOWLIST) == "tenant-a"


def test_valid_allowed_algorithm_hs384() -> None:
    token = _encode({"sub": "tenant-b", "exp": _future_exp()}, algorithm="HS384")
    assert verify_bearer_token(token, secret=SECRET, algorithms=ALLOWLIST) == "tenant-b"


def test_expired_token() -> None:
    token = _encode({"sub": "tenant-a", "exp": int(time.time()) - 60})
    with pytest.raises(AuthenticationError) as exc:
        verify_bearer_token(token, secret=SECRET, algorithms=ALLOWLIST)
    assert exc.value.reason is AuthReason.EXPIRED


def test_wrong_secret() -> None:
    token = _encode({"sub": "tenant-a", "exp": _future_exp()})
    with pytest.raises(AuthenticationError) as exc:
        verify_bearer_token(
            token, secret="another-secret-0123456789abcdef0123456789abcdef", algorithms=ALLOWLIST
        )
    assert exc.value.reason is AuthReason.INVALID_SIGNATURE


def test_unsupported_algorithm() -> None:
    token = _encode({"sub": "tenant-a", "exp": _future_exp()}, algorithm="HS512")
    with pytest.raises(AuthenticationError) as exc:
        verify_bearer_token(token, secret=SECRET, algorithms=frozenset({"HS256"}))
    assert exc.value.reason is AuthReason.UNSUPPORTED_ALGORITHM


def test_alg_none_rejected() -> None:
    token = _encode({"sub": "tenant-a", "exp": _future_exp()}, secret=None, algorithm="none")
    with pytest.raises(AuthenticationError) as exc:
        verify_bearer_token(token, secret=SECRET, algorithms=ALLOWLIST)
    assert exc.value.reason is AuthReason.UNSUPPORTED_ALGORITHM


def test_missing_sub() -> None:
    token = _encode({"exp": _future_exp()})
    with pytest.raises(AuthenticationError) as exc:
        verify_bearer_token(token, secret=SECRET, algorithms=ALLOWLIST)
    assert exc.value.reason is AuthReason.MISSING_TENANT_CLAIM


def test_empty_sub() -> None:
    token = _encode({"sub": "", "exp": _future_exp()})
    with pytest.raises(AuthenticationError) as exc:
        verify_bearer_token(token, secret=SECRET, algorithms=ALLOWLIST)
    assert exc.value.reason is AuthReason.MISSING_TENANT_CLAIM


def test_whitespace_sub() -> None:
    token = _encode({"sub": "   ", "exp": _future_exp()})
    with pytest.raises(AuthenticationError) as exc:
        verify_bearer_token(token, secret=SECRET, algorithms=ALLOWLIST)
    assert exc.value.reason is AuthReason.MISSING_TENANT_CLAIM


def test_non_string_sub_maps_to_malformed_via_pyjwt() -> None:
    token = _encode({"sub": 123, "exp": _future_exp()})
    with pytest.raises(AuthenticationError) as exc:
        verify_bearer_token(token, secret=SECRET, algorithms=ALLOWLIST)
    assert exc.value.reason is AuthReason.MALFORMED


def test_defensive_non_string_sub_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jwt, "decode", lambda token, key, **kwargs: {"exp": 1, "sub": 123})
    with pytest.raises(AuthenticationError) as exc:
        verify_bearer_token("irrelevant", secret=SECRET, algorithms=ALLOWLIST)
    assert exc.value.reason is AuthReason.MISSING_TENANT_CLAIM


def test_missing_exp() -> None:
    token = _encode({"sub": "tenant-a"})
    with pytest.raises(AuthenticationError) as exc:
        verify_bearer_token(token, secret=SECRET, algorithms=ALLOWLIST)
    assert exc.value.reason is AuthReason.MALFORMED


def test_malformed_jwt() -> None:
    with pytest.raises(AuthenticationError) as exc:
        verify_bearer_token("not.a.jwt", secret=SECRET, algorithms=ALLOWLIST)
    assert exc.value.reason is AuthReason.MALFORMED


def test_invalid_key_maps_to_malformed() -> None:
    token = _encode({"sub": "tenant-a", "exp": _future_exp()})
    with pytest.raises(AuthenticationError) as exc:
        verify_bearer_token(token, secret="", algorithms=ALLOWLIST)
    assert exc.value.reason is AuthReason.MALFORMED


def test_missing_token_none() -> None:
    with pytest.raises(AuthenticationError) as exc:
        verify_bearer_token(None, secret=SECRET, algorithms=ALLOWLIST)
    assert exc.value.reason is AuthReason.MISSING_TOKEN


def test_missing_token_blank() -> None:
    for token in ("", "   "):
        with pytest.raises(AuthenticationError) as exc:
            verify_bearer_token(token, secret=SECRET, algorithms=ALLOWLIST)
        assert exc.value.reason is AuthReason.MISSING_TOKEN
