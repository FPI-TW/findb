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
  provider scope同樣維持既有值並依既有scope決策完成；RabbitMQ rotation與SSH recovery key退役已完成。
- [ ] 補齊Phase 6的disk／inode、Docker restart、RabbitMQ disk／memory、scheduler heartbeat、
  deployment failure與RDS backup failure告警。未完成風險：目前六個native alarms均為健康時，仍可能
  漏掉磁碟滿載、scheduler停滯、container crash loop、broker資源壓力、部署失敗或備份失敗。
- [ ] 為兩個current encrypted root volumes建立automated AWS Backup或DLM policy與可驗證的recurring
  recovery points。未完成風險：現有migration snapshots是一次性且標記短期保留，instance termination、
  volume損毀或誤刪時沒有符合Phase 6要求的current-volume持續復原鏈。
- [ ] 完成Fetcher SQLite一致性backup／restore與RabbitMQ由PostgreSQL outbox重建。未完成風險：
  provider checkpoint可能無法在host loss後可靠復原；broker全毀後的實際queue重建時間與重複delivery
  行為仍未知。
- [ ] 使用同Alembic revision、不同image digests的accepted predecessor完成application-only rollback。
  未完成風險：雖已有合格immutable候選，但尚未實證不重跑migration即可恢復previous release與health。
- [ ] 使用不同Alembic revision的accepted release完成schema-incompatibility rejection。未完成風險：
  尚未live證明preflight會fail closed、拒絕rollback且writers保持停止。
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
