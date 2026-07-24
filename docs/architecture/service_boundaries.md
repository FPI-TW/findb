# Monorepo 與服務邊界

## 核准方向

FinDB backend、Dashboard 與 Fetcher 放在同一個 Git repository，以便 contract、
fixtures 與整合測試在同一個 PR 演進；各服務仍為獨立 release unit。

目前結構：

```text
findb/
├── backend/              FinDB API、queue orchestration、normalization
├── dashboard/            營運介面
├── fetcher/              Contract validation與delivery client基礎
├── contracts/            發布後不可變的 machine-readable contracts
└── backend/tests/        Contract artifact drift與backend acceptance tests
```

Provider adapters、scheduler、checkpoint、S3 raw storage、跨process integration
tests與Fetcher deployment workflow尚未建立。

## Release 與部署單位

| 單位 | Image | 部署位置 | 狀態 |
| --- | --- | --- | --- |
| FinDB backend | `findb:<sha>` | FinDB EC2 | 已有 |
| Dashboard | `findb-dashboard:<sha>` | FinDB EC2 | 已有 |
| Fetcher | `findb-fetcher:<sha>` | 獨立 Fetch EC2 | Package/image/CI已有；部署規劃中 |

同 repo 不代表同時部署。每個服務需要獨立 path-filtered CI、image tag、deployment
workflow、concurrency group 與 rollback。環境必須記錄實際部署的 image SHA 與啟用的
contract versions。

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

Fetcher 負責：

- Provider SDK/API、抓取排程與限流
- Provider payload轉換成 versioned FinDB contract
- 原始檔寫入自己的 S3/object storage
- Stable idempotency key、retry、checkpoint與delivery status
- 真實 fixtures與adapter mapping tests

Fetcher 只能透過 HTTPS Source API 與 FinDB互動；不得取得 FinDB DB、RabbitMQ、
Admin或Serve credentials。

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
