# Nextronics PLC 資料擷取與錄影

M100=1 開始：輪詢三菱 PLC（SLMP）暫存器寫入 CSV，同時相機錄影；M100=0 結束並儲存。
每個批次產生同名的 `batch_YYYYMMDD_HHMMSS.csv` 與 `.mp4`。

## 執行

```bash
uv run nextronics-plc-ver5
```

設定檔：`~/NextronicsPLC/config.json`（可在 GUI「後台管理」修改）。

## 無實機測試（模擬 PLC）

```bash
uv run python -m nextronics_plc_ver5.plc.simulator --port 5000
```

模擬器在 127.0.0.1:5000 以 10 秒 ON／5 秒 OFF 切換 M100，並更新預設欄位資料。
後台管理把網址設為 `127.0.0.1`、埠選 5000 即可。

## 測試

```bash
uv run pytest
```

## PLC 端設定（iQ-F）
在 GX Works3 的乙太網路埠設定中新增 SLMP 連線裝置（TCP、二進位碼），埠號需與後台的候選埠一致。
