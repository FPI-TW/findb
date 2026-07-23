# FinDB 標的查詢頁面 — 技術規格

> **版本**: 1.2 | **日期**: 2026-04-24 | **交付對象**: Codex

---

## 1. 目標

在 FinDB FastAPI 專案內新增一個**獨立靜態 HTML 頁面**，讓使用者可以瀏覽資料庫中所有標的（instruments）的市場、名稱、Symbol / Ticker，並透過篩選與搜尋快速找到目標。資料透過後端定期產生的靜態 JSON 檔供頁面載入，無需前端即時呼叫 API。

---

## 2. 架構概述

```
┌─────────────────────────────────────────────┐
│  FastAPI 專案                                │
│                                             │
│  backend/scripts/generate_instrument_cache.py       │
│       │  定期執行（cron / 手動）              │
│       │  呼叫 GET /api/v1/serve/instruments  │
│       │  遍歷所有分頁                         │
│       ▼                                     │
│  backend/app/static/data/instruments.json           │
│       │                                     │
│       ▼                                     │
│  backend/app/static/instrument-lookup.html          │
│       （由 /instrument-lookup 與 /static/*   │
│        提供頁面/靜態資源）                   │
└─────────────────────────────────────────────┘
```

---

## 3. 交付物清單

| # | 檔案路徑 | 說明 |
|---|---------|------|
| 1 | `backend/scripts/generate_instrument_cache.py` | 靜態 JSON 產生腳本 |
| 2 | `backend/app/static/data/instruments.json` | 標的快取資料（腳本產出物，不進 git） |
| 3 | `backend/app/static/data/macro-series.json` | 宏觀序列快取資料（腳本產出物，不進 git） |
| 4 | `backend/app/static/instrument-lookup.html` | 獨立查詢頁面（單檔 HTML，含 CSS/JS） |
| 5 | FastAPI `StaticFiles` mount 設定 | 確保 `/static/` 路徑可存取 |

---

## 4. 靜態 JSON 產生腳本

### 4.1 檔案：`backend/scripts/generate_instrument_cache.py`

**功能**：呼叫 Serve API 的 `GET /api/v1/serve/instruments` 與 `GET /api/v1/serve/macro/series`，遍歷所有分頁後產出 `backend/app/static/data/instruments.json` 與 `backend/app/static/data/macro-series.json`。

**執行方式**：

```bash
uv --directory backend run python scripts/generate_instrument_cache.py
```

**環境變數**（皆有預設值）：

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `FINDB_STATIC_CACHE_BASE_URL` | `http://localhost:8080` | 靜態快取產生腳本呼叫 FinDB API 的 base URL |
| `FINDB_STATIC_CACHE_SERVE_API_KEY` | （空） | 若 Serve API 啟用認證則需設定 |

**邏輯**：

1. 以 `page_size=1000` 呼叫 `GET /api/v1/serve/instruments`，遍歷所有頁。
2. 每筆 instrument 僅保留以下欄位（減少檔案體積）：
   - `instrument_id`
   - `market`
   - `asset_class`
   - `symbol`
   - `name`
   - `currency`
   - `status`
   - `latest_trade_date`
   - `latest_price`
3. 產出 JSON 結構：

```json
{
  "generated_at": "2026-04-09T10:00:00Z",
  "total": 150,
  "markets": ["CRYPTO", "US", "FX", "TW", "HK", "CN", "GLOBAL"],
  "asset_classes": ["crypto", "equity", "index", "fx"],
  "data": [
    {
      "instrument_id": "...",
      "market": "CRYPTO",
      "asset_class": "crypto",
      "symbol": "BTC",
      "name": "Bitcoin",
      "currency": "USD",
      "status": "active",
      "latest_trade_date": "2026-04-09",
      "latest_price": "95709.01000000"
    }
  ]
}
```

4. `markets` 和 `asset_classes` 從實際資料中 distinct 提取，排序後寫入，供前端動態生成篩選選項。
5. 寫入 `backend/app/static/data/instruments.json` 與 `backend/app/static/data/macro-series.json`，若目錄不存在則自動建立。
6. 成功時 stdout 印出摘要（總數、各市場數量），失敗時 exit code 1 並印出錯誤。

**定期排程建議**（寫在腳本 docstring 與 README）：

```bash
# crontab 範例：每天 UTC 06:00 執行
0 6 * * * cd /app && python scripts/generate_instrument_cache.py
```

---

## 5. 靜態查詢頁面

### 5.1 檔案：`backend/app/static/instrument-lookup.html`

單檔 HTML（CSS + JS 內嵌），無外部框架依賴。視覺風格與現有 `api_tester.html` 保持一致（使用相同 CSS 變數與字體）。

### 5.2 頁面 Layout

