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

- [ ] 驗證staging branch protection、required CI及Environment protected-branch restriction。
- [ ] 依[staging AWS deployment plan](staging-aws-deployment-plan.md)完成OIDC、SSM、
  runtime secrets、image digest、release manifest、backup、監控與rollback rehearsal。
- [ ] 完成staging credential hygiene：取得Cloudflare與三個provider的action-time authority，
  分別輪替Raw／Canonical R2、Twelve Data／FinLab／Shioaji及RabbitMQ runtime credentials；
  以consumer reload、last-used／health與舊值撤銷留證後，才移除GitHub Environment runtime copies。

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
