"""Reddit cookie rotation: input handling, failure text and the grant/revoke flow."""

from __future__ import annotations

import importlib.util
import json
import urllib.error
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/rotate_reddit_cookie.py"
SPEC = importlib.util.spec_from_file_location("rotate_reddit_cookie", SCRIPT)
assert SPEC and SPEC.loader
rotate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rotate)

COOKIE = "abcdefghij0123456789.fake-cookie-value"
ASSIGNMENT_ID = "/assignments/created-by-this-run"
RECORD = {
    "attributes": {"enabled": True, "created": 100},
    "tags": {"envvar": "DIGEST_REDDIT_SESSION"},
    "contentType": "text/plain",
}


@pytest.mark.parametrize("raw", [COOKIE, f"  {COOKIE}\n", f"reddit_session={COOKIE}"])
def test_normalize_cookie_strips_whitespace_and_prefix(raw):
    assert rotate.normalize_cookie(raw) == COOKIE


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "abc123XYZ",
        f"{COOKIE};x",
        f"{COOKIE} x",
        f"{COOKIE}\nx",
        f"{COOKIE},x",
        COOKIE + "\x00",
    ],
)
def test_normalize_cookie_rejects_bad_values_without_echoing_them(raw):
    with pytest.raises(rotate.MigrationError) as excinfo:
        rotate.normalize_cookie(raw)
    message = str(excinfo.value)
    assert COOKIE not in message
    assert raw.strip() not in message or not raw.strip()


def test_describe_failure():
    http = urllib.error.HTTPError("https://example.invalid", 403, "Forbidden", {}, None)
    assert rotate.describe_failure(http) == "HTTP 403"
    assert "not logged in" in rotate.describe_failure(ValueError("x"))
    assert "not logged in" in rotate.describe_failure(json.JSONDecodeError("x", "", 0))
    assert rotate.describe_failure(TimeoutError("secret-ish detail")) == "TimeoutError"


def test_latest_version_picks_greatest_created():
    items = [
        {"id": "a", "attributes": {"created": 5}},
        {"id": "b", "attributes": {"created": 30}},
        {"id": "c", "attributes": {"created": 10}},
    ]
    assert rotate.latest_version(items)["id"] == "b"
    with pytest.raises(rotate.MigrationError):
        rotate.latest_version([])


class FakeVault:
    """Stands in for VaultClient; `put_error` simulates a failed write."""

    puts: list[dict] = []
    put_error: Exception | None = None

    def __init__(self, subscription=None):
        pass

    def request(self, vault, method, path, body=None):
        return {"value": [{"id": "https://v.vault.azure.net/secrets/s/v1", **RECORD}]}

    def put(self, vault, name, body):
        if self.put_error:
            raise self.put_error
        self.puts.append(body)
        return body

    def get(self, vault, name):
        return {"id": "https://v.vault.azure.net/secrets/s/newversion1", **self.puts[-1]}


@pytest.fixture
def flow(monkeypatch):
    calls: list[tuple[str, ...]] = []

    def fake_az(*args, subscription=None):
        calls.append(args)
        if args[:3] == ("ad", "signed-in-user", "show"):
            return "oid-1"
        if args[:2] == ("keyvault", "show"):
            return "/vaults/example"
        if args[:3] == ("role", "assignment", "create"):
            return ASSIGNMENT_ID
        return ""

    FakeVault.puts = []
    FakeVault.put_error = None
    monkeypatch.setattr(rotate, "az", fake_az)
    monkeypatch.setattr(rotate, "VaultClient", FakeVault)
    monkeypatch.setattr(rotate.reddit, "_verify_session", lambda cookie: "someone")
    monkeypatch.setattr(rotate.getpass, "getpass", lambda prompt: f"reddit_session={COOKIE}\n")
    return calls


def test_main_writes_preserved_metadata_and_revokes(flow, capsys):
    assert rotate.main(["--vault", "example-vault"]) == 0
    assert FakeVault.puts == [
        {
            "value": COOKIE,
            "attributes": {"enabled": True},
            "tags": RECORD["tags"],
            "contentType": "text/plain",
        }
    ]
    assert flow[-1] == ("role", "assignment", "delete", "--ids", ASSIGNMENT_ID)
    out = capsys.readouterr()
    assert "Temporary role removed." in out.out
    assert COOKIE not in out.out + out.err


def test_main_revokes_even_when_put_fails(flow, capsys):
    FakeVault.put_error = rotate.MigrationError("Key Vault PUT failed with HTTP 500.")
    assert rotate.main(["--vault", "example-vault"]) == 1
    deletes = [c for c in flow if c[:3] == ("role", "assignment", "delete")]
    assert deletes == [("role", "assignment", "delete", "--ids", ASSIGNMENT_ID)]
    out = capsys.readouterr()
    assert "Rotation stopped" in out.err
    assert COOKIE not in out.out + out.err


def test_main_does_not_touch_azure_when_reddit_rejects_cookie(flow, monkeypatch, capsys):
    def reject(cookie):
        raise urllib.error.HTTPError("https://example.invalid", 403, "Forbidden", {}, None)

    def browser_reject(cookie):
        raise ValueError("no name")

    monkeypatch.setattr(rotate.reddit, "_verify_session", reject)
    monkeypatch.setattr(rotate, "_browser_check", browser_reject)
    assert rotate.main(["--vault", "example-vault"]) == 1
    assert flow == []
    err = capsys.readouterr().err
    assert "HTTP 403" in err
    assert COOKIE not in err


def test_main_stops_before_azure_when_only_the_user_agent_is_rejected(flow, monkeypatch, capsys):
    def reject(cookie):
        raise urllib.error.HTTPError("https://example.invalid", 403, "Forbidden", {}, None)

    monkeypatch.setattr(rotate.reddit, "_verify_session", reject)
    monkeypatch.setattr(rotate, "_browser_check", lambda cookie: None)
    assert rotate.main(["--vault", "example-vault"]) == 1
    assert flow == []
    err = capsys.readouterr().err
    assert "User-Agent" in err and "--no-verify" in err
    assert COOKIE not in err


def test_main_refuses_a_disabled_current_version_and_still_revokes(flow, monkeypatch, capsys):
    disabled = {**RECORD, "attributes": {"enabled": False, "created": 100}}
    monkeypatch.setattr(
        FakeVault, "request", lambda self, vault, method, path, body=None: {"value": [disabled]}
    )
    assert rotate.main(["--vault", "example-vault"]) == 1
    assert FakeVault.puts == []
    assert flow[-1] == ("role", "assignment", "delete", "--ids", ASSIGNMENT_ID)
    assert "disabled" in capsys.readouterr().err


def test_put_is_retried_while_write_access_is_still_propagating(flow, monkeypatch, capsys):
    # Regression: a read-only assignment let the read probe pass before the new
    # write role had propagated, and the single PUT failed with HTTP 403.
    monkeypatch.setattr(rotate.time, "sleep", lambda seconds: None)
    forbidden = [rotate.MigrationError("Key Vault PUT failed with HTTP 403.")] * 2
    real_put = FakeVault.put

    def flaky_put(self, vault, name, body):
        if forbidden:
            raise forbidden.pop()
        return real_put(self, vault, name, body)

    monkeypatch.setattr(FakeVault, "put", flaky_put)
    assert rotate.main(["--vault", "example-vault"]) == 0
    assert len(FakeVault.puts) == 1
    assert flow[-1] == ("role", "assignment", "delete", "--ids", ASSIGNMENT_ID)
    out = capsys.readouterr()
    assert "Waiting for write access.. ready." in out.out
    assert COOKIE not in out.out + out.err
