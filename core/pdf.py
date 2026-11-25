"""A minimal text-only PDF writer for letters (decision letters, notices).

Deliberately tiny (no dependency): US Letter pages, Helvetica, wrapped
paragraphs, automatic page breaks. Text is restricted to printable Latin-1;
anything else is replaced, so user input cannot inject PDF syntax.
"""

from __future__ import annotations

import textwrap

PAGE_W, PAGE_H, MARGIN, LEADING, SIZE = 612, 792, 72, 15, 11
CHARS_PER_LINE = 88


def _escape(text: str) -> str:
    safe = "".join(c if 32 <= ord(c) < 256 and c not in "\x7f" else "?" for c in text)
    return safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _lines(paragraphs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    out = []
    for style, text in paragraphs:
        if not text:
            out.append(("normal", ""))
            continue
        for raw in text.splitlines() or [""]:
            wrapped = textwrap.wrap(raw, CHARS_PER_LINE) or [""]
            out.extend((style, w) for w in wrapped)
    return out


def render(paragraphs: list[tuple[str, str]], *, title: str = "Letter") -> bytes:
    """paragraphs: [("heading"|"normal"|"small", text), ...] -> PDF bytes."""
    per_page = (PAGE_H - 2 * MARGIN) // LEADING
    lines = _lines(paragraphs)
    pages = [lines[i : i + per_page] for i in range(0, len(lines), per_page)] or [[]]
    objects: list[bytes] = []

    def add(obj: bytes) -> int:
        objects.append(obj)
        return len(objects)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    bold = add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"
    )
    pages_id = len(objects) + 1 + 2 * len(pages)  # placeholder index after content+page objs
    page_ids = []
    for page_lines in pages:
        ops = ["BT", f"{MARGIN} {PAGE_H - MARGIN} Td"]
        for style, text in page_lines:
            f, size = (
                ("/F2", SIZE + 3)
                if style == "heading"
                else (("/F1", SIZE - 2) if style == "small" else ("/F1", SIZE))
            )
            ops += [f"{f} {size} Tf", f"({_escape(text)}) Tj", f"0 -{LEADING} Td"]
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1")
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        page_ids.append(
            add(
                (
                    f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
                    f"/Resources << /Font << /F1 {font} 0 R /F2 {bold} 0 R >> >> "
                    f"/Contents {content} 0 R >>"
                ).encode()
            )
        )
    kids = " ".join(f"{p} 0 R" for p in page_ids)
    if add(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode()) != pages_id:
        raise RuntimeError("PDF object numbering error")
    catalog = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode())
    info = add(f"<< /Title ({_escape(title)}) /Producer (HOA portal) >>".encode())

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R /Info {info} 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)
