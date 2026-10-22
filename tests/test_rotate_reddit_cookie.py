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
    versions_error: Exception | None = None

    def __init__(self, subscription=None):
        pass

    def request(self, vault, method, path, body=None):
        if self.versions_error:
            raise self.versions_error
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
    FakeVault.versions_error = None
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


# --- proxy-key mode ---

PROXY_KEY = "ab" * 32  # valid 64-hex format, deliberately low-entropy (gitleaks)
PROXY_URL = "https://reddit-proxy.example.workers.dev"
VAULT_ID = "/vaults/example"


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "proxy.key"
    path.write_text(PROXY_KEY + "\n")
    path.chmod(0o600)
    return path


def test_read_proxy_key_accepts_private_regular_file(key_file):
    assert rotate.read_proxy_key(str(key_file)) == PROXY_KEY


def test_read_proxy_key_rejects_symlink(key_file, tmp_path):
    link = tmp_path / "link.key"
    link.symlink_to(key_file)
    with pytest.raises(rotate.MigrationError, match="regular file"):
        rotate.read_proxy_key(str(link))


def test_read_proxy_key_rejects_non_regular_file(tmp_path):
    with pytest.raises(rotate.MigrationError, match="regular file"):
        rotate.read_proxy_key(str(tmp_path))


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660])
def test_read_proxy_key_rejects_group_or_other_access(key_file, mode):
    key_file.chmod(mode)
    with pytest.raises(rotate.MigrationError, match=r"private \(chmod 600\)"):
        rotate.read_proxy_key(str(key_file))


def test_read_proxy_key_rejects_missing_file(tmp_path):
    with pytest.raises(rotate.MigrationError, match="Cannot read"):
        rotate.read_proxy_key(str(tmp_path / "nope"))


@pytest.mark.parametrize(
    "content",
    ["", "short", PROXY_KEY.upper(), PROXY_KEY + "00", PROXY_KEY[:-1] + "g", PROXY_KEY + " x"],
)
def test_read_proxy_key_rejects_bad_format_without_echoing(key_file, content):
    key_file.write_text(content)
    with pytest.raises(rotate.MigrationError) as excinfo:
        rotate.read_proxy_key(str(key_file))
    message = str(excinfo.value)
    assert "64 lowercase hex" in message
    assert content.strip() not in message or not content.strip()


class FakeOpener:
    """Stands in for the no-redirect opener; records requests, raises or returns an outcome."""

    outcome: Exception | None = None
    requests: list = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        if self.outcome:
            raise self.outcome

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

        return _Resp()


def _http(code):
    return urllib.error.HTTPError(PROXY_URL, code, "x", {}, None)


@pytest.fixture
def opener(monkeypatch):
    FakeOpener.outcome = None
    FakeOpener.requests = []
    monkeypatch.setattr(rotate.urllib.request, "build_opener", lambda *handlers: FakeOpener())


def test_check_proxy_sends_only_the_key_header(opener, capsys):
    rotate.check_proxy(PROXY_URL + "/", PROXY_KEY)
    (request,) = FakeOpener.requests
    assert request.full_url == f"{PROXY_URL}/api/me.json"
    assert dict(request.header_items()) == {"X-proxy-key": PROXY_KEY}
    assert "Proxy accepted the key (HTTP 200)." in capsys.readouterr().out


def test_check_proxy_403_is_rejection(opener):
    FakeOpener.outcome = _http(403)
    with pytest.raises(rotate.MigrationError, match=r"proxy rejected the key \(HTTP 403\)"):
        rotate.check_proxy(PROXY_URL, PROXY_KEY)


@pytest.mark.parametrize("code", [302, 401, 502])
def test_check_proxy_other_status_is_accepted(opener, capsys, code):
    FakeOpener.outcome = _http(code)
    rotate.check_proxy(PROXY_URL, PROXY_KEY)
    assert f"Proxy accepted the key (HTTP {code})." in capsys.readouterr().out


def test_check_proxy_network_error_names_type_only(opener):
    FakeOpener.outcome = TimeoutError(f"detail {PROXY_KEY}")
    with pytest.raises(rotate.MigrationError) as excinfo:
        rotate.check_proxy(PROXY_URL, PROXY_KEY)
    assert "TimeoutError" in str(excinfo.value)
    assert PROXY_KEY not in str(excinfo.value)


@pytest.mark.parametrize("url", ["http://reddit-proxy.example.com", "ftp://x", "reddit-proxy", ""])
def test_check_proxy_requires_https(opener, url):
    with pytest.raises(rotate.MigrationError, match="https"):
        rotate.check_proxy(url, PROXY_KEY)
    assert FakeOpener.requests == []


@pytest.fixture
def proxy_flow(flow, key_file, opener):
    """`flow` plus a key file; returns az calls. Vault starts with no secret (404)."""
    FakeVault.versions_error = rotate.MigrationError("Key Vault GET failed with HTTP 404.")
    yield flow
    FakeVault.versions_error = None


def _run_proxy(key_file, *extra):
    return rotate.main(
        ["--vault", "example-vault", "--proxy-key", "--key-file", str(key_file), *extra]
    )


