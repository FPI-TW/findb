# Current Backlog

> 只保存未完成工作。完成項目應從本檔刪除，由 Git history 追溯。

## Active staging boundary

目前只啟用四個 provider/dataset 配對：

| Provider | Dataset |
| --- | --- |
| `twelve_data` | `us_equity_eod` |
| `finlab` | `tw_equity_eod` |
| `shioaji` | `tw_equity_minute` |
| `shioaji` | `tw_etf_minute` |

Canonical tables、Serve API 與 lookup read model 可保留歷史或預留資料域；這些資料域
沒有 active provider feed。舊 provider、direct ingest route 與 futures contract feed 已
移除；新增資料域必須另案完成完整新版 contract、registry、normalizer、DQ、Serve schema
與 staging 驗收。

## P0：Staging ingestion 與 deployment

- [ ] 以四個 active feed 完成 bounded live delivery、record count、freshness、coverage
  與 missing-delivery policy 校準。
- [ ] 完成 delivery-missing monitor、schema drift、provider 欄位消失與異常空 snapshot
  告警。
- [ ] 驗證 broker 全毀、worker kill、DB 短暫中斷與重複 delivery 演練。
- [ ] 為 workflow、infra 與 contract 設定 CODEOWNERS、staging Environment protection。
- [ ] 建立 service-specific GitHub OIDC AWS deploy roles，並以 SSM/deployment service
  取代長效 EC2 SSH key。
- [ ] Runtime secrets 搬至 AWS Secrets Manager/Parameter Store，建立 service-specific
  instance role、secret path 與 KMS policy。
- [ ] 為每個 active provider/client 簽發並輪替 DB-backed Source client key，完成一個
  完整排程週期的 credential usage 觀察。

## P1：資料完整性與效能

- [ ] 評估 canonical/raw 的 `run_id` FK 或定期 lineage consistency job。
- [ ] 壓測並優化 `admin/raw-payloads` 的 index 與 pagination。
- [ ] 監控 `market_data_eod_default`；若累積資料，建立安全搬移 runbook。
- [ ] 建立代表性 ingest 吞吐與 Serve latency baseline。
- [ ] 依量測結果導入 bulk insert/upsert 與 batch DQ，不先做無基線微調。

## P2：服務與治理

- [ ] 評估 Serve read replica；前提是維持完全唯讀。
- [ ] 將 credential usage aggregate 與 invalid-key 安全事件接入集中式告警。
- [ ] 需求出現後再建 S3/Parquet bulk export，不以深分頁 JSON 支援大型回測。
- [ ] 若要新增債券、ETF、macro、futures 或其他 asset，先核准完整新版 contract、
  fixtures、canonical mapping 與 Serve read model，再排入 staging 驗收。

## 不變約束

- 新資料來源只走 Source API 的 versioned provider-neutral contract。
- Serve 不寫 DB；canonical read model 不代表 active provider feed。
- Schema 只透過 Alembic 變更。
- Fetcher 不連 FinDB DB、不 import ORM/normalizer。
- Contract 升級採 backend-first expand/migrate/contract。
