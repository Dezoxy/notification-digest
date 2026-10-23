#!/usr/bin/env python3
"""Rotate the production Reddit session cookie in the dedicated digest Key Vault.

Run: uv run python scripts/rotate_reddit_cookie.py --vault YOUR_DIGEST_VAULT

Get the cookie from a logged-in browser: DevTools -> Application/Storage ->
Cookies -> https://www.reddit.com -> `reddit_session`. It is HttpOnly, so
`document.cookie` will not show it. Do NOT log out of that browser session
afterwards: logging out revokes the cookie.

The script checks the cookie against Reddit with the collector's own code path,
temporarily grants the operator Secrets Officer on that one secret, writes a new
version (preserving tags, content type and validity dates), reads it back and
revokes exactly the assignment it created. The cookie is never printed, logged
or passed as a command argument.

Deliberate deviation from the docs/azure-migration.md rotation runbook: no
pause/drain. The Reddit collector has no cursor or state, so a run overlapping
the rotation can at worst fail Reddit once.

Proxy key mode: --proxy-key --key-file PATH stores the shared secret of the
`reddit-proxy` Cloudflare Worker (REDDIT_PROXY_KEY) instead of the cookie, in
the secret `digest-reddit-proxy-key` unless --secret says otherwise. PATH must
be a private (chmod 600) regular file holding `openssl rand -hex 32`; there is
no prompt and no Reddit check. Optional --proxy-url https://... first sends one
GET /api/me.json with only the key (no cookie, no redirects) and stops before
touching Azure on HTTP 403. This mode creates the secret on first use (tags
envvar=REDDIT_PROXY_KEY, folder=digest, text/plain). A secret that does not
exist yet cannot be an RBAC scope, so the temporary Secrets Officer role is
granted on the VAULT in this mode (the cookie mode keeps the secret scope);
exactly the created assignment is still revoked in `finally`.
"""

from __future__ import annotations

import argparse
import getpass
import importlib.util
import itertools
import json
import os
import re
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

# See scripts/telegram_login.py: a directly-run script has scripts/ on sys.path,
# not the repo root, and `digest` is never installed into the venv.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from digest.collectors import reddit  # noqa: E402

_MIGRATE_SPEC = importlib.util.spec_from_file_location(
    "migrate_azure_secrets", Path(__file__).resolve().parent / "migrate_azure_secrets.py"
)
assert _MIGRATE_SPEC and _MIGRATE_SPEC.loader
_migrate = importlib.util.module_from_spec(_MIGRATE_SPEC)
_MIGRATE_SPEC.loader.exec_module(_migrate)

API_VERSION = _migrate.API_VERSION
MigrationError = _migrate.MigrationError
VaultClient = _migrate.VaultClient
validate_name = _migrate.validate_name
preserved_metadata = _migrate.preserved_metadata
preflight = _migrate.preflight

DEFAULT_SECRET = "digest-reddit-session"
DEFAULT_PROXY_SECRET = "digest-reddit-proxy-key"
# Metadata for a proxy-key secret that does not exist yet (mirrors the other
# digest secrets: envvar tag consumed by infra, text/plain).
NEW_PROXY_SECRET_METADATA: dict[str, Any] = {
    "attributes": {"enabled": True},
    "tags": {"envvar": "REDDIT_PROXY_KEY", "folder": "digest"},
    "contentType": "text/plain",
}
PROXY_KEY_RE = re.compile(r"[0-9a-f]{64}")  # openssl rand -hex 32
ROLE = "Key Vault Secrets Officer"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
MIN_COOKIE_LENGTH = 20
POLL_INTERVAL_SECONDS = 10
POLL_ATTEMPTS = 30  # 5 minutes


def normalize_cookie(raw: str) -> str:
    """Strip whitespace and an optional `reddit_session=` prefix; reject malformed values.

    Error messages never include the input.
    """
    cookie = raw.strip()
    cookie = cookie.removeprefix("reddit_session=")
    if not cookie:
        raise MigrationError("Cookie is empty.")
    if (
        any(c.isspace() or c in ";," or ord(c) < 32 or ord(c) >= 127 for c in cookie)
        or not cookie.isascii()
    ):
        raise MigrationError(
            "Cookie contains whitespace, ';', ',', control or non-ASCII characters; "
            "paste only the reddit_session value."
        )
    if len(cookie) < MIN_COOKIE_LENGTH:
        raise MigrationError("Cookie is too short to be a reddit_session value.")
    return cookie


