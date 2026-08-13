"""The custom builder and saved-report persistence.

* :func:`build_report` parses and validates a custom report definition — a JSON
  ``{id, title, description, sections:[{kind, title, params}]}`` object, or a bare
  ``[{kind, title, params}]`` section list — into a :class:`ReportSpec`. Unknown
  section kinds are rejected up front (against the registry), so a bad spec fails
  at build time, not mid-render.
* :func:`spec_to_dict` / :func:`spec_from_dict` are the JSON round-trip used to
  persist specs.
* :class:`SavedReportStore` is the per-tenant persistence seam, with the house
  trio: a Protocol, an in-memory impl for tests/local, and a DB-API SQL impl for
  production (mirroring ``rgnr8_web.apikeys.SqlApiKeyStore``). Specs are stored as
  JSON text keyed by ``(tenant_id, report_id)``.
"""

from __future__ import annotations

import json
from typing import Mapping, Protocol

from .model import ReportSpec, SectionSpec
from .sections import known_kinds


class ReportSpecError(ValueError):
    """A custom report definition was malformed or used an unknown section kind."""


def _mapping(o: object, what: str) -> Mapping[str, object]:
    if not isinstance(o, Mapping):
        raise ReportSpecError(f"{what} must be an object, got {type(o).__name__}")
    return o


def build_report(spec_json: object) -> ReportSpec:
    """Parse + validate a custom report spec into a :class:`ReportSpec`.

    Accepts a JSON string, a ``{id, title, description, sections, params}`` mapping,
    or a bare ``[{kind, title, params}]`` section list. Every section's ``kind``
    must be a registered kind, or :class:`ReportSpecError` is raised.
    """
    if isinstance(spec_json, str):
        spec_json = json.loads(spec_json)

    if isinstance(spec_json, list):
        data: Mapping[str, object] = {"sections": spec_json}
    else:
        data = _mapping(spec_json, "report spec")

    raw_sections = data.get("sections")
    if not isinstance(raw_sections, list):
        raise ReportSpecError("report spec must have a 'sections' list")

    valid = known_kinds()
    sections: list[SectionSpec] = []
    for i, raw in enumerate(raw_sections):
        m = _mapping(raw, f"section[{i}]")
        kind = m.get("kind")
        if not isinstance(kind, str):
            raise ReportSpecError(f"section[{i}] is missing a string 'kind'")
        if kind not in valid:
            raise ReportSpecError(
                f"section[{i}] has unknown kind {kind!r}; valid kinds: {sorted(valid)}"
            )
        title_raw = m.get("title", "")
        title = title_raw if isinstance(title_raw, str) else ""
        params_raw = m.get("params", {})
        params = params_raw if isinstance(params_raw, Mapping) else {}
        sections.append(SectionSpec(kind=kind, title=title, params=dict(params)))

    def _s(key: str, default: str) -> str:
        v = data.get(key, default)
        return v if isinstance(v, str) else default

    params_raw = data.get("params", {})
    report_params = dict(params_raw) if isinstance(params_raw, Mapping) else {}
    return ReportSpec(
        id=_s("id", "custom"),
        title=_s("title", "Custom Report"),
        description=_s("description", ""),
        sections=tuple(sections),
        params=report_params,
    )


def spec_to_dict(spec: ReportSpec) -> dict[str, object]:
    """A JSON-safe dict for a :class:`ReportSpec` (the persisted form)."""
    return {
        "id": spec.id,
        "title": spec.title,
        "description": spec.description,
        "params": dict(spec.params),
        "sections": [
            {"kind": s.kind, "title": s.title, "params": dict(s.params)}
            for s in spec.sections
        ],
    }


def spec_from_dict(data: object) -> ReportSpec:
    """Rebuild a :class:`ReportSpec` from its persisted dict (validates kinds)."""
    return build_report(data)


# --- persistence -------------------------------------------------------------
class SavedReportStore(Protocol):
    """Per-tenant persistence for saved custom report definitions."""

    def save(self, tenant_id: str, spec: ReportSpec) -> None: ...
    def get(self, tenant_id: str, report_id: str) -> ReportSpec | None: ...
    def list_for_tenant(self, tenant_id: str) -> list[ReportSpec]: ...


class InMemorySavedReportStore:
    """Saved reports held in memory — for tests and local development."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], ReportSpec] = {}

    def save(self, tenant_id: str, spec: ReportSpec) -> None:
        self._by_key[(tenant_id, spec.id)] = spec

    def get(self, tenant_id: str, report_id: str) -> ReportSpec | None:
        return self._by_key.get((tenant_id, report_id))

    def list_for_tenant(self, tenant_id: str) -> list[ReportSpec]:
        return [
            spec
            for (tid, _rid), spec in sorted(self._by_key.items())
            if tid == tenant_id
        ]


class _DbApiCursor(Protocol):
    def execute(self, sql: str, params: object = ..., /) -> object: ...
    def fetchall(self) -> list[tuple[object, ...]]: ...
    def close(self) -> None: ...


class _DbApiConnection(Protocol):
    def cursor(self) -> _DbApiCursor: ...
    def commit(self) -> None: ...


class SqlSavedReportStore:
    """Saved report specs over any DB-API 2.0 connection; each spec is stored as
    JSON text keyed by ``(tenant_id, report_id)``. ``placeholder`` is ``?``
    (sqlite) or ``%s`` (psycopg)."""

    def __init__(
        self,
        connection: _DbApiConnection,
        *,
        table: str = "rgnr8_saved_report",
        placeholder: str = "?",
    ) -> None:
        self._conn = connection
        self._t = table
        self._ph = placeholder

    def create_schema(self) -> None:
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {self._t} "
                "(tenant_id TEXT NOT NULL, report_id TEXT NOT NULL, spec_json TEXT NOT NULL, "
                "PRIMARY KEY (tenant_id, report_id))"
            )
        finally:
            cur.close()
        self._conn.commit()

    def save(self, tenant_id: str, spec: ReportSpec) -> None:
        p = self._ph
        payload = json.dumps(spec_to_dict(spec), sort_keys=True)
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"INSERT INTO {self._t} (tenant_id, report_id, spec_json) "
                f"VALUES ({p}, {p}, {p}) "
                "ON CONFLICT (tenant_id, report_id) DO UPDATE SET spec_json=excluded.spec_json",
                (tenant_id, spec.id, payload),
            )
        finally:
            cur.close()
        self._conn.commit()

    def _rows(self, sql: str, params: tuple[object, ...]) -> list[tuple[object, ...]]:
        cur = self._conn.cursor()
        try:
            cur.execute(sql, params)
            return cur.fetchall()
        finally:
            cur.close()

    def get(self, tenant_id: str, report_id: str) -> ReportSpec | None:
        p = self._ph
        rows = self._rows(
            f"SELECT spec_json FROM {self._t} WHERE tenant_id={p} AND report_id={p}",
            (tenant_id, report_id),
        )
        if not rows:
            return None
        return spec_from_dict(json.loads(str(rows[0][0])))

    def list_for_tenant(self, tenant_id: str) -> list[ReportSpec]:
        rows = self._rows(
            f"SELECT spec_json FROM {self._t} WHERE tenant_id={self._ph} ORDER BY report_id",
            (tenant_id,),
        )
        return [spec_from_dict(json.loads(str(r[0]))) for r in rows]
