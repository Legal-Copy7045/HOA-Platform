"""QR codes for printed documents (D43): the modules only, as a matrix of
booleans for ``core.pdf.QrBlock``. Encoding is segno's (pure Python, no
dependencies); drawing stays in core/pdf.py."""

from __future__ import annotations

import segno


def matrix(text: str) -> tuple[tuple[bool, ...], ...]:
    """The QR symbol for ``text`` (a full QR code, never a Micro QR; error
    correction at least M, raised when it fits in the same size), without
    its quiet zone: True = dark module."""
    code = segno.make(text, error="m", micro=False)
    return tuple(tuple(bool(v) for v in row) for row in code.matrix)
