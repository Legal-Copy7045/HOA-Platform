"""A minimal text-only PDF writer for letters (decision letters, notices).

Deliberately tiny (no dependency): US Letter pages, Helvetica, wrapped
paragraphs, automatic page breaks. Text is restricted to printable Latin-1;
anything else is replaced, so user input cannot inject PDF syntax.

Besides text, a few drawn shapes for statements (D43): thin rules, a dashed
cut line, a box around a run of lines, and a QR code. Shapes take no text
from the caller except the QR code's caption, which is escaped like any
other text; the QR code itself is a validated matrix of booleans
(``QrBlock``), drawn as filled squares, so nothing a person typed can reach
the drawing operators.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass

PAGE_W, PAGE_H, MARGIN, LEADING, SIZE = 612, 792, 72, 15, 11
CHARS_PER_LINE = 88
MONO_CHARS = 78  # Courier at 9pt across the text width
_FOOT = 18  # the lowest baseline sits this far above the bottom margin

# Text styles: font resource, size and line height (baseline to baseline).
_TEXT = {
    "heading": ("/F2", SIZE + 3, LEADING),
    "normal": ("/F1", SIZE, LEADING),
    "small": ("/F1", SIZE - 2, LEADING),
    "mono": ("/F3", SIZE - 2, LEADING),
    # Compact styles for statements (D43).
    "title": ("/F2", 17, 24),
    "subheading": ("/F2", 11.5, 22),
    "big": ("/F2", 14, 21),
    "note": ("/F1", 8.5, 11.5),
    "row": ("/F3", 8.5, 11),
    "rowhead": ("/F4", 8.5, 13),
}
_BOLD_MARKERS = {"normal", "small", "note"}  # **bold** spans allowed
_FIXED = {"mono", "row", "rowhead"}  # never re-wrapped, cut at the page edge
# Shapes: line height only (they carry no text).
_SHAPES = {"gap": 6, "rule": 10, "cut": 16, "box_start": 10, "box_end": 8, "bottom": 0}
_CAPTION_SIZE, _CAPTION_LEADING = 9.5, 12.5


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


@dataclass(frozen=True)
class QrBlock:
    """A QR code drawn as filled squares, with caption lines to its right.

    ``modules`` is the symbol without its quiet zone: a square tuple of
    tuples of real booleans (True = dark), from a QR encoder (core/qr.py).
    Nothing else is accepted, so the drawing is numbers only. The caption is
    ordinary text (escaped; ``**bold**`` allowed, so pass user text through
    ``plain``)."""

    modules: tuple
    caption: tuple = ()
    module_pt: float = 2.0

    def __post_init__(self):
        n = len(self.modules) if isinstance(self.modules, tuple) else 0
        if not 21 <= n <= 177 or (n - 17) % 4:
            raise ValueError("A QR code is a square of 21 to 177 modules.")
        for row in self.modules:
            if not isinstance(row, tuple) or len(row) != n:
                raise ValueError("A QR code is a square of modules.")
            if any(type(v) is not bool for v in row):
                raise ValueError("QR modules must be booleans.")
        if not isinstance(self.caption, tuple) or any(not isinstance(c, str) for c in self.caption):
            raise ValueError("A QR caption is a tuple of strings.")
        if type(self.module_pt) not in (int, float) or not 1 <= self.module_pt <= 4:
            raise ValueError("Module size must be 1 to 4 points.")

    @property
    def side(self) -> float:
        """Width and height in points, with the 4-module quiet zone."""
        return (len(self.modules) + 8) * self.module_pt


def _wrap_chars(style: str, width: float) -> int:
    if style == "mono":
        return int(MONO_CHARS * width / (PAGE_W - 2 * MARGIN))
    if style in ("normal", "small", "heading"):
        return int(CHARS_PER_LINE * width / (PAGE_W - 2 * MARGIN))
    _font, size, _lead = _TEXT[style]
    if style in _FIXED:
        return int(width / (size * 0.6))  # Courier
    return int(width / (size * (0.56 if style in ("title", "subheading", "big") else 0.47)))


def _wrap_bold(raw: str, chars: int) -> list[str]:
    out = []
    bold_open = False
    for w in textwrap.wrap(raw, chars) or [""]:
        # Keep **bold** spans balanced across wrapped lines.
        line = ("**" if bold_open else "") + w
        if line.count("**") % 2:
            line += "**"
            bold_open = True
        else:
            bold_open = False
        out.append(line)
    return out


def _lines(paragraphs: list, width: float = PAGE_W - 2 * MARGIN) -> list[tuple[str, object]]:
    out: list[tuple[str, object]] = []
    for style, text in paragraphs:
        if style == "qr":
            if not isinstance(text, QrBlock):
                raise TypeError("A qr paragraph takes a QrBlock.")
            out.append((style, text))
            continue
        if text is None:
            text = ""
        if not isinstance(text, str):
            raise TypeError("Paragraph text must be a string.")
        if style == "pagebreak" or style in _SHAPES:
            out.append((style, ""))
            continue
        if style not in _TEXT:
            raise ValueError(f"Unknown paragraph style {style!r}.")
        if not text:
            out.append(("normal" if style in ("normal", "heading", "small", "mono") else style, ""))
            continue
        chars = _wrap_chars(style, width)
        for raw in text.splitlines() or [""]:
            if style in _FIXED:  # tables: never re-wrapped, cut at the page edge
                out.append((style, raw[:chars]))
            elif style in _BOLD_MARKERS:
                out += [(style, line) for line in _wrap_bold(raw, chars)]
            else:
                out += [(style, w) for w in textwrap.wrap(raw, chars) or [""]]
    return out


def _segments(text: str):
    """Split on ** markers: [(bold?, text), ...]."""
    parts = text.split("**")
    return [(i % 2 == 1, part) for i, part in enumerate(parts) if part]


def _caption(block: QrBlock, width: float) -> list[str]:
    chars = int((width - block.side - 12) / (_CAPTION_SIZE * 0.47))
    out = []
    for line in block.caption:
        out += _wrap_bold(line, chars) if line else [""]
    return out


def _height(style: str, payload, width: float) -> float:
    if style == "qr":
        lines = len(_caption(payload, width))
        return max(payload.side, lines * _CAPTION_LEADING + 4) + 8
    if style in _SHAPES:
        return _SHAPES[style]
    return _TEXT[style][2]


def _num(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".")


def _text_ops(style: str, text: str, x: float, y: float) -> list[str]:
    font, size, _lead = _TEXT[style]
    ops = ["BT", f"{_num(x)} {_num(y)} Td"]
    if style in _BOLD_MARKERS:
        # Each segment in its own font; Tj advances along the line.
        for is_bold, part in _segments(text) or [(False, "")]:
            ops += [f"{'/F2' if is_bold else '/F1'} {_num(size)} Tf", f"({_escape(part)}) Tj"]
    else:
        ops += [f"{font} {_num(size)} Tf", f"({_escape(text)}) Tj"]
    ops.append("ET")
    return ops


def _qr_ops(block: QrBlock, x: float, top: float, width: float) -> list[str]:
    m, q = block.module_pt, 4
    ops = ["q", "0 0 0 rg"]
    for r, row in enumerate(block.modules):
        y = top - (q + r + 1) * m
        c = 0
        while c < len(row):  # one rectangle per run of dark modules
            if not row[c]:
                c += 1
                continue
            start = c
            while c < len(row) and row[c]:
                c += 1
            ops.append(
                f"{_num(x + (q + start) * m)} {_num(y)} {_num((c - start) * m)} {_num(m)} re"
            )
    ops += ["f", "Q"]
    cx = x + block.side + 12
    for i, line in enumerate(_caption(block, width)):
        y = top - 11 - i * _CAPTION_LEADING
        ops += ["BT", f"{_num(cx)} {_num(y)} Td"]
        for is_bold, part in _segments(line) or [(False, "")]:
            ops += [f"{'/F2' if is_bold else '/F1'} {_CAPTION_SIZE} Tf", f"({_escape(part)}) Tj"]
        ops.append("ET")
    return ops


def _layout(items: list, margin: float) -> list[list[tuple[str, object, float]]]:
    """Place each line: [[(style, payload, baseline y), ...] per page]."""
    width = PAGE_W - 2 * margin
    start, floor = PAGE_H - margin + LEADING, margin + _FOOT
    heights = [_height(s, p, width) if s != "pagebreak" else 0 for s, p in items]
    pages: list[list] = [[]]
    y = start
    for i, (style, payload) in enumerate(items):
        if style == "pagebreak":
            if pages[-1]:
                pages.append([])
                y = start
            continue
        if style == "bottom":
            # What follows, up to the next page break, ends at the foot of
            # the page (a tear-off slip).
            breaks = [j for j in range(i + 1, len(items)) if items[j][0] == "pagebreak"]
            rest = sum(heights[i + 1 : breaks[0] if breaks else len(items)])
            if y - rest < floor and pages[-1]:
                pages.append([])
                y = start
            y = min(y, floor + rest)
            continue
        h = heights[i]
        if y - h < floor and pages[-1]:
            pages.append([])
            y = start
        y -= h
        pages[-1].append((style, payload, y))
    return pages


def _page_ops(lines: list, margin: float, open_box: float | None):
    """Content stream operators for one page; returns (ops, box still open
    at the page end: its top y on the next page, else None)."""
    width = PAGE_W - 2 * margin
    background: list[str] = []
    ops: list[str] = []

    def box(top: float, bottom: float) -> None:
        x, w = _num(margin - 8), _num(width + 16)
        rect = f"{x} {_num(bottom)} {w} {_num(top - bottom)} re"
        background.extend(["q", "0.95 g", "0.6 G", "0.75 w", rect, "B", "Q"])

    box_top = open_box
    for style, payload, y in lines:
        if style == "box_start":
            box_top = y
        elif style == "box_end":
            if box_top is not None:
                box(box_top, y)
            box_top = None
        elif style == "rule":
            ops += ["q", "0.5 G", "0.5 w", f"{_num(margin)} {_num(y + 4)} m"]
            ops += [f"{_num(margin + width)} {_num(y + 4)} l", "S", "Q"]
        elif style == "cut":
            ops += ["q", "0.4 G", "0.75 w", "[4 3] 0 d", f"{_num(margin - 8)} {_num(y + 5)} m"]
            ops += [f"{_num(margin + width + 8)} {_num(y + 5)} l", "S", "Q"]
        elif style == "qr":
            ops += _qr_ops(payload, margin, y + _height(style, payload, width) - 3, width)
        elif style == "gap":
            continue
        elif payload or style in _TEXT:
            ops += _text_ops(style, payload, margin, y)
    if box_top is not None and lines:
        box(box_top, margin + _FOOT - 6)
        return background + ops, PAGE_H - margin + 6
    return background + ops, None


def page_count(paragraphs: list, *, margin: float = MARGIN) -> int:
    """How many pages ``paragraphs`` take (to choose a layout before rendering)."""
    return len(_layout(_lines(paragraphs, PAGE_W - 2 * margin), margin))


def render(paragraphs: list, *, title: str = "Letter", margin: float = MARGIN) -> bytes:
    """paragraphs: [(style, text), ...] -> PDF bytes.

    Styles: "heading", "normal", "small", "mono" (fixed-width, for tables and
    stubs) and "pagebreak". In normal and small text, **double asterisks**
    mark bold spans (used to show data filled in from the portal).

    Statement styles (D43): "title", "subheading", "big" (bold), "note"
    (small text, **bold** allowed), "row" / "rowhead" (compact fixed-width
    table rows); shapes "gap", "rule", "cut" (dashed tear-off line),
    "box_start" / "box_end" (a shaded box around the lines between),
    "bottom" (what follows ends at the foot of the page) and "qr" with a
    ``QrBlock`` instead of text."""
    pages = _layout(_lines(paragraphs, PAGE_W - 2 * margin), margin)
    streams = []
    open_box = None
    for page_lines in pages:
        ops, open_box = _page_ops(page_lines, margin, open_box)
        streams.append(ops)
    return _document(streams, PAGE_W, PAGE_H, title)


def _document(streams: list[list[str]], width: float, height: float, title: str) -> bytes:
    """Assemble pages (each a list of content-stream operators) into a PDF."""
    objects: list[bytes] = []

    def add(obj: bytes) -> int:
        objects.append(obj)
        return len(objects)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    bold = add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"
    )
    mono = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>")
    mono_bold = add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier-Bold /Encoding /WinAnsiEncoding >>"
    )
    pages_id = len(objects) + 1 + 2 * len(streams)  # placeholder index after content+page objs
    page_ids = []
    box = f"[0 0 {_num(width)} {_num(height)}]"
    for ops in streams:
        stream = "\n".join(ops).encode("latin-1")
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        page_ids.append(
            add(
                (
                    f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox {box} "
                    f"/Resources << /Font << /F1 {font} 0 R /F2 {bold} 0 R /F3 {mono} 0 R "
                    f"/F4 {mono_bold} 0 R >> >> /Contents {content} 0 R >>"
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


# --- Absolute positions (paper mail, D55) -----------------------------------------------
#
# Letters for window envelopes, #10 envelopes and address labels need text at
# exact places on the sheet. ``render_placed`` takes pages of ``Text`` and
# ``Line``/``Rect`` items with coordinates in points from the bottom-left
# corner. Text goes through the same Latin-1 escaping as everything else;
# coordinates and sizes must be real, finite numbers inside the page, so
# nothing a person typed can reach the drawing operators.

INCH = 72

# Advance widths (thousandths of the font size) of Helvetica and
# Helvetica-Bold for printable ASCII, from the standard Adobe metrics, so
# lines can be wrapped, centred and cut to fit a window, envelope or label.
_HELVETICA = (
    "278 278 355 556 556 889 667 191 333 333 389 584 278 333 278 278 "
    "556 556 556 556 556 556 556 556 556 556 278 278 584 584 584 556 "
    "1015 667 667 722 722 667 611 778 722 278 500 667 556 833 722 778 "
    "667 778 722 667 611 722 667 944 667 667 611 278 278 278 469 556 "
    "333 556 556 500 556 556 278 556 556 222 222 500 222 833 556 556 "
    "556 556 333 500 278 556 500 722 500 500 500 334 260 334 584"
)
_HELVETICA_BOLD = (
    "278 333 474 556 556 889 722 238 333 333 389 584 278 333 278 278 "
    "556 556 556 556 556 556 556 556 556 556 333 333 584 584 584 611 "
    "975 722 722 722 722 667 611 778 722 278 556 722 611 833 722 778 "
    "667 778 722 667 611 722 667 944 667 667 611 333 278 333 584 556 "
    "333 556 611 556 611 556 333 611 611 278 278 556 278 889 611 611 "
    "611 611 389 556 333 611 556 778 556 556 500 389 280 389 584"
)
_WIDTHS = {
    False: dict(zip(map(chr, range(32, 127)), map(int, _HELVETICA.split()), strict=True)),
    True: dict(zip(map(chr, range(32, 127)), map(int, _HELVETICA_BOLD.split()), strict=True)),
}


def safe_text(text: str) -> str:
    """``text`` as it will print: one line, typographic characters kept where
    the PDF fonts have them, anything else outside Latin-1 replaced by "?"."""
    text = " ".join(str(text or "").split())
    text = "".join(_WINANSI.get(c, c) for c in text)
    return "".join(
        c if (32 <= ord(c) < 127 or 160 <= ord(c) < 256 or c in _WINANSI.values()) else "?"
        for c in text
    )


def text_width(text: str, size: float, bold: bool = False) -> float:
    """Printed width of one line of ``text`` in points."""
    widths = _WIDTHS[bool(bold)]
    return sum(widths.get(c, 556) for c in safe_text(text)) * size / 1000


def fit_text(text: str, size: float, width: float, bold: bool = False) -> str:
    """One line cut to ``width`` points, ending in "..." when cut."""
    text = safe_text(text)
    if text_width(text, size, bold) <= width:
        return text
    while text and text_width(text + "...", size, bold) > width:
        text = text[:-1]
    return text.rstrip() + "..."


def wrap_text(text: str, size: float, width: float, bold: bool = False) -> list[str]:
    """Wrap one paragraph to lines of at most ``width`` points (a word longer
    than the line is cut)."""
    lines: list[str] = []
    current = ""
    for word in safe_text(text).split(" "):
        candidate = f"{current} {word}" if current else word
        if text_width(candidate, size, bold) <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word if text_width(word, size, bold) <= width else fit_text(word, size, width)
    if current or not lines:
        lines.append(current)
    return lines


def _real(value, name: str) -> float:
    import math

    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{name} must be a number.")
    return float(value)


@dataclass(frozen=True)
class Text:
    """One line of text at (x, y): the baseline's left end, or its centre
    with ``align="center"``, or its right end with ``align="right"``."""

    x: float
    y: float
    text: str
    size: float = 11
    bold: bool = False
    align: str = "left"

    def __post_init__(self):
        _real(self.x, "x")
        _real(self.y, "y")
        if not 4 <= _real(self.size, "size") <= 36:
            raise ValueError("Text size must be 4 to 36 points.")
        if not isinstance(self.text, str):
            raise TypeError("Text must be a string.")
        if self.align not in ("left", "center", "right"):
            raise ValueError("Unknown alignment.")


@dataclass(frozen=True)
class Line:
    """A thin line from (x1, y1) to (x2, y2); dashed for guides and fold marks."""

    x1: float
    y1: float
    x2: float
    y2: float
    dashed: bool = False

    def __post_init__(self):
        for name in ("x1", "y1", "x2", "y2"):
            _real(getattr(self, name), name)


@dataclass(frozen=True)
class Rect:
    """An outline (for the alignment test page: where a window will be)."""

    x: float
    y: float
    w: float
    h: float
    dashed: bool = True

    def __post_init__(self):
        for name in ("x", "y", "w", "h"):
            _real(getattr(self, name), name)


# Intelligent Mail barcode dimensions (USPS-B-3200): bar width 0.020 in,
# 22 bars per inch, full bar 0.145 in, tracker 0.048 in.
IMB_BAR_W = 0.020 * INCH
IMB_PITCH = INCH / 22
IMB_FULL = 0.145 * INCH
IMB_TRACKER = 0.048 * INCH
IMB_LENGTH = 64 * IMB_PITCH + IMB_BAR_W  # about 2.93 in


@dataclass(frozen=True)
class Bars:
    """An Intelligent Mail barcode: 65 bars ("F", "A", "D", "T") drawn as
    filled rectangles from (x, y), the bottom-left of the full-bar height."""

    x: float
    y: float
    bars: str

    def __post_init__(self):
        _real(self.x, "x")
        _real(self.y, "y")
        if not isinstance(self.bars, str) or len(self.bars) != 65 or set(self.bars) - set("FADT"):
            raise ValueError("An Intelligent Mail barcode is 65 bars of F, A, D or T.")


def _bars_ops(item: Bars) -> list[str]:
    mid = item.y + IMB_FULL / 2
    t_low, t_high = mid - IMB_TRACKER / 2, mid + IMB_TRACKER / 2
    top, bottom = item.y + IMB_FULL, item.y
    ops = ["q", "0 0 0 rg"]
    for i, bar in enumerate(item.bars):
        low = bottom if bar in "FD" else t_low
        high = top if bar in "FA" else t_high
        x = item.x + i * IMB_PITCH
        ops.append(f"{_num(x)} {_num(low)} {_num(IMB_BAR_W)} {_num(high - low)} re")
    return [*ops, "f", "Q"]


def _placed_ops(items, width: float, height: float) -> list[str]:
    ops: list[str] = []
    for item in items:
        if isinstance(item, Text):
            text = safe_text(item.text)
            if not text:
                continue
            x = item.x
            if item.align != "left":
                w = text_width(text, item.size, item.bold)
                x -= w / 2 if item.align == "center" else w
            if not (-1 <= x <= width + 1 and -1 <= item.y <= height + 1):
                raise ValueError("Text is outside the page.")
            font = "/F2" if item.bold else "/F1"
            ops += ["BT", f"{_num(x)} {_num(item.y)} Td", f"{font} {_num(item.size)} Tf"]
            ops += [f"({_escape(text)}) Tj", "ET"]
        elif isinstance(item, Line):
            dash = ["[3 3] 0 d"] if item.dashed else []
            ops += ["q", "0.5 G", "0.5 w", *dash, f"{_num(item.x1)} {_num(item.y1)} m"]
            ops += [f"{_num(item.x2)} {_num(item.y2)} l", "S", "Q"]
        elif isinstance(item, Rect):
            dash = ["[3 3] 0 d"] if item.dashed else []
            ops += ["q", "0.5 G", "0.5 w", *dash]
            ops += [f"{_num(item.x)} {_num(item.y)} {_num(item.w)} {_num(item.h)} re", "S", "Q"]
        elif isinstance(item, Bars):
            ops += _bars_ops(item)
        else:
            raise TypeError("A placed page holds Text, Line, Rect and Bars items only.")
    return ops


def render_placed(
    pages: list, *, width: float = PAGE_W, height: float = PAGE_H, title: str = "Letter"
) -> bytes:
    """Pages of placed items -> PDF bytes. ``width`` x ``height`` in points
    (US Letter by default; a #10 envelope is 684 x 297). An empty page list
    gives one blank page; an empty page is a blank page (duplex padding)."""
    _real(width, "width")
    _real(height, "height")
    if not (72 <= width <= 1224 and 72 <= height <= 1224):
        raise ValueError("Unsupported page size.")
    streams = [_placed_ops(items, width, height) for items in (pages or [[]])]
    return _document(streams, width, height, title)
