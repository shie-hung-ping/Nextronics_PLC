"""以模擬 PLC 驗證 SLMP 通訊、輪詢、批次切分與 CSV／錄影輸出。"""

import csv
import os
import time
from pathlib import Path

import cv2
import numpy as np
import pytest
from PySide6.QtGui import QGuiApplication

from nextronics_plc_ver5.acquisition import LOST_ROW_MESSAGE, PlcPoller
from nextronics_plc_ver5.camera import VideoRecorder, lost_frame
from nextronics_plc_ver5.config import AppConfig, FieldDef
from nextronics_plc_ver5.incidents import IncidentLog
from nextronics_plc_ver5.plc.devices import parse_device
from nextronics_plc_ver5.plc.simulator import SimulatorServer, _ascii_words
from nextronics_plc_ver5.plc.slmp import SlmpClient, probe_port


@pytest.fixture(scope="module")
def app():
    # 「畫面遺失」需要字型繪製，故使用 QGuiApplication（offscreen 不開視窗）
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QGuiApplication.instance() or QGuiApplication([])


@pytest.fixture
def sim():
    server = SimulatorServer("127.0.0.1", 0)
    server.start_background()
    yield server
    server.shutdown()
    server.server_close()


def wait_until(app, cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_slmp_read_words_and_bits(sim):
    port = sim.server_address[1]
    sim.plc.set_words("D100", [1, 2, 0xFFFF])
    sim.plc.set_bit("M101", True)
    with SlmpClient("127.0.0.1", port) as c:
        assert c.read_words(parse_device("D100"), 3) == [1, 2, 0xFFFF]
        assert c.read_bits(parse_device("M100"), 3) == [False, True, False]
        # 超過單次上限時自動分段
        assert len(c.read_words(parse_device("D0"), 1000)) == 1000
    assert probe_port("127.0.0.1", port, parse_device("M100")).ok
    assert not probe_port("127.0.0.1", 1, parse_device("M100"), timeout=0.3).ok


def test_batch_produces_csv_with_decoded_rows(app, sim, tmp_path):
    cfg = AppConfig()
    cfg.plc.host, cfg.plc.port, cfg.plc.poll_ms = "127.0.0.1", sim.server_address[1], 50
    cfg.storage.csv_dir, cfg.storage.video_dir = str(tmp_path / "csv"), str(tmp_path / "video")
    cfg.fields = [FieldDef("溫度", "D101", decimals=1), FieldDef("批號", "D110", kind="ascii", chars=8)]
    sim.plc.set_word("D101", 251)
    sim.plc.set_words("D110", _ascii_words("LOT-1", 4))

    started, finished = [], []
    poller = PlcPoller(cfg)
    poller.batch_started.connect(started.append)
    poller.batch_finished.connect(lambda b, rows, reason: finished.append((b, rows, reason)))
    poller.start()
    try:
        assert not wait_until(app, lambda: started, 0.5), "M100=0 時不應開始批次"
        sim.plc.set_bit("M100", True)
        assert wait_until(app, lambda: started)
        time.sleep(0.4)
        sim.plc.set_bit("M100", False)
        assert wait_until(app, lambda: finished)
    finally:
        poller.stop()

    batch, rows, reason = finished[0]
    assert batch == started[0] and reason == "觸發訊號 OFF"
    assert batch.csv_path.stem == batch.video_path.stem  # CSV 與錄影檔可由檔名對應
    with open(batch.csv_path, encoding="utf-8-sig", newline="") as f:
        data = list(csv.reader(f))
    assert data[0] == ["時間", "溫度", "批號"]
    assert len(data) - 1 == rows >= 3
    assert all(r[1:] == ["25.1", "LOT-1"] for r in data[1:])


def _offline_cfg(sim, tmp_path) -> AppConfig:
    cfg = AppConfig()
    cfg.plc.host, cfg.plc.port, cfg.plc.poll_ms = "127.0.0.1", sim.server_address[1], 50
    cfg.plc.reconnect_alert_sec = 1
    cfg.storage.csv_dir, cfg.storage.video_dir = str(tmp_path / "csv"), str(tmp_path / "video")
    cfg.storage.error_log_dir = str(tmp_path / "ErrorLog")
    return cfg


def _error_log_text(cfg) -> str:
    return "".join(p.read_text(encoding="utf-8") for p in Path(cfg.storage.error_log_dir).glob("error_*.log"))


def test_disconnect_in_batch_keeps_batch_and_marks_data_lost(app, sim, tmp_path):
    cfg = _offline_cfg(sim, tmp_path)
    sim.plc.set_bit("M100", True)
    lost, back, timeouts, finished = [], [], [], []
    poller = PlcPoller(cfg)
    poller.connection_lost.connect(lambda t, in_batch: lost.append(in_batch))
    poller.reconnected.connect(lambda t0, t1: back.append(t0))
    poller.outage_timeout.connect(lambda t, sec: timeouts.append(sec))
    poller.batch_finished.connect(lambda b, rows, reason: finished.append((b, reason)))
    poller.start()
    try:
        assert wait_until(app, lambda: poller.in_batch)
        batch = poller._batch
        poller.set_endpoint("127.0.0.1", 1)  # 切到不存在的埠，模擬 PLC 斷線
        assert wait_until(app, lambda: lost, 8)
        assert wait_until(app, lambda: timeouts, 8), "斷線超過門檻應發出逾時警告"
        assert poller.in_batch and not finished, "斷線期間批次不應結束"
        poller.set_endpoint("127.0.0.1", sim.server_address[1])  # PLC 恢復
        assert wait_until(app, lambda: back, 8)
        time.sleep(0.3)
        assert poller.in_batch and not finished, "重新連線後 M100 仍為 1，批次繼續"
        sim.plc.set_bit("M100", False)
        assert wait_until(app, lambda: finished)
    finally:
        poller.stop()

    assert lost == [True] and timeouts == [1] and len(back) == 1
    assert finished == [(batch, "觸發訊號 OFF")]
    with open(batch.csv_path, encoding="utf-8-sig", newline="") as f:
        data = list(csv.reader(f))[1:]
    flags = [r[1] == LOST_ROW_MESSAGE for r in data]
    # 正常資料 → 斷線期間持續寫入錯誤列（含時間戳記）→ 恢復後正常資料
    assert flags[0] is False and flags[-1] is False and sum(flags) >= 10
    assert all(r[0] for r in data)
    pending = IncidentLog(cfg.storage.error_log_dir).pending()   # 重新載入，確認已存檔
    assert set(pending) == {batch.csv_path.resolve(), batch.video_path.resolve()}
    log = _error_log_text(cfg)
    assert "PLC 斷線" in log and batch.batch_id in log and "超過 1 秒" in log and "自動重新連線" in log


def test_disconnect_outside_batch_flags_no_files(app, sim, tmp_path):
    cfg = _offline_cfg(sim, tmp_path)
    lost = []
    poller = PlcPoller(cfg)
    poller.connection_lost.connect(lambda t, in_batch: lost.append(in_batch))
    poller.start()
    try:
        assert wait_until(app, lambda: poller._ever_connected)
        poller.set_endpoint("127.0.0.1", 1)
        assert wait_until(app, lambda: lost, 8)
    finally:
        poller.stop()
    assert lost == [False]
    assert IncidentLog(cfg.storage.error_log_dir).pending() == []
    assert "當下未記錄批次" in _error_log_text(cfg)


def test_never_connected_is_not_an_outage(app, tmp_path):
    cfg = AppConfig()
    cfg.plc.host, cfg.plc.port = "127.0.0.1", 1
    cfg.storage.error_log_dir = str(tmp_path / "ErrorLog")
    lost = []
    poller = PlcPoller(cfg)
    poller.connection_lost.connect(lambda *a: lost.append(a))
    poller.start()
    try:
        assert not wait_until(app, lambda: lost, 3)
    finally:
        poller.stop()


def test_incident_acknowledge_persists(tmp_path):
    log = IncidentLog(tmp_path)
    f = tmp_path / "batch_x.csv"
    log.flag([f], "PLC 斷線，資料不完整")
    assert IncidentLog(tmp_path).pending() == [f.resolve()]
    IncidentLog(tmp_path).acknowledge(f)
    reloaded = IncidentLog(tmp_path)
    assert reloaded.pending() == [] and reloaded.status(f)["acknowledged"] is True
    assert "使用者已知悉" in "".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("error_*.log"))


