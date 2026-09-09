"""Account email: the invitation / verify / reset messages actually leaving.

The thing that matters here is that the recipient gets a *usable link*. A token
in a log or an API response is not a delivered invitation, which is exactly why
signup was auto-verifying in production before this existed.
"""

from __future__ import annotations

from rgnr8_briefing import FakeEmailTransport
from rgnr8_ops import Settings
from rgnr8_ops.account_mail import account_emailer, account_link

BASE = "https://acctg.rgnr8ventures.com"


def _emailer(transport: FakeEmailTransport, errors: list[str] | None = None):
    return account_emailer(transport, BASE,
                           on_error=(errors.append if errors is not None else None))


def test_each_purpose_sends_a_usable_absolute_link() -> None:
    t = FakeEmailTransport()
    send = _emailer(t)
    send("ada@acme.com", "invitation", "inv-tok")
    send("ada@acme.com", "verify_email", "ver-tok")
    send("ada@acme.com", "password_reset", "res-tok")

    links = [f"{BASE}/signup?token=inv-tok",
             f"{BASE}/verify?token=ver-tok",
             f"{BASE}/password/reset?token=res-tok"]
    assert len(t.sent) == 3
    for mail, link in zip(t.sent, links, strict=True):
        assert mail["recipient"] == "ada@acme.com"
        assert mail["subject"]                      # never blank
        # the link has to survive in BOTH bodies — plenty of clients show text only
        assert link in mail["text"]
        assert link in mail["html"]
        assert f'href="{link}"' in mail["html"]


def test_tokens_are_url_encoded_into_the_link() -> None:
    # secrets.token_urlsafe is safe, but the link builder must not assume that of
    # every token factory — an unescaped '&' or '#' would truncate the token.
    assert account_link(BASE, "invitation", "a+b/c=d&e") == (
        f"{BASE}/signup?token=a%2Bb%2Fc%3Dd%26e")


def test_base_url_trailing_slash_does_not_double_up() -> None:
    assert account_link("https://x.test/", "verify_email", "t") == "https://x.test/verify?token=t"


def test_html_body_carries_no_remote_assets() -> None:
    # Mail clients block remote images and strip <style> blocks; anything but
    # inline styles arrives as unstyled soup, and a remote asset leaks a read
    # receipt to whoever hosts it.
    t = FakeEmailTransport()
    _emailer(t)("ada@acme.com", "invitation", "tok")
    html = t.sent[0]["html"]
    assert "<style" not in html and "<img" not in html
    assert "src=" not in html
    assert html.count("http") == html.count(BASE)   # the only URL is our own link


def test_a_provider_outage_does_not_raise() -> None:
    # This runs inside request handling: if SendGrid is down, minting an
    # invitation must still succeed — the owner can copy the link from the team
    # page. Failing the request would lose the invitation that was just created.
    errors: list[str] = []
    send = _emailer(FakeEmailTransport(fail=True), errors)
    send("ada@acme.com", "invitation", "super-secret-token")
    assert len(errors) == 1
    assert "invitation" in errors[0] and "ada@acme.com" in errors[0]
    # the report must never carry the token — the link IS the credential
    assert "super-secret-token" not in errors[0]


def test_an_unknown_purpose_is_ignored_rather_than_raised() -> None:
    errors: list[str] = []
    t = FakeEmailTransport()
    _emailer(t, errors)("ada@acme.com", "some_future_purpose", "tok")
    assert t.sent == []
    assert len(errors) == 1 and "no template" in errors[0]


# --- config gate -------------------------------------------------------------


def _settings(**env: str) -> Settings:
    base = {"RGNR8_AUTH_MODE": "static", "RGNR8_DATABASE_URL": "postgres://x/y"}
    return Settings.from_env({**base, **env})


def test_account_mail_needs_both_a_key_and_a_base_url() -> None:
    assert _settings().account_mail_enabled is False
    # a key with nowhere to point the links is worse than nothing: the mail goes
    # out and the recipient can't act on it
    assert _settings(RGNR8_SENDGRID_API_KEY="SG.x").account_mail_enabled is False
    assert _settings(RGNR8_PUBLIC_BASE_URL=BASE).account_mail_enabled is False
    assert _settings(RGNR8_SENDGRID_API_KEY="SG.x",
                     RGNR8_PUBLIC_BASE_URL=BASE).account_mail_enabled is True


def test_base_url_falls_back_to_the_qbo_redirect_origin() -> None:
    # Intuit forces the redirect URI to be the real public origin, so a correctly
    # configured deployment needs no second variable saying the same thing twice.
    s = _settings(RGNR8_SENDGRID_API_KEY="SG.x",
                  RGNR8_QBO_REDIRECT_URI=f"{BASE}/oauth/qbo/callback")
    assert s.public_base_url == BASE
    assert s.account_mail_enabled is True


def test_an_explicit_base_url_wins_over_the_derived_one() -> None:
    s = _settings(RGNR8_PUBLIC_BASE_URL="https://explicit.test/",
                  RGNR8_QBO_REDIRECT_URI=f"{BASE}/oauth/qbo/callback")
    assert s.public_base_url == "https://explicit.test"   # trailing slash trimmed


def test_missing_mail_config_warns_instead_of_failing_the_boot() -> None:
    # The beta runs without email on purpose; it must boot, and it must say so.
    warnings = " ".join(_settings().warnings)
    assert "RGNR8_SENDGRID_API_KEY" in warnings
    assert "copied by hand" in warnings
    keyed = " ".join(_settings(RGNR8_SENDGRID_API_KEY="SG.x").warnings)
    assert "RGNR8_PUBLIC_BASE_URL" in keyed


def test_redacted_config_reports_mail_state_without_the_key() -> None:
    r = _settings(RGNR8_SENDGRID_API_KEY="SG.super-secret",
                  RGNR8_PUBLIC_BASE_URL=BASE).redacted()
    assert r["sendgrid"] == "set" and r["account_mail"] == "on"
    assert "SG.super-secret" not in str(r)
