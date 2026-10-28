"""系統設定：PLC、相機、儲存路徑與顯示欄位，以 JSON 檔保存。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_DIR = Path.home() / "NextronicsPLC"
CONFIG_PATH = APP_DIR / "config.json"


@dataclass
class FieldDef:
    """一個顯示欄位：從 start 起的暫存器解碼成人類可讀的值。"""
    name: str
    start: str = "D0"
    kind: str = "number"         # number / ascii / bcd
    int_type: str = "int16"      # number 用：int16 / int32
    decimals: int = 0            # number 用：值 × 10^-decimals
    chars: int = 8               # ascii 用：最大字元數
    allow_letters: bool = True   # ascii 用：允許英文字母（不分大小寫）
    allow_digits: bool = True
    allow_symbols: bool = True   # 全部可見 ASCII 符號（含空白）
    digits: int = 4              # bcd 用：位數

    def register_count(self) -> int:
        if self.kind == "number":
            return 2 if self.int_type == "int32" else 1
        if self.kind == "ascii":
            return max(1, (self.chars + 1) // 2)
        if self.kind == "bcd":
            return max(1, (self.digits + 3) // 4)
        raise ValueError(f"未知欄位類型：{self.kind}")


@dataclass
class PlcConfig:
    host: str = "192.168.3.250"          # iQ-F 出廠預設 IP
    protocol: str = "SLMP"
    connection: str = "ethernet"
    candidate_ports: list[int] = field(default_factory=lambda: [5000, 5001, 5002, 5003])
    port: int = 5000
    poll_ms: int = 200
    trigger: str = "M100"
    reconnect_alert_sec: int = 60        # 斷線後超過此秒數仍連不上 → GUI 警告資料儲存不完全


@dataclass
class CameraConfig:
    index: int = 0                       # 0 = 電腦內建相機
    video_format: str = "mp4"            # mp4 / avi
    fps: int = 30


@dataclass
class StorageConfig:
    video_dir: str = str(APP_DIR / "videos")
    csv_dir: str = str(APP_DIR / "csv")
    error_log_dir: str = str(APP_DIR / "ErrorLog")


def default_fields() -> list[FieldDef]:
    return [
        FieldDef("計數", "D100"),
        FieldDef("溫度(°C)", "D101", decimals=1),
        FieldDef("累計值", "D102", int_type="int32"),
        FieldDef("批號", "D110", kind="ascii", chars=8),
        FieldDef("序號", "D120", kind="bcd", digits=8),
    ]


@dataclass
class AppConfig:
    plc: PlcConfig = field(default_factory=PlcConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    fields: list[FieldDef] = field(default_factory=default_fields)

    @classmethod
    def from_dict(cls, d: dict) -> "AppConfig":
        return cls(
            plc=PlcConfig(**d.get("plc", {})),
            camera=CameraConfig(**d.get("camera", {})),
            storage=StorageConfig(**d.get("storage", {})),
            fields=[FieldDef(**f) for f in d.get("fields", [])] if "fields" in d else default_fields(),
        )

    def to_dict(self) -> dict:
        return asdict(self)


def load_config(path: Path = CONFIG_PATH) -> AppConfig:
    if path.exists():
        return AppConfig.from_dict(json.loads(path.read_text(encoding="utf-8")))
    return AppConfig()


def save_config(cfg: AppConfig, path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
