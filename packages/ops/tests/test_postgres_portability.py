"""Every SQL store's schema, exercised against a REAL Postgres.

Four production bugs shipped because this file did not exist. The Python suites
run on sqlite, which is forgiving in exactly the places Postgres is not, so
dialect breakage stayed invisible until a container failed to boot:

  * audit_event's DDL used sqlite-only AUTOINCREMENT -- Postgres rejected it, so
    the audit log simply had no table on a Postgres deployment.
  * rgnr8_api_key ran a CREATE and a deliberately-failing ALTER probe in ONE
    transaction; on Postgres the failed statement aborted the transaction and the
    swallowed exception took the CREATE down with it. The table silently never
    existed, with no error anywhere.
  * the migrations bookkeeping table used the same probe-then-create shape.
  * concurrent gunicorn workers raced on CREATE TABLE IF NOT EXISTS and died on a
    pg_type unique violation.

Every one of them is a create_schema() that works on sqlite and not on Postgres.
The store list is DISCOVERED, not hand-maintained, so a store added tomorrow is
covered without anyone remembering to add it here.
"""

from __future__ import annotations

import importlib
import inspect
import os
import pkgutil
import threading

import pytest

pytest.importorskip("psycopg")
import psycopg  # noqa: E402

from rgnr8_ops import bootstrap_python_schemas  # noqa: E402

DSN = os.environ.get("RGNR8_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DSN, reason="RGNR8_TEST_DATABASE_URL not set (needs real Postgres)")

# Top-level packages that can own a durable table.
_ROOTS = (
    "rgnr8_web", "rgnr8_ops", "rgnr8_runtime", "rgnr8_billing",
    "rgnr8_qbo", "rgnr8_reports",
)


def _discover_stores() -> list[type]:
    """Every class owning a create_schema(), found by walking the packages."""
    seen: dict[str, type] = {}
    for root in _ROOTS:
        try:
            pkg = importlib.import_module(root)
        except ImportError:  # pragma: no cover - package not installed in this env
            continue
        for info in pkgutil.walk_packages(pkg.__path__, prefix=f"{root}."):
            try:
                mod = importlib.import_module(info.name)
            except Exception:  # noqa: BLE001 - optional deps must not fail discovery
                continue
            for name, obj in vars(mod).items():
                if (inspect.isclass(obj) and obj.__module__ == info.name
                        and callable(getattr(obj, "create_schema", None))
                        and not name.startswith("_")):
                    seen[f"{info.name}.{name}"] = obj
    return [seen[k] for k in sorted(seen)]


def _construct(cls: type, conn: object) -> object:
    """Build a store over `conn`. Only the shapes this codebase actually uses."""
    kwargs: dict[str, object] = {"placeholder": "%s"}
    params = inspect.signature(cls.__init__).parameters
    if "cipher" in params:
        from rgnr8_qbo import cipher_from_env
        kwargs["cipher"] = cipher_from_env({})
    return cls(conn, **kwargs)  # type: ignore[call-arg]


@pytest.fixture()
def conn() -> "psycopg.Connection[object]":
    c = psycopg.connect(DSN)
    c.execute("DROP SCHEMA IF EXISTS portability CASCADE")
    c.execute("CREATE SCHEMA portability")
    c.execute("SET search_path TO portability")
    c.commit()
    try:
        yield c
    finally:
        c.rollback()
        c.execute("DROP SCHEMA IF EXISTS portability CASCADE")
        c.commit()
        c.close()


def test_discovery_actually_finds_the_stores() -> None:
    """Guard the guard: if discovery silently returns nothing, every test below
    passes vacuously."""
    stores = _discover_stores()
    assert len(stores) >= 15, f"expected the full set of SQL stores, found {len(stores)}"


@pytest.mark.parametrize("cls", _discover_stores(), ids=lambda c: c.__name__)
def test_create_schema_runs_on_postgres(cls: type, conn: "psycopg.Connection[object]") -> None:
    """The DDL must be valid Postgres, and must actually leave tables behind.

    Asserting on pg_tables (not just "no exception") is the point: rgnr8_api_key
    raised nothing and still created nothing.
    """
    before = _table_names(conn)
    _construct(cls, conn).create_schema()  # type: ignore[attr-defined]
    conn.commit()
    after = _table_names(conn)
    assert after > before, f"{cls.__name__}.create_schema() completed but created no table"


@pytest.mark.parametrize("cls", _discover_stores(), ids=lambda c: c.__name__)
def test_create_schema_is_idempotent_on_postgres(cls: type, conn: "psycopg.Connection[object]") -> None:
    """Every deploy re-runs these; a second run must be a clean no-op."""
    store = _construct(cls, conn)
    store.create_schema()  # type: ignore[attr-defined]
    conn.commit()
    first = _table_names(conn)
    store.create_schema()  # type: ignore[attr-defined]
    conn.commit()
    assert _table_names(conn) == first


def test_bootstrap_creates_every_table_it_claims(conn: "psycopg.Connection[object]") -> None:
    """bootstrap_python_schemas returns the tables it says it ensured. It used to
    return 18 names while creating 17 -- the return value was lying."""
    claimed = bootstrap_python_schemas(conn, placeholder="%s")
    conn.commit()
    missing = sorted(set(claimed) - _table_names(conn))
    assert not missing, f"claimed but never created: {missing}"


def test_concurrent_bootstrap_does_not_race(conn: "psycopg.Connection[object]") -> None:
    """gunicorn boots N workers that each bootstrap at import time. Unserialized,
    `CREATE TABLE IF NOT EXISTS` races in the system catalogs and the losers die
    with a pg_type unique violation -- measured at 9 failures in 12 runs before
    the advisory lock. One connection per thread, as in production.
    """
    workers, errors = 8, []
    barrier = threading.Barrier(workers)

    def run() -> None:
        c = psycopg.connect(DSN)
        try:
            c.execute("SET search_path TO portability")
            barrier.wait(timeout=30)          # maximise the overlap
            bootstrap_python_schemas(c, placeholder="%s")
            c.commit()
        except Exception as exc:  # noqa: BLE001 - collected and asserted on below
            errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            c.close()

    threads = [threading.Thread(target=run) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, f"concurrent bootstrap raced: {errors[:2]}"


def _table_names(c: "psycopg.Connection[object]") -> set[str]:
    return {
        str(r[0]) for r in
        c.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'portability'").fetchall()
    }
