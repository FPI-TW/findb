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

適用`us_equity_eod`與`tw_equity_eod`：

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
