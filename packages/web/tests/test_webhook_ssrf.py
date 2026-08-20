"""Webhook SSRF hardening + secret-at-rest (H2-6).

The guard now resolves DNS (a hostname that resolves to a private/metadata
address is blocked, not just IP literals), the delivery client refuses redirects,
and the SQL store encrypts the signing secret at rest.
"""

import base64
import sqlite3
from typing import Any, cast

from rgnr8_qbo import cipher_from_env
from rgnr8_web import SqlWebhookEndpointStore, WebhookEndpoint, validate_target

_FERNET_KEY = base64.urlsafe_b64encode(b"0" * 32).decode()


def test_hostname_resolving_to_private_ip_is_blocked() -> None:
    # public → allowed
    assert validate_target("https://hooks.acme.com/x", resolver=lambda h: ["93.184.216.34"]) is None
    # resolves to loopback → blocked (DNS-rebinding defense)
    err = validate_target("https://evil.acme.com/x", resolver=lambda h: ["127.0.0.1"])
    assert err is not None and "non-public" in err
    # resolves to the cloud metadata address → blocked
    err2 = validate_target("https://meta.acme.com/x", resolver=lambda h: ["169.254.169.254"])
    assert err2 is not None
    # unresolvable → blocked (fail closed)
    assert validate_target("https://nope.acme.com/x", resolver=lambda h: []) is not None


def test_ip_literal_private_targets_still_blocked_without_resolving() -> None:
    for url in ("https://127.0.0.1/x", "https://10.0.0.5/x", "https://169.254.169.254/x",
                "https://localhost/x"):
        assert validate_target(url, resolver=lambda h: ["93.184.216.34"]) is not None


def test_sql_store_encrypts_the_signing_secret_at_rest() -> None:
    conn = sqlite3.connect(":memory:")
    cipher = cipher_from_env({"RGNR8_SECRET_KEY": _FERNET_KEY})
    store = SqlWebhookEndpointStore(cast("Any", conn), placeholder="?", cipher=cipher)
    store.create_schema()
    store.save(WebhookEndpoint("ep1", "acme", "https://hooks.acme.com/x", "super-secret", ("close.sealed",)))
    # the raw DB column is ciphertext, not the plaintext secret
    raw = conn.execute("SELECT secret FROM webhook_endpoint WHERE id='ep1'").fetchall()[0][0]
    assert "super-secret" not in str(raw)
    # but the store decrypts on read
    got = store.get("ep1")
    assert got is not None and got.secret == "super-secret"
