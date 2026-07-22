# 統一 Ingress Contract 計劃

## 狀態

- 狀態：實作中
- 決策日期：2026-07-21
- 適用範圍：所有向 Source API 發送市場資料的 fetch-layer client
- 首批 contract：`market_eod.v1`、`futures_continuous_eod.v1`

實作進度：

- [x] Typed contracts、contract registry 與 dataset audit declaration。
- [x] Durable `ingestion_attempt`、schema lineage 與 canonical `POST /api/v1/source/ingest`。
- [x] Provider-neutral market EOD / futures continuous normalizer routing。
- [x] 發布 versioned JSON Schema、固定 semantic validation codes 與 dataset-aware currency 規則。
- [ ] 真實 FinLab/Bloomberg fixtures、mapping 表與欄位語意最終盤點。
- [ ] Fetch client shadow migration 與 production feed 切換。
- [ ] Batch completeness/freshness policy 強制執行與 delivery-missing monitor。

## 目標

FinDB 定義 provider-neutral、可版本化的 ingress contract。Bloomberg、FinLab 與未來新增的 fetch client 必須先在 fetch layer 將 provider 原始欄位轉為 contract，再送入 Source API。

這項改造要達成：

1. 同一邏輯 dataset 不因 provider 不同而分裂。
2. normalizer 不再解析 Bloomberg、FinLab 等 provider-specific 欄位。
3. 格式錯誤在 Source API boundary 被拒絕並留下可查詢的失敗紀錄。
4. 每次 delivery 帶有足夠的批次描述，讓系統能判斷筆數驟降、過期資料與不完整 snapshot。
5. contract 可以逐版本演進，既有 raw payload 在遷移期間仍可 rerun。

非目標：

- 不要求所有商品共用一個通用 row schema。
- 不在本階段改動 canonical tables 或 Serve API。
- 不要求 FinDB 保存 provider 未轉換前的原始檔；需要保存時由 fetch layer 使用自己的 object storage，並傳入 `source_raw_ref` 與 checksum。

## 核心識別

三種識別必須分離：

| 欄位 | 定義 | 範例 |
| --- | --- | --- |
| `dataset_key` | FinDB 內的邏輯資料流與資料治理單位 | `tw_equity_eod` |
| `schema_id` + `schema_version` | payload 的欄位與語意契約 | `market_eod` + `1` |
| `source` | 實際供應資料的 provider | `finlab`、`bloomberg` |

Feed 不另建 provider-specific dataset。需要在人或監控介面表示特定 feed 時，使用 `{source}:{dataset_key}`，例如 `finlab:tw_equity_eod`。

### Dataset 命名

新 dataset key 使用 `{market}_{instrument_family}_{data_kind}`，全部為 lowercase snake case：

- `tw_equity_eod`
- `tw_etf_eod`
- `us_equity_eod`
- `hk_equity_eod`
- `cn_equity_eod`
- `wtx_futures_continuous_eod`
- `macro_observation`

provider 不得出現在新 dataset key。既有 provider-specific key 在相容期保留為 inactive/legacy alias，不立即改寫歷史 `ingestion_run`。

### Schema 分化原則

Schema 優先依資料形狀分化，其次才是商品特有語意，最後才是市場特例：

1. 股票、ETF、指數、crypto、FX 若都是日 OHLCV，使用 `market_eod.v1`。
2. 期貨連續資料若有轉倉規則，使用 `futures_continuous_eod.v1`。
3. 只有共同 schema 無法清楚表達市場規則時，才增加如 `market_eod.tw.v1` 的變體。
4. provider 差異只能在 fetch adapter 解決，不能產生 `*.bloomberg.*` 或 `*.finlab.*` schema。

## 通用請求 Envelope

所有新格式由單一 canonical ingest path 接收；實際 endpoint 名稱在實作切片決定。請求格式如下：

```json
{
  "dataset_key": "tw_equity_eod",
  "schema_id": "market_eod",
  "schema_version": 1,
  "source": "finlab",
  "request_key": "finlab_tw_equity_eod_20260721_01",
  "idempotency_key": "finlab_tw_equity_eod_20260721",
  "fetched_at": "2026-07-21T08:00:00Z",
  "payload": {
    "batch": {},
    "data": []
  }
}
```

