"""PLC 斷線事件紀錄：ErrorLog 文字檔 + 資料不完整檔案登錄（含使用者「已知悉」狀態）。

ErrorLog 資料夾內：
- error_YYYYMMDD.log  每日一檔，逐行附時間戳記
- issues.json         資料不完整的 CSV／錄影檔與是否已知悉，GUI 重啟後仍保留未確認的提示
輪詢執行緒（寫入）與 GUI（確認）會同時使用，所有操作以鎖保護。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

ISSUES_FILE = "issues.json"


class IncidentLog:
    def __init__(self, log_dir: str | Path):
        self.dir = Path(log_dir)
        self._lock = threading.Lock()
        self._issues: dict[str, dict] = {}   # 檔案絕對路徑 → {reason, time, acknowledged}
        p = self.dir / ISSUES_FILE
        if p.exists():
            try:
                self._issues = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._issues = {}

    def log(self, message: str, now: datetime | None = None) -> None:
        now = now or datetime.now()
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            with open(self.dir / f"error_{now:%Y%m%d}.log", "a", encoding="utf-8") as f:
                f.write(f"{now:%Y-%m-%d %H:%M:%S} {message}\n")

    def flag(self, paths: list[Path], reason: str, now: datetime | None = None) -> None:
        """標示檔案資料不完整；已標示過的檔案保留原紀錄，但重新變為未知悉。"""
        now = now or datetime.now()
        with self._lock:
            for p in paths:
                key = str(Path(p).resolve())
                entry = self._issues.setdefault(key, {"reason": reason, "time": f"{now:%Y-%m-%d %H:%M:%S}"})
                entry["acknowledged"] = False
            self._save()

    def acknowledge(self, path: Path) -> None:
        with self._lock:
            entry = self._issues.get(str(Path(path).resolve()))
            if entry is None or entry["acknowledged"]:
                return
            entry["acknowledged"] = True
            self._save()
        self.log(f"使用者已知悉資料不完整：{path}")

    def status(self, path: Path) -> dict | None:
        with self._lock:
            entry = self._issues.get(str(Path(path).resolve()))
            return dict(entry) if entry else None

    def pending(self) -> list[Path]:
        """尚未知悉的問題檔案。"""
        with self._lock:
            return [Path(k) for k, v in self._issues.items() if not v["acknowledged"]]

    def _save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / ISSUES_FILE).write_text(json.dumps(self._issues, ensure_ascii=False, indent=2), encoding="utf-8")
