"""Is the ledger serving the build this web app expects?

The two services deploy separately from one repo. When the ledger's deploy fails,
Render keeps the previous ledger running and the web app goes live on its own,
so the pair runs mismatched and nothing says so. That happened with #24: the old
ledger answered a settings write with 200 and dropped the fields it didn't know.

This module decides, from the ledger's `/ready` answer, whether that is the
situation. Pure: the caller fetches and caches; this only judges.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

# The schema level this web app needs from the ledger: the highest table
# migration in packages/ledger-service/src/migrations.ts. A test reads that file
# and fails when a new migration lands without this being raised to match.
EXPECTED_LEDGER_SCHEMA = 26


@dataclass(frozen=True, slots=True)
class LedgerBuild:
    """What the ledger said about itself; None for anything it didn't say."""

    reachable: bool
    version: str | None
    schema: int | None

    @classmethod
    def from_ready(cls, status: int, body: Mapping[str, object]) -> LedgerBuild:
        version = body.get("version")
        schema = body.get("schema")
        return cls(
            # Every ledger build, old or new, answers /ready with 200; a 503 from
            # one whose database is down still names its build. Anything else
            # (a transport failure, a stand-in without the route) says nothing
            # about which build is serving, so it isn't judged.
            reachable=status == 200 or "version" in body,
            version=version if isinstance(version, str) and version else None,
            schema=schema if isinstance(schema, int) and not isinstance(schema, bool) else None,
        )

    def as_json(self) -> dict[str, object]:
        return {"reachable": self.reachable, "version": self.version, "schema": self.schema}


def ledger_skew(own_version: str, ledger: LedgerBuild) -> str | None:
    """A plain sentence when the ledger isn't the build this app expects, else None.

    An unreachable ledger is not skew: every screen already says when the books
    can't be read. Two "dev" or unknown commits aren't compared, so local runs
    and tests stay quiet.
    """
    if not ledger.reachable:
        return None
    if ledger.schema is None or ledger.schema < EXPECTED_LEDGER_SCHEMA:
        # The schema numbers stay on /ready; on an owner's screen they're noise.
        return (
            "The ledger service is running an older build than this app, so its"
            " last deploy may have failed. Some changes may not save until it is"
            " redeployed."
        )
    own = own_version if own_version and own_version != "dev" else None
    theirs = ledger.version if ledger.version != "dev" else None
    if own and theirs and own != theirs:
        return (
            f"The ledger service is on build {theirs[:7]} and this app is on {own[:7]}."
            " One of them may still be deploying, or its deploy failed."
        )
    return None
