"""斷線相關提示元件：需按「已知悉」才關閉的警告視窗、右下角重新連線提醒、問題檔案列。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget

ACK_TEXT = "已知悉"


def ack_dialog(parent: QWidget, title: str, text: str) -> QMessageBox:
    """非強制回應的警告視窗：不擋住主畫面，只有按「已知悉」才會關閉（停用標題列關閉鈕與 Esc）。"""
    box = QMessageBox(QMessageBox.Icon.Warning, title, text, QMessageBox.StandardButton.NoButton, parent)
    box.addButton(ACK_TEXT, QMessageBox.ButtonRole.AcceptRole)
    box.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
    box.setEscapeButton(None)
    box.setWindowModality(Qt.WindowModality.NonModal)
    box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    box.show()
    return box


class ReconnectToast(QFrame):
    """主畫面右下角的提醒：累積每次「斷線後已自動重新連線」，按「已知悉」才清除並隱藏。"""
    acknowledged = Signal(list)   # 被確認的訊息

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setStyleSheet("ReconnectToast{background:#fff8e1;border:2px solid #f9a825;border-radius:6px}")
        self._messages: list[str] = []
        self._label = QLabel()
        self._label.setWordWrap(True)
        btn = QPushButton(ACK_TEXT)
        btn.clicked.connect(self._ack)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("<b>PLC 斷線提醒</b>"))
        lay.addWidget(self._label)
        lay.addWidget(btn, 0, Qt.AlignmentFlag.AlignRight)
        self.setFixedWidth(340)
        self.hide()

    def add(self, message: str) -> None:
        self._messages.append(message)
        self._label.setText("\n".join(self._messages))
        self.adjustSize()
        self.reposition()
        self.show()
        self.raise_()

    def reposition(self) -> None:
        p = self.parentWidget()
        self.move(p.width() - self.width() - 16, p.height() - self.height() - 36)  # 避開狀態列

    def _ack(self):
        msgs, self._messages = self._messages, []
        self.hide()
        self.acknowledged.emit(msgs)


class IssueItemWidget(QFrame):
    """清單中資料不完整的檔案：紅框（錄影）或紅字（CSV）＋「我已知悉問題」按鈕。"""
    acknowledged = Signal()

    def __init__(self, text: str, pixmap: QPixmap | None = None, red_frame: bool = False, parent=None):
        super().__init__(parent)
        self.setObjectName("issue")
        if red_frame:
            self.setStyleSheet("#issue{border:3px solid #d32f2f}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        if pixmap is not None:
            pic = QLabel()
            pic.setPixmap(pixmap)
            lay.addWidget(pic)
        label = QLabel(text)
        label.setWordWrap(True)
        if not red_frame:
            label.setStyleSheet("color:#d32f2f;font-weight:bold")
        btn = QPushButton("我已知悉問題")
        btn.clicked.connect(self.acknowledged)
        # 按鈕放在文字下方，避免清單較窄時被擠出可視範圍
        col = QVBoxLayout()
        col.addWidget(label)
        col.addWidget(btn, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addLayout(col, 1)
