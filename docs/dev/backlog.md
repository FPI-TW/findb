# Current Backlog

> 只保存已核准範圍內的未完成工作。完成後直接移除；歷史由Git追溯。Active feeds與pilot
> 範圍見[現行架構](../architecture/overview.md#active-feeds)。

## P0：Active feed acceptance

- [ ] 讓四個active feeds各完成至少兩個有效交易日的bounded live observation，保存
  image／config SHA、universe、日期／row上限、credits及pre／post counts。
- [ ] 驗證Raw R2、Source `202`、outbox／RabbitMQ、normalization terminal state、DQ、
  canonical及Serve／Admin／Dashboard lineage一致。
- [ ] 在相同Source client identity、dataset及raw retention窗口內，驗證相同idempotency key
  重送、相同key不同內容`409`，並覆蓋retry、lease recovery、graceful stop、holiday、DST
  及late delivery。
- [ ] 以真實feeds校準minimum record count、freshness、coverage、missing deadline及
  provider欄位消失／異常空snapshot告警，再決定policy是否從`warn`升級。
- [ ] 演練broker全毀、worker kill、DB短暫中斷與重複delivery，保存實測恢復結果。
- [ ] 為每個active provider/client簽發並輪替獨立DB-backed Source key，觀察完整排程週期。

## P0：Staging deployment

- [ ] 等待accepted SHA後的完整原生provider cycle依排程時間觸發，驗證四個active feeds的freshness、
  terminal state與lineage；不得以repair rerun、accepted replay或skipped acquisition smoke取代。
- [ ] 在完整原生provider cycle gate通過後，另行取得移除授權並確認last-used與health，再撤銷GitHub
  Environment runtime copies；此項不與R2實際rotation綁定。Raw與Canonical R2 scope已依使用者核准的
  acceptance criterion變更而完成：既有值維持，未建立新key、未輪替、未替換、未撤銷舊key，故不構成
  rotation或old-value invalidation evidence，也不代表曾執行Cloudflare操作。Twelve Data／FinLab／Shioaji
  provider scope同樣維持既有值並依既有scope決策完成；RabbitMQ rotation已完成。SSH recovery項目仍依
  staging deployment plan的Phase 6 exit gate保留。
- [ ] 完成Fetcher Phase 5 SSM日常deployment transport與相應演練。
- [ ] 完成Phase 6的CloudWatch alarms與synthetic notification、RDS restore、Fetcher SQLite recovery、
  RabbitMQ rebuild、current EBS encrypted backup chain、SSH ingress退場、不同image digest的
  previous-release rollback，以及不同Alembic revision的schema-incompatibility rejection。

## P1：資料完整性與效能

- [ ] 評估canonical／raw `run_id` FK或定期lineage consistency job。
- [ ] 壓測`admin/raw-payloads`的index與pagination。
- [ ] 監控`market_data_eod_default`；資料累積到門檻時建立安全搬移runbook。
- [ ] 建立代表性ingest throughput與Serve latency baseline，再決定batch insert／upsert優化。

## P2：治理

- [ ] 將credential usage aggregate與invalid-key事件接入集中式告警。
- [ ] 新增資料域前，先核准provider entitlement、bounded universe、versioned contract、
  registry、normalizer、DQ、Serve shaping、monitoring與staging acceptance。

## 不變約束

- 新資料來源只走provider-neutral Source contract；不恢復legacy direct routes。
- Serve不寫DB；canonical read model不代表active provider feed。
- Schema只經Alembic改變；Fetcher不連FinDB DB、不import backend runtime。
- Contract升級採backend-first expand/migrate/contract。
