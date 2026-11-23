# FinDB API 使用指南

> 精確request/response model以部署版本的`/docs`、`/openapi.json`與Source contract
> endpoint為準。本文件只維護穩定的使用規則。

## API families與認證

```text
/api/v1/source   Contract ingest、delivery status與scheduler control
/api/v1/serve    Canonical唯讀查詢
/api/v1/admin    營運、credential、修正與治理
/health          Process liveness
```

Machine API使用：

```http
X-API-Key: <key>
```

Dashboard human session使用：

```http
Authorization: Bearer <admin-session>
```

| API | 認證與權限 |
| --- | --- |
| Source | 必須使用DB-backed Source client key，受`source_name`、datasets與rate limit限制 |
| Serve | 由`SERVE_REQUIRE_AUTH`控制；有帶 key 時一律驗證並套用該 key 的限制；即使開放仍保持唯讀 |
| Admin | 永遠需要具名session、DB-backed machine key或break-glass recovery key |

Rate limit 與 security event 依 client IP 分桶。Admin 登入有兩層限制，window 都是
`ADMIN_LOGIN_RATE_LIMIT_WINDOW`（預設 300 秒）：

- 「client IP + username」：`ADMIN_LOGIN_RATE_LIMIT_REQUESTS`，預設 5 次。
- 同一個 client IP 的所有 username 合計：`ADMIN_LOGIN_IP_RATE_LIMIT_REQUESTS`，預設 20 次。
  只換 username 也無法無限嘗試，或無限建立新的桶。

`POST /api/v1/admin/auth/change-password` 以 session 計算舊密碼錯誤次數。同一個 session 累計
`ADMIN_PASSWORD_CHANGE_MAX_FAILURES`（預設 5）次後，該 session 會被撤銷並回 401，必須重新登入；
同一使用者的其他 session 各自計算。每次錯誤都會寫入 `change_password_failed` admin audit
（`resource_type=admin_session`），次數由 audit 計算，所以各 worker 共用、restart 也不會歸零。
撤銷時另寫 `admin_session_revoked` security event，不記錄輸入的密碼。

Security event 的 log 是單行，欄位直接寫在訊息中，不依賴 log formatter：

- `API credential rejected credential_kind=... endpoint='...' client_ip='...'`
- `Admin session revoked reason=... session_id=... endpoint='...' client_ip='...'`

來自請求的 `endpoint` 與 `client_ip` 以引號輸出並跳脫控制字元，偽造的換行不能產生假的 log 行。
任何 log 都不包含輸入的 key 或密碼。

Source、Serve 與登入的限流狀態都存在各 process 的記憶體中，restart 後重置。每個 limiter 只保留
window 內的時間戳；過期的身分會定期清掉，追蹤數量超過 `RATE_LIMIT_MAX_TRACKED_KEYS`
（預設 10,000）時，最久未使用的身分會先被淘汰。所以一次性的訪客 IP 或輪換的 username
不會讓記憶體無限成長。代價是：來自大量不同 IP 的請求擠滿容量時，被淘汰身分的計數會重新開始。

Client IP 的信任邊界如下：

- 只有 nginx 對外開 port。nginx 對 `/api/v1/*` 與 `/dashboard/` 一律以 Cloudflare 驗證後的
  `$remote_addr` 覆寫 `X-Real-IP`／`X-Forwarded-For`，瀏覽器自帶的值不會原樣傳入。
- Dashboard 以 server-side fetch 直連 ingest，登入時只轉送 nginx 寫入的 `X-Real-IP`
  （必須是單一 IP literal），作為 `X-Forwarded-For`。
- Backend 只在 `SOURCE_TRUST_PROXY_HEADERS=true` 時採用 `X-Forwarded-For` 的第一個值。
  部署環境必須設為 `true`；否則所有經 nginx 與 Dashboard 的請求都會合併成 proxy container 的 IP，
  任何人對某個 username 失敗 5 次，就會讓所有人在 window 內無法登入該帳號。
- ingest 不對外開 port；直連 ingest 不在這個信任邊界內。

每個 provider/client 使用獨立 Source key；共用 Fetcher runtime 從 Source 取得有效執行政策，
不再注入 calendar Serve key。`ADMIN_BREAK_GLASS_API_KEY` 只供 bootstrap 與緊急復原。

## Daily 排程設定發布

```http
GET /api/v1/source/execution-config?execution_profile=full_market&schema_version=1
X-API-Key: <source-client-key>
```