def test_lost_frame_is_black_with_text(app):
    # 合成畫面僅用於測試錄影補幀，不作任何訓練用途
    img = lost_frame((240, 320, 3))
    assert img.shape == (240, 320, 3) and img.dtype == np.uint8
    assert img[:20, :20].max() == 0, "背景應為黑色"
    assert img[100:140, 100:220].max() > 200, "中央應有白色「畫面遺失」字樣"


def test_switch_port_keeps_batch(app, sim, tmp_path):
    other = SimulatorServer("127.0.0.1", 0, sim.plc)  # 同一台 PLC 的另一個埠
    other.start_background()
    cfg = AppConfig()
    cfg.plc.host, cfg.plc.port, cfg.plc.poll_ms = "127.0.0.1", sim.server_address[1], 50
    cfg.storage.csv_dir, cfg.storage.video_dir = str(tmp_path / "csv"), str(tmp_path / "video")
    sim.plc.set_bit("M100", True)
    finished, conns = [], []
    poller = PlcPoller(cfg)
    poller.batch_finished.connect(lambda *a: finished.append(a))
    poller.connection_changed.connect(lambda ok, text: conns.append(text))
    poller.start()
    try:
        assert wait_until(app, lambda: poller.in_batch)
        poller.set_endpoint("127.0.0.1", other.server_address[1])
        assert wait_until(app, lambda: any(str(other.server_address[1]) in c for c in conns))
        time.sleep(0.3)
        assert poller.in_batch and not finished
    finally:
        poller.stop()
        other.shutdown()
        other.server_close()


def test_video_recorder_paces_frames_by_elapsed_time(tmp_path):
    # 合成畫面僅用於測試錄影器，不作任何訓練用途
    path = tmp_path / "t.mp4"
    rec = VideoRecorder(path, fps=10, video_format="mp4")
    frame = np.zeros((48, 64, 3), np.uint8)
    for t in (0.0, 0.5, 1.0):   # 相機只給 3 張，經過 1 秒 → 應補到 11 幀
        rec.write(frame, t)
    assert rec.close()
    cap = cv2.VideoCapture(str(path))
    assert int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) == 11
    cap.release()
