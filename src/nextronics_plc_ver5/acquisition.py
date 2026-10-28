"""PLC 輪詢執行緒：讀取觸發訊號與暫存器、解碼、偵測批次開始/結束並寫入 CSV。

CSV 寫在本執行緒中，GUI 卡頓不會造成資料遺漏；錄影則透過 batch_started /
batch_finished 訊號交由相機執行緒處理。

斷線處理：曾連上的 PLC 斷線後自動重試連線，批次不中斷；斷線期間每個輪詢週期寫入
一列資料遺失紀錄，並把該批次 CSV／錄影檔登錄為資料不完整、寫入 ErrorLog。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime

from PySide6.QtCore import QThread, Signal

from .config import AppConfig
from .decoding import compute_read_blocks, decode, slice_field
from .incidents import IncidentLog
from .plc.devices import parse_device
from .plc.slmp import SlmpClient, SlmpError
from .storage import BatchPaths, CsvBatchWriter, new_batch

RECONNECT_SEC = 2.0
LOST_ROW_MESSAGE = "資料遺失：PLC 斷線"


class PlcPoller(QThread):
    connection_changed = Signal(bool, str)   # 是否連線、說明（含本機來源位址）
    values_updated = Signal(list, bool)      # 各欄位解碼值、觸發訊號狀態
    batch_started = Signal(object)           # BatchPaths
    batch_finished = Signal(object, int, str)  # BatchPaths、CSV 列數、結束原因
    connection_lost = Signal(object, bool)   # 斷線時間（datetime）、當下是否在批次中（M100=1）
    reconnected = Signal(object, object)     # 斷線時間、重新連線時間
    outage_timeout = Signal(object, int)     # 斷線時間、警告門檻秒數（斷線超過門檻時發出一次）
    error = Signal(str)

    def __init__(self, cfg: AppConfig, parent=None, incidents: IncidentLog | None = None):
        super().__init__(parent)
        self.cfg = cfg
        self.incidents = incidents or IncidentLog(cfg.storage.error_log_dir)
        self._running = True
        self._lock = threading.Lock()
        self._endpoint = (cfg.plc.host, cfg.plc.port)
        self._reconnect = False
        self._batch: BatchPaths | None = None
        self._writer: CsvBatchWriter | None = None
        self._ever_connected = False             # 只有連上過才算「斷線」，開機連不上不發警告
        self._lost_at: datetime | None = None    # 目前斷線事件的開始時間
        self._lost_mono = 0.0
        self._timeout_alerted = False

    @property
    def offline(self) -> bool:
        return self._lost_at is not None

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
                    self._on_connected()
                except OSError as e:
                    client = None
                    self.connection_changed.emit(False, f"{host}:{port} 連線失敗：{e}")
                    self._on_lost(f"{host}:{port} 連線失敗：{e}")
                    self._wait_offline(RECONNECT_SEC, period)
                    continue

            t0 = time.monotonic()
            try:
                on = client.read_bits(trigger, 1)[0] if trigger.is_bit else client.read_words(trigger, 1)[0] != 0
                data = {b: client.read_words(b.head, b.count) for b in blocks}
            except (OSError, SlmpError) as e:
                client.close()
                client = None
                self.connection_changed.emit(False, f"通訊錯誤：{e}")
                self._on_lost(f"通訊錯誤：{e}")
                continue   # 立即重試連線

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

    # ---------------- 斷線處理 ----------------
    def _on_connected(self) -> None:
        self._ever_connected = True
        if self._lost_at is None:
            return
        now = datetime.now()
        lost_at, self._lost_at = self._lost_at, None
        self.incidents.log(f"PLC 已自動重新連線（{lost_at:%Y-%m-%d %H:%M:%S} 斷線，"
                           f"歷時 {(now - lost_at).total_seconds():.1f} 秒）", now)
        self.reconnected.emit(lost_at, now)

    def _on_lost(self, detail: str) -> None:
        """斷線事件開始：登錄問題檔案、寫 ErrorLog 與第一列資料遺失紀錄。重試失敗不重複觸發。"""
        if not self._ever_connected or self._lost_at is not None:
            return
        now = datetime.now()
        self._lost_at, self._lost_mono, self._timeout_alerted = now, time.monotonic(), False
        b = self._batch
        if b is not None:
            self.incidents.flag([b.csv_path, b.video_path], "PLC 斷線，資料不完整", now)
            self.incidents.log(f"PLC 斷線（{detail}），批次 {b.batch_id} 記錄中，"
                               f"CSV 與錄影資料不完整：{b.csv_path}、{b.video_path}", now)
        else:
            self.incidents.log(f"PLC 斷線（{detail}），當下未記錄批次", now)
        self.connection_lost.emit(now, b is not None)
        self._write_lost_row(now)

    def _wait_offline(self, sec: float, period: float) -> None:
        """等待下次重試；斷線中每個輪詢週期寫一列資料遺失紀錄，並檢查是否超過警告門檻。"""
        end = time.monotonic() + sec
        while self._running and time.monotonic() < end:
            self._check_timeout()
            self._sleep(min(period, end - time.monotonic()))
            self._write_lost_row(datetime.now())

    def _check_timeout(self) -> None:
        limit = self.cfg.plc.reconnect_alert_sec
        if self._lost_at is None or self._timeout_alerted or time.monotonic() - self._lost_mono < limit:
            return
        self._timeout_alerted = True
        self.incidents.log(f"PLC 重新連線超過 {limit} 秒（{self._lost_at:%Y-%m-%d %H:%M:%S} 斷線），資料儲存不完全")
        self.outage_timeout.emit(self._lost_at, limit)

    def _write_lost_row(self, now: datetime) -> None:
        if self._lost_at is None or self._writer is None:
            return
        try:
            self._writer.write_error(now, LOST_ROW_MESSAGE)
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
