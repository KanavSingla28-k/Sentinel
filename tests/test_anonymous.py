"""Unit tests for anonymous client identity (Phase 19)."""

import time

import pytest
from sentinel.anonymous import (
    anonymous_cookie_identity,
    anonymous_ip_identity,
    client_ip,
    hash_identity,
    is_private_ip,
    mint_client_id,
    mint_cookie,
    parse_cookie,
    sign_cookie,
)

SECRET = "anon-secret-0123456789abcdef0123456789abcdef"
TTL = 2_592_000


def _valid_cookie(now: int, *, secret: str = SECRET) -> str:
    _, cookie = mint_cookie(secret, TTL, now=now)
    return cookie


def test_mint_client_id_is_32_hex_chars_and_unique() -> None:
    first = mint_client_id()
    second = mint_client_id()
    assert len(first) == 32
    assert all(c in "0123456789abcdef" for c in first)
    assert first != second


def test_cookie_round_trip() -> None:
    now = int(time.time())
    client_id, cookie = mint_cookie(SECRET, TTL, now=now)
    assert parse_cookie(cookie, secret=SECRET, now=now) == client_id


def test_cookie_value_format() -> None:
    now = int(time.time())
    client_id, cookie = mint_cookie(SECRET, TTL, now=now)
    parts = cookie.split(".")
    assert len(parts) == 3
    assert parts[0] == client_id
    assert parts[1] == str(now + TTL)
    assert len(parts[2]) == 64


@pytest.mark.security
def test_tampered_cookie_is_rejected() -> None:
    now = int(time.time())
    _, cookie = mint_cookie(SECRET, TTL, now=now)
    client_id, exp, mac = cookie.split(".")
    forged_mac = "0" * 64
    assert parse_cookie(f"{client_id}.{exp}.{forged_mac}", secret=SECRET, now=now) is None
    forged_client = "f" * 32
    assert parse_cookie(f"{forged_client}.{exp}.{mac}", secret=SECRET, now=now) is None
    assert parse_cookie(f"{client_id}.{int(exp) + 1}.{mac}", secret=SECRET, now=now) is None


@pytest.mark.security
def test_expired_cookie_is_rejected() -> None:
    now = int(time.time())
    _, cookie = mint_cookie(SECRET, TTL, now=now)
    assert parse_cookie(cookie, secret=SECRET, now=now + TTL + 1) is None


@pytest.mark.security
def test_wrong_secret_is_rejected() -> None:
    now = int(time.time())
    _, cookie = mint_cookie(SECRET, TTL, now=now)
    other = "other-secret-0123456789abcdef0123456789abcdef"
    assert parse_cookie(cookie, secret=other, now=now) is None


def test_malformed_cookies_are_rejected() -> None:
    now = int(time.time())
    for value in (None, "", " ", "not-a-cookie", "a.b", "a.b.c.d", "x" * 100):
        assert parse_cookie(value, secret=SECRET, now=now) is None


def test_non_hex_client_id_is_rejected() -> None:
    now = int(time.time())
    _, cookie = mint_cookie(SECRET, TTL, now=now)
    _, exp, mac = cookie.split(".")
    assert parse_cookie(f"{'g' * 32}.{exp}.{mac}", secret=SECRET, now=now) is None
    assert parse_cookie(f"{'a' * 31}.{exp}.{mac}", secret=SECRET, now=now) is None


def test_non_digit_exp_is_rejected() -> None:
    now = int(time.time())
    _, cookie = mint_cookie(SECRET, TTL, now=now)
    client_id, _, mac = cookie.split(".")
    assert parse_cookie(f"{client_id}.abc.{mac}", secret=SECRET, now=now) is None
    assert parse_cookie(f"{client_id}.12x34.{mac}", secret=SECRET, now=now) is None


def test_overflowing_exp_is_rejected() -> None:
    now = int(time.time())
    _, cookie = mint_cookie(SECRET, TTL, now=now)
    client_id, _, mac = cookie.split(".")
    huge = "9" * 5000
    assert parse_cookie(f"{client_id}.{huge}.{mac}", secret=SECRET, now=now) is None


def test_mint_cookie_defaults_to_wall_clock() -> None:
    client_id, cookie = mint_cookie(SECRET, TTL)
    parts = cookie.split(".")
    assert parts[0] == client_id
    assert int(parts[1]) > int(time.time())


def test_expired_at_exact_boundary_is_rejected() -> None:
    now = int(time.time())
    client_id, cookie = mint_cookie(SECRET, TTL, now=now)
    exp_epoch = int(cookie.split(".")[1])
    assert parse_cookie(cookie, secret=SECRET, now=exp_epoch) is None
    assert parse_cookie(cookie, secret=SECRET, now=exp_epoch - 1) == client_id


def test_sign_cookie_is_deterministic() -> None:
    assert sign_cookie("a" * 32, 12345, SECRET) == sign_cookie("a" * 32, 12345, SECRET)
    assert sign_cookie("a" * 32, 12345, SECRET) != sign_cookie("a" * 32, 12346, SECRET)


def test_hash_identity_is_sha256_hex_and_deterministic() -> None:
    first = hash_identity("anon:ip:1.2.3.4")
    second = hash_identity("anon:ip:1.2.3.4")
    assert first == second
    assert len(first) == 64
    assert all(c in "0123456789abcdef" for c in first)
    assert hash_identity("anon:ip:1.2.3.4") != hash_identity("anon:ip:1.2.3.5")


def test_identity_string_formats() -> None:
    assert anonymous_cookie_identity("abc") == "anon:cookie:abc"
    assert anonymous_ip_identity("1.2.3.4") == "anon:ip:1.2.3.4"


def test_is_private_ip_classification() -> None:
    assert is_private_ip("127.0.0.1") is True
    assert is_private_ip("10.0.0.5") is True
    assert is_private_ip("192.168.1.10") is True
    assert is_private_ip("::1") is True
    assert is_private_ip("169.254.169.254") is True
    assert is_private_ip("8.8.8.8") is False
    assert is_private_ip("2001:4860:4860::8888") is False
    assert is_private_ip("not-an-ip") is False


def test_client_ip_keeps_public_peer() -> None:
    assert client_ip("8.8.8.8") == "8.8.8.8"
    assert client_ip("2001:4860:4860::8888") == "2001:4860:4860::8888"


def test_client_ip_collapses_missing_peer(caplog: pytest.LogCaptureFixture) -> None:
    assert client_ip(None) == "unknown"
    assert client_ip("") == "unknown"
    assert client_ip("   ") == "unknown"
    assert any(
        "collapsing to the shared bucket" in record.getMessage() for record in caplog.records
    )


def test_client_ip_collapses_non_ip_peer(caplog: pytest.LogCaptureFixture) -> None:
    assert client_ip("testclient") == "unknown"
    assert any("non-IP client peer" in record.getMessage() for record in caplog.records)


def test_client_ip_warns_on_private_peer(caplog: pytest.LogCaptureFixture) -> None:
    assert client_ip("127.0.0.1") == "127.0.0.1"
    assert any("trusted-proxy" in record.getMessage() for record in caplog.records)
