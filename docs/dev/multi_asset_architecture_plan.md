# FinDB 多資產擴展架構計劃

本文件是針對「導入大量多市場、多資產類別資料」的架構整理與重構計劃，來源為 2026-07 的全案 review。目標市場：US、TW、HK、CN；目標類型：指數、個股、期貨、外匯、債券、ETF、crypto。

資料消費情境：

- 資訊站：純資料呈現，低延遲、重複讀取多。
- 研究 / 回測：長時間維度、大量批次抓取。
- 大語言模型查詢：API（function-calling，固定資料查詢）與 RAG（語意 / 模糊查詢，屬下游衍生系統）。

與 `scalability_optimization_checklist.md` 的分工：該文件處理 ingest/normalize 吞吐與查詢延遲的效能優化；本文件處理 schema 設計決策、部署角色切分與 API 消費者治理。兩者的執行順序建議交錯進行，本文件各 Phase 會標註對應關係。

---

## 架構決策紀錄（ADR）

以下四個問題在 review 時已有結論，除非前提改變（團隊規模、流量量級、基礎設施預算），不再重新討論。

### ADR-1: 不為多種消費需求切分多個服務

資訊站、回測、LLM API 查詢走同一條 read path（Serve API），差異只在流量特徵（page size、頻率、延遲容忍），以 API key tier 區分即可。維持 modular monolith。

例外：**RAG 是獨立的下游服務**。語意查詢需要 embedding pipeline 與向量索引（pgvector 或獨立 vector store），屬 canonical layer 的衍生系統，另立 repo/服務，消費 findb 的 Serve API 或 DB replica，不回寫 findb。

### ADR-2: 補資料腳本與服務共 repo、分執行資源

腳本留在本 repo（共用 models / config / migrations），但不得跑在 serve 流量所在的 app container 內。與 `findb-raw-cleanup` 相同模式：同 image、獨立 container。單機資源不足時，將 ingest 角色遷至獨立 instance 或 EventBridge + ECS scheduled task；因角色已分離，遷移為零程式碼改動。

### ADR-3: 不採用微服務

單機 EC2 + RDS、小團隊，微服務的分散式成本（部署矩陣、跨服務 debug、交易一致性）遠大於收益。需要分離的是 **workload**（ingest vs serve 的 process/container），不是 service。現有 module 邊界（source / normalize / serve）已清楚，未來真有需要時沿此邊界拆分。

### ADR-4: LLM 與其他 server 共用入口、以 key 身分區分

不另設 endpoint。將 `SERVE_API_KEYS` flat list 升級為 DB-backed API key 表（見 Phase 3），以 tier 區分 LLM 與一般服務流量，解決個別撤銷、個別限流、用量審計三個問題。未來若做 MCP server，也是包在 Serve API 外的薄層，不動 read path。

### ADR-5: 所有資料來源一律經由 Source API 入庫

背景：ingest 來源將從單一（地端服務定期 POST）變成多個（地端服務 + EC2 內定期抓取腳本），未來可能再增加。

結論：**不更動架構**。Source API 本來就是多 fetch 來源的統一入口，新來源一律作為 fetch-layer client 對 Source API 發送，禁止直接寫 DB 或在腳本內 import normalizer 入庫。走統一入口才能保有冪等去重、raw layer 保存、`ingestion_run` 血緣、DQ 檢查與 rerun 能力；第二條寫入路徑是本專案的反模式。

---

## 多來源 Ingest 規範

適用於所有向 Source API 發送資料的 fetch-layer client（地端服務、EC2 內排程腳本、未來新增來源）。

### 入口路徑

- EC2 本機腳本：POST `http://127.0.0.1`（nginx loopback CIDR 永遠在 allowlist 內，見 `infra/nginx/source-allowlist.conf`），或直接打 app container port。**不得**走公網域名繞經 Cloudflare 回來。
- 外部來源（地端服務）：走既有公網入口，來源 IP 需加入 `SOURCE_ALLOWLIST_CIDRS`。
- 所有請求一律帶 `X-API-Key: {SOURCE_API_KEY}`。

### source 命名與冪等

- 每個來源使用穩定 lowercase provider name（比照 `bloomberg`、`finlab`），入庫後即為 canonical rows 的 `source` 欄位值，不得混用別名。
- `idempotency_key` 由 client 產生且必須穩定可重現，建議格式：`{source}_{dataset_key}_{data_date}`（或其 hash）。腳本重跑同一天資料時 key 不變，由 Source API 去重。
- `request_key` 帶入該次抓取的可追溯識別（例如含抓取時間戳），供人工排查。

