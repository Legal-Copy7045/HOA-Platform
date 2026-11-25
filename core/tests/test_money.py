import pytest

from core.money import format_cents, parse_cents


@pytest.mark.parametrize(
    ("text", "cents"),
    [("125", 12500), ("125.5", 12550), ("$1,234.56", 123456), ("0.07", 7), ("-6.00", -600)],
)
def test_parse(text, cents):
    assert parse_cents(text) == cents


@pytest.mark.parametrize("text", ["", "1.234", "abc", "1e5", "12.3.4", "9999999999"])
def test_parse_rejects(text):
    with pytest.raises(ValueError):
        parse_cents(text)


def test_format():
    assert format_cents(123456) == "$1,234.56"
    assert format_cents(-600) == "-$6.00"
