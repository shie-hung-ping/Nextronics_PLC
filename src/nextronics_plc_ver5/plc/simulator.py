"""SLMP 3E 二進位模擬 PLC：供測試與無實機時展示使用。

執行 `python -m nextronics_plc_ver5.plc.simulator` 會在 127.0.0.1:5000 啟動，
並以示範模式週期性切換 M100、更新預設顯示欄位用到的暫存器。
"""

from __future__ import annotations

import argparse
import socket
import socketserver
import struct
import threading
import time
from collections import defaultdict

from .devices import parse_device


class SimulatedPlc:
    """以 {元件代碼: {編號: 值}} 儲存記憶體，執行緒安全。"""

    def __init__(self):
        self._mem: dict[int, dict[int, int]] = defaultdict(dict)
        self._lock = threading.Lock()

    def set_word(self, addr: str, value: int) -> None:
        d = parse_device(addr)
        with self._lock:
            self._mem[d.code][d.number] = value & 0xFFFF

    def set_words(self, addr: str, values: list[int]) -> None:
        d = parse_device(addr)
        with self._lock:
            for i, v in enumerate(values):
                self._mem[d.code][d.number + i] = v & 0xFFFF

    def set_bit(self, addr: str, on: bool) -> None:
        d = parse_device(addr)
        with self._lock:
            self._mem[d.code][d.number] = 1 if on else 0

    def handle(self, frame: bytes) -> bytes:
        """處理一個完整請求框架並回傳回應框架。"""
        route = frame[2:7]
        command, sub = struct.unpack("<HH", frame[11:15])
        if command != 0x0401 or sub not in (0, 1):
            return self._response(route, 0xC059, b"")
        number = int.from_bytes(frame[15:18], "little")
        code = frame[18]
        points = struct.unpack("<H", frame[19:21])[0]
        with self._lock:
            mem = self._mem[code]
            values = [mem.get(number + i, 0) for i in range(points)]
        if sub == 0:
            data = struct.pack(f"<{points}H", *values)
        else:
            packed = bytearray((points + 1) // 2)
            for i, v in enumerate(values):
                packed[i // 2] |= (v & 1) << (4 if i % 2 == 0 else 0)
            data = bytes(packed)
        return self._response(route, 0, data)

    @staticmethod
    def _response(route: bytes, end_code: int, data: bytes) -> bytes:
        body = struct.pack("<H", end_code) + data
        return b"\xd0\x00" + route + struct.pack("<H", len(body)) + body


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        sock: socket.socket = self.request
        plc: SimulatedPlc = self.server.plc  # type: ignore[attr-defined]
        try:
            while True:
                header = _recv_exact(sock, 9)
                if header is None or self.server.drop_clients:  # type: ignore[attr-defined]
                    return
                length = struct.unpack("<H", header[7:9])[0]
                body = _recv_exact(sock, length)
                if body is None:
                    return
                sock.sendall(plc.handle(header + body))
        except OSError:
            return


def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


class SimulatorServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, host: str = "127.0.0.1", port: int = 5000, plc: SimulatedPlc | None = None):
        super().__init__((host, port), _Handler)
        self.plc = plc or SimulatedPlc()
        # True：模擬 iQ-F 拔線重插後的狀態——接受 TCP 連線但收到要求即中斷（既有連線也會被切斷）
        self.drop_clients = False

    def start_background(self) -> threading.Thread:
        t = threading.Thread(target=self.serve_forever, daemon=True)
        t.start()
        return t


def _ascii_words(text: str, n_regs: int) -> list[int]:
    raw = text.encode("ascii").ljust(n_regs * 2, b"\0")
    return [raw[i] | (raw[i + 1] << 8) for i in range(0, n_regs * 2, 2)]


def run_demo(plc: SimulatedPlc, cycle_on: float, cycle_off: float) -> None:
    """示範資料：對應 config 的預設顯示欄位（D100、D101、D102-103、D110-113、D120-121）。"""
    counter, lot = 0, 0
    while True:
        lot += 1
        plc.set_words("D110", _ascii_words(f"LOT-{lot:03d}", 4))
        plc.set_words("D120", [0x5678, 0x1234 + lot])  # BCD：低位暫存器放低 4 位數
        plc.set_bit("M100", True)
        end = time.monotonic() + cycle_on
        while time.monotonic() < end:
            counter += 1
            plc.set_word("D100", counter)
            plc.set_word("D101", 250 + counter % 50)       # 溫度 ×0.1
            v = -100000 + counter
            plc.set_words("D102", [v & 0xFFFF, (v >> 16) & 0xFFFF])
            time.sleep(0.1)
        plc.set_bit("M100", False)
        time.sleep(cycle_off)


def main() -> None:
    ap = argparse.ArgumentParser(description="SLMP 模擬 PLC")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, nargs="+", default=[5000])
    ap.add_argument("--on", type=float, default=10.0, help="M100=1 持續秒數")
    ap.add_argument("--off", type=float, default=5.0, help="M100=0 持續秒數")
    args = ap.parse_args()
    plc = SimulatedPlc()
    for p in args.port:
        SimulatorServer(args.host, p, plc).start_background()
        print(f"模擬 PLC 監聽 {args.host}:{p}")
    try:
        run_demo(plc, args.on, args.off)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
