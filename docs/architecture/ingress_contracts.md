# Versioned Ingress Contract

> 狀態：`market_eod.v1` 與 `futures_continuous_eod.v1` 已發布。

## 邊界

FinDB 定義 provider-neutral、可版本化的 ingress contract。Fetcher 必須先把
Bloomberg、FinLab 或其他 provider 格式轉成 contract，再呼叫：

```text
POST /api/v1/source/ingest
```

Backend 不解析新 feed 的 provider-specific 欄位。Provider 差異只能存在於
Fetcher adapter。

三種識別必須分開：

| 欄位 | 意義 | 範例 |
| --- | --- | --- |
| `dataset_key` | 邏輯資料流與治理單位 | `tw_equity_eod` |
| `schema_id` + `schema_version` | payload 欄位與語意 | `market_eod` + `1` |
| `source` | 實際 provider | `finlab` |

## Envelope

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
    "data": [
      {
        "symbol": "2330",
        "source_symbol": "2330 TT Equity",
        "trade_date": "2026-07-24",
        "currency": "TWD",
        "close": "1140.00",
        "volume": 25000000
      }
    ]
  }
}
```

規則：

- 所有 envelope 欄位必填。
- `source` 與 `schema_id` 使用穩定 lowercase 名稱。
- `fetched_at` 必須包含 timezone，進入系統後正規化為 UTC。
- 同一 source、dataset、idempotency key 與內容重送時回既有 run。
- 相同 key 搭配不同 source、schema/version 或內容時回 `409`。
- `request_key` 用於追蹤單次抓取；它不取代 idempotency key。

## Batch contract

| 欄位 | 必填 | 說明 |
| --- | --- | --- |
| `data_date` | 是 | 主要業務日期 |
| `delivery_mode` | 是 | `full_snapshot`、`incremental` 或 `backfill` |
| `declared_record_count` | 是 | 必須等於 `len(data)` |
| `coverage_start_date` / `coverage_end_date` | 條件式 | 跨多個業務日期時成對提供 |
| `source_raw_ref` | 否 | Provider 原始檔參照，不得包含 credentials |
| `source_raw_sha256` | 否 | 原始內容的 lowercase SHA-256 |
| `sequence` / `sequence_count` | 否 | 分批 delivery 時成對提供 |

`full_snapshot` 代表該資料日的完整 dataset universe；只送異動或部分 symbols 必須
使用 `incremental`。`backfill` 可以跨日期，但 `data_date` 必須等於 coverage end。

## 已發布 schema

### `market_eod.v1`

適用股票、ETF、指數、crypto 與 FX 日 OHLCV。主要 row 欄位：

- 必填：`symbol`、`trade_date`、`close`
- 選填：`source_symbol`、`name`、`currency`、`open`、`high`、`low`、`volume`、
  `turnover`、`total_ticks`
- `(symbol, trade_date)` 在同一 delivery 內不可重複。
- OHLC、volume 與 turnover 不得為負數；high/low 必須包住其他已提供價格。
- Dataset 沒有 default currency 時，每列必須帶 `currency`。

### `futures_continuous_eod.v1`

適用連續期貨 EOD。除 OHLCV 外，包含：

- 必填：`symbol`、`trade_date`、`close`、`roll_rule`
- 選填：`source_symbol`、`name`、`turnover`、`open_interest`、
  `active_contract_code`、`roll_adjustment`

精確型別、長度與 additional-properties 規則以 machine-readable schema 為準：

```text
GET /api/v1/source/contracts/{schema_id}/versions/{schema_version}
```

發布版本的 `$id` 為：

```text
urn:findb:ingress-contract:{schema_id}:v{schema_version}
```

## Dataset declaration

Dataset registry 必須聲明：

- `schema_id`
- `accepted_schema_versions`
- `current_schema_version`
- `schema_enforcement`
- `defaults.market`
- `defaults.asset_class`
- 視需求設定 `defaults.currency` 與 `delivery_expectation`

Contract scope 必須與 dataset market、asset class 一致，避免 payload 寫入錯誤
canonical scope。

## 版本演進

已發布 schema 不得原地做 breaking change。升級固定採：

1. Backend 新增並部署 `v2`，同時接受 `v1`、`v2`。
2. 使用真實 fixtures 與 shadow delivery 驗證。
3. Fetcher pin 到 `v2`；production 不自動追蹤 latest。
4. 觀察 attempt、DQ、record count、freshness 與 normalization。
5. 所有 producer 完成切換並經過保留期後，才停止接受 `v1`。

Optional 欄位只能在缺省語意明確且舊 producer 安全時加入既有版本；否則新增版本。

## Fetcher 規則

- Fetcher 與 FinDB 可位於同一 monorepo，但必須是獨立 application 與 image。
- Fetcher 不得 import backend ORM、normalizer 或 DB config。
- Fetcher 不得直接連線 FinDB DB。
- Provider 原始檔由 Fetcher 存 object storage。
- 每個 provider/client 使用獨立 source client key、dataset allowlist與rate limit。
- Retry 429、timeout、502、503、504 時必須沿用相同 idempotency key。
- 必須分別記錄 fetch、delivery accepted、normalization terminal state。

Legacy direct endpoints 只供既有 feed 過渡；新 feed 不得新增 provider-specific endpoint
或 normalizer。
