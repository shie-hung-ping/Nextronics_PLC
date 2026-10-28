"""簡易錄影檔播放視窗（以 OpenCV 解碼，不依賴系統影片編碼器）。"""

from __future__ import annotations

from pathlib import Path

import cv2
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout

from ..camera import frame_to_qimage


class VideoPlayerDialog(QDialog):
    def __init__(self, path: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle(path.name)
        self.resize(800, 560)
        self._cap = cv2.VideoCapture(str(path))
        fps = self._cap.get(cv2.CAP_PROP_FPS) or 30
        self._total = int(self._cap.get(cv2.CAP_PROP_FRAME_COUNT))

        self._view = QLabel("無法讀取錄影檔" if not self._cap.isOpened() else "", alignment=Qt.AlignmentFlag.AlignCenter)
        self._view.setMinimumSize(320, 240)
        self._view.setStyleSheet("background:#000;color:#fff")
        self._play = QPushButton("暫停")
        self._play.clicked.connect(self._toggle)
        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, max(0, self._total - 1))
        self._slider.sliderMoved.connect(self._seek)
        self._time = QLabel()

        ctrl = QHBoxLayout()
        ctrl.addWidget(self._play)
        ctrl.addWidget(self._slider, 1)
        ctrl.addWidget(self._time)
        lay = QVBoxLayout(self)
        lay.addWidget(self._view, 1)
        lay.addLayout(ctrl)

        self._fps = fps
        self._timer = QTimer(self, interval=int(1000 / fps))
        self._timer.timeout.connect(self._next)
        if self._cap.isOpened():
            self._timer.start()

    def _toggle(self):
        if self._timer.isActive():
            self._timer.stop()
            self._play.setText("播放")
        else:
            if self._cap.get(cv2.CAP_PROP_POS_FRAMES) >= self._total - 1:
                self._seek(0)
            self._timer.start()
            self._play.setText("暫停")

    def _seek(self, pos: int):
        self._cap.set(cv2.CAP_PROP_POS_FRAMES, pos)
        self._next()

    def _next(self):
        ok, frame = self._cap.read()
        if not ok:
            self._timer.stop()
            self._play.setText("播放")
            return
        pos = int(self._cap.get(cv2.CAP_PROP_POS_FRAMES))
        self._slider.setValue(pos)
        self._time.setText(f"{pos / self._fps:.1f} / {self._total / self._fps:.1f} 秒")
        pix = QPixmap.fromImage(frame_to_qimage(frame))
        self._view.setPixmap(pix.scaled(self._view.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                        Qt.TransformationMode.SmoothTransformation))

    def closeEvent(self, e):
        self._timer.stop()
        self._cap.release()
        super().closeEvent(e)