Envelope 規則：

- `dataset_key`、`schema_id`、`schema_version`、`source`、`request_key`、`idempotency_key`、`fetched_at` 必填。
- `source` 使用穩定 lowercase provider name，只能出現在 envelope；`payload.batch` 與 row 不重複傳遞。
- `schema_id` 使用 lowercase snake case；`schema_version` 為正整數。
- `fetched_at` 必須是 UTC-aware ISO 8601 timestamp。
- 同一 source client、dataset 與 idempotency key 的語意維持現況：相同內容回既有 run，不同內容回 `409`。
- `payload` 必須先通過指定 schema 的強型別驗證，不能再用 `dict[str, Any]` 作為實際資料契約。

## 通用 Batch Contract

每一種 schema 都共用以下 batch 欄位：

| 欄位 | 型別 | 必填 | 說明 |
| --- | --- | --- | --- |
| `data_date` | date | 是 | 本批資料代表的主要業務日期 |
| `delivery_mode` | enum | 是 | `full_snapshot`、`incremental`、`backfill` |
| `declared_record_count` | integer >= 0 | 是 | fetch layer 宣告的 `data` 筆數 |
| `coverage_start_date` | date | 條件式 | 批次含多個業務日期時必填 |
| `coverage_end_date` | date | 條件式 | 批次含多個業務日期時必填，且不得早於 start |
| `source_raw_ref` | string | 否 | provider 原始檔/object storage 參照，不得含存取憑證 |
| `source_raw_sha256` | 64-char hex | 否 | provider 原始內容 checksum |
| `sequence` | integer >= 1 | 否 | 同一資料日分批傳送時的順序 |
| `sequence_count` | integer >= 1 | 否 | 同一資料日預期總批數 |

共同驗證：

- `declared_record_count` 必須等於 `len(data)`，不相等時回
  `422 DECLARED_RECORD_COUNT_MISMATCH`。
- `sequence` 與 `sequence_count` 必須同時提供，且 `sequence <= sequence_count`。
- `full_snapshot` 表示 `data_date` 當天完整的 dataset universe；只傳異動或部分 symbol 必須使用 `incremental`。
- `data` 含有 `data_date` 以外的業務日期時，必須提供 coverage start/end，且其值必須涵蓋實際 row 日期範圍。
- `full_snapshot` 不允許缺少 dataset 設定要求的 coverage；缺少時拒絕或建立 warning 由 dataset policy 決定。
- `backfill` 可以包含多個業務日期；`data_date` 必須等於 coverage end date，實際日期仍以 row 為準。

## `market_eod.v1`

適用股票、ETF、指數、crypto 與 FX 的日 OHLCV。

### Dataset context

`market`、`asset_class` 與預設 `currency` 由 `dataset_registry` 決定，不在每列重複。若
dataset 未設定預設 currency，`market_eod.v1` 每一列都必須提供 currency，任一列缺少即回
`422 CURRENCY_REQUIRED`，且不建立 raw/run/job。單一 dataset 不應混合市場或 asset class；
既有 `hkchina_mixed_eod` 必須在 fetch layer 拆成 HK/CN 與 equity/index 對應的 deliveries。

### Row contract

| 欄位 | 型別 | 必填 | 規則 |
| --- | --- | --- | --- |
| `symbol` | string | 是 | FinDB 使用的穩定 symbol，trim 後不可為空 |
| `source_symbol` | string | 否 | provider 原始識別碼，例如 Bloomberg ticker |
| `trade_date` | date | 是 | 該筆 OHLCV 的交易日 |
| `name` | string | 否 | 不提供時不得覆寫既有 canonical name |
| `currency` | ISO-like uppercase string | 條件式 | dataset 無預設 currency 時必填 |
| `open` | decimal | 否 | 不得為負數 |
| `high` | decimal | 否 | 不得為負數 |
| `low` | decimal | 否 | 不得為負數 |
| `close` | decimal | 是 | 不得為負數 |
| `volume` | integer | 否 | 不得為負數 |
| `turnover` | decimal | 否 | 不得為負數 |
| `total_ticks` | integer | 否 | 不得為負數 |