def read_proxy_key(path: str) -> str:
    """Read the proxy key from a private regular file; never echo its content.

    Rejects symlinks, non-regular files and any group/other permission bit,
    then requires exactly 64 lowercase hex characters (`openssl rand -hex 32`).
    """
    try:
        info = os.lstat(path)
    except OSError:
        raise MigrationError("Cannot read key file.") from None
    if not stat.S_ISREG(info.st_mode):
        raise MigrationError("Key file must be a regular file (not a symlink).")
    if info.st_mode & 0o077:
        raise MigrationError("Key file must be private (chmod 600).")
    try:
        with open(path, encoding="ascii") as handle:
            key = handle.read().strip()
    except (OSError, UnicodeError):
        raise MigrationError("Cannot read key file.") from None
    if not PROXY_KEY_RE.fullmatch(key):
        raise MigrationError("Key file must contain 64 lowercase hex characters.")
    return key


def check_proxy(url: str, key: str) -> None:
    """GET `{url}/api/me.json` with only the proxy key; stop on 403, accept anything else.

    No cookie is sent and redirects are not followed (the Worker strips
    `Location`, so a 302 is the normal "key accepted, not logged in" answer).
    Sends the collector's own User-Agent: Cloudflare answers urllib's default
    `Python-urllib/x.y` with `403 error code: 1010` before the Worker runs,
    which would look exactly like a rejected key. That block is reported
    separately from the Worker's own JSON `forbidden`.
    """
    if not url.startswith("https://") or any(c.isspace() for c in url):
        raise MigrationError("--proxy-url must be an https:// URL.")
    request = urllib.request.Request(
        f"{url.rstrip('/')}/api/me.json",
        headers={"x-proxy-key": key, "User-Agent": reddit._USER_AGENT},  # noqa: SLF001
    )
    opener = urllib.request.build_opener(_migrate.NoRedirect())
    try:
        with opener.open(request, timeout=reddit._REQUEST_TIMEOUT_SECONDS):  # noqa: SLF001
            code = 200
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            body = exc.read(200).decode("utf-8", "replace") if exc.fp else ""
            if "error code: 1010" in body:
                raise MigrationError(
                    "Cloudflare blocked the request before the Worker (error 1010); "
                    "the key was not checked."
                ) from None
            raise MigrationError("proxy rejected the key (HTTP 403)") from None
        code = exc.code
    except (OSError, ValueError) as exc:
        raise MigrationError(f"proxy check failed ({type(exc).__name__}).") from None
    print(f"Proxy accepted the key (HTTP {code}).")


def describe_failure(exc: BaseException) -> str:
    """Operator-safe failure description: status code or type name, never the message."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, ValueError):  # includes json.JSONDecodeError
        return "not logged in (Reddit returned no username)"
    return type(exc).__name__


# The collector no longer uses a cookie (it reads anonymous Atom feeds), so the
# logged-in check this script needs lives here, not in digest/collectors/reddit.py.
_LEGACY_ME_URL = "https://old.reddit.com/api/me.json"


def verify_session(cookie: str, user_agent: str = reddit._USER_AGENT) -> None:  # noqa: SLF001
    """GET `/api/me.json` with the cookie; raise ValueError unless it names a user."""
    request = urllib.request.Request(
        _LEGACY_ME_URL,
        headers={"Cookie": f"reddit_session={cookie}", "User-Agent": user_agent},
    )
    with urllib.request.urlopen(
        request,
        timeout=reddit._REQUEST_TIMEOUT_SECONDS,  # noqa: SLF001
    ) as response:
        raw_bytes = response.read()
    data = json.loads(raw_bytes)
    name = data.get("data", {}).get("name") if isinstance(data, dict) else None
    if not isinstance(name, str) or not name:
        raise ValueError("not logged in")


def _browser_check(cookie: str) -> None:
    """Same request as `verify_session`, but with a browser User-Agent."""
    verify_session(cookie, BROWSER_USER_AGENT)


def check_reddit(cookie: str) -> None:
    """Verify with the collector's User-Agent; on failure diagnose and raise MigrationError."""
    try:
        verify_session(cookie)
    except Exception as exc:
        collector_failure = describe_failure(exc)
    else:
        print("Reddit: cookie verified as logged in (collector User-Agent).")
        return
    try:
        _browser_check(cookie)
    except Exception as exc:
        raise MigrationError(
            f"Reddit check failed with both User-Agents ({collector_failure} / "
            f"{describe_failure(exc)}). An HTTP 403 or 'not logged in' means the cookie is "
            "dead: log in again in the browser and copy a fresh value."
        ) from None
    raise MigrationError(
        "Cookie is valid, but Reddit rejects the collector's User-Agent "
        f"(reddit._USER_AGENT; collector failure: {collector_failure}). Storing the cookie "
        "will not fix collection; the UA must change. Re-run with --no-verify to store it anyway."
    )