`execution_profile` 必填且只接受 `full_market`（已退役的 `pilot` 回 422），`schema_version` 預設 1。
Provider 與 feeds 依 credential 的非空合法 scope 決定，不接受 provider／environment override。
回應為完整快照，含環境、profile、schema version、schedule digest、逐 feed 當地觸發時間與時區，
另列 completion deadline、bounded monitor 及發布重試窗口；`Cache-Control: no-store`。
沒有／無效 credential 回 401／403，無效 scope 回 403，不支援的查詢參數回 422。
此 API 不授予抓取權限，不改 controls、first dates、plans 或 historical lease。
設定欄位、digest 與 Admin 逐 feed 投影見 [provider metadata](../architecture/provider_metadata.md#backend-排程發布)。

## Canonical ingest

```http
POST /api/v1/source/ingest
Content-Type: application/json
X-API-Key: <source-client-key>
```

最小範例：

```json
{
  "dataset_key": "tw_equity_eod",
  "schema_id": "market_eod",
  "schema_version": 1,
  "source": "finlab",
  "request_key": "finlab_tw_equity_eod_20260724_01",
  "idempotency_key": "finlab_tw_equity_eod_20260724",
  "fetched_at": "2026-07-24T08:00:00Z",
  "payload": {
    "batch": {
      "data_date": "2026-07-24",
      "delivery_mode": "full_snapshot",
      "declared_record_count": 1
    },
    "data": [{"symbol": "2330", "trade_date": "2026-07-24", "close": "1140.00"}]
  }
}
```

成功回`202`及`attempt_id`、`run_id`、schema version與初始status。`202`只代表durable
accept，不代表canonical完成；producer必須保存兩個ID並等待run進入terminal state。

完整contract見[Versioned Ingress Contract](../architecture/ingress_contracts.md)，或讀取：

```http
GET /api/v1/source/contracts/{schema_id}/versions/{schema_version}
```

## Delivery status、重試與rerun

```http
GET  /api/v1/source/attempts/{attempt_id}
GET  /api/v1/source/runs/{run_id}
POST /api/v1/source/runs/{run_id}/rerun
GET  /api/v1/source/datasets
```

- Attempt記錄通過auth／rate-limit gate的呼叫，包括contract rejection。
- Run代表已durable accept並排入normalization的工作。
- Rerun從仍保存的raw建立新run，不修改原run，也不算新的delivery arrival。
- 已認證 Admin 可使用 `POST /api/v1/admin/runs/{run_id}/rerun` 或既有
  `POST /api/v1/admin/runs/bulk-rerun`，每個新 run 保留原 raw 與 lineage，並原子記錄具名 audit。
  啟用 provider work 時先排入同 provider FIFO，取得自己的 fence 後才建立 normalization job；
  不會重設既有 failed run 或改寫舊 receipt。
- 冪等範圍是已認證`source_client_id + dataset_key + idempotency_key`。
- 同一範圍內的canonical payload、source及schema/version皆相同時回既有run；任一項不同
  時回`409 IDEMPOTENCY_PAYLOAD_MISMATCH`。
- Credential綁定的source與request不符時，會先回`403 SOURCE_IDENTITY_MISMATCH`。
- `request_key`、`fetched_at`與`delivery` metadata不參與內容衝突比對。
- Raw retention清除對應payload後不再保證key可去重；Source key輪替產生的新client identity
  也屬新的冪等範圍。
- Timeout、429、502、503、504重試時沿用相同key並遵守`Retry-After`。

不要只看HTTP `202`判斷資料完成，也不要因client timeout自行生成新key。

## Scheduler與calendar

Fetcher以provider-scoped Source key輪詢並回報heartbeat：

```http
POST /api/v1/source/scheduler-controls/{scheduler_key}/poll
```

回應包含provider、dataset mapping、slot、本地時間、timezone、desired state與revision。
Scope或reviewed workload不一致時consumer必須fail closed。

每次 acquisition 的最後授權使用 protected 唯讀 probes；poll／heartbeat 的 cached desired state
不能替代最後檢查：

```http
GET  /api/v1/source/scheduler-controls/{scheduler_key}/authorization
GET  /api/v1/source/full-market/authorization?consumer=full_market
GET  /api/v1/source/full-market/authorization?consumer=maintenance
POST /api/v1/source/historical-backfills/{item_id}/authorization
Content-Type: application/json

{"lease_token":"<current-item-lease-token>"}
```

Scheduler probe 綁定精確 scheduler key、provider 與完整 dataset scope；Full probe 另遵守 running
admission、每日窗口與帳號配額，maintenance 只依 Source credential 綁定的 scopes。`consumer` 只接受
`full_market`／`maintenance`，`pilot` 回 422。
Scheduler／historical 的當次 acquisition probe 必須使用 DB-backed credential 的明確非空 dataset
清單；SQL NULL、空清單、非清單、非字串成員、重複或跨 provider dataset 一律拒絕。
每次認證重新讀取 DB scope，scheduler probe 也重新載入 scheduler associations，不能沿用先前允許的清單。
Historical probe 綁定 request／item／provider／dataset、current lease token、expiry 與 cancellation；
不續租、不更改 request／item/control、heartbeat、revision 或 audit。Scope 不符拒絕，item 不存在或
不可見回 404；已取消、lease 過期或 token 已改的可見 item 回 `acquisition_allowed=false`。

成功回應包含 `acquisition_allowed`、`provider`、UTC `checked_at`、`max_age_seconds=5`；
Scheduler probe 包含 `scheduler_key`／`dataset_keys`，historical 包含 `item_id`／`request_id`／`dataset_key`
及允許時的 `lease_expires_at`。回應 header `X-FinDB-Acquisition-Checked-At` 與 body timestamp 相符，
並使用 `Cache-Control: no-store`。Fetcher 要求 server check／RTT 小於 5 秒、header receipt 起
最多 50ms 開始 provider invocation，且不能越過 current minute 或 lease expiry；過期須重新授權。
此 freshness contract 不保證遠端 stop 與 provider invocation 原子執行。

Admin `GET /api/v1/admin/schedulers` 與 Owner `PATCH /api/v1/admin/schedulers/{scheduler_key}`
回應另包含 `start_allowed: boolean` 與 `start_blockers: string[]`。全市場啟動會在同一
transaction 鎖定 registry／control 並重新驗證資格；資格不符回 `409`，detail 為
`{"code":"scheduler_start_blocked","start_blockers":["full_market_no_enabled_datasets"]}`
（原因依未完成條件而異），不變更 desired state、revision 或 audit。既有 revision conflict
仍回 `409`；停止永遠保留既有 Owner 權限／revision 檢查，但不要求啟動資格。
Admin 回應也包含 `acquisition_allowed`／`acquisition_blockers`、`expected_cycle_interval_seconds`
（Dashboard 判讀心跳與 cycle 的 poll 間隔）及 `running_since`（目前 running revision 的
admission 時間，停止時為 `null`）；Source scheduler poll 回應形狀不變。

獨立查詢有效年度日曆：

```http
GET /api/v1/serve/calendar/years/{market}/{year}
```

已發布日期優先，缺少的日期依市場`weekend_days`補足；尚未建立market row的內建市場使用
週一至週五開市、週六日休市。回應逐日標示
`calendar_source` 與可空的 `calendar_revision`，預設資料不會寫入 DB 或發布。
Dashboard calendar preview、draft、publish 與 rollback 由 Admin API 處理；只有 Owner 可
publish／rollback，draft 不影響抓取。Source 依 Backend 各 provider 的 `*_SKIP_NON_TRADING_DAYS` 開關
決定新工作是否略過非交易日；各 provider 預設皆略過。

`GET /api/v1/admin/market-freshness` 保留既有 freshness／heartbeat 欄位，另回傳
`monitor_kind`（`bounded`／`full_market`）、`activation_state`（bounded 為 null；
全市場為 `not_activated`／`activated`／`deactivated`）、`active_dataset_keys`、
`pending_feeds[{dataset_key, blockers}]`、`markets`（control 涵蓋的所有市場，排序後的清單；`market` 仍是第一條
feed 的市場）、`slot_ids`（control 底下各 feed 的 slot，排序後的清單；`slot_id` 仍是 control 自己的 slot）。
`slot_id` 篩選依 `slot_ids`，一個 control 可出現在多個 slot，回傳的 `slot_id` 不一定等於篩選值。
`include_feeds=false` 仍保留待啟用前提。
Feed 明細新增 `runtime_eligible` 與 `activation_blockers`；全市場 aggregate freshness、
coverage 與 feed counts 只計入 enabled 範圍，歷史 fetched／completed 與 Feed 明細仍可查詢。
`configuration_status=ready` 不代表已啟用、已部署或通過 full-market acceptance。

## Serve API

Serve只讀canonical tables，且只公開active registry可解析的EOD／minute／futures scopes。現行路由為：

- `GET /serve/datasets`：provider-free active catalog與coverage；每筆附 `operation_id`（查這份資料要用的
  operation，如 `list_eod`）與 `volume_unit`（`share`、`lot`、`contract` 或 `provider_defined`）。
- `GET /serve/instruments`、`GET /serve/instruments/{instrument_id}`：商品、facets與分離的
  `coverage.eod`／`coverage.minute`／`coverage.futures`。有 `q` 且沒指定 `sort_by` 時依相關度排序：
  代號完全相符、代號開頭相符、名稱開頭相符、其他，同級內代號短的在前（`2330` 排在 `23301` 前）。
- `GET /serve/instruments/resolve?symbols=TW:2330,US:AAPL,HK:700`：以「市場:代號」精確查商品，最多 50 個；
  沒寫市場的值用 `market` 參數。代號不分大小寫，港股純數字代號補成 5 位（`700` → `00700`）。
  每個輸入各回一筆 `status`：`resolved`、`ambiguous`（同代號有多種資產類別，從 `matches` 選）、
  `not_found`、`not_available`（商品存在但不在 active 資料域），查不到不會讓整個請求失敗。
- `GET /serve/eod`：二擇一指定 `instruments`（最多 50 個，每個是 UUID 或 `MARKET:SYMBOL`，可跨市場，
  代號比照 resolve 的正規化）或 `market`（整個市場）。同一個 `MARKET:SYMBOL` 對到多種資產類別時回 422
  `AMBIGUOUS_INSTRUMENT`，`details.matches` 列出候選 id；查不到回 404，`details.inputs` 列出是哪幾個。
  `order=desc`（預設，最新優先）或 `asc`；cursor 綁定 `instruments`／`market`、日期與 `order`。
  回應的 `units` 說明價格未還原、日期是交易所當地日期，以及各資產類別的成交量單位（股票與 ETF 是股，
  指數是 provider 自己的數字）。每列的 `currency` 是商品的報價幣別；`source` 是整列的來源，
  `field_sources` 列出值不是 `source` 提供的欄位，例如港股以 HKEX 官方值覆寫後為
  `{"close": "hkex", "volume": "hkex", ...}`、Admin 修正後為 `{"close": "manual"}`；所有欄位都是
  `source` 自己的值時為 null。
- `GET /serve/eod/latest?instruments=TW:2330,US:AAPL`：每檔在 `as_of_date`（預設最新）當天或之前的最後一根
  日 K，附前一根的日期與收盤、`change` 與 `change_percent`（小數 4 位）。`missing_sessions` 是兩根之間
  該市場交易日曆上的開市日數；不是 0（缺資料或停牌）時 `change` 與 `change_percent` 為 null，避免把跨多日的
  差距當成當天漲跌；`previous_close` 仍照常回傳。答不出的輸入列在 `missing`，
  原因是 `not_found`、`ambiguous`（附 `matches`）、`not_available` 或 `no_data`，不會讓整個請求失敗。
- `GET /serve/minute`：單一`instrument_id`；明確日期需成對且最多五個曆年。省略日期時以
  最新canonical minute日期向前一個曆月，resolved range會固定在cursor內。
- `GET /serve/futures/eod`：實際到期合約與 `regular`／`after_hours` session，使用 opaque cursor；
  `product_code` 不分大小寫，`order` 同 `/eod`，回應的 `units` 說明成交量與未平倉都以口計；
  OHLC（包含 close）、volume、`settlement_price`／`open_interest` 缺漏時回傳 null；
  零成交但來源仍提供 settlement／OI 的紀錄可查詢，明確的 0 保留，不補造價格或 continuous series。
- `GET /serve/futures/contracts`：TAIFEX 各到期合約與 FinDB 有報價的第一個、最後一個交易日
  （`first_trade_date`／`last_trade_date`），依最後報價日新到舊。`active_on` 只列該交易日落在兩者之間的
  合約（例如找當天的近月合約），`contract_type` 由 `contract_month` 判斷（`monthly`、`weekly`、
  `friday_weekly`）。`expiry_date` 尚未寫入，目前一律 null。
- `GET /serve/exchange-quotes?instruments=HK:700`：交易所官方每日報表的列，目前只有 HKEX 港股。含前收、
  官方收盤、最高、最低、收盤買賣價、`board`（`main`／`gem`）與 `trading_status`（`traded`／`no_trade`，
  停牌不在報表內所以沒有列），不含開盤價。官方收盤、最高、最低與成交量已覆寫進 `/eod`，這個端點用於前收、
  買賣價與交易狀態。參數與分頁同 `/eod` 的 `instruments`、日期、`order`、cursor；不屬於報表範圍的商品回
  `DATASET_NOT_AVAILABLE`。`/serve/datasets` 也列出這條 dataset（`data_kind=exchange_quotes`）。
- `GET /serve/intraday/bars`、`GET /serve/intraday/ticks`：盤中 1 分 K 與逐筆成交，見下方
  [盤中資料](#盤中資料)。
- `GET /serve/intraday/downloads`：研究用的 Parquet 下載清單，見 [盤中資料的大量下載](#盤中資料的大量下載)。
- `GET /serve/calendar`、`GET /serve/calendar/years/{market}/{year}`與
  `GET /serve/market-freshness`。

成功回應使用`{success, data}` typed envelope。Instrument list使用page pagination，EOD／minute
使用opaque cursor且不做total count。Catalog若遇到未知或矛盾的active registry config會fail closed，
不回傳provider、scheduler或credential資訊。

### Serve 錯誤格式

`/api/v1/serve/*` 的所有錯誤（含參數驗證、認證、限流與不存在的路徑）都是同一個格式，client 依 `code`
分類，不要解析 `message`：

```json
{"detail": {"code": "INVALID_PARAMETER", "message": "Invalid parameter: start_date",
            "details": {"errors": [{"location": "query", "param": "start_date", "reason": "..."}]}}}
```

`details` 只在有額外資訊時出現。Source 與 Admin 維持各自的格式。

| Status | `code` | 意義與處理 |
| --- | --- | --- |
| 400 | `PAGE_SIZE_EXCEEDS_KEY_LIMIT` | `page_size` 超過 key 的上限；`details.limit` 是上限，不會自動縮小 |
| 401 | `API_KEY_REQUIRED` | 需要 key 卻沒有帶 `X-API-Key` |
| 401 | `API_KEY_INVALID` | key 不存在、已撤銷、已過期，或不是 Serve key |
| 403 | `SCOPE_REQUIRED` | key 缺少 `details.scope`（`serve` 或 `intraday_download`） |
| 404 | `INSTRUMENT_NOT_FOUND`、`NOT_FOUND` | 找不到商品；`NOT_FOUND` 是路徑不存在 |
| 422 | `INVALID_PARAMETER` | 參數格式或值不合法；`details.errors` 逐一列出參數與原因 |
| 422 | `QUERY_SCOPE_REQUIRED`、`INVALID_SYMBOLS`、`INVALID_DATE_RANGE`、`INVALID_CURSOR` | 查詢組合、代號、日期範圍或 cursor 不合法 |
| 422 | `AMBIGUOUS_INSTRUMENT` | `MARKET:SYMBOL` 對到多種資產類別；改用 `details.matches` 裡的 `instrument_id` |
| 422 | `DATASET_NOT_AVAILABLE` | 商品或市場存在，但不屬於 active 資料域 |
| 429 | `RATE_LIMITED` | 超過 key 的限流；依 `Retry-After`（同 `details.retry_after_seconds`）重試；`details.bucket` 為 `serve` 或 `intraday` |
| 429 | `DOWNLOAD_QUOTA_EXCEEDED` | 下載清單的每日額度用完，`Retry-After` 為到下一個 UTC 日的秒數 |
| 503 | `SERVE_CONFIGURATION_INVALID`、`INTRADAY_READS_BUSY`、`INTRADAY_DATA_CHANGING`、`INTRADAY_STORAGE_UNAVAILABLE` | 暫時無法服務，可稍後重試 |

只要請求帶了 key，即使 `SERVE_REQUIRE_AUTH=false` 也會驗證，並套用該 key 的 page-size 與限流；
無效的 key 一律回 401 `API_KEY_INVALID`。

### 給 agent 串接

其他產品的 LLM agent 以 function calling 呼叫 Serve 時，直接由部署環境的 `/openapi.json` 產生 tool。
Serve role 只掛 Serve 路由，所以該文件只含 Serve：

- 每個 operation 有固定的 `operationId`（如 `list_eod`、`list_instruments`），用來當 tool 名稱；路徑改了也不變。
- `summary` 與 `description` 是給模型看的英文說明，寫明何時用、必填組合、日期語意、上限與分頁方式；
  每個參數與回應欄位都有說明（單位、幣別、時區、null 的意思）。
- 錯誤回應都列出可能的 `code`，body 一律是 `ServeErrorResponse`。

建議的呼叫流程是 `list_datasets`（有哪些資料）→ `resolve_instruments`（已知市場與代號）或
`list_instruments`（搜尋）取得 `instrument_id` 與 `coverage` → 資料端點。只要最新收盤時直接用
`get_latest_eod`，它接受 `MARKET:SYMBOL`，一次呼叫就能回答「這幾檔最新收盤多少、漲跌多少」。由 OpenAPI 轉 tool 的作法與 LLM 供應商無關：

- tool 名稱用 `operationId`，說明用 `summary` 加 `description`。
- 參數用 query parameters 組成 JSON Schema object，`required` 照 OpenAPI。
- 執行時以 GET 帶 query 參數與 `X-API-Key`，key 留在呼叫端的後端，不放進 prompt。
- 錯誤把 `detail` 原樣回給模型，讓它依 `code` 修正參數。429 由呼叫端依 `Retry-After` 等待。
- 有 `next_cursor` 的端點，建議在 tool 層限制最多讀幾頁，避免一次塞爆 context。

```json
{
  "name": "list_eod",
  "description": "Daily OHLCV bars. Daily bars for equities, ETFs and indices. Give exactly one scope: `instruments` ...",
  "input_schema": {
    "type": "object",
    "properties": {
      "instruments": {"type": "string", "description": "Comma-separated instruments, at most 50, each a FinDB UUID or `MARKET:SYMBOL`. Do not combine with `market`."},
      "start_date": {"type": "string", "format": "date", "description": "First exchange-local trade date to include (YYYY-MM-DD)."},
      "end_date": {"type": "string", "format": "date", "description": "Last exchange-local trade date to include (YYYY-MM-DD)."},
      "page_size": {"type": "integer", "minimum": 1, "maximum": 1000}
    }
  }
}
```

價格等 decimal 在 JSON 是字串（如 `"1300.00000000"`），日線成交量是股、盤中是張。
`backend/tests/test_serve_openapi.py` 會檢查每個 Serve operation、參數與回應欄位都有說明，新增端點沒寫說明會失敗。

Serve key的`page_size_limit`只套用於分頁路由，並以路由實際預設值檢查：instrument list為50，
EOD、minute、futures EOD與calendar為100，盤中 bars 與 ticks 為1000。未分頁的datasets、單筆instrument、published calendar year
與market freshness不套用page-size限制。

### 盤中資料

FinLab 的台股與 ETF 1 分 K、逐筆成交存在 Canonical R2 的 Parquet 物件（見
[盤中資料的儲存](../architecture/intraday_storage.md)）。與 `/serve/minute`（Shioaji 時期的歷史，成交量以股計）
是不同的端點，不合併。

- **一次只查一檔**：必須指定 `instrument_id`，不提供跨標的查詢。
- **一律需要 Serve API key**，不受 `SERVE_REQUIRE_AUTH` 影響；`page_size` 仍受 key 的 `page_size_limit` 限制。
  Key 的 `page_size_limit` 最多可設 5000（逐筆單頁的上限），預設 1000，所以逐筆單頁超過 1000 列要另外調高。
- **單位**：每個回應帶 `units`。成交量是**張**（與以股計的日 K 不同）、價格未還原、時間是 UTC，
  `trade_date` 是交易所當地日期（`market_timezone`）。每列附該日的 `source` 與 `source_fetched_at`。
- **順序**：依時間由舊到新；以 `pagination.next_cursor` 是否為 null 判斷是否還有資料。一頁可能少於
  `page_size`（一頁最多讀 40 個物件），仍要跟著 cursor 讀完。cursor 綁定查詢條件，條件不同時回 `INVALID_CURSOR`。

| | `/serve/intraday/bars` | `/serve/intraday/ticks` |
| --- | --- | --- |
| 參數 | `instrument_id`、`interval`（`1m`、`5m`、`15m`、`30m`、`60m`，預設 `1m`）、`start_date`／`end_date`（成對）、`cursor`、`page_size` | `instrument_id`、`trade_date`、可選的 `start_time`／`end_time`（交易所當地時間，含端點）、`cursor`、`page_size` |
| 範圍上限 | `1m`：31 個日曆日；其他刻度：366 個日曆日 | 一個交易日 |
| 省略日期 | 該標的最新一個有資料的交易日（`range.anchor = latest_available`） | 同左（`anchor = latest_available`） |
| `page_size` | 1–1000，預設 1000 | 1–5000，預設 1000 |
| 限流權重 | 每次 1 | 每 1,000 列 1（`page_size` 2,500 計 3） |

**彙總刻度**照 FinLab 的定義，由 1 分 K 在查詢時計算：以 09:00 起算，每個區間以結束時間標示，
跨過收盤的最後一個區間標為 13:33（例如 60 分為 10:00、11:00、12:00、13:00、13:33）。開＝第一根開、
高／低＝極值、收＝最後一根收、`volume` 加總；`trade_count` 在每根都有值時加總，否則為 null；
`vwap` 以成交量加權，任一根沒有 `vwap` 時為 null。只有有成交的分鐘才有 1 分 K，所以沒有成交的區間不產生列
（FinLab 自己的 60 分行情會保留量為 0 的列）。

**限流與保護**：盤中端點與其他 Serve 端點分開計算，使用同一把 key 的 `rate_limit_requests`／`rate_limit_window`，
超過時回 429 `RATE_LIMITED`（`details.bucket` 為 `intraday`）並帶 `Retry-After`。每個 Serve process 同時讀 R2 的數量有上限
（`INTRADAY_SERVE_MAX_CONCURRENT_READS`，預設 4），等待超過 `INTRADAY_SERVE_READ_WAIT_SECONDS`（預設 2 秒）
回 503 `INTRADAY_READS_BUSY`；最近讀過的交易日保留在記憶體（`INTRADAY_SERVE_CACHE_BYTES`，預設 64 MiB），
以物件版本為鍵，物件被取代後不會讀到舊值。

**一致性**：只讀索引表 `intraday_partition` 指派的物件版本，月檔只讀該日的 row group，不列舉 bucket。
物件在讀取時已被月底合併或重新發布取代，會重查索引一次；仍不一致時回 503 `INTRADAY_DATA_CHANGING`，
不回傳半天的資料。R2 無法使用時回 503 `INTRADAY_STORAGE_UNAVAILABLE`。

`/serve/datasets` 列出四條盤中 dataset（`data_kind` 為 `intraday_bars`／`intraday_ticks`，`interval` 為
`1m`／`tick`），coverage 取自索引表。商品的 `coverage.intraday_bars`／`coverage.intraday_ticks` 是最早與最新
有資料的交易日。

### 盤中資料的大量下載

研究用途（多年、全市場、整月逐筆）不經 JSON 分頁，改為直接下載 Canonical R2 的 Parquet 物件。
`GET /serve/intraday/downloads?instrument_id=<uuid>&kind=bars|ticks&month=YYYY-MM` 回傳一檔標的一個月的
下載清單：

- `layout`：物件的欄位（契約 `intraday_partition` v1 的 `x-findb-row-layouts`），`units` 與查詢端點相同。
- `objects`：每個物件一個預簽網址（`url`，有效到 `expires_at`，預設 15 分鐘，
  `INTRADAY_DOWNLOAD_URL_TTL_SECONDS`）、`object_layout`（`day`／`month`）、`object_etag`，以及**只能從這個物件取的
  交易日**與每日列數（`trade_dates`）。
- `quota`：這把 key 今天（UTC）的上限、已用與剩餘。

**一定要依 `trade_dates` 過濾。** 某一天在月底合併後又重新發布時，那天改由新的日檔提供，但月檔要到下一輪
合併才清掉舊列；清單會把那天列在日檔下，月檔的 `trade_dates` 不含那天。只取清單列出的日期，每天的列數
應等於 `row_count`：

```python
import io, urllib.request
import pyarrow.compute as pc
import pyarrow.parquet as pq

tables = []
for item in manifest["objects"]:
    with urllib.request.urlopen(item["url"]) as response:
        if response.headers["ETag"] != item["object_etag"]:
            raise RuntimeError("object was replaced; request a new manifest")
        table = pq.read_table(io.BytesIO(response.read()))
    for day in item["trade_dates"]:
        rows = table.filter(pc.equal(table["trade_date"], pc.strptime(day["trade_date"], "%Y-%m-%d", "s").cast("date32")))
        assert rows.num_rows == day["row_count"]
        tables.append(rows)
```

- 下載時物件已被取代（`ETag` 與 `object_etag` 不同），或日檔已在合併後刪除（404），重新取一份清單即可。
- 需要 key 同時有 `serve` 與 `intraday_download` 兩個 scope；Dashboard 查詢頁經 nginx 帶入的 key 沒有後者。
- **每日額度**：清單裡每個物件計 1，以 key 的 `intraday_download_daily_limit`（預設 5000，建立 key 時設定、
  rotate 時沿用）為每個 UTC 日的上限。剩餘額度不足以涵蓋整份清單時回 429 `DOWNLOAD_QUOTA_EXCEEDED`，
  `Retry-After` 為到下一個 UTC 日的秒數，不會只給部分網址。用量在發出網址前原子扣減，多個 Serve process
  同時請求也不會超過上限。
- 這是 Serve 唯一會寫入的地方：只寫 `intraday_download_usage` 的每日計數，不寫任何行情資料。每次發出清單會
  記錄 log（key、dataset、代號、月份、物件數、用量）。

`/serve/lookup/*`、`/serve/eod/{instrument_id}`、corporate actions、macro與bonds已移除；期貨使用獨立 `/serve/futures/eod`。
Canonical tables仍保留，但沒有active feed的資料域不構成公開Serve contract。

範例：

```bash
curl -H "X-API-Key: $SERVE_KEY" \
  "https://<host>/api/v1/serve/eod?instrument_id=<uuid>&start_date=2026-07-01&end_date=2026-07-31&page_size=100"
```

## Full-market control API

Source 以自身 provider-scoped key 呼叫 universe／delivery plan v1；完整 request 型別以部署版本
OpenAPI 為準。Universe first baseline 或異動超過 2% 的 candidate，需要 Owner
呼叫 `POST /admin/universes/{release_id}/publish`，提供 `evidence_note` 與對應
`first_baseline_approved`／`threshold_exception_approved`，並留下 audit。
Viewer 可用 `GET /admin/universes?status=candidate` 列出待發布版本，並以
`GET /admin/universes/{release_id}/diff` 比較該版本與同 dataset／provider、生效日不晚於候選的
最近 published release。列表另支援 `dataset_key`、`provider`、`effective_date`、`page`、
`page_size`；兩個端點均為唯讀，不會發布候選或建立 daily plan。

`GET /admin/universes/feeds`（Viewer 以上）列出每個可更新名單的 provider／dataset：目前 published 的 release、
較新的待發布 candidate，以及最近一次手動更新的狀態。Owner 可用 `POST /admin/universes/refresh` 排入一次名單
更新，body 為 `provider`、`dataset_key` 與至少 10 個字的 `evidence_note`，回應 202 與 `work_id`。
**手動更新等同核准**：這筆更新送出的名單直接發布，不受 2% 門檻限制，沒有 baseline 時作為第一份 baseline；
`published_by` 是提出要求的 Owner，要求與發布各留一筆 audit。同一 dataset 已有未結束的手動更新時回 409；
TAIFEX 的名單由每日報表產生，不提供手動更新（404）。

Owner 使用既有 `PATCH /admin/schedulers/{scheduler_key}`，傳 `desired_state` 與
`expected_revision`。啟動資格檢查 Owner published baseline，不要求設定日曆；通過後，
凍結 ready feeds；部分 scope 可 start，重複
running PATCH 不重凍結。首次日期為交易所當地今天，合法 legacy date 保留，停止／重新啟動不重設。
舊 `/admin/feeds/{dataset_key}/activate|deactivate` 需 Owner 認證並回傳 410。
Source `POST /source/full-market/readiness` 只接受已 enrollment 的 ID/digest/runtime/scope acknowledgement；
`GET /source/full-market/authorization` 為唯讀 immediate acquisition projection，heartbeat 不延長 evidence。
`GET /source/full-market/enrollment-verification` 需要 Source key 與 `enrollment_id`、`source_client_id`、
`declaration_sha256`、`runtime_id` query；只驗證目前有效管理 enrollment 和 90 秒內 installed ACK，
回傳 typed canonical declaration、installation digest、UUID 與 ACK timestamp。Owner stopped/flag-off
仍可核對容量修復證據；它不授權抓取，也不寫 control/audit。
旗標關閉、Owner 停止或 Source key 撤銷／到期阻擋新 acquisition/plan，既有 frozen prepared delivery 與 normalization 可完成。
詳細管理 enrollment、平台限制與治理資料見 [runbook](../operations/full_market.md)。
所有環境的 startup 都不以日曆完整性為 gate；新抓取不要求 enrollment、readiness、expiry 或 capacity proof。
`GET /source/full-market/authorization` 仍驗證 Source key/provider/scope、flag 與 running control；
`consumer=maintenance` 允許已認證且 Source credential 綁定的 candidate 蒐集，無須先 start、enrollment
或已發布 baseline。Authorization 與 scheduler 回應已不含 `full_market_enabled`。
未知 consumer 拒絕；舊 digest/runtime query 不再影響新抓取資格。此分支尚未部署。
`GET /admin/delivery-plans` 與 `/{plan_id}/summary` 提供 expected／data／no_data／missing／blocked、
deadline、late 與 gaps。這些 control mutation 不可使用 Serve key，也不可將 Admin key 提供給 Fetcher。

`ExecutionProfile` 只剩 `full_market`，貫穿 admission、plans、freshness、缺漏與告警；Pilot 已退役。
Source／Admin plan 列表、dataset 篩選與監控端點仍接受 `execution_profile`，但只接受 `full_market`，其他值
回 422；未指定時為 `full_market`。Canonical 最新 run 更新不得使已完成計畫倒退。
`FULL_MARKET_ENABLED` 已退役，新抓取只受 scheduler control 與 Source 授權約束。

Admin 計畫查詢支援 `dataset_key` 與 `trade_date`；指定任一 `page/page_size` 即啟用分頁（缺省值 1／25，
每頁上限 100）；回傳 `data` 及標準 `pagination`。依交易日、建立時間、plan ID 降冪穩定排序。
只使用舊 `limit` 時保持原 `{data}` 回應與預設 20 筆；`limit` 與任一分頁參數混用回傳 422。
列表加上 `summary_only=true` 時只回傳已持久化的統計：`summary.gaps` 為空陣列、各 part 的 `member_keys` 為空陣列，
其餘欄位與計數不變。未指定時每個 plan 都會重新分類全部成員並列出缺口，資料量大時每次請求要數秒；需要缺口明細時
改對單一 plan 讀 `/{plan_id}/summary`。
`GET /admin/delivery-plans/datasets` 回傳 `{"data":["dataset_key", ...]}`，由指定 profile 的全部計畫去重並排序，
不受日期或最近筆數限制。以上端點須具 viewer 以上 Admin 權限，皆為唯讀，不改交付狀態。
缺口總數使用 `missing + blocked`；`summary.gaps` 明細最多 1,000 筆。

## Admin API

Admin只供Dashboard與維運，主要能力包括：

- user session、Source／Serve／Admin credentials的簽發、輪替與撤銷；
- scheduler desired state、queue health、missing delivery與market freshness；
- DQ、raw payload、rerun、EOD correction與audit；
- instrument cache及市場日曆draft／publish／rollback。

EOD correction（`PATCH /api/v1/admin/eod/{instrument_id}/{trade_date}`）在同一 transaction 內
先鎖定該筆行情，再合併修正欄位、執行阻擋性 DQ、寫入 audit 與 stats。同一行情的並行修正會依序執行；
後到的修正以前一筆提交後的值重新驗證，合併後違反 OHLC 規則時回 422，不會把兩筆部分修正合成
未經 DQ 的值。Normalizer upsert 取得同一個 row lock，因此與修正之間也是以整列為單位依序套用。
港股修正後開盤或收盤落在最高、最低之外仍可寫入，最高低於最低則拒絕，與 ingest 的 DQ 規則相同。

人工修正優先於之後的所有 ingest。修正會把該列的 `source_priority` 設為 Admin 專用值
（int4 最小值，比任何 dataset `source_precedence` 都優先），`source` 與 `source_fetched_at`
保持原值，修正的欄位在 `field_sources` 記為 `"manual"`。之後的 rerun、同 provider 較新的 fetch、
優先度更高的 provider，或交易所官方行情的欄位層修正都不會再覆寫這一列。要再改這一列，只能送出新的修正。

Instrument cache的coverage與Serve instrument契約一致，包含`eod`、`minute`及可空的`futures`；
既有只含前兩者的cache仍可讀取，下一次Admin替換或更新時會正規化為`futures: null`。

Credential plaintext只在簽發時顯示一次。Admin machine key不得提供給Fetcher或一般
Serve consumer；同一請求不得同時帶Bearer session與`X-API-Key`。完整端點以OpenAPI的
`Admin API` tag為準。

## Error、時間與ID

| Status | 意義 |
| --- | --- |
| `202` | Delivery已durable accept並排隊 |
| `400`／`422` | Request語意、policy或schema不合法 |
| `401`／`403` | 缺少credential、credential無效、scope或來源IP不符 |
| `409` | Idempotency或狀態衝突 |
| `413` | Request或payload超限 |
| `429` | Rate limit，依`Retry-After`重試 |
| `503` | DB或ingestion暫時不可用，可安全重試 |

### 403 從哪裡來

所有對外請求都先經過 Cloudflare，再經過 nginx，最後才到 FinDB。三層都可能回 403，用 response body 判斷是哪一層：

| 來源 | Body 特徵 | 原因與處理 |
| --- | --- | --- |
| Cloudflare | JSON 含 `"cloudflare_error": true`、`"error_code": 1010`、`"error_name": "browser_signature_banned"` | 依 User-Agent 封鎖。目前只有 Python 標準庫 `urllib` 的預設 UA（`Python-urllib/3.x`）會被擋；`requests`、`httpx`、`aiohttp` 與 curl 的預設 UA 都可以通過。用 `urllib` 時請自訂 `User-Agent`，例如 `my-app/1.0`。請求沒有到達 FinDB，不會留下 security event，也不會計入 rate limit。 |
| nginx | `{"detail":"Source API client IP not allowlisted"}` | 只出現在 `/api/v1/source/*`：來源 IP 不在 Source allowlist 中。 |
| FinDB | JSON `detail`，例如 `Invalid admin credential`、`Insufficient admin role`、`Password change required`、`Source ... scope denied`；Serve 為 `{"code": "SCOPE_REQUIRED", ...}` | credential 無效、權限不足，或 scope 不符。Serve 的無效 key 回 401，見 [Serve 錯誤格式](#serve-錯誤格式)。 |

上表的 Cloudflare 行為是 2026-11-12 在 staging 與 production 實測的結果，兩個環境相同。Cloudflare 的 security 設定不在 repo 的 IaC 內，調整後須重新實測並更新本表。

Canonical ingest error提供穩定`code`及可用時的`attempt_id`；client依code分類，不解析
人類訊息。Datetime使用含timezone的ISO 8601並正規化為UTC；業務日期用`YYYY-MM-DD`；
UUID視為opaque string；財務decimal不得轉成binary float後計算。

## 本機驗證

```bash
make up-db
make migrate
make seed
make up-server
curl http://localhost:8080/health
```

互動式schema位於`http://localhost:8080/docs`與`/openapi.json`。Staging smoke使用
專用client、bounded payload及可安全重送的固定idempotency key，並確認terminal state。
