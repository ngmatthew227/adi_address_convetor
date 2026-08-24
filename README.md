# ADI Address Converter

香港 ADI（地址資料庫）地址資料轉換管線，將原始 MySQL dump 轉換為 SQLite 資料庫。

## 目錄結構

```
adi_address_convetor/
├── config.json              # 集中設定（路徑、MySQL 連線參數
├── requirements.txt
├── scripts/                # 所有 .py 腳本
│   ├── zip_to_mysql_import.py    # ① 解壓 + 匯入 MySQL
│   ├── mysql_to_sqlite_sync.py     # ② 三語轉換 + 寫入 SQLite + 驗證
│   └── md_to_street_json.py     # ③ 街道名表轉換
├── data/
│   ├── raw/                    # 原始輸入（zip、md、xls）
│   ├── reference/              # 參考表（street_names.json、sub_district_map*.sql）
│   └── output/                 # 產出（*.sqlite、*.json）
└── docs/                       # 文件
```

## 執行流程

```bash
# 1. 匯入 MySQL（需先啟動 MySQL）
python scripts/zip_to_mysql_import.py

# 2. 轉換並寫入 SQLite
python scripts/mysql_to_sqlite_sync.py

# 3.（可選）重建街道名表
python scripts/md_to_street_json.py
```

所有腳本從專案根目錄執行即可，路徑會自動解析為相對於根目錄的絕對路徑。