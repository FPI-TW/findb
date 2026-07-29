# Current Backlog

> 只保存未完成工作。完成項目應從本檔刪除，由Git history追溯。

## P0：Fetcher落地

- [ ] 依資料來源優先序補FinLab/Bloomberg真實fixtures與provider mapping；
  Twelve Data Common Stock日線adapter、fixture與mock Source API整合測試已完成。
- [ ] Shadow delivery後逐一將legacy feed切到canonical `/source/ingest`。
- [ ] 所有保留raw不再需要legacy rerun後，移除provider-specific endpoints/normalizers。

## P0：Deployment與secret isolation

- [ ] 建立 `production-findb`與`production-fetcher` Environment，將production部署
  secrets隔離到對應Environment，並確認repository scope無殘留。
- [ ] 為workflow、infra與contract設定CODEOWNERS與environment protection。
- [ ] 建立service-specific GitHub OIDC AWS deploy roles。
- [ ] 以SSM/deployment service取代長效EC2 SSH key。
- [ ] Runtime secrets搬至AWS Secrets Manager/Parameter Store。
- [ ] 建立不同EC2 instance role與secret path/KMS policy。
- [ ] 為每個Fetcher/provider簽發DB-backed source client key，移除legacy共享
  `SOURCE_API_KEY`。
- [ ] 完成一個完整排程週期的credential usage觀察後，移除 legacy
  `ADMIN_API_KEY`。

## P1：Ingress production readiness

- [ ] 若要在FinDB全域強制raw provenance成對，採backend-first新增ingress
  contract v2；不得原地收緊已發布的v1 contract。
- [ ] 用真實feed校準record count、freshness、coverage與missing-delivery policy。
- [ ] 完成warn → reject切換準則與production觀察窗口。
- [ ] 建立delivery-missing monitor與告警接收流程。
- [ ] 為schema drift、provider欄位消失與異常空snapshot建立告警。
- [ ] 驗證broker全毀、worker kill、DB短暫中斷與重複delivery演練。

## P1：資料完整性與效能

- [ ] 評估canonical/raw的 `run_id` FK或定期lineage consistency job。
- [ ] 壓測並優化 `admin/raw-payloads` 的index與pagination。
- [ ] 監控 `market_data_eod_default`；若累積資料，建立安全搬移runbook。
- [ ] 建立代表性ingest吞吐與Serve latency baseline。
- [ ] 依量測結果導入bulk insert/upsert與batch DQ，不先做無基線微調。

## P2：服務與治理

- [ ] 評估Serve read replica；前提是維持完全唯讀。
- [ ] 將credential usage aggregate與invalid-key安全事件接入集中式告警。
- [ ] 需求出現後再建S3/Parquet bulk export，不以深分頁JSON支援大型回測。
- [ ] 新增債券、ETF或其他asset時，先取得真實payload contract與fixtures。

## 不變約束

- 新資料來源只走Source API。
- 新feed使用provider-neutral versioned contract。
- Serve不寫DB。
- Schema只透過Alembic變更。
- Fetcher不連FinDB DB、不import ORM/normalizer。
- Contract升級採backend-first expand/migrate/contract。
