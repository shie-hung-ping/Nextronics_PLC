import pytest

from nextronics_plc_ver5.config import FieldDef
from nextronics_plc_ver5.decoding import (
    compute_read_blocks, decode_ascii, decode_bcd, decode_number, field_range_text, slice_field,
)
from nextronics_plc_ver5.plc.devices import parse_device


def test_parse_device_bases():
    assert parse_device("d100").number == 100
    assert parse_device("W1A").number == 0x1A
    assert parse_device("X17").number == 0o17
    assert parse_device("M100").is_bit
    with pytest.raises(ValueError):
        parse_device("Q10")


def test_number_int16_signed_and_decimals():
    assert decode_number([0xFFFF], "int16", 0) == "-1"
    assert decode_number([251], "int16", 1) == "25.1"
    assert decode_number([0xFFFB], "int16", 2) == "-0.05"


def test_number_int32_low_word_first():
    v = -100000
    assert decode_number([v & 0xFFFF, (v >> 16) & 0xFFFF], "int32", 0) == "-100000"
    assert decode_number([0x0000, 0x0001], "int32", 0) == "65536"


def test_ascii_low_byte_first_and_null_terminated():
    words = [ord("A") | ord("b") << 8, ord("1") | 0 << 8, ord("Z") | ord("Z") << 8]
    assert decode_ascii(words, 8, True, True, True) == "Ab1"


def test_ascii_allowed_chars_and_length_limit():
    words = [ord("A") | ord("-") << 8, ord("7") | ord("x") << 8]
    assert decode_ascii(words, 8, True, False, False) == "A??x"
    assert decode_ascii(words, 3, True, True, True) == "A-7"


def test_bcd_low_register_holds_low_digits():
    assert decode_bcd([0x5678, 0x1234], 8) == "12345678"
    assert decode_bcd([0x0042], 3) == "042"
    assert decode_bcd([0x12A4], 4) == "12?4"


def test_register_counts():
    assert FieldDef("a", kind="ascii", chars=5).register_count() == 3
    assert FieldDef("b", kind="bcd", digits=5).register_count() == 2
    assert FieldDef("c", int_type="int32").register_count() == 2


def test_read_blocks_cover_all_fields_per_device():
    fields = [
        FieldDef("a", "D110", kind="ascii", chars=8),   # D110-113
        FieldDef("b", "D100"),
        FieldDef("c", "W10", int_type="int32"),         # W10-W11 (hex)
    ]
    blocks = compute_read_blocks(fields)
    d = next(b for b in blocks if b.head.name == "D")
    w = next(b for b in blocks if b.head.name == "W")
    assert (d.head.number, d.count) == (100, 14)
    assert (w.head.number, w.count) == (0x10, 2)
    data = {d: list(range(100, 114)), w: [7, 0]}
    assert slice_field(fields[0], data) == [110, 111, 112, 113]
    assert slice_field(fields[2], data) == [7, 0]


def test_bit_device_rejected_for_field():
    with pytest.raises(ValueError):
        compute_read_blocks([FieldDef("x", "M0")])
    assert "D100–D101" == field_range_text(FieldDef("y", "D100", int_type="int32"))