### dataset_key 與 normalizer 路由

- 新來源 payload 格式與既有 dataset 相同 → 直接共用既有 `dataset_key`，零改動。
- 格式不同 → 二擇一：
  1. 開新 `dataset_key` + 新 normalizer（依 `AGENTS.md`「新增 Normalizer」五步流程）。
  2. 共用 `dataset_key`，依 payload `metadata.source` 路由到不同 normalizer——既有範例為 `wtx_eod`（`app/services/ingestion.py` 的 `_WTX_SOURCE_NORMALIZERS`）。同一 dataset 有兩種來源格式時優先採用此模式。

### 來源衝突（precedence）

多來源可能寫入同一筆 canonical row（同 instrument 同 trade_date）。原則：

- 為每個 dataset 指定**主來源**；次要來源預設「補缺不覆寫」。
- 實作位置：normalizer upsert 時比較既有 row 的 `source` 欄位，依 precedence 決定是否覆寫（例：`bloomberg` > 本地抓取來源）。
- precedence 順序定義在 dataset config（`dataset_registry.config`），不 hardcode 在 normalizer。
- 發生「次要來源與主來源數值不一致」時記 DQ issue（severity=`warning`），不阻擋寫入。

### 執行資源

- 排程腳本依 ADR-2：獨立 container / cron 執行，不進 app container，不共用 app 的 DB pool。
- 多個定期來源避免排在同一時段（錯開 cron 時間），降低 normalize 峰值互撞；根本解法為 Phase 2 的 ingest/serve 角色分離。

---

## Schema 設計原則

### Instrument master：單表不拆

`instruments` 單表 + `asset_class` 欄位是標準 security master 模式，跨資產查詢（instrument-lookup、LLM 查詢）依賴它，**不拆**。

各資產類別特有欄位以「延伸表」承載（`futures_contract` 已是此模式的範例），不塞 `extra` JSONB：

| 資產類別 | 延伸表 | 欄位示意 |
| --- | --- | --- |
| 期貨 | `futures_contract`（既有） | contract_code、expiry_date |
| 債券 | `bond_details`（新增） | coupon、maturity_date、issuer、rating、face_value |
| ETF | `etf_details`（新增） | 追蹤指數、expense_ratio、發行商 |

判斷準則：**會被當查詢條件的欄位進延伸表；純展示的雜項才進 `extra`**。

### 時序資料：按資料形狀拆，不按資產類別拆

- 個股、ETF、指數、crypto、FX、期貨連續：OHLCV 同構，**共用 `market_data_eod`**（期貨連續維持既有 `futures_continuous_eod`）。
- **債券是唯一形狀不同的類型**：核心欄位是 yield、clean/dirty price、duration，不硬塞 OHLCV。公司債走 instrument + 新表 `bond_eod`；公債殖利率曲線可走既有 `macro_series` / `macro_observation`。

### 受控 vocabulary

`asset_class`、`market` 目前是自由字串（`String(20)`），由各 normalizer 自行填值；`scripts/fix_misrouted_tw_futures.py` 與 `scripts/cleanup_stale_instruments.py` 的存在證明 drift 已發生。市場 × 類型即將從 4×3 成長為 4×7，合法值必須收斂到 DB 層（lookup table + FK，或 Postgres enum + CHECK）。

### 量級估算（實體設計依據）

US + TW + HK + CN 全市場個股 + ETF 約 3 萬檔 × 250 交易日 ≈ **750 萬列 / 年**；20 年歷史 backfill 約 **1.5 億列**。PostgreSQL 單表可承受，但 partition 讓 backfill、VACUUM、retention 輕鬆一個量級，且遷移成本隨資料量單調上升——**趁小做**。

---

## 執行計劃

### Phase 1: Schema 治理與實體設計（便宜、擋未來的坑，最優先）

前置：無。對應 checklist Phase 0（先建 baseline 再動 schema）。

- [x] `asset_class`、`market` 收斂為受控值：
  - [x] 新增 lookup tables（`asset_class_def`、`market_def`）或 Postgres enum / CHECK。
  - [x] Alembic migration：先清理既有 drift 資料（盤點 `SELECT DISTINCT asset_class, market FROM instruments`），再加約束。
  - [x] normalizer base 增加寫入前驗證；錯誤值以逐筆 DQ issue（`INVALID_VOCABULARY`, severity=error）隔離，不落庫也不 fail 整個 run。
