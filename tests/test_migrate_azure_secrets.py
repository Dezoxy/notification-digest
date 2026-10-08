"""Migration preflight, metadata preservation and credential-output boundaries."""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import urllib.error
from pathlib import Path
from unittest.mock import Mock

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/migrate_azure_secrets.py"
SPEC = importlib.util.spec_from_file_location("migrate_secrets", SCRIPT)
assert SPEC and SPEC.loader
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)

VALUE = "private-cookie-and-token-value"
ENTRY = {"source": "digest-a", "target": "digest-a"}
RECORD = {
    "attributes": {"enabled": True, "nbf": 10, "exp": 4_000_000_000, "created": 50},
    "tags": {"folder": "digest", "tenant": "homelab"},
    "contentType": "application/json",
}


class FakeClient:
    def __init__(self, sources=None, targets=None, values=None):
        self.sources = sources if sources is not None else {"digest-a": copy.deepcopy(RECORD)}
        self.targets = targets or {}
        self.values = values or {}
        self.reads = []
        self.writes = []

    def metadata(self, vault):
        return self.sources if vault == "source-vault" else self.targets

    def get(self, vault, name):
        self.reads.append((vault, name))
        return copy.deepcopy(self.values.get((vault, name), {"value": VALUE, **RECORD}))

    def put(self, vault, name, body):
        self.writes.append((vault, name, body))
        return copy.deepcopy(body)


def test_preview_does_not_read_or_write_values():
    client = FakeClient()
    result = migration.migrate(client, "source-vault", "target-vault", [ENTRY])
    assert result == ["digest-a -> digest-a: would copy"]
    assert client.reads == client.writes == []


def test_preview_existing_reports_equality_unchecked():
    client = FakeClient(targets={"digest-a": copy.deepcopy(RECORD)})
    result = migration.migrate(client, "source-vault", "target-vault", [ENTRY])
    assert "value equality not checked" in result[0]
    assert not client.reads


def test_copy_preserves_secret_and_user_metadata_only():
    client = FakeClient()
    result = migration.migrate(client, "source-vault", "target-vault", [ENTRY], "apply")
    body = client.writes[0][2]
    assert body == {
        "value": VALUE,
        "attributes": {"enabled": True, "nbf": 10, "exp": 4_000_000_000},
        "tags": RECORD["tags"],
        "contentType": "application/json",
    }
    assert VALUE not in str(result)


@pytest.mark.parametrize("mode", ["apply", "verify"])
def test_equal_existing_is_verified_without_new_version(mode):
    client = FakeClient(targets={"digest-a": copy.deepcopy(RECORD)})
    assert migration.migrate(client, "source-vault", "target-vault", [ENTRY], mode) == [
        "digest-a: verified unchanged"
    ]
    assert len(client.reads) == 2
    assert not client.writes


def test_value_conflict_blocks_entire_manifest_before_writes():
    sources = {"digest-b": copy.deepcopy(RECORD), "digest-a": copy.deepcopy(RECORD)}
    client = FakeClient(
        sources=sources,
        targets={"digest-a": copy.deepcopy(RECORD)},
        values={("target-vault", "digest-a"): {"value": "different-private-value", **RECORD}},
    )
    entries = [{"source": "digest-b", "target": "digest-b"}, ENTRY]
    with pytest.raises(migration.MigrationError, match="refusing overwrite") as exc:
        migration.migrate(client, "source-vault", "target-vault", entries, "apply")
    assert VALUE not in str(exc.value)
    assert not client.writes


@pytest.mark.parametrize(
    "change",
    [
        {"tags": {"folder": "other"}},
        {"contentType": "text/plain"},
        {"attributes": {"enabled": False}},
    ],
)
def test_existing_metadata_conflict_blocks_value_reads(change):
    client = FakeClient(targets={"digest-a": {**copy.deepcopy(RECORD), **change}})
    with pytest.raises(migration.MigrationError, match="conflicting metadata"):
        migration.migrate(client, "source-vault", "target-vault", [ENTRY], "apply")
    assert not client.reads and not client.writes


@pytest.mark.parametrize(
    "attributes,message",
    [
        ({"enabled": False}, "disabled"),
        ({"enabled": True, "exp": 1}, "expired"),
        ({"enabled": True, "nbf": 9_000_000_000}, "not yet valid"),
    ],
)
def test_unusable_source_fails_closed(attributes, message):
    client = FakeClient(sources={"digest-a": {"attributes": attributes}})
    with pytest.raises(migration.MigrationError, match=message):
        migration.migrate(client, "source-vault", "target-vault", [ENTRY], "apply")
    assert not client.reads and not client.writes


def test_missing_source_blocks_copy():
    client = FakeClient(sources={})
    with pytest.raises(migration.MigrationError, match="source secret.*missing"):
        migration.migrate(client, "source-vault", "target-vault", [ENTRY], "apply")
    assert not client.writes


def test_verify_missing_destination_never_writes():
    client = FakeClient()
    with pytest.raises(migration.MigrationError, match="target secret.*missing"):
        migration.migrate(client, "source-vault", "target-vault", [ENTRY], "verify")
    assert not client.writes


def test_source_metadata_change_before_value_read_stops_copy():
    client = FakeClient(
        values={
            ("source-vault", "digest-a"): {"value": VALUE, **RECORD, "tags": {"changed": "yes"}}
        }
    )
    with pytest.raises(migration.MigrationError, match="changed during preflight"):
        migration.migrate(client, "source-vault", "target-vault", [ENTRY], "apply")
    assert not client.writes


