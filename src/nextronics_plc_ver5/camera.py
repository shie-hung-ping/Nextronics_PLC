"""相機擷取執行緒與錄影器。

CameraWorker 持續讀取相機畫面供 GUI 預覽；錄影時把畫面交給 VideoRecorder。
VideoRecorder 依時間補幀/丟幀，使影片長度與實際經過時間一致（相機實際 FPS 常低於設定值）。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QImage

_FOURCC = {"mp4": "mp4v", "avi": "MJPG"}


class VideoRecorder:
    def __init__(self, path: Path, fps: int, video_format: str):
        self.path = path
        self.fps = fps
        self._fourcc = cv2.VideoWriter_fourcc(*_FOURCC.get(video_format, "mp4v"))
        self._writer: cv2.VideoWriter | None = None
        self._t0 = 0.0
        self.frames = 0

    def write(self, frame: np.ndarray, now: float) -> None:
        if self._writer is None:
            h, w = frame.shape[:2]
            self._writer = cv2.VideoWriter(str(self.path), self._fourcc, self.fps, (w, h))
            if not self._writer.isOpened():
                raise RuntimeError(f"無法建立錄影檔：{self.path}")
            self._t0 = now
        # 依經過時間應有的幀數補寫；單次最多補 1 秒，避免卡頓後爆量寫入
        target = int((now - self._t0) * self.fps) + 1
        for _ in range(min(target - self.frames, self.fps)):
            self._writer.write(frame)
            self.frames += 1
        self.frames = max(self.frames, target)

    def close(self) -> bool:
        """關閉檔案，回傳是否有寫入任何畫面。"""
        if self._writer is None:
            return False
        self._writer.release()
        self._writer = None
        return True


def frame_to_qimage(frame: np.ndarray) -> QImage:
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    h, w = rgb.shape[:2]
    return QImage(rgb.data, w, h, w * 3, QImage.Format.Format_RGB888).copy()


class CameraWorker(QThread):
    frame_ready = Signal(QImage)
    recording_saved = Signal(str)     # 錄影檔路徑
    recording_failed = Signal(str)    # 錯誤訊息
    status = Signal(str)

    def __init__(self, index: int, fps: int, video_format: str, parent=None):
        super().__init__(parent)
        self.index, self.fps, self.video_format = index, fps, video_format
        self._lock = threading.Lock()
        self._pending_start: Path | None = None
        self._pending_stop = False
        self._running = True

    # 以下兩個方法可由其他執行緒呼叫，實際動作在擷取迴圈中執行
    def start_recording(self, path: Path) -> None:
        with self._lock:
            self._pending_start, self._pending_stop = path, False

    def stop_recording(self) -> None:
        with self._lock:
            self._pending_start, self._pending_stop = None, True

    def stop(self) -> None:
        self._running = False
        self.wait(3000)

    def run(self) -> None:
        recorder: VideoRecorder | None = None
        cap = None
        frame_interval = 1.0 / max(1, self.fps)
        while self._running:
            if cap is None or not cap.isOpened():
                cap = self._open()
                if cap is None:
                    self.status.emit(f"無法開啟相機 {self.index}，2 秒後重試")
                    recorder = self._apply_commands(recorder)
                    time.sleep(2)
                    continue
                self.status.emit(f"相機 {self.index} 已開啟")
            ok, frame = cap.read()
            now = time.monotonic()
            recorder = self._apply_commands(recorder)
            if not ok:
                cap.release()
                cap = None
                continue
            if recorder is not None:
                try:
                    recorder.write(frame, now)
                except RuntimeError as e:
                    self.recording_failed.emit(str(e))
                    recorder = None
            self.frame_ready.emit(frame_to_qimage(frame))
            # 部分相機 read() 不會阻塞，避免空轉
            rest = frame_interval - (time.monotonic() - now)
            if rest > 0:
                time.sleep(rest)
        self._finish(recorder)
        if cap is not None:
            cap.release()

    def _open(self):
        backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
        cap = cv2.VideoCapture(self.index, backend)
        if not cap.isOpened():
            return None
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        return cap

    def _apply_commands(self, recorder: VideoRecorder | None) -> VideoRecorder | None:
        with self._lock:
            start, stop = self._pending_start, self._pending_stop
            self._pending_start, self._pending_stop = None, False
        if stop or start is not None:
            self._finish(recorder)
            recorder = None
        if start is not None:
            recorder = VideoRecorder(start, self.fps, self.video_format)
        return recorder

    def _finish(self, recorder: VideoRecorder | None) -> None:
        if recorder is None:
            return
        if recorder.close():
            self.recording_saved.emit(str(recorder.path))
        else:
            self.recording_failed.emit("錄影期間未取得任何相機畫面，未產生錄影檔")


def list_cameras(max_index: int = 5) -> list[int]:
    """測試 0..max_index-1 哪些相機可開啟。"""
    found = []
    backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
    for i in range(max_index):
        cap = cv2.VideoCapture(i, backend)
        if cap.isOpened():
            found.append(i)
        cap.release()
    return found
