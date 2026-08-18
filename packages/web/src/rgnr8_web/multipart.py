"""Minimal multipart/form-data parsing — enough to upload a receipt.

The app otherwise speaks urlencoded forms and JSON, both of which are text. A
file upload is not text, and the usual ways of avoiding that fact are both bad:
asking the browser to base64 a file in JavaScript excludes anyone with scripting
off and hides failures, and storing whatever survived a lossy decode stores a
corrupted receipt that looks fine in a list.

So the body is carried through the stack as a `str` decoded with
``surrogateescape`` (PEP 383), which is lossless for arbitrary bytes: any byte
that isn't valid UTF-8 becomes a lone surrogate, and re-encoding with the same
handler gives back exactly the bytes that arrived. This module re-encodes and
splits on the boundary.

Deliberately small. It handles what a browser sends for a form with one or two
file inputs, and refuses anything it doesn't understand rather than guessing —
a parser that half-understands a body produces a half-file.
"""

from __future__ import annotations

from dataclasses import dataclass


class MultipartError(ValueError):
    """The body was not something this parser is willing to interpret."""


@dataclass(frozen=True, slots=True)
class UploadedFile:
    field: str
    filename: str
    content_type: str
    content: bytes

    @property
    def size(self) -> int:
        return len(self.content)


def to_bytes(body: str) -> bytes:
    """Recover the exact bytes that arrived, including invalid-UTF-8 ones."""
    return body.encode("utf-8", "surrogateescape")


def boundary_of(content_type: str) -> str:
    """The boundary token, or "" when this isn't a multipart body."""
    parts = [p.strip() for p in content_type.split(";")]
    if not parts or not parts[0].lower().startswith("multipart/form-data"):
        return ""
    for p in parts[1:]:
        if p.lower().startswith("boundary="):
            value = p[len("boundary="):].strip()
            return value[1:-1] if value.startswith('"') and value.endswith('"') else value
    return ""


def _header_value(headers: str, name: str) -> str:
    for line in headers.split("\r\n"):
        key, _, value = line.partition(":")
        if key.strip().lower() == name:
            return value.strip()
    return ""


def _disposition_param(disposition: str, name: str) -> str:
    for part in disposition.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.strip().lower() != name:
            continue
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        return value.replace("\\\\", "\\").replace('\\"', '"')
    return ""


def parse_multipart(
    body: str, content_type: str, *, max_bytes: int = 16 * 1024 * 1024
) -> tuple[dict[str, str], list[UploadedFile]]:
    """Split a multipart body into its text fields and its files.

    Returns ``(fields, files)``. Text fields are decoded as UTF-8 with
    replacement, because a mangled label is survivable; file content is returned
    as raw bytes, because a mangled receipt is not.
    """
    boundary = boundary_of(content_type)
    if not boundary:
        raise MultipartError("not a multipart/form-data body")

    raw = to_bytes(body)
    if len(raw) > max_bytes:
        raise MultipartError(f"the upload is larger than {max_bytes // 1_048_576} MB")

    marker = b"--" + boundary.encode("ascii", "replace")
    segments = raw.split(marker)
    if len(segments) < 2:
        raise MultipartError("the upload was cut short before any part arrived")

    fields: dict[str, str] = {}
    files: list[UploadedFile] = []
    for segment in segments[1:]:
        if segment[:2] == b"--":       # the closing boundary
            break
        segment = segment.lstrip(b"\r\n")
        head, sep, content = segment.partition(b"\r\n\r\n")
        if not sep:
            continue                   # a part with no body is not a part
        content = content[:-2] if content.endswith(b"\r\n") else content

        headers = head.decode("utf-8", "replace")
        disposition = _header_value(headers, "content-disposition")
        name = _disposition_param(disposition, "name")
        if not name:
            continue
        filename = _disposition_param(disposition, "filename")
        if filename:
            if not content:
                continue               # an empty file input the user left alone
            files.append(UploadedFile(
                field=name,
                filename=filename,
                content_type=_header_value(headers, "content-type") or "application/octet-stream",
                content=content,
            ))
        else:
            fields[name] = content.decode("utf-8", "replace")

    return fields, files