- [x] `market_data_eod` 實體設計修正：
  - [x] 主鍵改為 `(instrument_id, trade_date)` 複合 PK，移除 surrogate UUID `id`。
  - [x] 刪除與 `uq_eod` 完全重複的 `idx_eod_inst_date`（億級資料時省一整份 B-tree）。
  - [x] 確認 Serve API / normalizer 無依賴 `id` 欄位後再遷移。
- [x] `market_data_eod` range partition by `trade_date`（按年）：
  - [x] 評估 PostgreSQL native partitioning 遷移路徑（新 partitioned table + 資料搬遷 + rename，維護窗口執行）。
  - [x] `futures_continuous_eod`、`macro_observation` 視量級決定是否比照（預期量級小一個數量級以上，可延後）。
- [x] 產出 migration 演練紀錄（staging 或 partial dump 環境驗證後才上 production，流程比照 `migration_workflow.md`）。

Phase 1 執行紀錄（2026-07-09）：

- 受控 vocabulary 採用 `CHECK` 約束與 shared constants，暫不新增 lookup tables，避免為少量固定值引入 seed / FK 管理成本。
- `market_data_eod` 改為 PostgreSQL `RANGE (trade_date)` partitioned table，migration 以 `market_data_eod_old` 搬遷到新表；既有年份建立 yearly partitions，另建 default partition 承接未預建年份。
- `futures_continuous_eod` 與 `macro_observation` 暫不 partition；目前量級預期低於 EOD 主表，待 Phase 0 baseline 或資料量接近瓶頸再處理。
- `canonical_correction.record_id` 維持 UUID 型別：EOD 修正改用 `instrument_id + trade_date` 導出的穩定 logical UUID（md5），migration 同步改寫歷史紀錄，downgrade 亦以同一導出規則重建 id，round-trip 一致。
- 年度 partition 由兩層機制維護：ingest 時 `ensure_eod_partition()` 寫入前自動建立、migration 預建未來 5 年 + DEFAULT partition 作為安全網（DEFAULT 累積資料時的處置見 known_issues R7）。
- macro normalizer 的 vocabulary 驗證為逐筆隔離：`map_fields` 標記 `vocabulary_error`、`process` 轉為 `INVALID_VOCABULARY` DQ issue，單筆格式錯誤（如 `S&P`）不再使整批 run 失敗。
- 本地驗證：`make test-db` 通過（223 passed, 2 skipped）；臨時 DB `findb_migration_test` 跑 `alembic upgrade head` 與 `alembic downgrade e7f8a9b0c1d2` 通過。

### Phase 2: Ingest 與 Serve 部署角色分離（導入大量資料前必做）

前置：無，可與 Phase 1 並行。對應 checklist 的 BackgroundTasks 瓶頸項。

- [x] 現況問題：normalization 以 FastAPI `BackgroundTasks` 跑在 serve 流量同一 process，大量 backfill 會搶 serve 的 event loop 與 DB connection pool。
- [x] `docker-compose.prod.yml` 拆兩個 app container（同一 image）：
  - [x] `findb-serve`：只掛 serve router（+ static、health），nginx upstream 指向此。
  - [x] `findb-ingest`：掛 source / admin router，承接 normalize workload。
  - [x] `app/main.py` 以 env（如 `APP_ROLE=serve|ingest|all`）控制 router 掛載；本機開發維持 `all`。
- [x] 兩容器各自獨立 DB pool 設定（ingest 寫入 pool 與 serve 讀取 pool 不互搶）。
- [x] backfill 腳本執行規範：獨立 container / cron 執行，不進 app container；文件化於 `instrument_name_backfill_deployment.md` 的既有流程。
- [x] Serve 讀取熱點修正：
  - [x] `list_instruments` 的 4 個 correlated scalar subqueries（first/latest trade date、latest price）物化為 `instrument_stats` 表，由 normalize 完成時更新（或先以 lateral join 改寫過渡）。
  - [x] list endpoints 增加 keyset（cursor）pagination；`count(*)` 改為可選參數。
- [x] 回測情境的 bulk export 路徑（可延後至需求出現）：產 CSV/Parquet 上 S3 pre-signed URL，取代深分頁 JSON。

Phase 2 執行紀錄（2026-07-09）：

