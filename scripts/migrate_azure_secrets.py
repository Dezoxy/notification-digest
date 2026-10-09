#!/usr/bin/env python3
"""Copy the digest's secrets to its dedicated vault without printing their values.

Default mode lists metadata only. --apply and --verify read values into memory;
neither mode writes values to files, command arguments, logs or Terraform state.
After reviewing a rotated source, --apply --replace-target NAME creates a new
version of exactly that manifest target; the default never overwrites conflicts.
Pause operator secret edits during migration: Key Vault PUT has no create-only
condition, so the conflict preflight cannot fence concurrent human writes.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

MANIFEST = Path(__file__).resolve().parents[1] / "infra/azure/secret-migration.json"
API_VERSION = "7.4"


class MigrationError(Exception):
    """An operator-safe failure message with no credential or response body."""


def validate_name(name: str, *, vault: bool = False) -> str:
    """Reject names that could escape the fixed Azure public-cloud endpoint."""
    pattern = r"[A-Za-z][A-Za-z0-9-]{1,22}[A-Za-z0-9]" if vault else r"[A-Za-z0-9-]{1,127}"
    if not isinstance(name, str) or not re.fullmatch(pattern, name) or (vault and "--" in name):
        raise MigrationError("Invalid vault or secret name in configuration.")
    return name.lower() if vault else name


def load_manifest(path: Path) -> tuple[str, list[dict[str, str]]]:
    """Load and validate the nonsecret source-to-target mapping."""
    try:
        data = json.loads(path.read_text())
        source = validate_name(data["source_vault"], vault=True)
        entries = data["secrets"]
        if not isinstance(entries, list) or not entries:
            raise ValueError
        for entry in entries:
            validate_name(entry["source"])
            validate_name(entry["target"])
        for key in ("source", "target"):
            names = [entry[key].lower() for entry in entries]
            if len(names) != len(set(names)):
                raise ValueError
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise MigrationError("Invalid or unreadable migration manifest.") from exc
    return source, entries


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward the vault bearer token to a redirected endpoint."""

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def _is_vault_url(parsed: urllib.parse.SplitResult, vault: str) -> bool:
    """True only for https on the vault's own host, default port, no userinfo."""
    try:
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == f"{vault}.vault.azure.net".lower()
        and port in (None, 443)
        and parsed.username is None
        and parsed.password is None
    )


