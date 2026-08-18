#!/usr/bin/env python3
"""Run RGNR8 locally so you can connect QuickBooks Online end-to-end.

This is a **development** runner (not the production entrypoint). It stands up the
web app on http://localhost:8000 with the QuickBooks connect flow wired, one
placeholder company, and a simple email-only dev login — just enough to click
"Connect QuickBooks," approve on Intuit, and land back connected.

Usage
-----
    export QBO_CLIENT_ID="...from your Intuit app's Keys tab..."
    export QBO_CLIENT_SECRET="...from your Intuit app's Keys tab..."
    export QBO_ENVIRONMENT="sandbox"        # or "production" for your live company
    # optional overrides:
    #   QBO_REDIRECT_URI  (default http://localhost:8000/oauth/qbo/callback)
    #   OWNER_EMAIL       (default owner@example.com)
    #   PORT              (default 8000)
    python run_local.py

Then open http://localhost:8000/login, sign in with the owner email it prints,
and click Connect → QuickBooks Online.

No secrets are stored in this file; everything sensitive comes from the
environment. Tokens live in memory for this run (a restart means reconnect);
production uses the SQL stores instead.
"""

from __future__ import annotations

import glob
import os
import sys
from datetime import date

# --- make the monorepo packages importable without installing them ----------
_ROOT = os.path.dirname(os.path.abspath(__file__))
for _src in sorted(glob.glob(os.path.join(_ROOT, "packages", "*", "src"))):
    if _src not in sys.path:
        sys.path.insert(0, _src)

from rgnr8_forecast import CashPosition, ForecastConfig, ForecastInputs, Money  # noqa: E402
LEDGER_URL = os.environ.get("RGNR8_LEDGER_URL", "http://localhost:8181")
LEDGER_TOKEN = os.environ.get("RGNR8_LEDGER_TOKEN", "")
LEDGER_SEED = os.environ.get("RGNR8_LEDGER_SEED_CATEGORY", "SERVICE_GENERAL")

from rgnr8_qbo import (  # noqa: E402
    InMemoryConnectionStore,
    QboConnectService,
    QboEnvironment,
    QboOAuthConfig,
    UrllibHttpClient,
)
from rgnr8_web import (  # noqa: E402
    InMemoryUserDirectory,
    Role,
    User,
    LedgerClient,
    UrllibTransport,
    WebApp,
)
from rgnr8_web.server import serve  # noqa: E402

# --- configuration from the environment -------------------------------------
CLIENT_ID = os.environ.get("QBO_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("QBO_CLIENT_SECRET", "")
ENVIRONMENT = os.environ.get("QBO_ENVIRONMENT", "sandbox")
OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "owner@example.com")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "local-dev-session-secret-change-me")
PORT = int(os.environ.get("PORT", "8000"))
# Default the redirect URI to this server's port so it always matches; override
# with QBO_REDIRECT_URI if you register a different one on your Intuit app.
REDIRECT_URI = os.environ.get("QBO_REDIRECT_URI", f"http://localhost:{PORT}/oauth/qbo/callback")

TENANT_ID = "myco"
TENANT_NAME = os.environ.get("COMPANY_NAME", "My Company")


def build_app() -> WebApp:
    # One owner user who can manage connectors (needed for the Connect page).
    users = InMemoryUserDirectory()
    users.upsert_user(User(id=OWNER_EMAIL, email=OWNER_EMAIL, name="Owner"))
    users.set_membership(OWNER_EMAIL, TENANT_ID, Role.OWNER)

    # The QBO connect service — only wired if credentials are present, otherwise
    # the Connect page shows a friendly "not configured" note.
    qbo: QboConnectService | None = None
    if CLIENT_ID and CLIENT_SECRET:
        qbo = QboConnectService(
            QboOAuthConfig(
                client_id=CLIENT_ID,
                client_secret=CLIENT_SECRET,
                redirect_uri=REDIRECT_URI,
                environment=QboEnvironment(ENVIRONMENT),
            ),
            UrllibHttpClient(),
            InMemoryConnectionStore(),
            state_secret=SESSION_SECRET,
        )

    app = WebApp(
        users=users,
        session_secret=SESSION_SECRET,  # enables the simple email dev login
        qbo=qbo,
    )
    # A placeholder company so there's a tenant to connect. The forecast inputs
    # are a minimal opening balance; real numbers arrive once QBO is syncing.
    app.add_tenant(
        TENANT_ID,
        TENANT_NAME,
        ForecastInputs(opening=CashPosition(as_of=date.today(), available=Money.from_decimal("25000.00"))),
        ForecastConfig(minimum_cash=Money.from_decimal("10000.00")),
        token="local-dev",
    )

    # The books. The accounting core runs as its own service (@rgnr8/ledger-service);
    # point the web app at it so /t/<tenant>/books is a real general ledger. If the
    # service isn't running, the Books screen says so plainly instead of pretending.
    ledger = LedgerClient(UrllibTransport(LEDGER_URL), token=LEDGER_TOKEN)
    app.set_ledger(ledger)
    if ledger.health():
        seeded = ledger.accounts(TENANT_ID)
        existing = seeded.body.get("accounts") if seeded.ok else None
        if not existing:
            ledger.seed_chart(TENANT_ID, LEDGER_SEED)
    return app


def main() -> None:
    app = build_app()
    configured = bool(CLIENT_ID and CLIENT_SECRET)
    httpd = serve(app, port=PORT)
    line = "=" * 68
    print(line)
    print("  RGNR8 — local dev server")
    print(line)
    print(f"  URL:            http://localhost:{PORT}/login")
    print(f"  Sign in as:     {OWNER_EMAIL}   (any email box, no password needed)")
    print(f"  Company:        {TENANT_NAME}  (tenant id: {TENANT_ID})")
    ledger_up = LedgerClient(UrllibTransport(LEDGER_URL), token=LEDGER_TOKEN).health()
    print(f"  Books (ledger): {'UP at ' + LEDGER_URL if ledger_up else 'DOWN — start it: npm run start -w @rgnr8/ledger-service'}")
    print(f"  QBO connect:    {'CONFIGURED (' + ENVIRONMENT + ')' if configured else 'NOT configured — set QBO_CLIENT_ID / QBO_CLIENT_SECRET'}")
    if configured:
        print(f"  Redirect URI:   {REDIRECT_URI}")
        print("                  (register this EXACT string on your Intuit app)")
    print(line)
    print("  After signing in, open Connect → QuickBooks Online → Connect.")
    print("  Press Ctrl+C to stop.")
    print(line, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
