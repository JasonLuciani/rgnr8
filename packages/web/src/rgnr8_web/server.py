"""http.server wrapper around the pure WebApp, plus a runnable demo.

    python -m rgnr8_web            # serves a demo tenant on :8080
    python -m rgnr8_web 9000       # custom port
"""

from __future__ import annotations

import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Type

from .app import Request, Response, WebApp
from .middleware import with_security_headers
from .wsgi import MAX_BODY_BYTES


def make_handler(app: WebApp) -> Type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "rgnr8-web/0.1"

        def _dispatch(self, method: str) -> None:
            length = int(self.headers.get("Content-Length", "0") or "0")
            # Cap the body before reading it into memory, matching the WSGI adapter
            # (no framework is doing this for us).
            if length > MAX_BODY_BYTES:
                resp = Response(413, '{"error":"request body too large"}')
            else:
                # surrogateescape (PEP 383) so an uploaded file survives the trip
                # as a str: re-encoding with the same handler gives back exactly
                # the bytes that arrived. Plain UTF-8 text is unaffected.
                body = (
                    self.rfile.read(length).decode("utf-8", "surrogateescape")
                    if length else ""
                )
                headers = {k.lower(): v for k, v in self.headers.items()}
                resp = app.handle(Request(method=method, path=self.path, headers=headers, body=body))
            # Merge the standard hardening headers for parity with the WSGI path.
            resp = with_security_headers(resp)
            encoded = resp.body_bytes()
            self.send_response(resp.status)
            for k, v in resp.headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def log_message(self, *args: object) -> None:  # silence default logging
            return

    return Handler


def serve(app: WebApp, port: int = 8080, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    return httpd


def _demo_app() -> WebApp:
    from datetime import date

    from rgnr8_forecast import (
        CashPosition,
        Category,
        CustomerHistory,
        Direction,
        ForecastConfig,
        ForecastInputs,
        Frequency,
        Invoice,
        Money,
        PayrollSchedule,
        Recurrence,
        RecurringItem,
    )

    def usd(s: str) -> Money:
        return Money.from_decimal(s)

    inputs = ForecastInputs(
        opening=CashPosition(as_of=date(2026, 8, 3), available=usd("68000.00"), restricted=usd("8000.00")),
        invoices=(
            Invoice("INV-201", "northwind", date(2026, 7, 5), date(2026, 8, 20), usd("22000.00")),
            Invoice("INV-202", "contoso", date(2026, 7, 20), date(2026, 8, 31), usd("16500.00")),
        ),
        customer_histories=(CustomerHistory("northwind", override_days_late=8),),
        payroll=(
            PayrollSchedule("Payroll", Recurrence(Frequency.BIWEEKLY, anchor=date(2026, 8, 7)),
                            usd("21000.00"), usd("5600.00")),
        ),
        recurring=(
            RecurringItem("Office rent", Category.RENT, Direction.OUTFLOW, usd("7200.00"),
                          Recurrence(Frequency.MONTHLY, anchor=date(2026, 8, 1))),
        ),
    )
    app = WebApp()
    app.add_tenant("bright", "Bright Agency", inputs, ForecastConfig(minimum_cash=usd("15000.00")), token="demo-token")
    return app


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    app = _demo_app()
    httpd = serve(app, port)
    print(f"RGNR8 web on http://127.0.0.1:{port}  (tenant 'bright', bearer 'demo-token')")
    print(f"  open   http://127.0.0.1:{port}/t/bright   with header  Authorization: Bearer demo-token")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
