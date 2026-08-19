"""The money/rate/quantity parsers accept ASCII digits only.

Python's ``str.isdigit()`` is True for Arabic-Indic, Devanagari and superscript
digits, and ``int()`` will happily parse some of them — so a field that a naive
"is it blank?" filter would wave through could still smuggle a number into the
books. These are exact-money paths; they take 0-9 and nothing else.
"""

import pytest
from rgnr8_web import app as m

# One class on the module owns the amount parser as a staticmethod; find it once.
_AMOUNT = next(
    obj._amount_to_minor
    for name in dir(m)
    if isinstance(obj := getattr(m, name), type) and hasattr(obj, "_amount_to_minor")
)


def test_ascii_numbers_still_parse_exactly():
    assert _AMOUNT("1,234.56") == 123456
    assert _AMOUNT("-0.05") == -5
    assert _AMOUNT("") is None
    assert m._percent_to_ppm("8.25") == 82500
    assert m._quantity_to_milli("2.5") == 2500


@pytest.mark.parametrize("bad", ["٥", "1٥.00", "1²", "५", "1.٠"])
def test_unicode_digits_are_rejected_not_silently_parsed(bad):
    with pytest.raises(ValueError):
        _AMOUNT(bad)
    with pytest.raises(ValueError):
        m._percent_to_ppm(bad)
    with pytest.raises(ValueError):
        m._quantity_to_milli(bad)


def test_a_plain_python_int_would_have_accepted_the_arabic_digit():
    # Guards the premise: without the ASCII gate, int("٥") == 5 sails through.
    assert int("٥") == 5