def az(*args: str, subscription: str | None = None) -> str:
    """Run an Azure CLI command and return stripped stdout.

    Failures raise a generic error naming only the subcommand: stderr may echo inputs.
    """
    command = ["az", *args]
    if subscription:
        command += ["--subscription", subscription]
    name = " ".join(itertools.takewhile(lambda a: not a.startswith("-"), args))
    try:
        result = subprocess.run(command, capture_output=True, check=True, timeout=120)
        return result.stdout.decode().strip()
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise MigrationError(f"Azure CLI command failed: az {name}.") from None


def latest_version(items: list[dict[str, Any]]) -> dict[str, Any]:
    """The version item with the greatest `attributes.created`."""
    if not items:
        raise MigrationError("Secret has no versions; create it first.")
    return max(items, key=lambda item: item.get("attributes", {}).get("created", 0))


def list_versions(client: Any, vault: str, secret: str) -> list[dict[str, Any]]:
    """All version metadata items (no values), following `nextLink` pages."""
    items: list[dict[str, Any]] = []
    path = f"/secrets/{secret}/versions?api-version={API_VERSION}"
    seen: set[str] = set()
    while path:
        if path in seen:
            raise MigrationError("Key Vault pagination repeated a page.")
        seen.add(path)
        page = client.request(vault, "GET", path)
        items.extend(page.get("value", []))
        path = page.get("nextLink") or ""
    return items


def retry_while_forbidden[T](action: Callable[[], T], label: str) -> T:
    """Retry `action` on Key Vault HTTP 403 until the new role assignment has propagated.

    Each permission is polled with the call that needs it: a read succeeding says
    nothing about write access (another, read-only assignment may already allow
    it), and a 403'd PUT stored nothing, so retrying it is safe.
    """
    print(f"Waiting for {label} access", end="", flush=True)
    for _ in range(POLL_ATTEMPTS):
        try:
            result = action()
        except MigrationError as exc:
            if "HTTP 403" not in str(exc):
                print()
                raise
            print(".", end="", flush=True)
            time.sleep(POLL_INTERVAL_SECONDS)
            continue
        print(" ready.")
        return result
    print()
    raise MigrationError(f"Role assignment did not grant {label} access within 5 minutes.")


def _write_and_verify(client: Any, vault: str, secret: str, value: str, latest: dict) -> str:
    body = {"value": value, **preserved_metadata(latest)}
    retry_while_forbidden(lambda: client.put(vault, secret, body), "write")
    stored = client.get(vault, secret)
    if stored.get("value") != value or preserved_metadata(stored) != preserved_metadata(body):
        raise MigrationError("Read-back verification failed; stop and inspect the secret.")
    return str(stored.get("id", "unknown")).rstrip("/").rsplit("/", 1)[-1]


