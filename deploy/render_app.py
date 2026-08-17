"""Production WSGI entrypoint for deploying RGNR8 (e.g. on Render).

Exposes ``application`` for a WSGI server:

    gunicorn deploy.render_app:application --bind 0.0.0.0:$PORT

It builds the web app from environment variables — the QuickBooks connect flow is
wired only when `QBO_CLIENT_ID` / `QBO_CLIENT_SECRET` are present, so the service
boots fine before the Intuit credentials exist (the Connect page just shows
"not configured" until they do). The OAuth redirect URI defaults to this service's
own public URL (`RENDER_EXTERNAL_URL`) so it always matches what the platform
serves — register that value on the Intuit app.

Tokens are held in memory for this first cut (a redeploy means reconnect); swap in
`SqlConnectionStore` + a Postgres `DATABASE_URL` for durability.
"""

from __future__ import annotations

import glob
import os
import sys
from datetime import date

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _src in sorted(glob.glob(os.path.join(_ROOT, "packages", "*", "src"))):
    if _src not in sys.path:
        sys.path.insert(0, _src)

from rgnr8_forecast import CashPosition, ForecastConfig, ForecastInputs, Money  # noqa: E402
from rgnr8_qbo import (  # noqa: E402
    InMemoryConnectionStore,
    QboConnectService,
    QboEnvironment,
    QboOAuthConfig,
    UrllibHttpClient,
)
from rgnr8_web import InMemoryUserDirectory, Role, User, WebApp  # noqa: E402
from rgnr8_web.wsgi import wsgi_app  # noqa: E402

OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "owner@example.com")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "change-me-in-env")
ENVIRONMENT = os.environ.get("QBO_ENVIRONMENT", "sandbox")
CLIENT_ID = os.environ.get("QBO_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("QBO_CLIENT_SECRET", "")
TENANT_ID = "myco"
TENANT_NAME = os.environ.get("COMPANY_NAME", "My Company")

# Render exposes the service's public URL here; the OAuth redirect must match it.
_public = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
REDIRECT_URI = os.environ.get(
    "QBO_REDIRECT_URI",
    (f"{_public}/oauth/qbo/callback" if _public else "http://localhost:8000/oauth/qbo/callback"),
)


def build_app() -> WebApp:
    users = InMemoryUserDirectory()
    users.upsert_user(User(id=OWNER_EMAIL, email=OWNER_EMAIL, name="Owner"))
    users.set_membership(OWNER_EMAIL, TENANT_ID, Role.OWNER)

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

    app = WebApp(users=users, session_secret=SESSION_SECRET, qbo=qbo)
    app.add_tenant(
        TENANT_ID,
        TENANT_NAME,
        ForecastInputs(opening=CashPosition(as_of=date.today(), available=Money.from_decimal("25000.00"))),
        ForecastConfig(minimum_cash=Money.from_decimal("10000.00")),
        token="deploy",
    )
    return app


application = wsgi_app(build_app())
