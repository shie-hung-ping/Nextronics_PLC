"""PLC 輪詢執行緒：讀取觸發訊號與暫存器、解碼、偵測批次開始/結束並寫入 CSV。

CSV 寫在本執行緒中，GUI 卡頓不會造成資料遺漏；錄影則透過 batch_started /
batch_finished 訊號交由相機執行緒處理。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime

from PySide6.QtCore import QThread, Signal

from .config import AppConfig
from .decoding import compute_read_blocks, decode, slice_field
from .plc.devices import parse_device
from .plc.slmp import SlmpClient, SlmpError
from .storage import BatchPaths, CsvBatchWriter, new_batch

RECONNECT_SEC = 2.0


class PlcPoller(QThread):
    connection_changed = Signal(bool, str)   # 是否連線、說明（含本機來源位址）
    values_updated = Signal(list, bool)      # 各欄位解碼值、觸發訊號狀態
    batch_started = Signal(object)           # BatchPaths
    batch_finished = Signal(object, int, str)  # BatchPaths、CSV 列數、結束原因
    error = Signal(str)

    def __init__(self, cfg: AppConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._running = True
        self._lock = threading.Lock()
        self._endpoint = (cfg.plc.host, cfg.plc.port)
        self._reconnect = False
        self._batch: BatchPaths | None = None
        self._writer: CsvBatchWriter | None = None

    @property
    def in_batch(self) -> bool:
        return self._batch is not None

    def set_endpoint(self, host: str, port: int) -> None:
        """切換 PLC 位址/埠號；批次進行中也可切換，批次不中斷。"""
        with self._lock:
            self._endpoint = (host, port)
            self._reconnect = True

    def stop(self) -> None:
        self._running = False
        self.wait(5000)

    def run(self) -> None:
        trigger = parse_device(self.cfg.plc.trigger)
        fields = list(self.cfg.fields)
        try:
            blocks = compute_read_blocks(fields)
        except ValueError as e:
            self.error.emit(f"顯示欄位設定錯誤：{e}")
            blocks, fields = [], []
        period = max(self.cfg.plc.poll_ms, 20) / 1000
        client: SlmpClient | None = None

        while self._running:
            with self._lock:
                host, port = self._endpoint
                if self._reconnect and client is not None:
                    client.close()
                    client = None
                self._reconnect = False

            if client is None:
                client = SlmpClient(host, port)
                try:
                    client.connect()
                    src = client.local_address()
                    self.connection_changed.emit(True, f"{host}:{port}（本機來源 {src[0]}:{src[1]}）")
                except OSError as e:
                    client = None
                    self.connection_changed.emit(False, f"{host}:{port} 連線失敗：{e}")
                    self._end_batch("PLC 無法連線")
                    self._sleep(RECONNECT_SEC)
                    continue

            t0 = time.monotonic()
            try:
                on = client.read_bits(trigger, 1)[0] if trigger.is_bit else client.read_words(trigger, 1)[0] != 0
                data = {b: client.read_words(b.head, b.count) for b in blocks}
            except (OSError, SlmpError) as e:
                client.close()
                client = None
                self.connection_changed.emit(False, f"通訊錯誤：{e}")
                self._end_batch("PLC 通訊中斷")
                continue

            now = datetime.now()
            values = [decode(f, slice_field(f, data)) for f in fields]
            self._update_batch(on, now, values, [f.name for f in fields])
            self.values_updated.emit(values, on)
            self._sleep(period - (time.monotonic() - t0))

        if client is not None:
            client.close()
        self._end_batch("程式停止擷取")

    def _update_batch(self, on: bool, now: datetime, values: list[str], names: list[str]) -> None:
        if on and self._batch is None:
            s = self.cfg.storage
            self._batch = new_batch(s.csv_dir, s.video_dir, self.cfg.camera.video_format, now)
            try:
                self._writer = CsvBatchWriter(self._batch.csv_path, names)
            except OSError as e:
                self.error.emit(f"無法建立 CSV：{e}")
            self.batch_started.emit(self._batch)
        elif not on and self._batch is not None:
            self._end_batch("觸發訊號 OFF")
        if self._writer is not None:
            try:
                self._writer.write(now, values)
            except OSError as e:
                self.error.emit(f"CSV 寫入失敗：{e}")

    def _end_batch(self, reason: str) -> None:
        if self._batch is None:
            return
        rows = 0
        if self._writer is not None:
            rows = self._writer.rows
            self._writer.close()
        batch, self._batch, self._writer = self._batch, None, None
        self.batch_finished.emit(batch, rows, reason)

    def _sleep(self, sec: float) -> None:
        # 分段睡眠，讓停止要求能及時生效
        end = time.monotonic() + sec
        while self._running:
            rest = end - time.monotonic()
            if rest <= 0:
                break
            time.sleep(min(0.05, rest))
