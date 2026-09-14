"""Unit coverage for the secret-redaction patterns and the Redactor."""

import json

import pytest

from flightrec.redact import REDACTED, Redactor


@pytest.fixture
def r() -> Redactor:
    return Redactor()


@pytest.mark.parametrize("secret", [
    "sk-ant-api03-abcdEFGH1234567890abcdEFGH1234567890",
    "sk-proj-abcdefghij0123456789ABCDEFGHIJ",
    "sk-abcdefghijklmnop0123456789",
    "ghp_0123456789abcdefghij0123456789abcdef",
    "gho_0123456789abcdefghij0123456789abcdef",
    "github_pat_11ABC0000000abcdefghij_0123456789abcdefghijklmnop",
    "xoxb-1234567890-abcdefghijkl",
    "AIzaSyA0000000000000000000000000000000",
    "AKIAIOSFODNN7EXAMPLE",
    "ASIAIOSFODNN7EXAMPLE",
])
def test_known_key_shapes_are_removed(r, secret):
    out = r.text(f"my key is {secret} ok")
    assert secret not in out
    assert REDACTED in out


def test_bearer_token_in_text(r):
    out = r.text("Authorization: Bearer abcdef0123456789ABCDEF.token-value")
    assert "abcdef0123456789ABCDEF" not in out
    assert REDACTED in out


def test_jwt_is_removed(r):
    jwt = ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
           "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
    out = r.text(f"token={jwt}")
    assert jwt not in out
    assert REDACTED in out


def test_private_key_block_is_removed(r):
    pem = ("-----BEGIN RSA PRIVATE KEY-----\n"
           "MIIEpAIBAAKCAQEA0Z...lines...of...base64\n"
           "moredata==\n"
           "-----END RSA PRIVATE KEY-----")
    out = r.text(f"before\n{pem}\nafter")
    assert "MIIEpAIBAAKCAQEA" not in out
    assert out.startswith("before")
    assert out.endswith("after")
    assert REDACTED in out


def test_non_secret_text_is_untouched(r):
    text = "The quick brown fox edits calc.py and runs pytest -q (exit 0)."
    assert r.text(text) == text


def test_redaction_preserves_json_validity(r):
    body = json.dumps({"model": "m", "api_key": "sk-ant-abcdEFGH1234567890abcdEFGH",
                       "note": "hello"})
    scrubbed = r.text(body)
    reparsed = json.loads(scrubbed)          # still valid JSON
    assert reparsed["api_key"] == REDACTED
    assert reparsed["note"] == "hello"


def test_auth_headers_are_fully_redacted(r):
    headers = {"Authorization": "Bearer secretvalue", "X-Api-Key": "sk-ant-xyz",
               "Content-Type": "application/json", "User-Agent": "agent/1.0"}
    out = r.headers(headers)
    assert out["Authorization"] == REDACTED
    assert out["X-Api-Key"] == REDACTED
    assert out["Content-Type"] == "application/json"
    assert out["User-Agent"] == "agent/1.0"


def test_extra_pattern_literal_and_regex():
    r = Redactor(extra_patterns=["hunter2", r"CORP-\d{4}"])
    out = r.text("password hunter2 and ticket CORP-1234 here")
    assert "hunter2" not in out
    assert "CORP-1234" not in out


def test_invalid_regex_falls_back_to_literal():
    # An unbalanced paren must not raise; it is treated as literal text.
    r = Redactor(extra_patterns=["secret(value"])
    assert r.text("a secret(value b") == f"a {REDACTED} b"


def test_disabled_redactor_is_passthrough():
    r = Redactor(enabled=False)
    assert r.text("sk-ant-abcdEFGH1234567890abcdEFGH") == \
        "sk-ant-abcdEFGH1234567890abcdEFGH"
    headers = {"Authorization": "Bearer x"}
    assert r.headers(headers) == headers


def test_from_env_honours_escape_hatch():
    assert Redactor.from_env(env={}).enabled is True
    assert Redactor.from_env(env={"FLIGHTREC_NO_REDACT": "1"}).enabled is False
    assert Redactor.from_env(env={"FLIGHTREC_NO_REDACT": "yes"}).enabled is False
    assert Redactor.from_env(env={"FLIGHTREC_NO_REDACT": "0"}).enabled is True


def test_empty_and_none_safe(r):
    assert r.text("") == ""
    assert r.headers({}) == {}