class VaultClient:
    """Minimal Key Vault REST client, authenticated by the operator's Azure CLI."""

    def __init__(self, subscription: str | None = None) -> None:
        command = [
            "az",
            "account",
            "get-access-token",
            "--resource",
            "https://vault.azure.net",
            "--query",
            "accessToken",
            "--output",
            "tsv",
        ]
        if subscription:
            command += ["--subscription", subscription]
        try:
            result = subprocess.run(command, capture_output=True, check=True, timeout=60)
            self._token = result.stdout.decode().strip()
            if not self._token:
                raise ValueError
        except (OSError, subprocess.SubprocessError, UnicodeError, ValueError) as exc:
            raise MigrationError("Azure CLI authentication failed; run az login first.") from exc
        self._opener = urllib.request.build_opener(NoRedirect())

    def request(
        self, vault: str, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Perform a request; sanitize transport and server failures."""
        vault = validate_name(vault, vault=True)
        origin = f"https://{vault}.vault.azure.net"
        url = origin + path if path.startswith("/") else path
        parsed = urllib.parse.urlsplit(url)
        if not _is_vault_url(parsed, vault):
            raise MigrationError("Key Vault returned an unexpected pagination endpoint.")
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
        )
        try:
            with self._opener.open(req, timeout=60) as response:
                result = json.load(response)
                if not isinstance(result, dict):
                    raise ValueError
                return result
        except urllib.error.HTTPError as exc:
            raise MigrationError(f"Key Vault {method} failed with HTTP {exc.code}.") from None
        except (OSError, ValueError) as exc:
            raise MigrationError("Key Vault request failed; response details suppressed.") from exc

    def metadata(self, vault: str) -> dict[str, dict[str, Any]]:
        """List current secret metadata, without requesting secret values."""
        records: dict[str, dict[str, Any]] = {}
        path = f"/secrets?api-version={API_VERSION}"
        seen: set[str] = set()
        while path:
            if path in seen:
                raise MigrationError("Key Vault pagination repeated a page.")
            seen.add(path)
            page = self.request(vault, "GET", path)
            for record in page.get("value", []):
                parsed = urllib.parse.urlsplit(record["id"])
                if not _is_vault_url(parsed, vault):
                    raise MigrationError("Key Vault returned an unexpected secret endpoint.")
                parts = parsed.path.strip("/").split("/")
                if len(parts) != 2 or parts[0] != "secrets":
                    raise MigrationError("Key Vault returned an unexpected secret identifier.")
                name = validate_name(parts[1])
                if name.lower() in records:
                    raise MigrationError("Key Vault returned duplicate secret metadata.")
                records[name.lower()] = record
            path = page.get("nextLink") or ""
        return records

    def get(self, vault: str, name: str) -> dict[str, Any]:
        """Read one current secret value into memory only."""
        validate_name(name)
        return self.request(vault, "GET", f"/secrets/{name}?api-version={API_VERSION}")

    def put(self, vault: str, name: str, body: dict[str, Any]) -> dict[str, Any]:
        """Create a destination version with the unchanged value and metadata."""
        validate_name(name)
        return self.request(vault, "PUT", f"/secrets/{name}?api-version={API_VERSION}", body)


def preserved_metadata(record: dict[str, Any]) -> dict[str, Any]:
    """Keep enabled, validity dates, tags and content type; omit system timestamps."""
    attributes = record.get("attributes", {})
    kept: dict[str, Any] = {
        "attributes": {"enabled": attributes.get("enabled", True)},
        "tags": record.get("tags") or {},
    }
    for name in ("exp", "nbf"):
        if attributes.get(name) is not None:
            kept["attributes"][name] = attributes[name]
    if record.get("contentType") is not None:
        kept["contentType"] = record["contentType"]
    return kept


def preflight(source: dict[str, Any], name: str, now: float) -> None:
    """Fail closed for a credential that jobs cannot currently use."""
    attrs = source.get("attributes", {})
    if attrs.get("enabled", True) is not True:
        raise MigrationError(f"Source secret {name} is disabled.")
    if attrs.get("exp") is not None and attrs["exp"] <= now:
        raise MigrationError(f"Source secret {name} is expired.")
    if attrs.get("nbf") is not None and attrs["nbf"] > now:
        raise MigrationError(f"Source secret {name} is not yet valid.")


def selected_entries(
    entries: list[dict[str, str]], mode: str, replace_target: str | None
) -> list[dict[str, str]]:
    """Limit an explicit rotation to one configured target before authentication."""
    if replace_target is None:
        return entries
    validate_name(replace_target)
    if mode != "apply":
        raise MigrationError("--replace-target requires --apply.")
    matches = [entry for entry in entries if entry["target"].lower() == replace_target.lower()]
    if len(matches) != 1:
        raise MigrationError("Replacement target must name exactly one manifest target.")
    return matches


def migrate(
    client: VaultClient,
    source_vault: str,
    target_vault: str,
    entries: list[dict[str, str]],
    mode: str = "preview",
    replace_target: str | None = None,
) -> list[str]:
    """Preflight before writes; replace only one explicitly selected target."""
    if source_vault.lower() == target_vault.lower():
        raise MigrationError("Source and target vault must be different.")
    if mode not in {"preview", "apply", "verify"}:
        raise MigrationError("Invalid migration mode.")
    entries = selected_entries(entries, mode, replace_target)
    sources, targets = client.metadata(source_vault), client.metadata(target_vault)
    pending: list[tuple[str, dict[str, Any], dict[str, Any] | None]] = []
    messages = []
    now = time.time()
    for entry in entries:
        source_name, target_name = entry["source"], entry["target"]
        source, target = sources.get(source_name.lower()), targets.get(target_name.lower())
        if source is None:
            raise MigrationError(f"Required source secret {source_name} is missing.")
        preflight(source, source_name, now)
        if replace_target is not None and target is None:
            raise MigrationError(f"Replacement target secret {target_name} is missing.")
        if (
            replace_target is None
            and target is not None
            and preserved_metadata(source) != preserved_metadata(target)
        ):
            raise MigrationError(f"Target secret {target_name} has conflicting metadata.")
        if mode == "preview":
            status = "exists; value equality not checked" if target is not None else "would copy"
            messages.append(f"{source_name} -> {target_name}: {status}")
            continue
        value = client.get(source_vault, source_name)
        preflight(value, source_name, now)
        if preserved_metadata(value) != preserved_metadata(source):
            raise MigrationError(f"Source secret {source_name} changed during preflight; retry.")
        if not isinstance(value.get("value"), str):
            raise MigrationError(f"Source secret {source_name} has no readable value.")
        body = {"value": value["value"], **preserved_metadata(value)}
        if target is not None:
            existing = client.get(target_vault, target_name)
            if existing.get("value") != value["value"] or preserved_metadata(existing) != (
                preserved_metadata(value)
            ):
                if replace_target is None:
                    raise MigrationError(
                        f"Target secret {target_name} differs; refusing overwrite."
                    )
                pending.append((target_name, body, existing))
            else:
                messages.append(f"{target_name}: verified unchanged")
        elif mode == "verify":
            raise MigrationError(f"Required target secret {target_name} is missing.")
        else:
            pending.append((target_name, body, None))
    if pending:
        # Check again after the potentially slow value preflight. Operators must
        # still stop other writers: Key Vault does not fence the final PUT race.
        latest_targets = client.metadata(target_vault)
        for name, _, existing in pending:
            if existing is None and name.lower() in latest_targets:
                raise MigrationError(f"Target secret {name} appeared during preflight; retry.")
            if existing is not None:
                if name.lower() not in latest_targets:
                    raise MigrationError(
                        f"Target secret {name} disappeared during preflight; retry."
                    )
                latest = client.get(target_vault, name)
                if latest.get("value") != existing.get("value") or preserved_metadata(latest) != (
                    preserved_metadata(existing)
                ):
                    raise MigrationError(f"Target secret {name} changed during preflight; retry.")
    for name, body, existing in pending:
        result = client.put(target_vault, name, body)
        if result.get("value") != body["value"] or preserved_metadata(result) != (
            preserved_metadata(body)
        ):
            raise MigrationError(
                f"Target secret {name} failed copy verification; stop and inspect."
            )
        stored = client.get(target_vault, name)
        if stored.get("value") != body["value"] or preserved_metadata(stored) != (
            preserved_metadata(body)
        ):
            raise MigrationError(
                f"Target secret {name} failed read-back verification; stop and inspect."
            )
        action = "new version copied" if existing is not None else "copied"
        messages.append(f"{name}: {action} and verified")
    return messages


def main(argv: list[str] | None = None) -> int:
    """Run the safe-by-default operator command."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--source-vault", help="Override manifest source vault")
    parser.add_argument(
        "--target-vault", required=True, help="Existing dedicated destination vault"
    )
    parser.add_argument("--subscription", help="Azure CLI authentication subscription ID or name")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--apply",
        action="store_true",
        help="Read and copy values; conflicts require --replace-target",
    )
    modes.add_argument("--verify", action="store_true", help="Read values and check equality only")
    parser.add_argument(
        "--replace-target",
        help="With --apply, copy one reviewed rotation as a new version of this manifest target",
    )
    args = parser.parse_args(argv)
    try:
        source, entries = load_manifest(args.manifest)
        source = validate_name(args.source_vault or source, vault=True)
        target = validate_name(args.target_vault, vault=True)
        mode = "apply" if args.apply else "verify" if args.verify else "preview"
        entries = selected_entries(entries, mode, args.replace_target)
        messages = migrate(
            VaultClient(args.subscription), source, target, entries, mode, args.replace_target
        )
        print(f"{mode}: {source} -> {target}; {len(entries)} secrets")
        for message in messages:
            print(message)
        return 0
    except MigrationError as exc:
        print(f"Migration stopped: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print(
            "Migration stopped: unexpected failure; details suppressed to protect secrets.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
