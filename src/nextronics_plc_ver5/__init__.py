"""Nextronics PLC 資料擷取與錄影系統。

模組分層（下層不依賴上層）：
- plc/        SLMP 通訊、元件位址、模擬 PLC
- config / decoding / storage   設定、欄位解碼、批次檔案
- acquisition / camera          輪詢與錄影執行緒（Qt 訊號對外）
- gui/        主畫面與後台管理，串接上述執行緒
"""


def main() -> None:
    import sys

    from PySide6.QtWidgets import QApplication

    from .config import config_path, load_config, save_config
    from .gui.main_window import MainWindow

    app = QApplication(sys.argv)
    cfg = load_config()
    if not config_path().exists():
        save_config(cfg)
    win = MainWindow(cfg)
    win.show()
    sys.exit(app.exec())
