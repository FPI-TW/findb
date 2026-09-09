# Current Backlog

> 只保存已核准範圍內的未完成工作。完成後直接移除；歷史由Git追溯。Active feeds與pilot
> 範圍見[現行架構](../architecture/overview.md#active-feeds)。

## P0：Active feed acceptance

- [ ] 在下一個新有效交易日後，以2026-09-09 `pre` manifest為基準產生SHA-linked `post`
  manifest。`pre`已由單一operator coordinator及兩個unit-local read-only probes完成：四個feed均有
  兩個交易日、config／universe SHA、bounded symbols、credits/cap、scheduler checkpoint state、
  Source／Raw／job／outbox／DQ／canonical counts；去敏manifest SHA-256為
  `7c234156bdf18964c7f1dc5d164a208a00c42db7458ee35fb30fffc0ec0e8e68`，保存於versioned、KMS-encrypted
  S3 object `evidence/staging/active-feeds/2026-09-09/pre-7c234156bdf18964.json`（version
  `Pw6cXtCwp7juQkCuI4AGvFxhulk4R03D`）。FinLab／Twelve Data的持久化`ingestion_attempt.http_status=202`
  證據已成立；minute lineage明確將Serve標為`not_applicable`，原因為
  `market_minute_read_model_not_exposed`，並要求Admin raw／freshness／Dashboard營運讀路徑。

  同日已用最新retained real Twelve Data delivery進行缺少必要`close`與異常空snapshot校準：前者
  持久化`422/INGRESS_SCHEMA_INVALID` attempt，後者依現行warn policy持久化`202`零筆run；
  `ActiveFeedRejectedAttempts`與`ActiveFeedEmptySnapshots`均呈現`0 -> 1`並使各自alarm由`OK -> ALARM`，
  `ActiveFeedDQErrors`維持`0/OK`。minimum count、freshness、coverage及missing deadline維持既有
  bounded設定與`warn`決策；此校準已完成，不再另列backlog。不得用同一交易日重跑冒充自然`post`。

## P0：Staging deployment

- [ ] 驗證daily DLM policy `policy-0d0a29c9e19f6323e`為兩個current encrypted root volumes
  連續產生首個及第二個排程recovery point，包括volume ID、encryption、policy tag與7份retention。
  2026-09-08已將schedule-only tag改為`BackupPurpose`並以fresh zero-delete OpenTofu plan原地apply；AWS
  回讀policy為`ENABLED`、`CopyTags=true`、每日`09:00 UTC`、保留7份。DLM-tagged snapshots目前仍為0，
  因此尚須等待首兩個實際排程週期。
  兩個即時manual encrypted snapshots仍為`completed`並保留至2026-10-03，但不能替代recurring chain。
  `DLMPolicyHealthy` custom metric與`findb-staging-dlm-policy-unhealthy` alarm已於2026-09-09上線並為`OK`；
  它監控policy state／status與collector missing data，但不能替代實際snapshot recovery-point驗收。
  2026-09-09 07:25 UTC的pre-window回讀再次確認policy為`ENABLED`、兩個current volumes均為in-use且
  encrypted並帶exact selector tag；當時尚未到每日09:00 UTC執行窗，DLM-tagged snapshots仍為0。
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
