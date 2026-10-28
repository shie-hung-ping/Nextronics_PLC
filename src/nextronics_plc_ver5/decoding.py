"""顯示欄位解碼與讀取範圍計算。

- 讀取範圍：由所有欄位用到的暫存器，依元件類型（D/W/R）各自算出最小～最大連續區段。
- 解碼：純數字（int16/int32 + 小數位）、ASCII、BCD。三菱慣例：int32 / 多暫存器
  BCD 皆為「低位暫存器放低位」，ASCII 每暫存器先低 byte 後高 byte。
"""

from __future__ import annotations

import string
from dataclasses import dataclass

from .config import FieldDef
from .plc.devices import Device, format_device, parse_device

_SYMBOLS = set(string.punctuation + " ")


@dataclass(frozen=True)
class ReadBlock:
    head: Device
    count: int

    def __str__(self) -> str:
        last = format_device(self.head.name, self.head.number + self.count - 1)
        return f"{self.head}–{last}（{self.count} 點）"


def field_registers(f: FieldDef) -> tuple[Device, int]:
    d = parse_device(f.start)
    if d.is_bit:
        raise ValueError(f"欄位「{f.name}」必須使用字元件（D/W/R），而非 {d.name}")
    return d, f.register_count()


def field_range_text(f: FieldDef) -> str:
    try:
        d, n = field_registers(f)
    except ValueError as e:
        return str(e)
    return str(d) if n == 1 else f"{d}–{format_device(d.name, d.number + n - 1)}"


def compute_read_blocks(fields: list[FieldDef]) -> list[ReadBlock]:
    """每種元件一個區段，涵蓋所有欄位使用的暫存器。"""
    spans: dict[str, tuple[Device, int]] = {}
    for f in fields:
        d, n = field_registers(f)
        lo, hi = d.number, d.number + n
        if d.name in spans:
            head, end = spans[d.name]
            lo, hi = min(lo, head.number), max(hi, end)
        spans[d.name] = (Device(d.name, lo, d.code, False), hi)
    return [ReadBlock(head, end - head.number) for head, end in spans.values()]


def slice_field(f: FieldDef, blocks: dict[ReadBlock, list[int]]) -> list[int]:
    d, n = field_registers(f)
    for b, words in blocks.items():
        if b.head.name == d.name and b.head.number <= d.number and d.number + n <= b.head.number + b.count:
            i = d.number - b.head.number
            return words[i:i + n]
    raise KeyError(f"欄位「{f.name}」不在讀取範圍內")


def decode(f: FieldDef, words: list[int]) -> str:
    if f.kind == "number":
        return decode_number(words, f.int_type, f.decimals)
    if f.kind == "ascii":
        return decode_ascii(words, f.chars, f.allow_letters, f.allow_digits, f.allow_symbols)
    if f.kind == "bcd":
        return decode_bcd(words, f.digits)
    raise ValueError(f"未知欄位類型：{f.kind}")


def decode_number(words: list[int], int_type: str, decimals: int) -> str:
    if int_type == "int32":
        v = (words[0] & 0xFFFF) | ((words[1] & 0xFFFF) << 16)
        if v & 0x8000_0000:
            v -= 1 << 32
    else:
        v = words[0] & 0xFFFF
        if v & 0x8000:
            v -= 1 << 16
    if decimals <= 0:
        return str(v)
    # 以整數運算避免浮點誤差，例如 251, n=1 → "25.1"
    sign = "-" if v < 0 else ""
    s = str(abs(v)).rjust(decimals + 1, "0")
    return f"{sign}{s[:-decimals]}.{s[-decimals:]}"


def decode_ascii(words: list[int], chars: int, letters: bool, digits: bool, symbols: bool) -> str:
    """讀到 0x00 結束；不在允許範圍內的字元以 '?' 顯示。"""
    out = []
    for w in words:
        for b in (w & 0xFF, (w >> 8) & 0xFF):
            if b == 0 or len(out) >= chars:
                return "".join(out)
            c = chr(b)
            ok = (letters and c.isascii() and c.isalpha()) or (digits and c.isdigit()) or (symbols and c in _SYMBOLS)
            out.append(c if ok else "?")
    return "".join(out)


def decode_bcd(words: list[int], digits: int) -> str:
    """每暫存器 4 位數；低位暫存器放低 4 位數。非 0–9 的半位元組以 '?' 顯示。"""
    s = ""
    for w in words:  # 由低位暫存器到高位，逐次往前接
        nibbles = [(w >> sh) & 0xF for sh in (12, 8, 4, 0)]
        s = "".join(str(n) if n <= 9 else "?" for n in nibbles) + s
    return s[-digits:]
