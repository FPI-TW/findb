# Current Backlog

> 只保存已核准範圍內的未完成工作。完成後直接移除；歷史由Git追溯。Active feeds與pilot
> 範圍見[現行架構](../architecture/overview.md#active-feeds)。

## P0：Active feed acceptance

- [ ] 讓四個active feeds各完成至少兩個有效交易日的bounded live observation，保存
  image／config SHA、universe、日期／row上限、credits及pre／post counts。2026-09-08已確認四個
  feed各至少兩個有效交易日的DB terminal／canonical counts及Raw R2 checksum；尚缺完整的
  config／universe／credits／pre-post evidence package，故本項不關閉。
- [ ] 驗證Raw R2、Source `202`、outbox／RabbitMQ、normalization terminal state、DQ、
  canonical及Serve／Admin／Dashboard lineage一致。2026-09-08已驗證16個Raw R2 objects實際內容、
  metadata與DB contract checksum一致，四個feed的run／job／outbox／DQ／canonical lineage及代表性
  Admin raw查詢均通過，Dashboard lookup與Referer注入的Serve freshness為HTTP 200，EOD查詢精確回傳
  FinLab 2筆及Twelve Data 3筆；當日Shioaji四個ingest有Nginx `202`。FinLab／Twelve Data所選歷史
  run的原始`202` access log未跨部署保留，且minute資料沒有Serve read model，仍須以可持久證據補齊。
- [ ] 補齊retry、lease recovery、graceful stop、holiday、DST及late delivery驗收。相同Source client
  identity、dataset及raw retention窗口內的固定idempotency驗收已於2026-09-08完成：相同內容重送
  回`202`並重用原run、attempt為`duplicate`；相同key不同內容回
  `409/IDEMPOTENCY_PAYLOAD_MISMATCH`、attempt為`rejected`，raw rows維持22不變。
- [ ] 以真實feeds校準minimum record count、freshness、coverage、missing deadline及
  provider欄位消失／異常空snapshot告警，再決定policy是否從`warn`升級。

## P0：Staging deployment

- [ ] 驗證daily DLM policy `policy-0d0a29c9e19f6323e`為兩個current encrypted root volumes
  連續產生首個及第二個排程recovery point，包括volume ID、encryption、policy tag與7份retention。
  2026-09-08已將schedule-only tag改為`BackupPurpose`並以fresh zero-delete OpenTofu plan原地apply；AWS
  回讀policy為`ENABLED`、`CopyTags=true`、每日`09:00 UTC`、保留7份。DLM-tagged snapshots目前仍為0，
  因此尚須等待首兩個實際排程週期。
  兩個即時manual encrypted snapshots仍為`completed`並保留至2026-10-03，但不能替代recurring chain。
  `DLMPolicyHealthy` custom metric與`findb-staging-dlm-policy-unhealthy` alarm已於2026-09-09上線並為`OK`；
  它監控policy state／status與collector missing data，但不能替代實際snapshot recovery-point驗收。
- [ ] DLM排程recovery point與新RabbitMQ持續健康確認後，另行核准清理
  `/var/lib/findb/rabbitmq.phase6-pre-rebuild-20260903T091531Z`；清理前保留為可復原的演練稽核副本，
  避免無期限占用FinDB root volume。
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