def test_proxy_mode_creates_missing_secret_with_vault_scope_role(proxy_flow, key_file, capsys):
    assert _run_proxy(key_file, "--proxy-url", PROXY_URL) == 0
    assert FakeVault.puts == [
        {
            "value": PROXY_KEY,
            "attributes": {"enabled": True},
            "tags": {"envvar": "REDDIT_PROXY_KEY", "folder": "digest"},
            "contentType": "text/plain",
        }
    ]
    (create,) = [c for c in proxy_flow if c[:3] == ("role", "assignment", "create")]
    assert create[create.index("--scope") + 1] == VAULT_ID
    assert proxy_flow[-1] == ("role", "assignment", "delete", "--ids", ASSIGNMENT_ID)
    out = capsys.readouterr()
    assert "Temporary role granted on the vault" in out.out
    assert "Temporary role removed." in out.out
    assert "Proxy accepted the key" in out.out
    assert PROXY_KEY not in out.out + out.err


def test_proxy_mode_creates_secret_when_version_list_is_empty(proxy_flow, key_file, monkeypatch):
    FakeVault.versions_error = None
    monkeypatch.setattr(
        FakeVault, "request", lambda self, vault, method, path, body=None: {"value": []}
    )
    assert _run_proxy(key_file) == 0
    assert FakeVault.puts[0]["tags"] == {"envvar": "REDDIT_PROXY_KEY", "folder": "digest"}


def test_proxy_mode_uses_default_secret_name(proxy_flow, key_file, monkeypatch):
    seen = []
    real_put = FakeVault.put

    def spy(self, vault, name, body):
        seen.append(name)
        return real_put(self, vault, name, body)

    monkeypatch.setattr(FakeVault, "put", spy)
    assert _run_proxy(key_file) == 0
    assert seen == ["digest-reddit-proxy-key"]


def test_proxy_mode_rotates_existing_secret_preserving_metadata(proxy_flow, key_file):
    FakeVault.versions_error = None  # FakeVault.request now returns RECORD
    assert _run_proxy(key_file) == 0
    assert FakeVault.puts == [
        {
            "value": PROXY_KEY,
            **{
                "attributes": {"enabled": True},
                "tags": RECORD["tags"],
                "contentType": "text/plain",
            },
        }
    ]


def test_proxy_mode_other_version_list_errors_are_not_treated_as_missing(
    proxy_flow, key_file, capsys
):
    FakeVault.versions_error = rotate.MigrationError("Key Vault GET failed with HTTP 500.")
    assert _run_proxy(key_file) == 1
    assert FakeVault.puts == []
    assert proxy_flow[-1] == ("role", "assignment", "delete", "--ids", ASSIGNMENT_ID)
    assert "HTTP 500" in capsys.readouterr().err


def test_proxy_mode_vault_scope_grant_waits_for_propagation(
    proxy_flow, key_file, monkeypatch, capsys
):
    monkeypatch.setattr(rotate.time, "sleep", lambda seconds: None)
    errors = [rotate.MigrationError("Key Vault GET failed with HTTP 403.")] * 2
    original = FakeVault.versions_error

    def flaky(self, vault, method, path, body=None):
        if errors:
            raise errors.pop()
        raise original

    monkeypatch.setattr(FakeVault, "request", flaky)
    assert _run_proxy(key_file) == 0
    assert len(FakeVault.puts) == 1
    assert "Waiting for read access.. ready." in capsys.readouterr().out


def test_proxy_mode_403_from_proxy_stops_before_azure(proxy_flow, key_file, capsys):
    FakeOpener.outcome = _http(403)
    assert _run_proxy(key_file, "--proxy-url", PROXY_URL) == 1
    assert proxy_flow == []
    err = capsys.readouterr().err
    assert "proxy rejected the key (HTTP 403)" in err
    assert PROXY_KEY not in err


def test_proxy_mode_302_from_proxy_proceeds(proxy_flow, key_file, capsys):
    FakeOpener.outcome = _http(302)
    assert _run_proxy(key_file, "--proxy-url", PROXY_URL) == 0
    assert "Proxy accepted the key (HTTP 302)." in capsys.readouterr().out
    assert len(FakeVault.puts) == 1


def test_proxy_mode_bad_key_file_stops_before_azure(proxy_flow, key_file, capsys):
    key_file.chmod(0o644)
    assert _run_proxy(key_file) == 1
    assert proxy_flow == []
    err = capsys.readouterr().err
    assert "chmod 600" in err
    assert PROXY_KEY not in err


def test_proxy_mode_does_not_prompt_or_check_reddit(proxy_flow, key_file, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("must not be called in proxy-key mode")

    monkeypatch.setattr(rotate.getpass, "getpass", boom)
    monkeypatch.setattr(rotate, "check_reddit", boom)
    assert _run_proxy(key_file) == 0


def test_proxy_mode_requires_key_file(flow):
    with pytest.raises(SystemExit) as excinfo:
        rotate.main(["--vault", "example-vault", "--proxy-key"])
    assert excinfo.value.code == 2
    assert flow == []


def test_key_file_without_proxy_key_flag_is_rejected(flow, key_file):
    with pytest.raises(SystemExit):
        rotate.main(["--vault", "example-vault", "--key-file", str(key_file)])
    assert flow == []


def test_cookie_mode_still_uses_secret_scope_and_no_creation(flow):
    assert rotate.main(["--vault", "example-vault"]) == 0
    (create,) = [c for c in flow if c[:3] == ("role", "assignment", "create")]
    assert create[create.index("--scope") + 1] == f"{VAULT_ID}/secrets/digest-reddit-session"
