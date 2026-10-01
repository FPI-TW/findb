# Versioned Ingress Contract

FinDB只接受provider-neutral、明確版本化的contract。Fetcher先完成provider mapping，再呼叫：

```text
POST /api/v1/source/ingest
```

Backend不為新feed解析provider-specific欄位。精確型別、長度與
`additionalProperties`規則以published JSON Schema為準。

## Identity與envelope

| 欄位 | 意義 | 範例 |
| --- | --- | --- |
| `dataset_key` | 邏輯資料流與治理單位 | `tw_equity_eod` |
| `schema_id + schema_version` | Payload欄位與語意 | `market_eod + 1` |
| `source` | 實際provider | `finlab` |
| `request_key` | 單次抓取追蹤 | Producer可穩定重建的request identity |
| `idempotency_key` | 可安全重送的業務identity | Source、dataset與資料範圍的穩定key |

最小envelope：

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

上述最小envelope欄位必填；`delivery`只供scheduler delivery選填。`source`與`schema_id`
使用穩定lowercase名稱；`fetched_at`必須含timezone並正規化為UTC。冪等範圍是已認證
`source_client_id + dataset_key + idempotency_key`；同一範圍內重送相同canonical payload、
source及schema/version時回既有run，任一項不同時回`409`。Credential綁定的source與request
不符會在冪等判定前回`403 SOURCE_IDENTITY_MISMATCH`。

內容比對不包含`request_key`、`fetched_at`或`delivery` metadata。Raw retention清除對應
`raw.market_payload`後，不再保證該key可去重；輪替為新的Source client identity也會形成新的
冪等範圍。

Scheduler delivery可額外帶完整`delivery` object：`slot_id`、`scheduled_for`、
`target_data_date`及`work_item_id`必須同時存在。Backend將它保存為run metadata，不
投影到canonical row。

## Batch規則

| 欄位 | 規則 |
| --- | --- |
| `data_date` | 主要業務日期 |
| `delivery_mode` | EOD使用`full_snapshot`、`incremental`或`backfill`；minute固定`sequenced_snapshot` |
| `declared_record_count` | 必須等於`len(data)` |
| `coverage_start_date/end_date` | 跨日期時成對提供，`data_date`等於coverage end |
| `source_raw_ref` | 選填的credential-free provider raw reference |
| `source_raw_sha256` | 選填的lowercase SHA-256 |
| `sequence/sequence_count` | 分批delivery時成對提供 |

`full_snapshot`代表該資料日完整dataset universe；部分symbol使用`incremental`。
Coverage metadata會保存到run供missing-delivery判定；同日incremental或明確覆蓋預期日期
的backfill可補齊incremental expectation。Rerun不算新的delivery arrival。

## `market_eod.v1`

適用股票／ETF EOD registry scopes（包括 US、HK、TW）；是否公開以 active registry 為準：

- 必填row欄位：`symbol`、`trade_date`、`close`。
- 選填：`source_symbol`、`name`、`currency`、OHLC、`volume`、`turnover`、`total_ticks`。
- 同一delivery的`(symbol, trade_date)`不可重複。
- OHLC、volume與turnover不得為負；high/low必須包住其他已提供價格。
- Dataset沒有default currency時，每列必須提供`currency`。

## `market_minute.v1`

適用`tw_equity_minute`與`tw_etf_minute`。每列是一分鐘bar：

- `trade_date`使用台灣本地日期；`market_timezone`固定`Asia/Taipei`。
- `bar_start_time`、`bar_end_time`、`signal_time`為UTC-aware時間，且
  `bar_end_time = bar_start_time + 1 minute`、`signal_time = bar_end_time`。
- 自然鍵`(symbol, bar_start_time)`在同一delivery不可重複。
- `price_adjustment=none`，`trade_count=null`。
- Shioaji `Volume`以lots提供，adapter先乘1,000轉為shares；turnover單位為TWD。
- Shioaji `ts`是台灣wall-clock nanoseconds，必須先以`Asia/Taipei` localize並視為bar end，
  不得直接當成UTC instant。

Minute batch固定使用`sequenced_snapshot`，並帶相同snapshot的`snapshot_id`、
`daily_update_id`、`universe_id`、`symbols_sha256`、`sequence`與`sequence_count`。
`symbols_sha256`是排序後symbols以LF串接UTF-8 bytes的SHA-256。每sequence治理上限為
50個symbols與15,000 rows；runtime payload limits仍可更嚴格。

Minute request identity使用compact、sorted-key JSON：

```json
{"data_date":"YYYY-MM-DD","dataset_key":"...","sequence":1,"snapshot_id":"..."}
```

其SHA-256 digest分別加`mmr:`作`request_key`、加`mms:`作`idempotency_key`；producer不得
自行替換identity算法。

`symbol_statuses`對每個symbol記錄`data`、`expected_no_data`或`error`；非`data`必須提供
reason。無效volume／turnover可映射為`null`，但必須有且只能有一筆對應anomaly；anomaly
包含安全化且不超過500字元的`raw_value`、`reason`及相符的`raw_value_sha256`。不得包含
provider request context、exception、trace、credential或其他敏感內容。

## `futures_eod.v1`

TAIFEX 使用 provider-neutral 的實際到期合約 contract；不是連續期貨序列。
`product_code` 限 TX／MTX／TMF／TE／TF，`contract_month` 是有效月份的 `YYYYMM`、
`YYYYMMW1..W5` 或 `YYYYMMF1..F5`；Universe 與 quote 使用相同格式驗證，保留官方實際到期識別。
`contract_code` 必須等於 `product_code:contract_month`。發布的自然鍵欄位為
`product_code`、`contract_code`、交易所歸屬 `trade_date` 與 `session`
（`regular`／`after_hours`）；同 delivery 不可重複，同合約的兩個 session 可同批交付。