Schema-level validation：

- `(symbol, trade_date)` 是單一 delivery 內的 natural key；不得重複，重複回
  `422 DUPLICATE_DELIVERY_KEY`。此規則與 envelope 的 `request_key`、`idempotency_key`
  無關，不留給 normalizer 靜默去重。
- `high` 存在時不得低於存在的 `open`、`low`、`close`。
- `low` 存在時不得高於存在的 `open`、`high`、`close`。
- 缺少 OHLC 但有 close 的情況可以通過 schema，交由 DQ policy 決定 warning/error，避免某些合法資料源無法輸入。
- `source_symbol` 只用於 identifier lineage，不作為 provider-specific parsing 的入口。

範例：

```json
{
  "batch": {
    "data_date": "2026-07-21",
    "delivery_mode": "full_snapshot",
    "declared_record_count": 1
  },
  "data": [
    {
      "symbol": "2330",
      "source_symbol": "2330 TT Equity",
      "trade_date": "2026-07-21",
      "name": "台積電",
      "currency": "TWD",
      "open": "1000.0",
      "high": "1020.0",
      "low": "995.0",
      "close": "1015.0",
      "volume": 32100000,
      "turnover": "32480000000"
    }
  ]
}
```

## Machine-readable request-body contract 發布

Fetch adapter 以 Source API key 讀取 immutable version endpoint：

```text
GET /api/v1/source/contracts/{schema_id}/versions/{schema_version}
```

目前發布 `market_eod.v1` 與 `futures_continuous_eod.v1` 的 Draft 2020-12 JSON Schema。
Artifact 僅描述 versioned request-body shape、normalization 與 body semantics，不是完整 API
acceptance contract。
回應具有固定 URN `$id` 與 `x-findb-contract`；`x-findb-semantic-rules` 描述 JSON Schema
本身無法表達的 declared count、delivery natural-key uniqueness 與 dataset-aware currency
規則，以及 timezone-aware `fetched_at`、sequence/coverage 關聯、backfill 日期、OHLC bounds、coverage row-date 範圍與
動態 data/byte limits。每筆 rule 都提供穩定 `id`、`scope`、`parameters`、`error_code` 與
`context_dependencies`。Fetch fixture tests 必須同時跑 JSON Schema 與 semantic rules；
`runtime_setting` dependency 由測試環境注入有效 limit，`dataset_context` dependency 由 dataset
declaration 提供。`x-findb-transformations` 列出 trim 等 pre-validation normalization；
`x-findb-contract-scope` 明示 `sufficient_for_api_acceptance=false`，並列出認證/DB credential
lookup、rate limit、source/dataset authorization、dataset registry/declaration/version、idempotency
與 infrastructure 等額外 acceptance boundaries。通過 body schema/rules 是必要而非充分條件；
動態狀態不得假裝成 JSON Schema constraint，HTTP 細節以 API 使用指南的錯誤表與 endpoint
說明為準。Canonical `POST /ingest`
不把 typed model 放到 FastAPI handler 參數，避免 framework validation 在 durable attempt 建立前
拒絕 request。

`request.body.max_bytes` 是已知 Content-Length 時在 attempt 前執行的 middleware gate；其 `413`
不使用 canonical error envelope，也沒有 attempt。其餘 contract semantic rules 在 durable attempt
建立後驗證並記錄固定 failure code。

## `futures_continuous_eod.v1`

適用連續期貨日資料。dataset context 必須提供 `market=WTX`、`asset_class=future` 與預設
currency；期貨 row 不另帶 currency，缺少 dataset default 時回 `422 CURRENCY_REQUIRED`。

### Row contract