- `APP_ROLE=serve|ingest|all` 控制 router 掛載；本機預設 `all`，production compose 拆成 `findb-serve` 與 `findb-ingest`，nginx 將 `/api/v1/serve/*` 與靜態頁導向 serve，`/api/v1/source/*`、`/api/v1/admin/*` 導向 ingest。
- serve / ingest 使用同一 image 但覆寫不同 `DATABASE_POOL_SIZE` / `DATABASE_MAX_OVERFLOW`，避免 backfill 或 normalize 搶 serve pool。
- 新增 `instrument_stats`，migration 從既有 `market_data_eod` / `futures_continuous_eod` 回填；EOD normalize 後更新 first/latest trade date 與 latest price，futures continuous normalize 後更新 first trade date。
- `/api/v1/serve/instruments` 改讀 `instrument_stats`，支援 `cursor` keyset pagination 與 `include_count=false` 跳過 `count(*)`。既有 page/page_size 與 count 預設維持相容。
- backfill runbook 改為 `docker compose run --rm raw-cleanup ...` one-off container，不再要求進入 app container 執行。
- Bulk export 暫不實作 endpoint；目前明確標記為需求出現後再做，避免在沒有消費者之前引入 S3/Parquet 維運面。

### Phase 3: API key 治理（key 外洩處理與消費者區分）

前置：無。與 ADR-4 對應。

- [x] 新增 `api_key` 表：`key_id`、`key_hash`（不存明文）、`owner`、`tier`、`scopes`、`created_at`、`revoked_at`。
- [x] `verify_serve_api_key` 改查表（保留 env fallback 過渡期），比對用 hash。
- [x] tier 決定 rate limit 與 page size 上限；LLM 流量獨立 tier。
- [x] Admin API 增加 key 簽發 / 撤銷 endpoints（沿用 `ADMIN_API_KEY` 保護）。
- [ ] 用量記錄（per key_id 計數）供審計與異常偵測。**欄位已備妥但尚未寫入**——inline 累計會破壞 Serve read-only invariant，改由 access log 或批次任務回填（見 known_issues R8）。
- [x] 遷移完成後移除 `SERVE_API_KEYS` env 機制與 nginx serve-key 注入的相容性確認（`infra/nginx/serve-key.conf` 注入的 key 也需入表）。

Phase 3 執行紀錄（2026-07-09）：

- 新增 `api_key` table，只保存 SHA-256 hash；Admin `POST /api/v1/admin/api-keys` 簽發時只回傳一次明文 key，`GET /api/v1/admin/api-keys` 不暴露 hash。
- Serve auth 優先查 DB active key，檢查 `serve` scope、per-key rate limit 與 `page_size_limit`；不在 Serve GET inline 更新 `usage_count` / `last_used_at`，避免破壞 Serve read-only invariant。用量審計欄位保留，後續應由 access log 或批次任務寫入（追蹤：known_issues R8）。
- 用量審計的延後決策背景：Phase 2–6 review 的 finding #9 指出原本每個 Serve GET 都 inline `usage_count += 1` + `commit()`，這會讓 serve 角色無法指向 read replica、DB 寫降級時整條 read path 回 500。移除 inline 寫入是刻意取捨（唯讀不變式 > 即時計數）。
- `SERVE_API_KEYS` env fallback 暫時保留，供 nginx `serve-key.conf` 與既有部署過渡；正式移除前需先把 nginx 注入的 key 透過 Admin endpoint 建入 DB。
- partial dump 預設排除 `api_key`，避免把 key hash 與用量審計資料帶到本機 seed。

### Phase 4: Cache 層（隨讀取流量跟上）

前置：Phase 2 完成（serve 角色獨立後才好設 cache 邊界）。

- [x] 第一步（最便宜）：評估 nginx `proxy_cache` 對 serve GET endpoints 做 micro-caching；review 後因 auth/revocation 風險撤回 protected Serve API proxy cache。
- [x] cache key 需含 query string 與 API key tier（避免 tier 間互吃 cache 額度差異）：結論是 nginx 無可信 tier，不能用 client header 或明文 key。
- [x] 觀察命中率後再評估 Redis（application-level cache）或 CloudFront：延後到 app 可產生可信 cache policy / surrogate key 後再做。
- [x] 資訊站的固定圖表資料（如大盤走勢）可比照 `app/static/data/` 的 generated cache 模式預產 JSON。

Phase 4 執行紀錄（2026-07-09）：

- 生產 nginx 的 `/api/v1/serve/*` `proxy_cache` 在 review 後撤回：DB-backed key、撤銷、scope 與 rate limit 都在 FastAPI 內驗證，nginx 無法安全地在 cache hit 前重驗，因此 protected Serve API response 不做 proxy cache。
- cache 重點改回既有 `app/static/data/` generated cache 模式；`findb-serve` 與 `findb-ingest` 透過 shared volume 共用 generated JSON，Admin refresh 在 ingest container 執行時會打 `http://serve:${PORT}` 的 Serve API。
- Redis / CloudFront 暫不導入；若要對受保護 Serve API 快取，需先讓 app 產生可信 cache policy / surrogate key，不能使用 client header 或明文 API key 做 nginx cache key。
- 固定圖表資料可沿用 generated cache 模式；本 phase 未新增未被消費的預產資料。