```
┌──────────────────────────────────────────────────┐
│  FinDB — 標的查詢                                 │
│  資料更新時間：2026-04-09 10:00 UTC | 共 150 筆    │
├──────────────────────────────────────────────────┤
│  [🔍 搜尋名稱或代碼...]                            │
│  市場：[全部] [CRYPTO] [US] [FX] [TW] [HK] [CN]  │
│  類別：[全部] [equity] [crypto] [index] [fx]      │
│  狀態：[全部] [active] [delisted]                  │
├──────────────────────────────────────────────────┤
│  Market  │ Symbol │ Name     │ Asset Class │ CCY  │
│  ────────┼────────┼──────────┼─────────────┼───── │
│  CRYPTO  │ BTC    │ Bitcoin  │ crypto      │ USD  │
│  CRYPTO  │ ETH    │ Ethereum │ crypto      │ USD  │
│  US      │ AAPL   │ Apple ..│ equity      │ USD  │
│  ...     │        │          │             │      │
├──────────────────────────────────────────────────┤
│  顯示 1–50 / 共 150 筆                            │
└──────────────────────────────────────────────────┘
```

### 5.3 功能需求

| # | 功能 | 說明 |
|---|------|------|
| F1 | **資料載入** | 頁面載入時 fetch `./data/instruments.json`，失敗時顯示錯誤提示 |
| F2 | **關鍵字搜尋** | 即時篩選（input 事件），比對 `symbol` 和 `name` 欄位，不區分大小寫 |
| F3 | **市場篩選** | 按鈕組（pill / tag 形式），從 JSON 的 `markets` 動態生成，支援「全部」 |
| F4 | **資產類別篩選** | 按鈕組，從 JSON 的 `asset_classes` 動態生成，支援「全部」 |
| F5 | **狀態篩選** | 按鈕組：全部 / active / delisted，預設 active |
| F6 | **表格排序** | 點擊表頭可依該欄位升/降冪排序（預設依 market → symbol 排序） |
| F7 | **分頁** | 前端分頁，每頁 50 筆，顯示頁碼導航 |
| F8 | **計數顯示** | 篩選後即時更新「顯示 X–Y / 共 Z 筆」 |
| F9 | **最新價格檢核** | 金融商品表格顯示每個標的最新 `latest_trade_date` 與 `latest_price`，方便確認資料是否更新到最新 |
| F10 | **更新時間** | 頁面頂部顯示 `generated_at` 時間 |
| F11 | **RWD** | 基本響應式，手機可橫滑表格 |

### 5.4 視覺規範 — 日系簡約風格

設計方向：和紙質感、大量留白、低對比度配色、細線分隔、克制的裝飾。整體氛圍接近無印良品 / 日本銀行報表的素雅感。

#### 色彩系統

```css
:root {
  /* 背景 — 和紙層次 */
  --bg:          #F5F3EE;   /* 主背景：生成色（きなり） */
  --bg-card:     #FDFCFA;   /* 卡片/表格背景：白磁 */
  --bg-hover:    #F0EDE6;   /* hover 行 */
  --bg-subtle:   #FAF8F5;   /* 斑馬紋偶數列 */

  /* 文字 */
  --text-1:      #2C2825;   /* 主文字：墨色 */
  --text-2:      #8A847B;   /* 次要說明 */
  --text-3:      #B5AFA6;   /* placeholder / 禁用 */

  /* 強調 — 朱色（しゅいろ）系 */
  --accent:      #B84C3A;   /* 朱色，用於選中狀態 */
  --accent-soft: #F5EAE7;   /* 朱色淡底 */

  /* 線條 */
  --border:      #E2DED6;   /* 主要分隔線 */
  --border-light:#EDEAE4;   /* 次要分隔線 */
}
```

#### 字型

```css
font-family: 'Noto Sans JP', 'Hiragino Kaku Gothic ProN', sans-serif;
```

- 頁面標題：`Noto Serif JP` weight 300，letter-spacing 0.15em
- 表格/篩選/正文：`Noto Sans JP` weight 300–400
- 代碼（Symbol）：`JetBrains Mono` weight 400，letter-spacing 0.05em

Google Fonts 載入（與 api_tester.html 共用）：

```html
<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+JP:wght@200;300&family=Noto+Sans+JP:wght@300;400;500&family=JetBrains+Mono:wght@400&display=swap" rel="stylesheet">
```

#### 排版與間距

- 頁面最大寬度：`960px`，居中，左右 padding `24px`
- 區塊間距：`32px`
- 表格行高：`48px`
- 篩選按鈕間距：`8px`
- 整體追求「呼吸感」——元素之間留足空白，不擁擠

#### 元件風格

**搜尋框**：
- 無外框，僅底部 1px `--border` 線
- 左側淡色搜尋 icon（SVG inline）
- placeholder 文字：「標的名稱、代碼…」
- focus 時底線變為 `--accent`，transition 0.2s

