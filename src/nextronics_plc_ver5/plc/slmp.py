"""SLMP（MC 協定 3E 框架、二進位碼）TCP 用戶端：批次讀取字元件與位元件。

只實作本系統需要的「批次讀取」(0x0401)；iQ-F 內建乙太網路需在 GX Works3
開啟對應的 SLMP 連線埠（TCP、二進位）。
"""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass

from .devices import Device

MAX_WORD_POINTS = 960   # 3E 框架單次字讀取上限
MAX_BIT_POINTS = 7168   # 3E 框架單次位元讀取上限

_CMD_BATCH_READ = 0x0401
_SUB_WORD = 0x0000
_SUB_BIT = 0x0001
_MONITOR_TIMER = 0x0010  # 單位 250ms → 4 秒


class SlmpError(Exception):
    """PLC 回傳錯誤碼或通訊格式錯誤。"""


@dataclass
class ProbeResult:
    port: int
    ok: bool
    message: str


class SlmpClient:
    def __init__(self, host: str, port: int, timeout: float = 2.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None

    # ---- 連線管理 ----
    def connect(self) -> None:
        self.close()
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self._sock.settimeout(self.timeout)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def local_address(self) -> tuple[str, int] | None:
        """本機端（來源）IP 與埠號，用於後台顯示「埠號來源」。"""
        return self._sock.getsockname()[:2] if self._sock else None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- 讀取 ----
    def read_words(self, head: Device, count: int) -> list[int]:
        """讀取 count 個字元件，回傳 0–65535 的無號值；超過上限自動分段。"""
        if head.is_bit:
            raise ValueError("read_words 需使用字元件")
        out: list[int] = []
        while count > 0:
            n = min(count, MAX_WORD_POINTS)
            data = self._request(_CMD_BATCH_READ, _SUB_WORD, head, n)
            if len(data) != n * 2:
                raise SlmpError("字讀取回應長度錯誤")
            out.extend(struct.unpack(f"<{n}H", data))
            head, count = head.offset(n), count - n
        return out

    def read_bits(self, head: Device, count: int) -> list[bool]:
        if not head.is_bit:
            raise ValueError("read_bits 需使用位元件")
        out: list[bool] = []
        while count > 0:
            n = min(count, MAX_BIT_POINTS)
            data = self._request(_CMD_BATCH_READ, _SUB_BIT, head, n)
            # 位元單位：每個 byte 放 2 點，高 4 bit 為前一點
            for i in range(n):
                b = data[i // 2]
                out.append(bool((b >> 4) & 1 if i % 2 == 0 else b & 1))
            head, count = head.offset(n), count - n
        return out

    # ---- 底層框架 ----
    def _request(self, command: int, sub: int, head: Device, points: int) -> bytes:
        if self._sock is None:
            raise SlmpError("尚未連線")
        body = struct.pack("<HHH", _MONITOR_TIMER, command, sub)
        body += head.number.to_bytes(3, "little") + bytes([head.code]) + struct.pack("<H", points)
        # 副標頭 5000、網路 00、PC FF、要求目標模組 03FF、站號 00、資料長度
        frame = bytes([0x50, 0x00, 0x00, 0xFF, 0xFF, 0x03, 0x00]) + struct.pack("<H", len(body)) + body
        self._sock.sendall(frame)

        header = self._recv_exact(9)
        if header[0:2] != b"\xd0\x00":
            raise SlmpError(f"回應副標頭錯誤：{header[0:2].hex()}")
        length = struct.unpack("<H", header[7:9])[0]
        payload = self._recv_exact(length)
        end_code = struct.unpack("<H", payload[0:2])[0]
        if end_code != 0:
            raise SlmpError(f"PLC 回傳錯誤碼 0x{end_code:04X}")
        return payload[2:]

    def _recv_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("PLC 中斷連線")
            buf += chunk
        return bytes(buf)


def probe_port(host: str, port: int, test_device: Device, timeout: float = 1.0) -> ProbeResult:
    """嘗試連線並讀取一點，判斷該埠是否可用 SLMP 通訊。"""
    try:
        with SlmpClient(host, port, timeout) as c:
            if test_device.is_bit:
                c.read_bits(test_device, 1)
            else:
                c.read_words(test_device, 1)
        return ProbeResult(port, True, "可連線")
    except (OSError, SlmpError) as e:
        return ProbeResult(port, False, str(e) or e.__class__.__name__)
