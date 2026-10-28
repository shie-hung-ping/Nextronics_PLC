"""後台管理視窗：PLC、相機、儲存、顯示欄位設定。

對設定的副本編輯，按「儲存」才回寫；唯獨 TCP 埠切換會立即透過 port_selected 生效。
"""

from __future__ import annotations

import copy

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QMessageBox,
    QPushButton, QSpinBox, QStackedWidget, QTabWidget, QVBoxLayout, QWidget,
)

from ..camera import list_cameras
from ..config import AppConfig, FieldDef
from ..decoding import compute_read_blocks, field_range_text
from ..plc.devices import parse_device
from ..plc.slmp import probe_port

KIND_LABELS = {"number": "純數字", "ascii": "文字（ASCII）", "bcd": "數字串（BCD）"}


class PortProbeWorker(QThread):
    """逐一測試候選埠，每測完一個發出結果。"""
    result = Signal(int, bool, str)

    def __init__(self, host: str, ports: list[int], trigger: str, parent=None):
        super().__init__(parent)
        self.host, self.ports, self.trigger = host, ports, trigger

    def run(self):
        dev = parse_device(self.trigger)
        for p in self.ports:
            r = probe_port(self.host, p, dev)
            self.result.emit(r.port, r.ok, r.message)


class AdminDialog(QDialog):
    port_selected = Signal(str, int)   # host, port：立即切換連線

    def __init__(self, cfg: AppConfig, connection_text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("後台管理")
        self.resize(760, 560)
        self.cfg = copy.deepcopy(cfg)
        self._probe: PortProbeWorker | None = None

        tabs = QTabWidget()
        tabs.addTab(self._build_plc_tab(connection_text), "PLC")
        tabs.addTab(self._build_camera_tab(), "相機")
        tabs.addTab(self._build_storage_tab(), "儲存")
        tabs.addTab(self._build_fields_tab(), "顯示欄位")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        lay.addWidget(buttons)
        self._refresh_range()

    # ---------------- PLC ----------------
    def _build_plc_tab(self, connection_text: str) -> QWidget:
        p = self.cfg.plc
        w = QWidget()
        form = QFormLayout()
        self.conn_label = QLabel(connection_text)
        self.conn_label.setWordWrap(True)
        form.addRow("目前連線／埠號來源：", self.conn_label)
        self.host = QLineEdit(p.host)
        form.addRow("網址（PLC IP）：", self.host)
        self.poll = QSpinBox(minimum=20, maximum=60000, singleStep=50, suffix=" ms", value=p.poll_ms)
        form.addRow("輪詢週期：", self.poll)
        proto = QComboBox()
        proto.addItem("SLMP（3E 框架，二進位）")  # 目前僅實作此協定
        form.addRow("MC 協定：", proto)
        conn = QComboBox()
        conn.addItem("乙太網路")
        form.addRow("連線方式：", conn)
        self.trigger = QLineEdit(p.trigger)
        form.addRow("開始／停止訊號：", self.trigger)

        box = QGroupBox("TCP 埠號（點選即切換使用中的埠）")
        bl = QVBoxLayout(box)
        row = QHBoxLayout()
        self.ports_edit = QLineEdit(", ".join(str(x) for x in p.candidate_ports))
        row.addWidget(QLabel("候選埠："))
        row.addWidget(self.ports_edit, 1)
        apply_btn = QPushButton("更新列表")
        apply_btn.clicked.connect(self._rebuild_port_buttons)
        test_btn = QPushButton("測試連線")
        test_btn.clicked.connect(self._probe_ports)
        row.addWidget(apply_btn)
        row.addWidget(test_btn)
        bl.addLayout(row)
        self.port_grid = QGridLayout()
        bl.addLayout(self.port_grid)
        self.port_group = QButtonGroup(self)
        self.port_group.idClicked.connect(self._select_port)
        self.port_status: dict[int, QLabel] = {}
        self._rebuild_port_buttons()

        self.range_label = QLabel()
        self.range_label.setWordWrap(True)

        lay = QVBoxLayout(w)
        lay.addLayout(form)
        lay.addWidget(box)
        lay.addWidget(QLabel("暫存器讀取範圍（由顯示欄位自動計算）："))
        lay.addWidget(self.range_label)
        lay.addStretch()
        return w

    def _parse_ports(self) -> list[int] | None:
        try:
            ports = [int(x) for x in self.ports_edit.text().replace("，", ",").split(",") if x.strip()]
        except ValueError:
            return None
        return ports if ports and all(0 < x < 65536 for x in ports) else None

    def _rebuild_port_buttons(self):
        ports = self._parse_ports()
        if ports is None:
            QMessageBox.warning(self, "格式錯誤", "候選埠請以逗號分隔，範圍 1–65535")
            return
        for b in self.port_group.buttons():
            self.port_group.removeButton(b)
        while self.port_grid.count():
            self.port_grid.takeAt(0).widget().deleteLater()
        self.port_status.clear()
        self.cfg.plc.candidate_ports = ports
        for i, port in enumerate(ports):
            btn = QPushButton(str(port), checkable=True)
            btn.setChecked(port == self.cfg.plc.port)
            self.port_group.addButton(btn, port)
            st = QLabel("未測試")
            self.port_status[port] = st
            self.port_grid.addWidget(btn, i, 0)
            self.port_grid.addWidget(st, i, 1)

    def _probe_ports(self):
        if self._probe is not None and self._probe.isRunning():
            return
        try:
            parse_device(self.trigger.text())
        except ValueError as e:
            QMessageBox.warning(self, "設定錯誤", str(e))
            return
        for st in self.port_status.values():
            st.setText("測試中…")
            st.setStyleSheet("")
        self._probe = PortProbeWorker(self.host.text().strip(), list(self.port_status), self.trigger.text(), self)
        self._probe.result.connect(self._on_probe_result)
        self._probe.start()

    def _on_probe_result(self, port: int, ok: bool, msg: str):
        st = self.port_status.get(port)
        if st:
            st.setText(("✔ " if ok else "✘ ") + msg)
            st.setStyleSheet("color:#1a7f37" if ok else "color:#c62828")

    def _select_port(self, port: int):
        self.cfg.plc.port = port
        self.port_selected.emit(self.host.text().strip(), port)

    def set_connection_text(self, text: str):
        self.conn_label.setText(text)

    # ---------------- 相機 ----------------
    def _build_camera_tab(self) -> QWidget:
        c = self.cfg.camera
        w = QWidget()
        form = QFormLayout(w)
        row = QHBoxLayout()
        self.cam = QComboBox()
        self.cam.addItem(f"相機 {c.index}" + ("（電腦內建相機）" if c.index == 0 else ""), c.index)
        detect = QPushButton("偵測相機")
        detect.clicked.connect(self._detect_cameras)
        row.addWidget(self.cam, 1)
        row.addWidget(detect)
        form.addRow("相機：", row)
        self.fmt = QComboBox()
        self.fmt.addItems(["mp4", "avi"])
        self.fmt.setCurrentText(c.video_format)
        form.addRow("錄影檔儲存格式：", self.fmt)
        self.fps = QSpinBox(minimum=1, maximum=120, value=c.fps)
        form.addRow("FPS：", self.fps)
        return w

    def _detect_cameras(self):
        cur = self.cam.currentData()
        found = list_cameras()
        self.cam.clear()
        for i in found or [cur]:
            self.cam.addItem(f"相機 {i}" + ("（電腦內建相機）" if i == 0 else ""), i)
        idx = self.cam.findData(cur)
        self.cam.setCurrentIndex(max(0, idx))
        if not found:
            QMessageBox.information(self, "偵測相機", "未偵測到可用相機")

    # ---------------- 儲存 ----------------
    def _build_storage_tab(self) -> QWidget:
        s = self.cfg.storage
        w = QWidget()
        form = QFormLayout(w)
        self.video_dir = QLineEdit(s.video_dir)
        self.csv_dir = QLineEdit(s.csv_dir)
        for label, edit in (("錄影檔儲存位址：", self.video_dir), ("CSV 檔儲存位置：", self.csv_dir)):
            row = QHBoxLayout()
            btn = QPushButton("瀏覽…")
            btn.clicked.connect(lambda _=False, e=edit: self._browse(e))
            row.addWidget(edit, 1)
            row.addWidget(btn)
            form.addRow(label, row)
        return w

    def _browse(self, edit: QLineEdit):
        d = QFileDialog.getExistingDirectory(self, "選擇資料夾", edit.text())
        if d:
            edit.setText(d)

    # ---------------- 顯示欄位 ----------------
    def _build_fields_tab(self) -> QWidget:
        w = QWidget()
        self.field_list = QListWidget()
        self.field_list.currentRowChanged.connect(self._load_field)
        btns = QHBoxLayout()
        for text, fn in (("新增", self._add_field), ("刪除", self._del_field),
                         ("上移", lambda: self._move_field(-1)), ("下移", lambda: self._move_field(1))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            btns.addWidget(b)
        left = QVBoxLayout()
        left.addWidget(QLabel("顯示順序（由上到下）："))
        left.addWidget(self.field_list, 1)
        left.addLayout(btns)

        form = QFormLayout()
        self.f_name = QLineEdit()
        self.f_start = QLineEdit()
        self.f_kind = QComboBox()
        for k, v in KIND_LABELS.items():
            self.f_kind.addItem(v, k)
        self.f_regs = QLabel()
        form.addRow("欄位名稱：", self.f_name)
        form.addRow("起始暫存器：", self.f_start)
        form.addRow("資料格式：", self.f_kind)

        self.f_stack = QStackedWidget()
        num = QWidget()
        nf = QFormLayout(num)
        self.f_int = QComboBox()
        self.f_int.addItems(["int16", "int32"])
        self.f_dec = QSpinBox(minimum=0, maximum=9)
        nf.addRow("PLC 存法：", self.f_int)
        nf.addRow("小數位 n（× 10⁻ⁿ）：", self.f_dec)
        asc = QWidget()
        af = QFormLayout(asc)
        self.f_chars = QSpinBox(minimum=1, maximum=256)
        self.f_letters = QCheckBox("英文字母（不分大小寫）")
        self.f_digits = QCheckBox("數字 0–9")
        self.f_symbols = QCheckBox("符號（全部可見 ASCII 符號）")
        af.addRow("顯示字元數：", self.f_chars)
        af.addRow("允許的字元：", self.f_letters)
        af.addRow("", self.f_digits)
        af.addRow("", self.f_symbols)
        af.addRow(QLabel("每暫存器 2 字元，讀到 0x00 即結束；不允許的字元顯示為 ?"))
        bcd = QWidget()
        bf = QFormLayout(bcd)
        self.f_bcd = QSpinBox(minimum=1, maximum=64)
        bf.addRow("位數：", self.f_bcd)
        bf.addRow(QLabel("每暫存器 4 位數，低位暫存器放低位數"))
        for page in (num, asc, bcd):
            self.f_stack.addWidget(page)
        form.addRow(self.f_stack)
        form.addRow("讀取暫存器：", self.f_regs)
        self.f_editor = QWidget()
        self.f_editor.setLayout(form)

        # 編輯任何欄位屬性即寫回目前 FieldDef
        self.f_name.textEdited.connect(self._store_field)
        self.f_start.textEdited.connect(self._store_field)
        self.f_kind.currentIndexChanged.connect(self._store_field)
        self.f_int.currentIndexChanged.connect(self._store_field)
        for sp in (self.f_dec, self.f_chars, self.f_bcd):
            sp.valueChanged.connect(self._store_field)
        for cb in (self.f_letters, self.f_digits, self.f_symbols):
            cb.toggled.connect(self._store_field)

        lay = QHBoxLayout(w)
        lay.addLayout(left, 1)
        lay.addWidget(self.f_editor, 2)
        self._loading = False
        for f in self.cfg.fields:
            self.field_list.addItem(f.name)
        self.field_list.setCurrentRow(0 if self.cfg.fields else -1)
        self.f_editor.setEnabled(bool(self.cfg.fields))
        return w

    def _load_field(self, row: int):
        self.f_editor.setEnabled(row >= 0)
        if row < 0:
            return
        f = self.cfg.fields[row]
        self._loading = True
        self.f_name.setText(f.name)
        self.f_start.setText(f.start)
        self.f_kind.setCurrentIndex(self.f_kind.findData(f.kind))
        self.f_int.setCurrentText(f.int_type)
        self.f_dec.setValue(f.decimals)
        self.f_chars.setValue(f.chars)
        self.f_letters.setChecked(f.allow_letters)
        self.f_digits.setChecked(f.allow_digits)
        self.f_symbols.setChecked(f.allow_symbols)
        self.f_bcd.setValue(f.digits)
        self._loading = False
        self.f_stack.setCurrentIndex(self.f_kind.currentIndex())
        self.f_regs.setText(field_range_text(f))

    def _store_field(self, *_):
        row = self.field_list.currentRow()
        if self._loading or row < 0:
            return
        f = self.cfg.fields[row]
        f.name = self.f_name.text()
        f.start = self.f_start.text().strip().upper()
        f.kind = self.f_kind.currentData()
        f.int_type = self.f_int.currentText()
        f.decimals = self.f_dec.value()
        f.chars = self.f_chars.value()
        f.allow_letters = self.f_letters.isChecked()
        f.allow_digits = self.f_digits.isChecked()
        f.allow_symbols = self.f_symbols.isChecked()
        f.digits = self.f_bcd.value()
        self.f_stack.setCurrentIndex(self.f_kind.currentIndex())
        self.field_list.item(row).setText(f.name)
        self.f_regs.setText(field_range_text(f))
        self._refresh_range()

    def _add_field(self):
        self.cfg.fields.append(FieldDef(f"欄位{len(self.cfg.fields) + 1}"))
        self.field_list.addItem(self.cfg.fields[-1].name)
        self.field_list.setCurrentRow(len(self.cfg.fields) - 1)
        self._refresh_range()

    def _del_field(self):
        row = self.field_list.currentRow()
        if row < 0:
            return
        del self.cfg.fields[row]
        self.field_list.takeItem(row)
        self._refresh_range()

    def _move_field(self, step: int):
        row = self.field_list.currentRow()
        new = row + step
        if row < 0 or not 0 <= new < len(self.cfg.fields):
            return
        fs = self.cfg.fields
        fs[row], fs[new] = fs[new], fs[row]
        self.field_list.insertItem(new, self.field_list.takeItem(row))
        self.field_list.setCurrentRow(new)

    def _refresh_range(self):
        if not hasattr(self, "range_label"):
            return
        try:
            blocks = compute_read_blocks(self.cfg.fields)
            self.range_label.setText("、".join(str(b) for b in blocks) or "（無欄位）")
            self.range_label.setStyleSheet("")
        except ValueError as e:
            self.range_label.setText(str(e))
            self.range_label.setStyleSheet("color:#c62828")

    # ---------------- 儲存 ----------------
    def _on_save(self):
        p = self.cfg.plc
        p.host = self.host.text().strip()
        p.poll_ms = self.poll.value()
        p.trigger = self.trigger.text().strip().upper()
        ports = self._parse_ports()
        c = self.cfg.camera
        c.index = self.cam.currentData()
        c.video_format = self.fmt.currentText()
        c.fps = self.fps.value()
        self.cfg.storage.video_dir = self.video_dir.text().strip()
        self.cfg.storage.csv_dir = self.csv_dir.text().strip()
        # 存檔前檢查，避免存入無法運作的設定
        try:
            parse_device(p.trigger)
            if ports is None:
                raise ValueError("候選埠格式錯誤")
            p.candidate_ports = ports
            if any(not f.name.strip() for f in self.cfg.fields):
                raise ValueError("欄位名稱不可空白")
            compute_read_blocks(self.cfg.fields)
            if not self.cfg.storage.video_dir or not self.cfg.storage.csv_dir:
                raise ValueError("請指定儲存位置")
        except ValueError as e:
            QMessageBox.warning(self, "設定錯誤", str(e))
            return
        self.accept()

    def done(self, r):
        if self._probe is not None:
            self._probe.wait(5000)
        super().done(r)
