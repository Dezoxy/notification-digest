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
"""

from __future__ import annotations

import argparse
import getpass
import importlib.util
import itertools
import json
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


def describe_failure(exc: BaseException) -> str:
    """Operator-safe failure description: status code or type name, never the message."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, ValueError):  # includes json.JSONDecodeError
        return "not logged in (Reddit returned no username)"
    return type(exc).__name__


def _browser_check(cookie: str) -> None:
    """Same request as `reddit._verify_session`, but with a browser User-Agent."""
    request = urllib.request.Request(
        f"{reddit._BASE_URL}/api/me.json",  # noqa: SLF001
        headers={"Cookie": f"reddit_session={cookie}", "User-Agent": BROWSER_USER_AGENT},
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


def check_reddit(cookie: str) -> None:
    """Verify with the collector's code path; on failure diagnose and raise MigrationError."""
    try:
        reddit._verify_session(cookie)  # noqa: SLF001
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


def _write_and_verify(client: Any, vault: str, secret: str, cookie: str, latest: dict) -> str:
    body = {"value": cookie, **preserved_metadata(latest)}
    retry_while_forbidden(lambda: client.put(vault, secret, body), "write")
    stored = client.get(vault, secret)
    if stored.get("value") != cookie or preserved_metadata(stored) != preserved_metadata(body):
        raise MigrationError("Read-back verification failed; stop and inspect the secret.")
    return str(stored.get("id", "unknown")).rstrip("/").rsplit("/", 1)[-1]


def rotate(vault: str, secret: str, subscription: str | None, cookie: str) -> None:
    """Grant, wait, write, verify; always revoke exactly the assignment created here."""
    oid = az("ad", "signed-in-user", "show", "--query", "id", "-o", "tsv")
    vault_id = az(
        "keyvault", "show", "--name", vault, "--query", "id", "-o", "tsv",
        subscription=subscription,
    )  # fmt: skip
    if not oid or not vault_id:
        raise MigrationError("Could not resolve operator or vault identity.")
    scope = f"{vault_id}/secrets/{secret}"
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
    print("Temporary role granted.")
    try:
        client = VaultClient(subscription)
        latest = latest_version(
            retry_while_forbidden(lambda: list_versions(client, vault, secret), "read")
        )
        # Fail closed rather than copy a disabled/expired state onto the new version.
        preflight(latest, secret, time.time())
        version = _write_and_verify(client, vault, secret, cookie, latest)
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
    parser.add_argument("--secret", default=DEFAULT_SECRET, help="Secret name")
    parser.add_argument("--subscription", help="Azure CLI subscription ID or name")
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Store the cookie even if the Reddit check fails",
    )
    args = parser.parse_args(argv)
    try:
        vault = validate_name(args.vault, vault=True)
        secret = validate_name(args.secret)
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
