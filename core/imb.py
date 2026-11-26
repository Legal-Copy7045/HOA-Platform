"""USPS Intelligent Mail barcode (IMb) encoder, USPS-B-3200 (D55).

Pure Python, no dependencies. ``encode(tracking, routing)`` returns the 65
bars as a string of "F" (full), "A" (ascender), "D" (descender) and "T"
(tracker). The steps follow the specification: the routing code and the
20-digit tracking code are combined into one 102-bit number, an 11-bit CRC
(frame check sequence) is taken over it, the number is split into ten
codewords, each codeword becomes a 13-bit character (5-of-13 or 2-of-13
tables), the CRC bits invert characters, and the bar table maps character
bits onto descenders and ascenders. Tested against the specification's
published example encodings (core/tests/test_imb.py).

The tracking code is: Barcode ID (2 digits, the second 0-4), Service Type ID
(3 digits), Mailer ID (6 or 9 digits) and serial number (9 or 6 digits).
The routing code is the delivery ZIP: none, 5, 9 or 11 digits.
"""

from __future__ import annotations


class ImbError(ValueError):
    """Tracking or routing code that cannot be encoded."""


def _nof13_table(n: int, length: int) -> list[int]:
    table = [0] * length
    lower, upper = 0, length - 1
    for count in range(8192):
        if bin(count).count("1") != n:
            continue
        reverse = int(f"{count:013b}"[::-1], 2)
        if reverse < count:
            continue
        if count == reverse:
            table[upper] = count
            upper -= 1
        else:
            table[lower] = count
            table[lower + 1] = reverse
            lower += 2
    return table


_TABLE_5 = _nof13_table(5, 1287)
_TABLE_2 = _nof13_table(2, 78)

# Bar n: (descender character, bit, ascender character, bit); characters A-J = 0-9.
_BARS = (
    (7, 2, 4, 3), (1, 10, 0, 0), (9, 12, 2, 8), (5, 5, 6, 11), (8, 9, 3, 1),
    (0, 1, 5, 12), (2, 5, 1, 8), (4, 4, 9, 11), (6, 3, 8, 10), (3, 9, 7, 6),
    (5, 11, 1, 4), (8, 5, 2, 12), (9, 10, 0, 2), (7, 1, 6, 7), (3, 6, 4, 9),
    (0, 3, 8, 6), (6, 4, 2, 7), (1, 1, 9, 9), (7, 10, 5, 2), (4, 0, 3, 8),
    (6, 2, 0, 4), (8, 11, 1, 0), (9, 8, 3, 12), (2, 6, 7, 7), (5, 1, 4, 10),
    (1, 12, 6, 9), (7, 3, 8, 0), (5, 8, 9, 7), (4, 6, 2, 10), (3, 4, 0, 5),
    (8, 4, 5, 7), (7, 11, 1, 9), (6, 0, 9, 6), (0, 6, 4, 8), (2, 1, 3, 2),
    (5, 9, 8, 12), (4, 11, 6, 1), (9, 5, 7, 4), (3, 3, 1, 2), (0, 7, 2, 0),
    (1, 3, 4, 1), (6, 10, 3, 5), (8, 7, 9, 4), (2, 11, 5, 6), (0, 8, 7, 12),
    (4, 2, 8, 1), (5, 10, 3, 0), (9, 3, 0, 9), (6, 5, 2, 4), (7, 8, 1, 7),
    (5, 0, 4, 5), (2, 3, 0, 10), (6, 12, 9, 2), (3, 11, 1, 6), (8, 8, 7, 9),
    (5, 4, 0, 11), (1, 5, 2, 2), (9, 1, 4, 12), (8, 3, 6, 6), (7, 0, 3, 7),
    (4, 7, 7, 5), (0, 12, 1, 11), (2, 9, 9, 0), (6, 8, 5, 3), (3, 10, 8, 2),
)  # fmt: skip


def _crc11(value: int) -> int:
    data = value.to_bytes(13, "big")
    fcs = 0x7FF
    for i, byte in enumerate(data):
        bits = 6 if i == 0 else 8  # the first byte's top 2 bits are not data
        byte <<= 5 if i == 0 else 3  # the next data bit sits at 0x400
        for _ in range(bits):
            if (fcs ^ byte) & 0x400:
                fcs = (fcs << 1) ^ 0xF35
            else:
                fcs <<= 1
            fcs &= 0x7FF
            byte <<= 1
    return fcs


def validate(tracking: str, routing: str = "") -> None:
    if not (isinstance(tracking, str) and len(tracking) == 20 and tracking.isascii()):
        raise ImbError("The tracking code is 20 digits.")
    if not tracking.isdigit() or tracking[1] not in "01234":
        raise ImbError("The tracking code is 20 digits; the second digit is 0 to 4.")
    if not isinstance(routing, str) or len(routing) not in (0, 5, 9, 11):
        raise ImbError("The routing code is empty or 5, 9 or 11 digits.")
    if routing and not (routing.isascii() and routing.isdigit()):
        raise ImbError("The routing code is digits only.")


def encode(tracking: str, routing: str = "") -> str:
    """The 65 bars ("F", "A", "D", "T") for a tracking and routing code."""
    validate(tracking, routing)
    value = {
        0: 0,
        5: int(routing or 0) + 1,
        9: int(routing or 0) + 100000 + 1,
        11: int(routing or 0) + 1000000000 + 100000 + 1,
    }[len(routing)]
    value = value * 10 + int(tracking[0])
    value = value * 5 + int(tracking[1])
    for digit in tracking[2:]:
        value = value * 10 + int(digit)
    fcs = _crc11(value)
    codewords = [0] * 10
    codewords[9] = value % 636
    value //= 636
    for i in range(8, 0, -1):
        codewords[i] = value % 1365
        value //= 1365
    codewords[0] = value
    codewords[9] *= 2
    if fcs & 0x400:
        codewords[0] += 659
    chars = [_TABLE_5[c] if c < 1287 else _TABLE_2[c - 1287] for c in codewords]
    for i in range(10):
        if fcs & (1 << i):
            chars[i] = ~chars[i] & 0x1FFF
    out = []
    for d_char, d_bit, a_char, a_bit in _BARS:
        down = (chars[d_char] >> d_bit) & 1
        up = (chars[a_char] >> a_bit) & 1
        out.append("F" if down and up else "A" if up else "D" if down else "T")
    return "".join(out)


def tracking_code(barcode_id: str, service_type: str, mailer_id: str, serial: int) -> str:
    """Barcode ID + Service Type ID + Mailer ID + serial (9 digits after a
    6-digit Mailer ID, 6 after a 9-digit one)."""
    width = 9 if len(mailer_id) == 6 else 6
    if len(mailer_id) not in (6, 9) or not mailer_id.isdigit():
        raise ImbError("A Mailer ID is 6 or 9 digits.")
    if not 0 <= int(serial) < 10**width:
        raise ImbError("Serial number out of range for this Mailer ID.")
    code = f"{barcode_id}{service_type}{mailer_id}{int(serial):0{width}d}"
    validate(code)
    return code


def routing_from_address(lines: list[str]) -> str:
    """The delivery ZIP from the last address line: 5 or 9 digits (ZIP or
    ZIP+4), or "" when there is none. No address-validation call."""
    import re

    for line in reversed([x for x in lines if x.strip()]):
        m = re.search(r"\b(\d{5})(?:-?(\d{4}))?\s*$", line.strip())
        if m:
            return m.group(1) + (m.group(2) or "")
        break
    return ""
