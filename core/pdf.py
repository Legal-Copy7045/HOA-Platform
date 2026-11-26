"""A minimal text-only PDF writer for letters (decision letters, notices).

Deliberately tiny (no dependency): US Letter pages, Helvetica, wrapped
paragraphs, automatic page breaks. Text is restricted to printable Latin-1;
anything else is replaced, so user input cannot inject PDF syntax.
"""

from __future__ import annotations

import textwrap

PAGE_W, PAGE_H, MARGIN, LEADING, SIZE = 612, 792, 72, 15, 11
CHARS_PER_LINE = 88
MONO_CHARS = 78  # Courier at 9pt across the text width


# Typographic characters outside Latin-1 that WinAnsiEncoding can still show.
_WINANSI = {"\u2013": "\x96", "\u2014": "\x97", "\u2018": "\x91", "\u2019": "\x92"}
_WINANSI |= {"\u201c": "\x93", "\u201d": "\x94", "\u2022": "\x95", "\u2026": "\x85"}


def _escape(text: str) -> str:
    text = "".join(_WINANSI.get(c, c) for c in text)
    safe = "".join(
        c if (32 <= ord(c) < 127 or 160 <= ord(c) < 256 or c in _WINANSI.values()) else "?"
        for c in text
    )
    return safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def plain(text: str) -> str:
    """User-typed text for a paragraph: ``**`` never switches on bold (only
    the portal marks data that way). Escaping happens in render() anyway."""
    text = text or ""
    while "**" in text:
        text = text.replace("**", "* *")
    return text


def _lines(paragraphs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    out = []
    for style, text in paragraphs:
        if style == "pagebreak":
            out.append(("pagebreak", ""))
            continue
        if not text:
            out.append(("normal", ""))
            continue
        for raw in text.splitlines() or [""]:
            if style == "mono":  # tables: never re-wrapped, cut at the page edge
                out.append((style, raw[:MONO_CHARS]))
                continue
            wrapped = textwrap.wrap(raw, CHARS_PER_LINE) or [""]
            bold_open = False
            for w in wrapped:
                # Keep **bold** spans balanced across wrapped lines.
                line = ("**" if bold_open else "") + w
                if line.count("**") % 2:
                    line += "**"
                    bold_open = True
                else:
                    bold_open = False
                out.append((style, line))
    return out


def _segments(text: str):
    """Split on ** markers: [(bold?, text), ...]."""
    parts = text.split("**")
    return [(i % 2 == 1, part) for i, part in enumerate(parts) if part]


def render(paragraphs: list[tuple[str, str]], *, title: str = "Letter") -> bytes:
    """paragraphs: [(style, text), ...] -> PDF bytes.

    Styles: "heading", "normal", "small", "mono" (fixed-width, for tables and
    stubs) and "pagebreak". In normal and small text, **double asterisks**
    mark bold spans (used to show data filled in from the portal)."""
    per_page = (PAGE_H - 2 * MARGIN) // LEADING
    pages: list[list[tuple[str, str]]] = [[]]
    for line in _lines(paragraphs):
        if line[0] == "pagebreak":
            if pages[-1]:
                pages.append([])
            continue
        if len(pages[-1]) >= per_page:
            pages.append([])
        pages[-1].append(line)
    objects: list[bytes] = []

    def add(obj: bytes) -> int:
        objects.append(obj)
        return len(objects)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    bold = add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"
    )
    mono = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>")
    pages_id = len(objects) + 1 + 2 * len(pages)  # placeholder index after content+page objs
    page_ids = []
    for page_lines in pages:
        ops = []
        for i, (style, text) in enumerate(page_lines):
            # Each line starts at the left margin at its own height.
            ops += ["BT", f"{MARGIN} {PAGE_H - MARGIN - i * LEADING} Td"]
            if style == "heading":
                ops += [f"/F2 {SIZE + 3} Tf", f"({_escape(text)}) Tj"]
            elif style == "mono":
                ops += [f"/F3 {SIZE - 2} Tf", f"({_escape(text)}) Tj"]
            else:
                size = SIZE - 2 if style == "small" else SIZE
                # Each segment in its own font; Tj advances along the line.
                for is_bold, part in _segments(text) or [(False, "")]:
                    ops += [f"{'/F2' if is_bold else '/F1'} {size} Tf", f"({_escape(part)}) Tj"]
            ops.append("ET")
        stream = "\n".join(ops).encode("latin-1")
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        page_ids.append(
            add(
                (
                    f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
                    f"/Resources << /Font << /F1 {font} 0 R /F2 {bold} 0 R /F3 {mono} 0 R >> >> "
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