**篩選按鈕組**：
- 標籤在按鈕組左側，`--text-2` 色，weight 400
- 按鈕：無邊框，padding `6px 16px`，border-radius `2px`
- 未選中：`--text-2` 文字，透明背景
- 選中：`--accent` 文字，`--accent-soft` 背景
- hover（未選中）：`--bg-hover` 背景
- 過渡：`transition: all 0.15s ease`

**表格**：
- 無外框，無縱向分隔線
- 表頭：`--text-2` 色、font-weight 400、底部 1px `--border` 線、字號略小（0.8rem）、letter-spacing 0.08em、全大寫
- 資料列：底部 1px `--border-light`，hover 時背景 `--bg-hover`
- Symbol 欄位：`JetBrains Mono`，`--accent` 色，font-weight 400
- 可排序欄位：表頭右側小三角 indicator，hover 時顯示

**分頁**：
- 極簡：「← 前 / 後 →」文字按鈕 + 中間頁碼
- 當前頁碼：`--accent` 色底線
- 非當前頁碼：`--text-2`

**狀態標籤**（active / delisted）：
- active：不顯示標籤（預設態）
- delisted：小字灰色標籤 `--text-3`，帶刪除線

**頁面 header**：
- 標題：Noto Serif JP weight 200，1.8rem
- 副標題（更新時間 + 總數）：`--text-2`，0.85rem
- 標題與副標題間距 `8px`
- 下方 1px `--border` 分隔線，margin-bottom `32px`

#### 設計參考意象

> 想像一份在京都老書店翻到的金融資料索引——素雅的紙面、最少的裝飾線、
> 工整但不冷硬的排版、朱色印章般的點綴色。

---

## 6. FastAPI 靜態檔案掛載

在 `main.py`（或 app 初始化處）確認已掛載 static 目錄：

```python
from fastapi.staticfiles import StaticFiles

app.mount("/static", StaticFiles(directory="app/static"), name="static")
```

掛載後頁面可透過 `http://localhost:8080/instrument-lookup` 存取；`/static/instrument-lookup.html` 也會保留為靜態檔直連路徑。

若 `static/` mount 已存在，不需重複新增，僅確認路徑正確。

---

## 7. 檔案結構變更

```
findb/
├── backend/scripts/
│   ├── generate_instrument_cache.py   ← 新增
│   └── ...
├── backend/app/static/
│   ├── data/
│   │   ├── instruments.json           ← 腳本產出（加入 .gitignore）
│   │   └── macro-series.json          ← 腳本產出（加入 .gitignore）
│   └── instrument-lookup.html         ← 新增
├── backend/app/main.py                ← 確認 StaticFiles mount
└── ...
```

`.gitignore` 新增：

```
backend/app/static/data/instruments.json
backend/app/static/data/macro-series.json
```

---

## 8. 實作注意事項

1. **腳本容錯**：API 連不上或回傳錯誤時，不應覆蓋既有的 `instruments.json`（先寫入 temp file 再 atomic rename）。
2. **大量資料**：目前預估標的數百筆，JSON 不超過 100KB。若未來超過 5000 筆再考慮拆分或 lazy load。
3. **無外部依賴**：HTML 頁面除了 Google Fonts（Noto Sans JP / JetBrains Mono）外不引入任何外部 JS 框架。
4. **腳本依賴**：使用 `httpx`（專案已有）或 stdlib `urllib`，不新增 pip 依賴。
5. **Ticker 欄位說明**：目前 instruments API 回傳的 `symbol` 即為使用者查詢時需要的代碼（如 `BTC`、`AAPL`、`2330`）。Bloomberg ticker（如 `XBTUSD BGN Curncy`）儲存在 EOD 資料層，不在 instruments 回傳中，查詢頁面以 `symbol` 為主要顯示欄位。

---

## 9. 驗收標準

| # | 項目 | 通過條件 |
|---|------|---------|
| A1 | 腳本執行 | `uv --directory backend run python scripts/generate_instrument_cache.py` 成功產出 `instruments.json` 與 `macro-series.json` |
| A2 | JSON 格式 | 包含 `generated_at`、`total`、`markets`、`asset_classes`、`data` 欄位 |
| A3 | 頁面載入 | 瀏覽器開啟 `/static/instrument-lookup.html` 可正常顯示表格 |
| A4 | 搜尋功能 | 輸入 "bit" 可篩出 Bitcoin，輸入 "AAPL" 可篩出 Apple |
| A5 | 市場篩選 | 點選 CRYPTO 只顯示加密貨幣標的 |
| A6 | 類別篩選 | 點選 equity 只顯示股票類標的 |
| A7 | 組合篩選 | 搜尋 + 市場 + 類別可同時作用 |
| A8 | 排序 | 點擊 Symbol 欄位可切換升降冪 |
| A9 | 分頁 | 超過 50 筆時出現分頁導航 |
| A10 | 容錯 | JSON 載入失敗時顯示友善錯誤提示 |