### Phase 5: 新資產類別導入（債券、ETF）

前置：Phase 1 完成（受控 vocabulary 與 partition 就緒）。

- [x] ETF：
  - [x] 進既有 `market_data_eod`；新增 `etf_details` 延伸表。
  - [x] 保留既有 ETF normalizer 路徑；新 provider-specific ETF normalizer 待 payload contract 到位後依 `AGENTS.md`「新增 Normalizer」流程註冊。
- [x] 債券：
  - [x] 新增 `bond_details`（instrument 延伸）與 `bond_eod`（yield、clean_price、dirty_price、duration 等）。
  - [x] 公債殖利率曲線評估走 `macro_series`，公司債走 instrument 路徑。
  - [x] Serve API 新增 `/bonds` 查詢 endpoints（維持唯讀）。
- [x] 各市場 trading calendar 補齊（債券與期貨的結算日曆差異確認）。

Phase 5 執行紀錄（2026-07-09）：

- 新增 `etf_details` 延伸表；ETF 價格仍使用 Phase 1 已 partition 的 `market_data_eod`，不新增平行 OHLCV 表。
- 新增 `bond_details` 與 `bond_eod`；債券日資料使用 yield / clean price / dirty price / duration 等欄位，不硬塞 OHLCV。
- Serve API 新增 `/api/v1/serve/bonds` 與 `/api/v1/serve/bonds/eod`，均為 read-only。
- 公債殖利率曲線維持走 `macro_series` / `macro_observation`；公司債與可交易債券走 instrument + bond extension。
- 各市場 trading calendar 沿用既有 `trading_calendar`，不同市場/品種的日曆補齊需由資料 feed 或 seed 處理。
- Provider-specific ETF/Bond normalizer 未在本 phase 先造假格式；等實際 payload contract 到位時依「新增 Normalizer」流程新增 dataset、normalizer 與 seed。

### Phase 6: RAG 衍生服務（獨立 repo）

前置：Phase 3 完成（RAG 服務以自己的 key tier 消費 Serve API）。本 repo 僅承擔資料供應方角色。

- [x] 界定供應介面：RAG 服務讀 Serve API 或 RDS read replica（量大時後者）。
- [x] findb 不新增向量欄位、不引入 embedding 依賴。
- [x] 若需要變更 Serve API 以利 RAG 抽取（如 changed-since 增量查詢參數），以一般 feature 流程處理。

Phase 6 執行紀錄（2026-07-09）：

- 新增 `rag_supply_contract.md`，明確定義 findb 只供應 canonical read path；embedding、vector index、ranking、prompt/chat runtime 屬下游 RAG repo。
- RAG 預設以 Phase 3 的 DB-backed Serve API key 消費，使用專屬 `rag` tier；大量抽取時改走 RDS read replica 的 read-only credentials。
- 未在本 repo 新增 pgvector、embedding 依賴或 RAG 寫入路徑。
- 若未來需要 `updated_since`、bulk export 或額外 projection，按一般 Serve API feature 開發，不把 RAG runtime 合併回 findb。

---

## 優先順序總表

| Phase | 內容 | 時機 | 依賴 |
| --- | --- | --- | --- |
| 1 | Schema 治理（vocabulary、複合 PK、partition） | 立即，趁資料量小 | 無 |
| 2 | ingest / serve 角色分離 + 讀取熱點 | 導入大量資料前 | 無（可與 1 並行） |
| 3 | DB-backed API key | 隨消費者增加 | 無 |
| 4 | nginx cache 層 | 隨讀取流量 | Phase 2 |
| 5 | 債券 / ETF 導入 | 資料到位時 | Phase 1 |
| 6 | RAG 衍生服務 | 需求確立時 | Phase 3 |

## 驗收原則

- 每個 Phase 的 schema 變更一律走 Alembic，先在 partial dump 環境演練（`make seed-upsert` + `migration_workflow.md` 流程）。
- Phase 1、2 完成後，以 `scalability_optimization_checklist.md` Phase 0 的壓測基線重新量測，確認無回歸。
- Serve API 在所有 Phase 中維持唯讀，不因任何新需求破例。
