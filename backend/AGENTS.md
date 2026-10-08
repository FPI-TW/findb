# FinDB Backend Agent 指南

先套用root `AGENTS.md`，再套用本檔。本檔涵蓋整個`backend/`，不再於API、models或
normalize子目錄維護個別規則。

## API v1

`app/api/v1/`是HTTP邊界：Source負責ingest write path，Serve只提供canonical read path，
Admin負責已認證的營運、修正與治理操作。

| 任務 | 位置 | 邊界 |
| --- | --- | --- |
| Router掛載 | `app/main.py` | 掛載`/api/v1/*`與health endpoints |
| Source ingest | `app/api/v1/source.py` | Auth、idempotency、run建立與rerun |
| Serve query | `app/api/v1/serve.py` | 唯讀filter、pagination與response shaping |
| Admin operations | `app/api/v1/admin.py` | DQ、EOD修正、raw查詢、bulk rerun與cache管理 |
| API auth | `app/api/deps.py` | Source、Serve、Admin認證、allowlist與rate limit |
| DB dependency | `app/dependencies.py` | Async session injection |

### 規則

- Handler保持薄層，business logic放在services；contract validation留在ingress schema／service。
- Source是ingest／rerun write path；Serve永遠唯讀；Admin correction／maintenance必須通過
  Admin認證。
- 使用dependency-injected `AsyncSession`，不得在handler建立ad hoc engine。
- 回應使用`app/schemas/source.py`、`serve.py`及`admin.py`的typed schemas，不直接回傳raw
  ORM object。
- 保持versioned contract ingest與rerun的idempotency行為。
- Error payload不得洩漏secret、internal trace或敏感設定；不得繞過受保護endpoint的認證。

### 驗證

- 依變更範圍更新`tests/test_source_routes.py`、`test_canonical_ingest_api.py`、
  `test_serve_api.py`或`test_admin_api.py`。
- 驗證`401`／`403`、market mismatch、response schema及pagination wrapper。
- Ingest行為改變時，同時執行`test_canonical_ingest_api.py`與
  `test_normalization_queue.py`。

## Models

`app/models/`定義canonical entities、ingestion registry及`raw.market_payload`。

| 任務 | 位置 | 邊界 |
| --- | --- | --- |
| Engine／session | `app/models/base.py` | Async engine、sessionmaker與revision／table驗證 |
| Canonical tables | `app/models/canonical.py` | Instrument、EOD、corporate action、macro與futures |
| Registry tables | `app/models/registry.py` | Dataset、run、job、outbox與DQ state |
| Raw payload | `app/models/raw.py` | `raw` schema短期保存與ingest idempotency scope |

### 規則

- Datetime欄位使用`DateTime(timezone=True)`及UTC-aware values；primary identifier依root
  規則使用UUIDv7。
- Business key與query path使用明確`Index`或`UniqueConstraint`；不得移除保障ingest
  idempotency的constraint。
- `IngestionRun`的status、counts、timestamps及error欄位語意必須一致。
- 保持`raw` schema隔離，不把registry語意混入canonical entity tables。
- Relationship名稱遵循既有`back_populates`對稱關係。
- 新增model時更新`app/models/__init__.py`；required field變更同步更新seed與fixtures。
- Schema變更只透過Alembic；驗證`init_db()` revision／required-table檢查及新constraint測試。
- 重大schema變更需在相關plan／runbook記錄migration、rollout與compatibility影響。

## Normalize

`app/services/normalize/`只處理provider-neutral versioned contracts。目前正式路徑為：

- `market_eod.v1` -> `MarketEODContractNormalizer`
- `market_minute.v1` -> `MarketMinuteContractNormalizer`
- `futures_eod.v1` -> `FuturesEODContractNormalizer`，依實際產品／到期契約／交易日期／交易時段寫入
  `futures_contract_eod`；不得由契約價格產生 continuous 或產品層級 latest close。
  到期識別保留實際 `YYYYMM`、`YYYYMMW1..W5` 或 `YYYYMMF1..F5`，不得接受價差或連續合約。

全市場 universe 與 daily plan 由 `app/services/full_market.py` 管理。Source credentials 必須同時
符合 provider 與 dataset scope；每日成員凍結後，以 `fp1:<part UUID hex>` 連結 ingestion run。
EOD 逐批寫入後可立即查詢；minute canonical bars 可逐 sequence 查詢，但成員完整性須等待
該 canonical row 所屬的 sequenced snapshot group 完成，同 part 的其他群組不會帶過部分交付。
`expected = data + no_data + missing + blocked` 只依 canonical
provenance 與持久化官方 no-data 證據計算，空批次或 quota failure 不代表正常無資料。

Full market 使用環境 opt-in flag、trusted installation/account enrollment 與 Owner scheduler admission；
registry `is_active` 保持普通 dataset acceptance，不能代替 Full flag。部分 ready feeds 可 start，
stopped→running transaction 以 environment→sorted datasets→calendar markets→enrollment→Source
client→control lock order 重查並凍結 scope/first dates/revision/audit。Baseline publish 先鎖 dataset 再 release。
容量 repair 必須經 Source-authenticated 唯讀 current enrollment verification 與 fresh installed ACK，
不能信任任意檔案的 UUID/hash；此 proof check 獨立於 acquisition/Owner stopped/flag，且 GET 不寫 audit/control。
旗標关闭由 ingest/all lifecycle 或 trusted management stop/audit，Serve/GET 不寫狀態。既有 prepared
`fp1` delivery/normalization 繼續完成；新 acquisition 不得用 cached desired-state 授權。
Source 實際交付仍拒絕未来 trade date、超過五分鐘 fetched_at skew，並要求已過市場收盤；
TAIFEX after_hours 採 attributed trade_date 當地05:00，regular 採 published close 或13:45。
Migration 保留合法 legacy activation_date，malformed 日期阻擋該 feed；啟動後只补首日後 gaps。
詳細 current contracts 與 local/deployed 操作見 `docs/operations/full_market.md`。

`app/services/ingestion.py`的`CONTRACT_NORMALIZER_MAP`是唯一dispatch來源，只能依明確
`(schema_id, schema_version)`選擇normalizer；缺少或不支援的metadata必須fail closed。

### 規則

- 新normalizer繼承`BaseNormalizer`並實作`map_fields`，再依root指南完成export、routing、
  registry與contract測試。
- Dataset defaults由`app/services/ingress_contracts.py`驗證；不得從任意payload metadata推測
  market、asset class、currency或provider。
- Canonical write前先評估DQ error，並保持idempotent upsert及source precedence行為。
- Trade timestamps必須UTC-aware；所有writes使用async SQLAlchemy session。
- 不得恢復dataset-key／provider-aware legacy routing、direct-format payload或legacy direct
  normalizer modules。
- Normalizer不得處理HTTP parsing或credential authorization。

### 驗證

- 新增或修改normalizer時覆蓋schema routing、mapping、DQ阻擋、canonical persistence、
  idempotent rerun及不支援version fail-closed測試。
- 至少執行對應market contract／canonical ingest測試與`tests/test_normalization_queue.py`。