| 欄位 | 型別 | 必填 | 規則 |
| --- | --- | --- | --- |
| `symbol` | string | 是 | 連續序列的穩定 symbol |
| `source_symbol` | string | 否 | provider 原始識別碼 |
| `trade_date` | date | 是 | 交易日 |
| `name` | string | 否 | 不提供時不得覆寫既有名稱 |
| `open`、`high`、`low`、`close` | decimal | 同 `market_eod.v1` | 相同 OHLC 規則 |
| `volume` | integer | 否 | 不得為負數 |
| `turnover` | decimal | 否 | 不得為負數 |
| `open_interest` | integer | 否 | 不得為負數 |
| `active_contract_code` | string | 否 | 該日實際採用的期貨契約 |
| `roll_rule` | string | 是 | 穩定的轉倉規則 key，例如 `front_month` |
| `roll_adjustment` | decimal | 否 | 若序列有價格調整，記錄該日調整量 |

此 schema 不接受 Bloomberg nested `price`/`timestamp` 或 FinLab `<Open>` 等別名；fetch adapter 必須先轉成上述欄位。

## Fetch Adapter 範例

Provider-specific mapping 只存在 fetch layer。以下示意 Bloomberg EOD row 如何轉成 `market_eod.v1`；Source API 不包含這段判斷：

```python
def bloomberg_to_market_eod(row: dict) -> dict:
    ticker = row["ticker"]
    return {
        "symbol": canonical_symbol(ticker),
        "source_symbol": ticker,
        "trade_date": row["timestamp"]["query_time"][:10],
        "name": row.get("name"),
        "currency": row.get("currency"),
        "open": row["price"].get("open"),
        "high": row["price"].get("high"),
        "low": row["price"].get("low"),
        "close": row["price"]["last"],
        "volume": row["price"].get("volume"),
    }
```

FinLab adapter 產生完全相同的輸出欄位，只在 fetch repo 內讀取 `date`、`total_volume` 等 FinLab 欄位。Adapter 必須有 fixture-based contract tests，證明 provider payload 轉換後可通過 FinDB 公開的 schema model 或對應 JSON Schema。

## Dataset Registry Contract

每個可接收新格式的 dataset 必須宣告：

```json
{
  "schema_id": "market_eod",
  "accepted_schema_versions": [1],
  "current_schema_version": 1,
  "defaults": {
    "market": "TW",
    "asset_class": "equity",
    "currency": "TWD"
  },
  "delivery_expectation": {
    "delivery_mode": "full_snapshot",
    "freshness_hours": 36,
    "minimum_record_count": 2100,
    "maximum_count_drop_ratio": 0.1
  }
}
```

第一版可以先存在 `dataset_registry.config`，但進入強制執行前應評估把 `schema_id` 與 current version 升為明確欄位，避免關鍵契約只存在 JSONB。

`defaults.market` 與 `defaults.asset_class` 必須存在、符合 canonical vocabulary，且與 `dataset_registry.market/asset_class` 一致；schema normalizer 不得使用通用 market 或 asset class fallback。Normalizer routing 使用完整 `(schema_id, schema_version)` key，不能只以 schema id 猜測版本。

## 驗證與失敗紀錄

Source API 依下列順序處理：

1. 認證 source client，解析 envelope。
2. 建立 ingestion attempt，讓後續所有拒絕都有追蹤識別。
3. 驗證 dataset existence、active、ownership 與 market context。
4. 驗證 dataset 接受的 schema id/version。
5. 以對應 Pydantic model 驗證 batch 與 rows。
6. 計算 batch completeness/freshness policy。
7. 寫入 standardized raw payload、ingestion run、job 與 outbox。

格式或授權失敗不得建立 normalization job，但必須留下 attempt 狀態、公開錯誤碼與 bounded error message。至少需要以下錯誤碼：

- `INGRESS_SCHEMA_UNSUPPORTED`
- `INGRESS_SCHEMA_INVALID`
- `DECLARED_RECORD_COUNT_MISMATCH`
- `DUPLICATE_DELIVERY_KEY`
- `BATCH_RECORD_COUNT_DROP`
- `STALE_PAYLOAD`
- `LATEST_DATE_MISSING`
- `DATASET_DELIVERY_MISSING`