def rotate(
    vault: str,
    secret: str,
    subscription: str | None,
    value: str,
    *,
    create_if_missing: bool = False,
) -> None:
    """Grant, wait, write, verify; always revoke exactly the assignment created here.

    `create_if_missing` (proxy-key mode) creates an absent secret and grants the
    role on the vault instead of the secret, since a missing secret is no scope.
    """
    oid = az("ad", "signed-in-user", "show", "--query", "id", "-o", "tsv")
    vault_id = az(
        "keyvault", "show", "--name", vault, "--query", "id", "-o", "tsv",
        subscription=subscription,
    )  # fmt: skip
    if not oid or not vault_id:
        raise MigrationError("Could not resolve operator or vault identity.")
    scope = vault_id if create_if_missing else f"{vault_id}/secrets/{secret}"
    assignment_id = az(
        "role", "assignment", "create",
        "--assignee-object-id", oid,
        "--assignee-principal-type", "User",
        "--role", ROLE,
        "--scope", scope,
        "--query", "id", "-o", "tsv",
        subscription=subscription,
    )  # fmt: skip
    if not assignment_id:
        raise MigrationError("Role assignment returned no id; check for a leftover assignment.")
    if create_if_missing:
        print("Temporary role granted on the vault (the secret may not exist yet).")
    else:
        print("Temporary role granted.")
    try:
        client = VaultClient(subscription)

        def read_versions() -> list[dict[str, Any]]:
            try:
                return list_versions(client, vault, secret)
            except MigrationError as exc:
                if create_if_missing and "HTTP 404" in str(exc):
                    return []  # secret absent: created below
                raise

        versions = retry_while_forbidden(read_versions, "read")
        if versions or not create_if_missing:
            latest = latest_version(versions)
            # Fail closed rather than copy a disabled/expired state onto the new version.
            preflight(latest, secret, time.time())
        else:
            latest = NEW_PROXY_SECRET_METADATA
            print(f"Secret {secret} does not exist; creating it.")
        version = _write_and_verify(client, vault, secret, value, latest)
        print(f"Stored and verified new version {version}.")
    finally:
        try:
            az("role", "assignment", "delete", "--ids", assignment_id, subscription=subscription)
            print("Temporary role removed.")
        except MigrationError:
            manual = f"az role assignment delete --ids {assignment_id}"
            if subscription:
                manual += f" --subscription {subscription}"
            print(
                f"WARNING: could NOT remove the temporary role. Remove it NOW with:\n  {manual}",
                file=sys.stderr,
            )


def main(argv: list[str] | None = None) -> int:
    """Run the interactive rotation."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--vault", required=True, help="Dedicated digest Key Vault name")
    parser.add_argument(
        "--secret",
        help=f"Secret name (default {DEFAULT_SECRET}; {DEFAULT_PROXY_SECRET} with --proxy-key)",
    )
    parser.add_argument("--subscription", help="Azure CLI subscription ID or name")
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Store the cookie even if the Reddit check fails",
    )
    parser.add_argument(
        "--proxy-key",
        action="store_true",
        help="Store the reddit-proxy shared secret (from --key-file) instead of the cookie",
    )
    parser.add_argument("--key-file", help="With --proxy-key: private file holding the key")
    parser.add_argument("--proxy-url", help="With --proxy-key: https URL of the Worker to check")
    args = parser.parse_args(argv)
    if args.proxy_key and not args.key_file:
        parser.error("--proxy-key requires --key-file PATH")
    if (args.key_file or args.proxy_url) and not args.proxy_key:
        parser.error("--key-file and --proxy-url require --proxy-key")
    try:
        vault = validate_name(args.vault, vault=True)
        secret = validate_name(
            args.secret or (DEFAULT_PROXY_SECRET if args.proxy_key else DEFAULT_SECRET)
        )
        if args.proxy_key:
            key = read_proxy_key(args.key_file)
            if args.proxy_url:
                check_proxy(args.proxy_url, key)
            rotate(vault, secret, args.subscription, key, create_if_missing=True)
            del key
            print(
                "Done. Make sure REDDIT_PROXY_KEY maps to this secret in the job secret_names, "
                "and that the same key is set on the Worker; jobs pick up a new version after "
                "the platform's Key Vault refresh (up to ~30 min)."
            )
            return 0
        cookie = normalize_cookie(
            getpass.getpass("Paste reddit_session cookie value (input hidden): ")
        )
        if not args.no_verify:
            check_reddit(cookie)
        rotate(vault, secret, args.subscription, cookie)
        # Best effort only: the str may live on in interpreter memory until exit.
        del cookie
        print(
            "Done. The Container Apps jobs reference this secret versionless, so the next "
            "executions pick it up after the platform's Key Vault refresh (up to ~30 min).\n"
            'Check that the next window run\'s run_summary has "reddit": "ok".'
        )
        return 0
    except MigrationError as exc:
        print(f"Rotation stopped: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Rotation cancelled.", file=sys.stderr)
        return 130
    except Exception:
        print(
            "Rotation stopped: unexpected failure; details suppressed to protect secrets.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
