# Current Backlog

> 只保存已核准範圍內的未完成工作。完成後直接移除；歷史由Git追溯。Active feeds與pilot
> 範圍見[現行架構](../architecture/overview.md#active-feeds)。

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
