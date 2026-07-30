# 受管理的市場交易日曆

交易日曆以 PostgreSQL 的年度修訂版為 source of truth。每個
`market + year` 獨立管理，Dashboard 不在瀏覽器解析或直接寫入 CSV；所有輸入都由
Backend 驗證、展開全年、建立草稿，再由 Owner 發布。

## 資料與狀態

- `calendar_market` 保存市場時區、週末規則與預設交易時段，migration 會依
  `app.vocabulary.KNOWN_MARKETS` 的既有市場代碼建立初始設定。
- `calendar_import_batch` 保存短效 preview 的安全化候選資料、來源雜湊及摘要，不保存
  原始上傳 bytes。
- `calendar_year_revision` 與 `calendar_revision_day` 保存不可變的年度版本及 365／366
  天投影。狀態為 `draft`、`published` 或歷史用的 `superseded`。
- `trading_calendar` 仍承接 normalizer 觀測到的日期，但一律標為
  `observed_ingestion`、revision 0；它不是 Scheduler 的管理真相，也不會覆寫受管理
  revision。

日期狀態只有 `open`、`closed`、`settlement_only`。只有 `open` 的 `is_open` 可為
`true`；`settlement_only` 不可觸發資料抓取。

## 管理流程

Authenticated Dashboard 路由 `/operations/calendars` 提供全年列表、單日修改、
canonical JSON、TWSE CSV、匯入紀錄、修訂紀錄、發布及回滾。Viewer 唯讀，Operator
可 preview／建立草稿，Owner 才可管理市場設定、發布及回滾。所有 Admin mutation 都
寫入 `admin_audit_event`。

TWSE CSV parser 支援 UTF-8、CP950／Big5，從標題解析民國年度，驗證四欄 header、
日期、中文星期、重複日期及 `o`／`*`／空白 marker，並把 `<br>` 清理為純文字換行。
此格式是例外清單：平日預設開市、週末預設休市，再由明示列覆寫。

## Scheduler 合約

`GET /api/v1/serve/calendar/years/{market}/{year}` 是唯一供 Scheduler 使用的年度合約。
它只在年度已有完整 published revision 時回傳；缺少、草稿或不完整一律 `404`。
Fetcher remote client 還會驗證 market/year/revision、IANA timezone、365／366 天、
日期連續性及 `day_status`／`is_open` 一致性，任何錯誤都 fail closed。

Fetcher Scheduler 一律使用此 published-year 合約，沒有 static fallback；schedule-file
中的 calendar 欄位只保留供向後相容的解析與測試，不能作為排程決策來源。部署前必須設定：

- `FINDB_SERVE_BASE_URL`
- `FETCHER_CALENDAR_SERVE_API_KEY`（專用 DB-backed Serve key，不可與 Source key共用）
- `FETCHER_CALENDAR_TIMEOUT_SECONDS`
- `FETCHER_CALENDAR_CACHE_TTL_SECONDS`

發布或回滾會建立單調遞增 revision；Fetcher cache 到期後才會取得新版本。實際啟用前
仍須在 staging 發布目標市場年度、以 Scheduler 時鐘驗證 open／closed／
settlement-only 行為，再將 Environment desired state 明確切換。
