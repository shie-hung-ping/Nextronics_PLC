"""以模擬 PLC 驗證 SLMP 通訊、輪詢、批次切分與 CSV／錄影輸出。"""

import csv
import time

import cv2
import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication

from nextronics_plc_ver5.acquisition import PlcPoller
from nextronics_plc_ver5.camera import VideoRecorder
from nextronics_plc_ver5.config import AppConfig, FieldDef
from nextronics_plc_ver5.plc.devices import parse_device
from nextronics_plc_ver5.plc.simulator import SimulatorServer, _ascii_words
from nextronics_plc_ver5.plc.slmp import SlmpClient, probe_port


@pytest.fixture(scope="module")
def app():
    return QCoreApplication.instance() or QCoreApplication([])


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


def test_disconnect_ends_batch(app, sim, tmp_path):
    cfg = AppConfig()
    cfg.plc.host, cfg.plc.port, cfg.plc.poll_ms = "127.0.0.1", sim.server_address[1], 50
    cfg.storage.csv_dir, cfg.storage.video_dir = str(tmp_path / "csv"), str(tmp_path / "video")
    sim.plc.set_bit("M100", True)
    finished = []
    poller = PlcPoller(cfg)
    poller.batch_finished.connect(lambda b, rows, reason: finished.append(reason))
    poller.start()
    try:
        assert wait_until(app, lambda: poller.in_batch)
        sim.shutdown()
        sim.server_close()
        poller.set_endpoint("127.0.0.1", 1)  # 切到不存在的埠，模擬 PLC 無法連線
        assert wait_until(app, lambda: finished, 8)
    finally:
        poller.stop()
    assert finished == ["PLC 無法連線"]


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