def test_destination_appearing_during_preflight_blocks_copy():
    client = FakeClient()
    original_metadata = client.metadata
    calls = 0

    def changed_metadata(vault):
        nonlocal calls
        if vault == "target-vault":
            calls += 1
            if calls == 2:
                return {"digest-a": RECORD}
        return original_metadata(vault)

    client.metadata = changed_metadata
    with pytest.raises(migration.MigrationError, match="appeared during preflight"):
        migration.migrate(client, "source-vault", "target-vault", [ENTRY], "apply")
    assert not client.writes


def test_readback_failure_stops_with_safe_error():
    client = FakeClient(
        values={("target-vault", "digest-a"): {"value": "wrong-secret-value", **RECORD}}
    )
    with pytest.raises(migration.MigrationError, match="read-back verification") as exc:
        migration.migrate(client, "source-vault", "target-vault", [ENTRY], "apply")
    assert len(client.writes) == 1
    assert VALUE not in str(exc.value)


def test_same_vault_rejected_case_insensitively():
    client = FakeClient()
    with pytest.raises(migration.MigrationError, match="must be different"):
        migration.migrate(client, "SOURCE-vault", "source-vault", [ENTRY])


@pytest.mark.parametrize(
    "entries",
    [
        [],
        [ENTRY, ENTRY],
        [ENTRY, {"source": "digest-b", "target": "DIGEST-A"}],
        [{"source": "../unsafe", "target": "digest-a"}],
    ],
)
def test_manifest_rejects_empty_duplicate_and_unsafe_names(tmp_path, entries):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"source_vault": "source-vault", "secrets": entries}))
    with pytest.raises(migration.MigrationError):
        migration.load_manifest(path)


def test_real_manifest_exact_runtime_and_cookie_scope():
    source, entries = migration.load_manifest(migration.MANIFEST)
    assert source == "kv-homelab-prod-th"
    names = {entry["source"] for entry in entries}
    assert len(names) == 18
    assert "digest-x-cookies" in names
    assert "digest-notify-telegram-bot-token" not in names
    assert "digest-notify-telegram-chat-id" not in names
    example = (SCRIPT.parents[1] / "infra/azure/production.auto.tfvars.example").read_text()
    for name in names - {"digest-x-cookies"}:
        assert name in example


def test_http_failure_does_not_include_response_or_token():
    client = object.__new__(migration.VaultClient)
    client._token = VALUE
    client._opener = Mock()
    client._opener.open.side_effect = urllib.error.HTTPError(
        "https://source-vault.vault.azure.net", 403, VALUE, {}, None
    )
    with pytest.raises(migration.MigrationError) as exc:
        client.request("source-vault", "GET", "/secrets?api-version=7.4")
    assert str(exc.value) == "Key Vault GET failed with HTTP 403."


def test_cli_failure_does_not_include_stderr(monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["az"], output=VALUE, stderr=VALUE)

    monkeypatch.setattr(migration.subprocess, "run", fail)
    with pytest.raises(migration.MigrationError) as exc:
        migration.VaultClient()
    assert VALUE not in str(exc.value)


def test_access_token_is_captured_and_only_subscription_in_command(monkeypatch):
    runner = Mock(return_value=subprocess.CompletedProcess(["az"], 0, stdout=VALUE.encode()))
    monkeypatch.setattr(migration.subprocess, "run", runner)
    client = migration.VaultClient("subscription-id")
    command = runner.call_args.args[0]
    assert VALUE not in command
    assert command[-2:] == ["--subscription", "subscription-id"]
    assert runner.call_args.kwargs["capture_output"] is True
    assert client._token == VALUE


def test_pagination_cross_host_rejected_before_token_use():
    client = object.__new__(migration.VaultClient)
    client._token = VALUE
    client._opener = Mock()
    with pytest.raises(migration.MigrationError, match="unexpected pagination endpoint"):
        client.request("source-vault", "GET", "https://attacker.example/secrets")
    client._opener.open.assert_not_called()


def test_metadata_follows_pages_and_rejects_loop():
    client = object.__new__(migration.VaultClient)
    first = {
        "value": [{"id": "https://source-vault.vault.azure.net/secrets/digest-a"}],
        "nextLink": "https://source-vault.vault.azure.net/secrets?skiptoken=2",
    }
    second = {"value": [{"id": "https://source-vault.vault.azure.net/secrets/digest-b"}]}
    client.request = Mock(side_effect=[first, second])
    assert set(client.metadata("source-vault")) == {"digest-a", "digest-b"}
    first["nextLink"] = "/secrets?api-version=7.4"
    client.request = Mock(return_value=first)
    with pytest.raises(migration.MigrationError, match="repeated a page"):
        client.metadata("source-vault")


def test_redirect_handler_never_forwards_credentials():
    assert migration.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil") is None


def test_unexpected_exception_main_suppresses_value(monkeypatch, capsys):
    monkeypatch.setattr(migration, "VaultClient", Mock(side_effect=ValueError(VALUE)))
    assert migration.main(["--target-vault", "target-vault"]) == 1
    captured = capsys.readouterr()
    assert VALUE not in captured.err + captured.out


def test_vault_names_use_canonical_dns_case():
    assert migration.validate_name("TARGET-Vault", vault=True) == "target-vault"
