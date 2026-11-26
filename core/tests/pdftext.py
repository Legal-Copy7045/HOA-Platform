"""Read the text back out of a PDF made by core.pdf (tests only)."""

import re


def pdf_text(data: bytes) -> str:
    """All text runs, one line per BT..ET block, whitespace collapsed, with
    the PDF string escapes undone, e.g. "165 Orchard Way (Lot 30)"."""
    raw = data.decode("latin-1")
    lines = []
    for block in re.findall(r"BT\n(.*?)\nET", raw, flags=re.S):
        runs = re.findall(r"\(((?:\\.|[^\\)])*)\) Tj", block)
        lines.append("".join(re.sub(r"\\(.)", r"\1", r) for r in runs))
    return " ".join(" ".join(lines).split())
