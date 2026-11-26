"""USPS Intelligent Mail barcode encoder (core/imb.py, D55)."""

import pytest

from core import imb

TRACKING = "01234567094987654321"

# The worked examples published in USPS-B-3200 (Appendix C).
SPEC_VECTORS = {
    "": "ATTFATTDTTADTAATTDTDTATTDAFDDFADFDFTFFFFFTATFAAAATDFFTDAADFTFDTDT",
    "01234": "DTTAFADDTTFTDTFTFDTDDADADAFADFATDDFTAAAFDTTADFAAATDFDTDFADDDTDFFT",
    "012345678": "ADFTTAFDTTTTFATTADTAAATFTFTATDAAAFDDADATATDTDTTDFDTDATADADTDFFTFA",
    "01234567891": "AADTFFDFTDADTAADAATFDTDDAAADDTDTTDAFADADDDTFFFDDTTTADFAAADFTDAADA",
}


@pytest.mark.parametrize("routing", sorted(SPEC_VECTORS))
def test_published_usps_examples(routing):
    assert imb.encode(TRACKING, routing) == SPEC_VECTORS[routing]


def test_character_tables_match_the_specification():
    assert len(imb._TABLE_5) == 1287 and len(set(imb._TABLE_5)) == 1287
    assert len(imb._TABLE_2) == 78 and len(set(imb._TABLE_2)) == 78
    assert all(bin(c).count("1") == 5 for c in imb._TABLE_5)
    # Every character bit drives exactly one bar half.
    pairs = [p for d0, d1, a0, a1 in imb._BARS for p in ((d0, d1), (a0, a1))]
    assert len(pairs) == 130 and len(set(pairs)) == 130


@pytest.mark.parametrize(
    ("tracking", "routing"),
    [
        ("0123456709498765432", ""),  # 19 digits
        ("05234567094987654321", ""),  # second digit above 4
        ("0123456709498765432x", ""),
        (TRACKING, "1234"),
        (TRACKING, "12345678a"),
        (TRACKING, "١٢٣٤٥"),  # non-ASCII digits
    ],
)
def test_bad_codes_are_refused(tracking, routing):
    with pytest.raises(imb.ImbError):
        imb.encode(tracking, routing)


def test_tracking_code_serial_width_follows_the_mailer_id():
    assert imb.tracking_code("00", "040", "123456", 7) == "00040123456000000007"
    assert imb.tracking_code("00", "040", "912345678", 7) == "00040912345678000007"
    with pytest.raises(imb.ImbError):
        imb.tracking_code("00", "040", "123456", 10**9)
    with pytest.raises(imb.ImbError):
        imb.tracking_code("00", "040", "912345678", 10**6)
    with pytest.raises(imb.ImbError):
        imb.tracking_code("00", "040", "12345", 1)


def test_routing_from_the_last_address_line():
    assert imb.routing_from_address(["1 Main St", "Town, PA 19380"]) == "19380"
    assert imb.routing_from_address(["1 Main St", "Town, PA 19380-1234"]) == "193801234"
    assert imb.routing_from_address(["1 Main St"]) == ""
    assert imb.routing_from_address([]) == ""
