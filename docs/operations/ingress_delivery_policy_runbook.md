# Ingress Delivery Policy 維運手冊

Canonical ingest 的 `delivery_expectation` 是同步 guardrail。同步規則目前四個首批 dataset 在
migration 與 seed 中使用 `warn`；完全未送達監控則一律預設 `disabled`，完成 feed cutover 前
不得啟用，避免對尚未遷移的來源誤報。

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

## 完全未送達監控

Feed cutover 後，在 dataset 的 `delivery_expectation` 加入：

```json
"missing_delivery": {
  "action": "warn",
  "expected_sources": ["finlab"]
}
```

來源名稱必須符合 canonical ingress 的 `^[a-z0-9_]+$`，identity 為
`source + dataset_key + schema_id/version`，feed 名稱
顯示為 `{source}:{dataset_key}`。Dispatcher 啟動時掃描一次，之後每
`DELIVERY_MONITOR_SECONDS`（預設 60 秒）掃描。Monitor 在獨立 background task 與 DB session
執行，不阻塞 outbox claim/publish；單次掃描由 `DELIVERY_MONITOR_TIMEOUT_SECONDS`（預設 30 秒）
取消並 rollback。它重用同步 policy 的 timezone、calendar、session
close/fallback close 與 grace resolver；calendar 不覆蓋評估日或尚無已關閉 open session 時，只記
bounded diagnostic，不猜 weekday、不建立 alert。

任一相同 identity/date、`full_snapshot`、非 rerun 的 `ingestion_run` 都視為已送達，不要求
normalization completed 或 policy pass；policy reject 因沒有 run，仍會被視為 missing。重複掃描只
更新同一 alert 的 `last_detected_at`，late delivery 會自動將其改為 `resolved`。停用 policy 或 dataset
不會假裝資料已送達，既有 open alerts 保留供人工稽核，只有實際 late run 會自動 resolve。
Monitor 對每個 feed 使用非阻塞 transaction advisory lock 與短 transaction；ingress acceptance
使用相同 feed lock，並在建立 run 的同一 transaction 主動 resolve exact alert，避免 ingest commit
後殘留 false-open 視窗。Monitor 不會在一個 transaction 累積多個 feed locks。

使用 `GET /api/v1/admin/missing-deliveries` 依 status、dataset、source 查詢；queue health 的
`missing_deliveries` 與 oldest 欄位可供外部監控。Slack、email、PagerDuty 等通知不在本功能內，應由
外部監控讀取 Admin health 指標後發送。
