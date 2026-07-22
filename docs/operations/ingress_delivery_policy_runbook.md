# Ingress Delivery Policy 維運手冊

Canonical ingest 的 `delivery_expectation` 是同步 guardrail；目前四個首批 dataset 在 migration 與
seed 中一律使用 `warn`，完成 shadow 校準前不得直接切為 production `reject`。

## 上線前校準

1. 累積至少 5–10 個交易日的 shadow deliveries。
2. 檢查 `ingestion_run.policy_details` 的 count 分布、baseline provenance、freshness 與 expected date。
3. 確認 `trading_calendar` 每日（包含週末／休市日）都有 market row，open day 的
   `session_close` 正確；缺資料會產生 `CALENDAR_UNAVAILABLE` warning，不會 reject。
4. 依 dataset 分別調整 `minimum_record_count`、drop ratio、fetch age 與 close grace。
5. 先逐條將 action 從 `warn` 改為 `reject`，每次只切一個 feed 並保留 rollback config。

## 觀測與診斷

- Warning：run 正常排入 queue，並有且只有一筆
  `DQIssue.issue_type=INGRESS_DELIVERY_POLICY_WARNING`。
- Reject：attempt 為 `rejected`，`failure_code` 是 primary code，`failure_details.violations` 保存所有
  結果；不應存在對應 raw/run/job/outbox。
- `BATCH_RECORD_COUNT_DROP` 的 `reason` 為 `absolute_minimum` 或 `relative_drop`。
- `STALE_PAYLOAD` 的 `reason` 為 `stale` 或 `future_clock`。
- Baseline 只使用相同 dataset/source/schema、較早資料日、status 恰為 `completed`、policy pass、
  full snapshot 且非 rerun 的 run。

若誤判阻擋正常資料，先把該規則 action 降回 `warn`，使用相同 idempotency key 重送只有在先前
已有 accepted run 時才會直接回原 run；曾被 policy reject 的新 delivery 沒有 raw/run，修正設定後
可用原 idempotency key 再送。

## 尚未涵蓋

此同步 policy 無法發現完全沒有 request 到達的 dataset。`DATASET_DELIVERY_MISSING` scheduler／alert
是下一個獨立 PR，不應以本功能取代外部 feed heartbeat。
