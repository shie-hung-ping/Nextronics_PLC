"""PLC 軟元件位址解析（例如 "D100"、"M100"、"W1A"）與 SLMP 元件代碼對照。"""

from __future__ import annotations

import re
from dataclasses import dataclass

# SLMP 3E 二進位碼的元件代碼；hex=True 代表位址以 16 進位表示（W、B 等）
_WORD_DEVICES = {"D": (0xA8, False), "W": (0xB4, True), "R": (0xAF, False)}
_BIT_DEVICES = {"M": (0x90, False), "X": (0x9C, False), "Y": (0x9D, False), "B": (0xA0, True)}

# iQ-F 的 X/Y 為 8 進位編號，SLMP 送出時也以 8 進位解讀後的數值傳送
_OCTAL_DEVICES = {"X", "Y"}

_ADDR_RE = re.compile(r"^\s*([A-Za-z]+)\s*([0-9A-Fa-f]+)\s*$")


@dataclass(frozen=True)
class Device:
    name: str      # 元件字母，例如 "D"
    number: int    # 元件編號（已轉成整數）
    code: int      # SLMP 元件代碼
    is_bit: bool

    def offset(self, n: int) -> "Device":
        return Device(self.name, self.number + n, self.code, self.is_bit)

    def __str__(self) -> str:
        return format_device(self.name, self.number)


def format_device(name: str, number: int) -> str:
    if name in _OCTAL_DEVICES:
        return f"{name}{number:o}"
    table = _WORD_DEVICES if name in _WORD_DEVICES else _BIT_DEVICES
    return f"{name}{number:X}" if table.get(name, (0, False))[1] else f"{name}{number}"


def parse_device(text: str) -> Device:
    """將 "D100" 之類的字串解析為 Device；格式錯誤時拋出 ValueError。"""
    m = _ADDR_RE.match(text or "")
    if not m:
        raise ValueError(f"無效的元件位址：{text!r}")
    name, num = m.group(1).upper(), m.group(2)
    if name in _WORD_DEVICES:
        code, is_hex = _WORD_DEVICES[name]
        is_bit = False
    elif name in _BIT_DEVICES:
        code, is_hex = _BIT_DEVICES[name]
        is_bit = True
    else:
        raise ValueError(f"不支援的元件類型：{name}")
    base = 16 if is_hex else 8 if name in _OCTAL_DEVICES else 10
    try:
        number = int(num, base)
    except ValueError:
        raise ValueError(f"無效的元件編號：{text!r}") from None
    return Device(name, number, code, is_bit)
