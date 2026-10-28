"""主畫面：上方相機即時影像 + 過往錄影；下方 PLC 解碼數值 + 一小時內 CSV。

負責串接 PlcPoller（資料/批次）與 CameraWorker（預覽/錄影）。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import cv2
from PySide6.QtCore import QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QListWidget, QListWidgetItem, QMainWindow,
    QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..acquisition import PlcPoller
from ..camera import CameraWorker, frame_to_qimage
from ..config import AppConfig, save_config
from ..decoding import field_range_text
from ..incidents import IncidentLog
from ..storage import BatchPaths, recent_csv_files, video_files
from .admin_dialog import AdminDialog
from .alerts import IssueItemWidget, ReconnectToast, ack_dialog
from .video_player import VideoPlayerDialog

PAGE_SIZE = 5
THUMB = QSize(160, 90)


class MainWindow(QMainWindow):
    def __init__(self, cfg: AppConfig):
        super().__init__()
        self.cfg = cfg
        self.setWindowTitle("Nextronics PLC 資料擷取與錄影")
        self.resize(1200, 820)
        self.poller: PlcPoller | None = None
        self.camera: CameraWorker | None = None
        self.batch: BatchPaths | None = None
        self._conn_text = "未連線"
        self._admin: AdminDialog | None = None
        self._video_page = 0
        self._thumb_cache: dict[Path, tuple[float, QIcon]] = {}
        self.incidents = IncidentLog(cfg.storage.error_log_dir)
        self._alerts: dict[str, QMessageBox] = {}   # 尚未按「已知悉」的警告視窗，同類只開一個

        self._build_ui()
        self._start_camera()
        self._start_poller()
        self._refresh_videos()
        self._refresh_csv()
        self._csv_timer = QTimer(self, interval=5000)
        self._csv_timer.timeout.connect(self._refresh_csv)
        self._csv_timer.start()

    # ---------------- 介面 ----------------
    def _build_ui(self):
        # 上方：相機 + 錄影紀錄
        self.cam_view = QLabel("相機啟動中…", alignment=Qt.AlignmentFlag.AlignCenter)
        self.cam_view.setMinimumSize(480, 320)
        self.cam_view.setStyleSheet("background:#111;color:#ccc")
        self.cam_status = QLabel()
        cam_box = QGroupBox("相機即時影像")
        cl = QVBoxLayout(cam_box)
        cl.addWidget(self.cam_view, 1)
        cl.addWidget(self.cam_status)

        self.video_list = QListWidget()
        self.video_list.setIconSize(THUMB)
        self.video_list.itemClicked.connect(self._play_video)
        self.prev_btn = QPushButton("◀ 上一頁")
        self.next_btn = QPushButton("下一頁 ▶")
        self.page_label = QLabel()
        self.prev_btn.clicked.connect(lambda: self._change_page(-1))
        self.next_btn.clicked.connect(lambda: self._change_page(1))
        nav = QHBoxLayout()
        nav.addWidget(self.prev_btn)
        nav.addWidget(self.page_label, 1, Qt.AlignmentFlag.AlignCenter)
        nav.addWidget(self.next_btn)
        vid_box = QGroupBox("過往錄影檔（點選播放）")
        vl = QVBoxLayout(vid_box)
        vl.addWidget(self.video_list, 1)
        vl.addLayout(nav)

        top = QHBoxLayout()
        top.addWidget(cam_box, 3)
        top.addWidget(vid_box, 2)

        # 下方：PLC 數值 + CSV 列表
        self.plc_status = QLabel()
        self.batch_label = QLabel("目前批次：無")
        admin_btn = QPushButton("後台管理")
        admin_btn.clicked.connect(self._open_admin)
        head = QHBoxLayout()
        head.addWidget(self.plc_status, 1)
        head.addWidget(admin_btn)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["欄位", "暫存器", "數值"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        plc_box = QGroupBox("PLC 數值")
        pl = QVBoxLayout(plc_box)
        pl.addLayout(head)
        pl.addWidget(self.batch_label)
        pl.addWidget(self.table, 1)

        self.csv_list = QListWidget()
        self.csv_list.itemDoubleClicked.connect(
            lambda it: QDesktopServices.openUrl(QUrl.fromLocalFile(it.data(Qt.ItemDataRole.UserRole))))
        csv_box = QGroupBox("一小時內的 CSV（雙擊開啟）")
        QVBoxLayout(csv_box).addWidget(self.csv_list)

        bottom = QHBoxLayout()
        bottom.addWidget(plc_box, 3)
        bottom.addWidget(csv_box, 2)

        root = QWidget()
        lay = QVBoxLayout(root)
        lay.addLayout(top, 3)
        lay.addLayout(bottom, 2)
        self.setCentralWidget(root)
        self.toast = ReconnectToast(self)
        self.toast.acknowledged.connect(
            lambda msgs: [self.incidents.log(f"使用者已知悉提醒：{m}") for m in msgs])
        self._reset_table()
        self._set_plc_status(False, "未連線")

    def _reset_table(self):
        self.table.setRowCount(len(self.cfg.fields))
        for r, f in enumerate(self.cfg.fields):
            self.table.setItem(r, 0, QTableWidgetItem(f.name))
            self.table.setItem(r, 1, QTableWidgetItem(field_range_text(f)))
            self.table.setItem(r, 2, QTableWidgetItem("—"))

    # ---------------- 擷取執行緒 ----------------
    def _start_poller(self):
        self.poller = PlcPoller(self.cfg, self, incidents=self.incidents)
        self.poller.connection_changed.connect(self._set_plc_status)
        self.poller.values_updated.connect(self._on_values)
        self.poller.batch_started.connect(self._on_batch_started)
        self.poller.batch_finished.connect(self._on_batch_finished)
        self.poller.connection_lost.connect(self._on_connection_lost)
        self.poller.reconnected.connect(self._on_reconnected)
        self.poller.outage_timeout.connect(self._on_outage_timeout)
        self.poller.error.connect(lambda m: self.statusBar().showMessage(m, 10000))
        self.camera.set_signal_lost(False)   # 新的輪詢執行緒從未斷線狀態開始
        self.poller.start()

    def _start_camera(self):
        c = self.cfg.camera
        self.camera = CameraWorker(c.index, c.fps, c.video_format, self)
        self.camera.frame_ready.connect(self._on_frame)
        self.camera.status.connect(self.cam_status.setText)
        self.camera.recording_saved.connect(self._on_video_saved)
        self.camera.recording_failed.connect(lambda m: self.statusBar().showMessage(f"錄影：{m}", 10000))
        self.camera.start()

    def _set_plc_status(self, ok: bool, text: str):
        self._conn_text = ("● 已連線 " if ok else "○ 未連線 ") + text
        self.plc_status.setText(self._conn_text)
        self.plc_status.setStyleSheet("color:#1a7f37" if ok else "color:#c62828")
        if self._admin is not None:
            self._admin.set_connection_text(self._conn_text)

    def _on_values(self, values: list, on: bool):
        for r, v in enumerate(values[: self.table.rowCount()]):
            self.table.item(r, 2).setText(v)
        trig = f"{self.cfg.plc.trigger}={'1' if on else '0'}"
        rows = f"，{trig}" if not self.batch else f"，{trig}，記錄中"
        self.plc_status.setText(self._conn_text + rows)

    def _on_batch_started(self, batch: BatchPaths):
        self.batch = batch
        self.batch_label.setText(f"目前批次：{batch.batch_id}（記錄中）")
        self.camera.start_recording(batch.video_path)
        self._refresh_csv()

    def _on_batch_finished(self, batch: BatchPaths, rows: int, reason: str):
        self.batch = None
        self.batch_label.setText(f"上一批次：{batch.batch_id}，{rows} 筆（{reason}）")
        self.camera.stop_recording()
        self._refresh_csv()

    # ---------------- 斷線提示 ----------------
    def _on_connection_lost(self, lost_at: datetime, in_batch: bool):
        self.camera.set_signal_lost(True)
        if in_batch:
            self._refresh_csv()   # 記錄中的 CSV 立即標紅；錄影檔儲存後才出現在清單
        else:
            self._alert("lost", "PLC 已失去連線",
                        f"PLC已失去連線資料儲存不完全\n斷線時間：{lost_at:%Y-%m-%d %H:%M:%S}")

    def _on_reconnected(self, lost_at: datetime, now: datetime):
        self.camera.set_signal_lost(False)
        self.toast.add(f"{lost_at:%Y-%m-%d %H:%M:%S} 發生斷線，但已自動重新連線")

    def _on_outage_timeout(self, lost_at: datetime, sec: int):
        dur = f"{sec // 60}分鐘" if sec % 60 == 0 else f"{sec}秒"
        self._alert("timeout", "PLC 重新連線逾時",
                    f"PLC重新連線超過{dur}，資料儲存不完全\n斷線時間：{lost_at:%Y-%m-%d %H:%M:%S}")

    def _alert(self, key: str, title: str, text: str):
        """同類警告未確認前只保留一個視窗，新的事件更新其內容。"""
        box = self._alerts.get(key)
        if box is not None:
            box.setText(text)
            box.raise_()
            return
        box = ack_dialog(self, title, text)
        self._alerts[key] = box
        box.finished.connect(lambda *_: self._on_alert_ack(key, text))

    def _on_alert_ack(self, key: str, text: str):
        box = self._alerts.pop(key, None)
        self.incidents.log(f"使用者已知悉警告：{(box.text() if box else text).splitlines()[0]}")

    def _acknowledge_file(self, path: Path):
        self.incidents.acknowledge(path)
        self._refresh_videos()
        self._refresh_csv()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.toast.reposition()

    def _on_frame(self, img: QImage):
        pix = QPixmap.fromImage(img).scaled(self.cam_view.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                            Qt.TransformationMode.FastTransformation)
        if self.batch is not None:
            p = QPainter(pix)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setBrush(QColor("#e53935"))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(12, 12, 16, 16)
            p.setPen(QColor("#ffffff"))
            p.setFont(QFont(self.font().family(), 14, QFont.Weight.Bold))
            p.drawText(36, 27, "錄影中")
            p.end()
        self.cam_view.setPixmap(pix)

    # ---------------- 錄影檔 / CSV 列表 ----------------
    def _on_video_saved(self, path: str):
        self.statusBar().showMessage(f"錄影已儲存：{path}", 10000)
        self._video_page = 0
        self._refresh_videos()

    def _change_page(self, step: int):
        self._video_page += step
        self._refresh_videos()

    def _refresh_videos(self):
        recording = self.batch.video_path if self.batch else None
        files = [p for p in video_files(self.cfg.storage.video_dir) if p != recording]
        pages = max(1, (len(files) + PAGE_SIZE - 1) // PAGE_SIZE)
        self._video_page = min(max(0, self._video_page), pages - 1)
        self.video_list.clear()
        for p in files[self._video_page * PAGE_SIZE:(self._video_page + 1) * PAGE_SIZE]:
            issue = self.incidents.status(p)
            if issue and not issue["acknowledged"]:
                # 資料不完整且未確認：紅框 + 「我已知悉問題」
                item = QListWidgetItem()
                w = IssueItemWidget(f"{p.name}\n⚠ 資料不完整", self._thumbnail(p).pixmap(THUMB), red_frame=True)
                w.setToolTip(f"{issue['reason']}（{issue['time']}）")
                w.acknowledged.connect(lambda p=p: self._acknowledge_file(p))
                item.setSizeHint(w.sizeHint())
                item.setData(Qt.ItemDataRole.UserRole, str(p))
                self.video_list.addItem(item)
                self.video_list.setItemWidget(item, w)
                continue
            tag = "\n資料不完整（已知悉）" if issue else ""
            item = QListWidgetItem(self._thumbnail(p), p.name + tag)
            item.setData(Qt.ItemDataRole.UserRole, str(p))
            self.video_list.addItem(item)
        self.page_label.setText(f"{self._video_page + 1} / {pages}（共 {len(files)} 筆）")
        self.prev_btn.setEnabled(self._video_page > 0)
        self.next_btn.setEnabled(self._video_page < pages - 1)

    def _thumbnail(self, path: Path) -> QIcon:
        mtime = path.stat().st_mtime
        cached = self._thumb_cache.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        cap = cv2.VideoCapture(str(path))
        ok, frame = cap.read()
        cap.release()
        if ok:
            icon = QIcon(QPixmap.fromImage(frame_to_qimage(frame)).scaled(THUMB, Qt.AspectRatioMode.KeepAspectRatio))
        else:
            blank = QPixmap(THUMB)
            blank.fill(QColor("#444"))
            icon = QIcon(blank)
        self._thumb_cache[path] = (mtime, icon)
        return icon

    def _play_video(self, item: QListWidgetItem):
        VideoPlayerDialog(Path(item.data(Qt.ItemDataRole.UserRole)), self).exec()

    def _refresh_csv(self):
        self.csv_list.clear()
        current = self.batch.csv_path.resolve() if self.batch else None
        files = [p.resolve() for p in recent_csv_files(self.cfg.storage.csv_dir)]
        # 未確認的問題 CSV 超過一小時也保留在清單，直到使用者按「我已知悉問題」
        csv_dir = Path(self.cfg.storage.csv_dir).resolve()
        files += [p for p in self.incidents.pending()
                  if p.suffix == ".csv" and p.parent == csv_dir and p.exists() and p not in files]
        for p in files:
            t = datetime.fromtimestamp(p.stat().st_mtime).strftime("%m-%d %H:%M:%S")
            tag = "（記錄中）" if p == current else ""
            item = QListWidgetItem(f"{p.name}　更新 {t}{tag}")
            item.setData(Qt.ItemDataRole.UserRole, str(p))
            self.csv_list.addItem(item)
            issue = self.incidents.status(p)
            if issue and not issue["acknowledged"]:
                # 資料不完整且未確認：紅字 + 「我已知悉問題」
                w = IssueItemWidget(f"{item.text()}\n⚠ 資料不完整")
                w.setToolTip(f"{issue['reason']}（{issue['time']}）")
                w.acknowledged.connect(lambda p=p: self._acknowledge_file(p))
                item.setText("")
                item.setSizeHint(w.sizeHint())
                self.csv_list.setItemWidget(item, w)
            elif issue:
                item.setText(item.text() + "　資料不完整（已知悉）")

    # ---------------- 後台管理 ----------------
    def _open_admin(self):
        self._admin = AdminDialog(self.cfg, self._conn_text, self)
        self._admin.port_selected.connect(self._switch_port)
        accepted = self._admin.exec()
        new_cfg = self._admin.cfg
        self._admin = None
        if not accepted:
            return
        if self.batch is not None and QMessageBox.question(
                self, "批次進行中", "套用新設定會結束目前批次（CSV 與錄影會先儲存），確定套用？") \
                != QMessageBox.StandardButton.Yes:
            return
        self._apply_config(new_cfg)

    def _switch_port(self, host: str, port: int):
        """後台點選埠號：立即切換並保存，不中斷批次。"""
        self.cfg.plc.host, self.cfg.plc.port = host, port
        save_config(self.cfg)
        self.poller.set_endpoint(host, port)

    def _apply_config(self, new_cfg: AppConfig):
        camera_changed = new_cfg.camera != self.cfg.camera
        self.poller.stop()       # 會結束批次並發出 batch_finished
        self.cfg = new_cfg
        save_config(self.cfg)
        self.incidents = IncidentLog(self.cfg.storage.error_log_dir)
        if camera_changed:
            self.camera.stop()
            self._start_camera()
        self._reset_table()
        self._start_poller()
        self._refresh_videos()
        self._refresh_csv()

    def closeEvent(self, e):
        if self.poller:
            self.poller.stop()
        if self.camera:
            self.camera.stop_recording()
            self.camera.stop()
        super().closeEvent(e)
