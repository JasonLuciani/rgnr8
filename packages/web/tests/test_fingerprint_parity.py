"""Cross-language canonicalization parity (Python side).

The financial-package fingerprint crosses the TS<->Python boundary: `@rgnr8/close`
seals a package and this web surface (`rgnr8_web.financial_package`) independently
re-verifies it. That only holds if BOTH sides canonicalize byte-for-byte
identically before hashing.

This pins that contract to a committed golden vector shared with the TypeScript
suite (`packages/close/test/fingerprint_vector.json`, also asserted by
`packages/close/test/fingerprintParity.test.ts`). Both suites check the SAME
`canonical` string and `sha256` over tricky content (nested arrays of objects, a
non-ASCII character, deliberately unsorted keys), so if both are green the two
serializers are byte-identical.
"""

from __future__ import annotations

import hashlib
import json
import os

from rgnr8_web import fingerprint_content
from rgnr8_web.financial_package import canonicalize

_VECTOR_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "close", "test", "fingerprint_vector.json"
)


def _vector() -> dict[str, object]:
    with open(_VECTOR_PATH, encoding="utf-8") as f:
        return json.load(f)  # type: ignore[no-any-return]


def test_python_canonicalization_matches_committed_golden_bytes() -> None:
    v = _vector()
    content = v["content"]
    assert isinstance(content, dict)
    # Byte-identical canonical string shared with the TS serializer.
    assert canonicalize(content) == v["canonical"]


def test_python_fingerprint_reproduces_committed_sha256() -> None:
    v = _vector()
    content = v["content"]
    assert isinstance(content, dict)
    assert fingerprint_content(content) == v["sha256"]


def test_golden_canonical_string_hashes_to_golden_sha256() -> None:
    v = _vector()
    canonical = v["canonical"]
    assert isinstance(canonical, str)
    assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() == v["sha256"]


def test_vector_contains_the_tricky_content_it_claims_to() -> None:
    # Guard the vector itself so a future edit can't quietly drop the hard cases.
    v = _vector()
    canonical = v["canonical"]
    assert isinstance(canonical, str)
    assert "café" in canonical  # non-ASCII survived canonicalization
    assert "☕" in canonical  # the coffee emoji, raw (not \u-escaped)
    # nested arrays of objects are present and key-sorted (a before z)
    assert '[[1,2],[3,{"a":null,"z":true}]]' in canonical
