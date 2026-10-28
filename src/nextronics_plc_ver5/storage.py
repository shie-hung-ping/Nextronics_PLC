"""批次檔案命名、CSV 寫入與檔案列表。

一個批次（M100=1 → M100=0）產生同名的 CSV 與錄影檔：batch_YYYYMMDD_HHMMSS.csv / .mp4。
"""

from __future__ import annotations

import csv
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

PREFIX = "batch_"
VIDEO_EXTS = (".mp4", ".avi")


@dataclass(frozen=True)
class BatchPaths:
    batch_id: str
    csv_path: Path
    video_path: Path


def new_batch(csv_dir: str | Path, video_dir: str | Path, video_ext: str, now: datetime | None = None) -> BatchPaths:
    """建立批次檔名；同一秒內重複觸發時加上 _2、_3 避免覆蓋。"""
    csv_dir, video_dir = Path(csv_dir), Path(video_dir)
    csv_dir.mkdir(parents=True, exist_ok=True)
    video_dir.mkdir(parents=True, exist_ok=True)
    base = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    bid, n = base, 1
    while (csv_dir / f"{PREFIX}{bid}.csv").exists() or (video_dir / f"{PREFIX}{bid}.{video_ext}").exists():
        n += 1
        bid = f"{base}_{n}"
    return BatchPaths(bid, csv_dir / f"{PREFIX}{bid}.csv", video_dir / f"{PREFIX}{bid}.{video_ext}")


class CsvBatchWriter:
    """每次輪詢寫入一列並立即 flush，程式異常中止時已寫入的資料仍保留。"""

    def __init__(self, path: Path, columns: list[str]):
        self.path = path
        self._f = open(path, "w", newline="", encoding="utf-8-sig")  # BOM 讓 Excel 正確顯示中文
        self._w = csv.writer(self._f)
        self._w.writerow(["時間"] + columns)
        self._f.flush()
        self.rows = 0

    def write(self, ts: datetime, values: list[str]) -> None:
        self._w.writerow([ts.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]] + values)
        self._f.flush()
        self.rows += 1

    def close(self) -> None:
        if not self._f.closed:
            self._f.close()


def recent_csv_files(csv_dir: str | Path, within_sec: float = 3600) -> list[Path]:
    """一小時內修改過的批次 CSV，新的在前。"""
    d = Path(csv_dir)
    if not d.is_dir():
        return []
    cutoff = time.time() - within_sec
    files = [p for p in d.glob(f"{PREFIX}*.csv") if p.stat().st_mtime >= cutoff]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def video_files(video_dir: str | Path) -> list[Path]:
    d = Path(video_dir)
    if not d.is_dir():
        return []
    files = [p for p in d.iterdir() if p.name.startswith(PREFIX) and p.suffix.lower() in VIDEO_EXTS]
    return sorted(files, key=lambda p: p.name, reverse=True)