`BATCH_RECORD_COUNT_DROP` 等完整度檢查應支援 dataset-specific `reject` 或 `warn` policy。Warning 必須建立彙總 DQ issue，不逐 row 灌入大量 issue。

## Versioning

- 新增 optional 欄位且不改變既有語意，可以留在同一 major contract version。
- 新增必填欄位、改名、改變 null/數值語意，必須新增 `schema_version`。
- Source API 可在過渡期同時接受 N 與 N+1，但 dataset 必須指定 current version。
- Fetch client 必須明確送版本，不由伺服器猜測。
- Raw payload 與 ingestion run 必須保存 schema id/version，確保 rerun 使用原版本。
- 版本 adapter 只能將舊標準 contract 升級到新標準 contract；不得重新引入 provider-specific parsing。

## 遷移策略

### Phase 0：盤點與 contract fixtures

- 蒐集目前實際使用中的 FinLab/Bloomberg payload fixtures。
- 為每個現有 feed 建立 provider payload → ingress contract 的 mapping 表。
- 定稿 decimal、timezone、symbol 與 currency 語意。

### Phase 1：新增 contract validation

- [x] 新增 typed envelope、schema registry 與首批 Pydantic models。
- [x] 新增 canonical ingest endpoint；舊 endpoints 行為不變。
- [x] 新增 attempt-level failure audit 與 schema/version lineage。
- [x] 發布 versioned JSON Schema，補固定 semantic error codes 與 currency 跨模型規則。

Phase 1 的 FinDB boundary 已完成；Phase 0 的真實 provider fixtures/mapping，以及 Phase 2 的
fetch adapter shadow migration 仍未完成，因此尚不能宣告 production feed cutover。

### Phase 2：Fetch client shadow migration

- 先遷移 `tw_equity_eod`、`tw_etf_eod`，再遷移 WTX。
- Fetch client 在測試環境同時產生舊格式與新格式，比對 record count、natural keys、數值與 DQ 結果。
- Production 每個 feed 個別切換，不一次切換全部來源。

### Phase 3：Normalizer consolidation

- 新格式改由 `MarketEODNormalizer` 與 `FuturesContinuousEODNormalizer` 處理。
- `NORMALIZER_MAP` 對新格式依 schema/data shape 路由，不依 provider 路由。
- 移除 WTX 依 `metadata.source` 選 normalizer 的新請求路徑。

### Phase 4：Legacy freeze

- `/direct` endpoints 標記 deprecated，禁止新增 client。
- 舊 provider-specific dataset 設為 inactive/legacy alias。
- 舊 normalizer 僅供既有 raw payload rerun，不接受新 ingest。

### Phase 5：Legacy removal

- 確認 legacy raw payload 已超出 retention 或已轉存。
- 移除 direct endpoints、provider-specific normalizers 與 provider-specific dataset seed。
- 歷史 ingestion run 保留原 dataset/schema lineage，不強制改寫。

## 首批驗收條件

`market_eod.v1` 與 `futures_continuous_eod.v1` 進入 production 前必須符合：

- 同一 contract 可接受至少兩個不同 provider 經 fetch adapter 產生的 payload。
- FinDB 程式碼不查詢 `metadata.source` 來選 normalizer。
- Provider alias/nested field 不出現在新 normalizer。
- Invalid row、重複 natural key、錯誤 count 與 unsupported version 都有固定 4xx 與失敗紀錄。
- 相同 idempotency key 的語意與現況相容。
- 新舊路徑 shadow comparison 的 canonical natural keys 與數值一致。
- Full snapshot 少量或過期時，會拒絕或建立可觀測 warning。
- 舊 raw payload rerun 在相容期仍可成功。

## 下一個實作切片

下一個 PR 處理 typed delivery policy：count-drop、freshness、latest-date 與彙總 DQ warning；
delivery-missing scheduler/alert 可在同一階段或緊接的維運 PR 完成。Fetch repo 同時補真實
FinLab/Bloomberg fixtures、TW equity/ETF adapters 與 shadow comparison。Phase 2 驗收完成前，
不切 production feed，也不開始 Phase 4 legacy freeze。
