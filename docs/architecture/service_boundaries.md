# Monorepo 與服務邊界

## 核准方向

FinDB backend、Dashboard 與 Fetcher 放在同一個 Git repository，以便 contract、
fixtures 與整合測試在同一個 PR 演進；各服務仍為獨立 release unit。

目前結構：

```text
findb/
├── backend/              FinDB API、queue orchestration、normalization
├── dashboard/            營運介面
├── fetcher/              Provider adapter、contract validation與delivery client
├── contracts/            發布後不可變的 machine-readable contracts
└── backend/tests/        Contract artifact drift與backend acceptance tests
```

Twelve Data Common Stock日線adapter、去識別化fixture、正式manual delivery/wait
CLI、versioned小型US symbol universe、bounded per-symbol orchestration、Fetcher-owned
SQLite scheduler state、persistent retry、lease recovery、逐symbol checkpoint與mock
Source API整合測試，以及exact-byte provider response的Fetcher-owned Cloudflare R2 persistence
與raw reference/checksum delivery已建立；自動化真實跨服務integration tests尚未
建立。Fetcher CD目前只負責immutable image release與獨立部署handoff，尚未啟用持續
運作的production fetch loop，也不代表production R2 bucket、API token或retention已建立。

## Release 與部署單位

| 單位 | Image | 部署位置 | 狀態 |
| --- | --- | --- | --- |
| FinDB backend | `findb:<sha>` | FinDB EC2 | 已有 |
| Dashboard | `findb-dashboard:<sha>` | FinDB EC2 | 已有 |
| Fetcher | `ghcr.io/fpi-tw/findb-fetcher:<sha>` | 獨立 Fetcher target | Runtime能力已有；production scheduler尚未啟用 |

同 repo 不代表同時部署。目前以四個workflow分離FinDB CI、FinDB CD、Fetcher CI與
Fetcher CD；FinDB deployment unit包含backend與Dashboard。各自使用path filter、
image tag、CD concurrency group與rollback，CD job分別綁定 `production-findb`和
`production-fetcher`。Contract變更可觸發兩個CI，但contract-only變更不自動部署
Fetcher；自動CD只部署已通過對應CI的同一commit。環境必須記錄實際部署的image SHA
與啟用的contract versions。

## FinDB 邊界

FinDB 負責：

- Source、Serve、Admin API
- ingress validation、idempotency、raw audit
- durable job/outbox 與 normalization
- DQ、canonical schema、migration
- source client與consumer API key治理

FinDB 不負責：

- Provider authentication與抓取排程
- Provider-specific欄位 mapping
- Provider 原始檔長期保存
- Fetcher checkpoint與retry state
- Embedding、vector index、LLM runtime

## Fetcher 邊界

Fetcher完整runtime完成後負責：

- Provider SDK/API、抓取排程與限流
- Provider payload轉換成 versioned FinDB contract
- 原始檔寫入自己的Cloudflare R2 bucket
- Stable idempotency key、retry、checkpoint與delivery status
- 真實 fixtures與adapter mapping tests

Fetcher 只能透過 HTTPS Source API 與 FinDB互動；不得取得 FinDB DB、RabbitMQ、
Admin或Serve credentials。

目前已將Twelve Data列為固定資料來源之一，並落地其日線adapter、contract驗證、
明確選用的manual delivery/wait CLI、受治理symbol universe、逐symbol獨立identity、
credit/record/date bounds、bounded HTTP retry、SQLite persistent scheduler state、
lease recovery、checkpoint、exact-byte raw R2 persistence、readiness與image/CI/CD
foundation；其餘項目保留在backlog。

## CI/CD 與credential邊界

- FinDB CD只能取得FinDB target、DB、RabbitMQ、Source/Admin/Serve、Dashboard與TLS
  credentials。
- Fetcher CD只能取得Fetcher target、provider、Fetcher Source client與Fetcher raw
  object storage credentials。
- Production secrets必須放入對應GitHub Environment並從repository-level移除；
  environment隔離不會自動限制仍留在repository scope的secret。
- GitHub Environments、OIDC roles、AWS secret paths與EC2 target均為外部資源，不能
  僅憑workflow檔宣稱已建立。
- 所有workflow可手動執行；contract rollout使用backend-first，先部署可同時接受
  新舊版本的FinDB，再明確啟動Fetcher CD切換版本。

## Dashboard 與下游

- Dashboard透過 Serve/Admin API運作，不直接連 DB。
- 公開 lookup只能使用預先生成的非敏感 cache。
- Dashboard session、Admin與Serve credentials不得進入 client bundle。
- RAG只消費 Serve API或明確授權的 read replica，不回寫 FinDB。

## 共享範圍

允許共享：

- Versioned JSON Schema與contract manifest
- 無 secrets 的 fixtures
- source naming、UTC、idempotency等純規則
- Contract validation測試工具

`contracts/` artifacts由backend registry確定性產生並以SHA-256 manifest固定。
`backend/scripts/export_ingress_contracts.py --check`負責阻擋drift。

禁止共享：

- Backend ORM與migration code
- FinDB database session/config
- Provider credentials
- Runtime secrets
- Normalizer implementation

## 相容性 gate

Monorepo CI至少要驗證：

- Provider fixture經 adapter後符合 pin 的 contract。
- Fetcher pin 的版本存在且被對應 dataset接受。
- 相同 idempotency key重送不產生重複 canonical row。
- 相同 key、不同內容回 `409`。
- full snapshot、coverage、record count與freshness規則。
- 架構測試禁止 `fetcher` import backend ORM、normalizer或DB modules。

Contract變更以 backend-first expand/migrate/contract順序部署，不能假設兩個 EC2會同時
成功更新。
