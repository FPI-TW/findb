# Fetcher 資料自動化 backlog

本文件只追蹤 staging active feeds 的驗收與「日後新增資料域」所需的完整工作；不把
canonical read model 誤列為目前 provider coverage。排程時區固定為 `Asia/Taipei`。

## 目前 active feed

| Provider | Dataset | 目前範圍 | 狀態 |
| --- | --- | --- | --- |
| `twelve_data` | `us_equity_eod` | reviewed bounded US equity universe | staging active |
| `finlab` | `tw_equity_eod` | reviewed bounded TW equity universe | staging active |
| `shioaji` | `tw_equity_minute` | `2330` pilot | staging active |
| `shioaji` | `tw_etf_minute` | `0050`、`0056`、`006201` pilot | staging active |

每個 active feed 都使用獨立 Source client key、provider credential、raw storage ref、
SQLite state 與 bounded universe。Fetcher 只能透過 `/api/v1/source/ingest` 送出
versioned provider-neutral contract；舊 direct/market route、provider-specific route、
退休 scheduler identity 與 futures contract feed 不在 staging。

## 現行安全邊界

- Scheduler 由 DB definition 與 desired state 控制；Fetcher 不直接連 FinDB DB，控制面失聯
  時 fail-closed。
- Calendar 必須是完整 published year；沒有 static weekday fallback。
- Daily scheduler 不得隱式執行歷史 backfill。任何 backfill、archive 或 universe 擴張都要
  另案建立 contract、state、分塊策略與 rollback/cleanup 計畫。
- Universe 只能來自 reviewed、versioned manifest；不得由名稱、cache 或「約 N 檔」推導。
- 同一 canonical row 只能有一個權威來源；source ownership、record count、freshness、
  coverage 與 missing-delivery policy 必須在 staging 觀察窗口中校準。
- FinLab 與 Shioaji optional SDK 只在各自隔離 image/runtime 使用；通用 Fetcher image
  不攜帶不需要的 provider SDK 或 credentials。

## Active feed 驗收 backlog

- [ ] 以四個 active feed 完成至少兩個有效交易日的 bounded live preflight，保存 image/config
  SHA、universe、日期/row 上限、預估 credits 與 post-run counts。
- [ ] 驗證 provider raw artifact、Source `202`、outbox/RabbitMQ、normalization terminal
  state、DQ、canonical rows、Serve/Admin/Dashboard lineage 一致。
- [ ] 驗證相同 idempotency key 重送不增加 canonical row；相同 key 不同內容回 `409`。
- [ ] 驗證 retry、lease recovery、graceful stop、calendar holiday、DST 與 late delivery。
- [ ] 以實際四個 dataset 校準 minimum record count、freshness、coverage 與 missing alert，
  並完成 rollback/cleanup runbook。

## 日後新增資料域的 gate

目前 canonical/Serve 可能保留 macro、bonds、futures、FX、crypto、HK/CN 或其他歷史
read model；這些不是 active provider feed。任何新增資料域（包含現有 read model 的重新
供應）都必須先完成：

1. provider entitlement、sanitized fixtures、source ownership 與 bounded universe。
2. provider-neutral versioned contract、dataset registry、normalizer、DQ 與 migration。
3. Source client scope、rate limit、raw retention、idempotency/retry、calendar 與 scheduler
   definition。
4. Serve read model/API shaping、Dashboard governance mapping、告警與稽核。
5. staging live preflight、至少兩個有效交易日觀察、failure/recovery 演練與 rollback。

未完成上述 gate 前，backlog 只可記為待評估，不得加入 active provider enum、dataset
mapping、scheduler row 或 deployment secret。

## 明確未承諾項目

- 全市場 universe、跨 sequence publication、archive/backfill、永久 Canonical object
  storage 與大型 Export API 尚未列入 active scope。
- 任何退休 provider、舊 direct route 或 futures contract 都不會以相容 fallback
  重新啟用；需求出現時需依完整新版 contract 流程另案設計。