OHLC／volume／settlement／OI 不得為負，high／low 必須包住所有已提供的 OHLC 價格。
OHLC（包含 close）、volume、`settlement_price` 與 `open_interest` 缺漏時保留 null，
不得用 settlement、前日或另一 session 補值；來源明確提供的 0 必須保留。
每筆 canonical quote 至少有一個來源提供的 OHLC／settlement／OI，或正成交量；
發布的 JSON Schema 與 `futures.row.minimum_observation` 語意規則也檢查此條件，供 producer 在交付前驗證。
零成交但仍有 settlement／OI 的資料照常入庫並可由 Serve 查詢。所有行情欄位缺漏且官方
volume 明確為 0 才由 Fetcher 提交具持久化證據的 `no_trade`，不建立空 canonical quote；
volume 也未知時保持 blocked，不視為 no_data。Batch 仍使用共用 EOD coverage、raw
reference 與 count validation；原始 TAIFEX 訊息由 Fetcher mapping，不由 Backend 解析。

## Frozen universe 與 daily plan protocol

Control protocol v1 定義在 `backend/app/schemas/full_market.py`，與 ingest JSON Schema
分開。Source key 只可操作自身 provider 與 dataset allowlist：

- `/source/universes` 提交／列出不可變 release，`/{release_id}` 讀取完整成員與 evidence。
  Catch-up 使用 `GET /source/universes?dataset_key=...&as_of=YYYY-MM-DD` 取得該日適用 release；
  不以今天的 published universe 改寫過去分母。
- `/source/delivery-plans` 建立／列出交易日 plan，`/{plan_id}` 讀取 frozen parts。
  列表依交易日降冪，`start_date`／`end_date` 為包含端點的日期篩選，`limit` 為 1–100；
  以 `start_date=end_date=目標日` 與 `limit=1` 找回原 frozen plan，provider 由 Source key 決定。
- `/source/delivery-plans/{plan_id}/outcomes` 記錄 no_data／blocked。

Universe material change 計入新增／刪除與 mapping、分類、幣別、exchange、契約身分等欄位變更；
僅每日 `raw_evidence_ref` 更新不計入 `>20` 或 `>2%` 異常門檻。Provenance 仍保留於
不可變 release 成員與 digest，重送相同內容仍使用既有 release。

每 part 最多 50 成員，work item identity 為 `fp1:<part UUID hex>`。Ingest 的 `delivery.work_item_id`
指向 plan part，dataset、provider、日期、member、currency／provider symbol 必須與 frozen
release 一致。重送既有日期只能使用原 release；不得混用新版 universe 補成另一份分母。
同 minute part 可連結多個 symbol 的 sequenced snapshot；canonical row 僅在自身 snapshot／
daily_update group 的所有 sequences 完成後計入 data，其他完成群組不會使部分交付成員完整。

治理中的 Source 實際交付不得使用交易所當地未來交易日，`fetched_at` 不得超過伺服器時間
五分鐘。EOD／minute 的伺服器時間與 `fetched_at` 都須已過 published calendar 的當日收盤；
缺個別日期時間時，保守採 US 16:00 New York、HK 16:10 Hong Kong（含收市競價）、
TW 13:30 Taipei、TAIFEX regular 13:45 Taipei。TAIFEX after_hours 採歸屬交易日 Taipei 05:00，
不等待日盤收盤。這些限制只適用啟用 full-market governance 的 feeds，未來 plan 仍可預先建立。

`no_data` 限 `halted`／`no_trade`，必須有 credential-free HTTPS URL、SHA-256、observed_at、
source_symbol、trade_date、source_status 與 bounded excerpt；futures 同時匹配 session。
最終 no_data 亦須通過上述交易日／session 收盤限制：伺服器與 evidence `observed_at`
皆已到收盤，`observed_at` 不得超過伺服器時間五分鐘；收盤前仍可登錄非最終 blocked。
缺權限、mapping gap、限流、下載錯誤、空 response 都不能視為 no_data；blocked 原因限
`mapping_gap`／`rate_limited`／`source_error`。Data 由 canonical reconciliation 判定，producer
不能直接宣告 data completion。

## 發布與dataset declaration

Machine-readable schema：

```text
GET /api/v1/source/contracts/{schema_id}/versions/{schema_version}
contracts/<schema_id>/v<schema_version>.schema.json
contracts/manifest.json
```

Artifacts由backend registry確定性產生，不可手改：

```bash
uv --directory backend run python scripts/export_ingress_contracts.py
uv --directory backend run python scripts/export_ingress_contracts.py --check
```

Dataset registry必須聲明schema ID、accepted/current versions、enforcement、market、
asset class及必要defaults／delivery expectation。Dataset scope必須與contract一致。

已發布schema不得原地做breaking change。升級順序固定為：backend同時接受新舊版本、
fixture／shadow驗證、Fetcher切換pin版本、觀察attempt／DQ／freshness、經保留期後停止舊版。

## Producer邊界

- Fetcher不得import backend ORM、normalizer或DB config，也不得直接連FinDB DB。
- Provider raw由Fetcher寫入Raw R2；contract只攜帶credential-free reference。
- 每個provider/client使用獨立Source key、dataset allowlist與rate limit。
- Retry 429、timeout、502、503、504時沿用相同idempotency key。
- 分別記錄fetch、durable accepted及normalization terminal state。
- 所有feed只走`/source/ingest`；不得重新加入direct、market或provider-specific routes。
