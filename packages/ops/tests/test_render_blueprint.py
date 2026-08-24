"""render.yaml is configuration that can only fail in production. Test it here.

Every defect this file guards was live in the merged blueprint, and not one of them
would have failed a deploy — Render would have reported four healthy services:

  * the cron worker had no RGNR8_JWT_SECRET, so `Settings.from_env()` would raise
    ConfigError under the default hs256 mode on line one of every run;
  * the operator console had its OWN generated JWT secret, so the tenant tokens it
    mints (Fleet.mint_token) would be rejected by the web app that validates them;
  * the worker had no RGNR8_SECRET_KEY, so it would build a NullCipher and fail to
    decrypt webhook endpoint secrets the web app had encrypted;
  * the worker had no SendGrid key, so it would run hourly forever, report success,
    and never send a briefing;
  * RGNR8_LEDGER_URL was hardcoded to `http://rgnr8-ledger:8181`, an address that
    does not resolve — Render's private hostnames carry a random suffix.

The point of asserting on the file is that these are all invisible at deploy time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

BLUEPRINT = Path(__file__).resolve().parents[3] / "render.yaml"

# Services built from the Python image; the ledger is Node and exempt.
_PYTHON_IMAGE = "./deploy/Dockerfile"


def _blueprint() -> dict:
    return yaml.safe_load(BLUEPRINT.read_text(encoding="utf-8"))


def _env_keys(service: dict, groups: dict[str, set[str]]) -> set[str]:
    """Every env key the service ends up with, group-supplied ones included."""
    keys: set[str] = set()
    for entry in service.get("envVars", []):
        if "fromGroup" in entry:
            keys |= groups.get(entry["fromGroup"], set())
        elif "key" in entry:
            keys.add(entry["key"])
    return keys


def _groups() -> dict[str, set[str]]:
    return {
        g["name"]: {e["key"] for e in g.get("envVars", [])}
        for g in _blueprint().get("envVarGroups", [])
    }


def _python_services() -> list[dict]:
    return [s for s in _blueprint()["services"] if s.get("dockerfilePath") == _PYTHON_IMAGE]


def test_discovery_finds_the_python_services() -> None:
    """Guard the guard: an empty list would make every assertion below vacuous."""
    names = {s["name"] for s in _python_services()}
    assert {"rgnr8-web", "rgnr8-operator", "rgnr8-worker"} <= names, names


@pytest.mark.parametrize("service", _python_services(), ids=lambda s: s["name"])
def test_every_python_service_gets_a_jwt_secret(service: dict) -> None:
    """hs256 is the default auth mode and Settings.from_env() raises without it."""
    assert "RGNR8_JWT_SECRET" in _env_keys(service, _groups()), (
        f"{service['name']} runs a Python entrypoint with no RGNR8_JWT_SECRET — "
        "Settings.from_env() raises ConfigError before it does any work."
    )


def test_the_jwt_secret_is_shared_not_per_service() -> None:
    """The operator mints the tenant tokens the web app validates: one key, or none work."""
    assert "RGNR8_JWT_SECRET" in _groups().get("rgnr8-shared", set()), (
        "RGNR8_JWT_SECRET must come from the shared env group. A per-service "
        "generateValue gives each service a DIFFERENT key, and operator-minted "
        "tenant tokens stop validating against the web app."
    )
    for service in _python_services():
        own = [e for e in service.get("envVars", []) if e.get("key") == "RGNR8_JWT_SECRET"]
        assert not own, (
            f"{service['name']} overrides RGNR8_JWT_SECRET locally — a service-level "
            "value wins over the group's and reintroduces exactly the drift the "
            "group exists to prevent."
        )


def test_both_cipher_users_carry_the_fernet_key() -> None:
    """The web app encrypts webhook endpoint secrets; the worker decrypts them."""
    groups = _groups()
    for name in ("rgnr8-web", "rgnr8-worker"):
        service = next(s for s in _blueprint()["services"] if s["name"] == name)
        assert "RGNR8_SECRET_KEY" in _env_keys(service, groups), (
            f"{name} needs RGNR8_SECRET_KEY — without it cipher_from_env() returns a "
            "NullCipher and at-rest decryption silently produces garbage."
        )


def test_the_fernet_key_is_never_generated() -> None:
    """generateValue would mint a DIFFERENT key per service and per re-provision."""
    for service in _blueprint()["services"]:
        for entry in service.get("envVars", []):
            if entry.get("key") == "RGNR8_SECRET_KEY":
                assert entry.get("sync") is False, (
                    f"{service['name']}: RGNR8_SECRET_KEY must be `sync: false` (pasted "
                    "by hand from the backed-up value). Generating it orphans every "
                    "QBO token already encrypted with the old key."
                )


def test_no_env_group_uses_sync_false() -> None:
    """Render rejects it: 'You can't define an environment variable with sync: false
    in an environment group.' A blueprint that tries fails at creation time."""
    for group in _blueprint().get("envVarGroups", []):
        for entry in group.get("envVars", []):
            assert "sync" not in entry, f"{group['name']}/{entry.get('key')}"


def test_private_service_addresses_are_never_hardcoded() -> None:
    """Render appends a random suffix to private hostnames — `rgnr8-ledger` is not
    a real address, and no blueprint can know the real one ahead of time."""
    for service in _blueprint()["services"]:
        for entry in service.get("envVars", []):
            if entry.get("key") == "RGNR8_LEDGER_URL":
                assert "value" not in entry, (
                    f"{service['name']}: RGNR8_LEDGER_URL is hardcoded. Use "
                    "`fromService: {name: rgnr8-ledger, type: pserv, property: hostport}`."
                )
                assert entry.get("fromService", {}).get("property") == "hostport", entry


def test_the_worker_can_actually_deliver() -> None:
    """No SendGrid key means RecordingDeliverer: hourly runs, zero briefings, no error."""
    worker = next(s for s in _blueprint()["services"] if s["type"] == "cron")
    assert "RGNR8_SENDGRID_API_KEY" in _env_keys(worker, _groups()), (
        "the delivery worker has no SendGrid key — it would report a successful run "
        "every hour while sending nothing."
    )


def test_only_the_web_service_is_public() -> None:
    """The ledger is the system of record and the operator console is the staff
    control plane. Neither belongs on public DNS."""
    for service in _blueprint()["services"]:
        if service["type"] != "web":
            assert "domains" not in service, service["name"]
            assert "healthCheckPath" not in service, (
                f"{service['name']}: healthCheckPath is web-services-only in the spec."
            )
    web = [s for s in _blueprint()["services"] if s["type"] == "web"]
    assert [s["name"] for s in web] == ["rgnr8-web"]
    assert web[0]["domains"] == ["acctg.rgnr8ventures.com"]


def test_the_web_service_is_on_a_paid_plan() -> None:
    """preDeployCommand — the only thing that runs migrations before traffic — is
    not available on free instances."""
    web = next(s for s in _blueprint()["services"] if s["type"] == "web")
    assert web.get("plan") not in (None, "free"), web.get("plan")
    assert web.get("preDeployCommand"), "nothing would migrate the schema before traffic"


def test_every_service_shares_the_database_region() -> None:
    """Render's private network spans exactly one region and one workspace."""
    blueprint = _blueprint()
    regions = {s.get("region") for s in blueprint["services"]}
    regions |= {d.get("region") for d in blueprint["databases"]}
    assert regions == {"oregon"}, regions
