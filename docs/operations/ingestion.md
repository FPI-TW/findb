# Durable Ingestion Runbook

## Durable承諾

RabbitMQ是單節點、可重建的delivery layer，不是durable truth。已接受工作的唯一真相：

- `raw.market_payload`
- `ingestion_run`
- `normalization_job`
- `normalization_outbox`

Source API只有在上述資料同一transaction commit後才回 `202`。Canonical endpoint另以
`ingestion_attempt`記錄通過auth/rate-limit gate的每次呼叫。

## 正常路徑

```text
Source API commit
  -> dispatcher publishes outbox
  -> RabbitMQ findb.normalize.v1
  -> Celery worker late ack
  -> normalize transaction
  -> run/job terminal state
```

同dataset以PostgreSQL advisory lock串行；不同dataset可依worker concurrency並行。
Consumer timeout必須高於Celery hard time limit與graceful shutdown所需時間。

目前 staging active feeds 僅為：`twelve_data/us_equity_eod`、`finlab/tw_equity_eod`、
`shioaji/tw_equity_minute`、`shioaji/tw_etf_minute`。Canonical/Serve read model 可
保留歷史資料域，但不應從監控結果推論存在其他 active provider feed。

## 上線前檢查

- `predeploy_db_check.py`通過。
- RabbitMQ EBS容量與CloudWatch告警正常。
- Queue policy `findb-normalization-consumer-timeout`與Compose設定一致。
- Worker ping成功且heartbeat小於90秒。
- Unpublished outbox、queued oldest age、expired lease沒有持續上升。
- `RAW_RETENTION_ENABLED=true`且`RAW_RETENTION_DAYS=30`；事故調查或queue
  recovery期間需要延長保存時，先暫停cleanup再處理。
- Source client已綁定正確source/datasets。

## Staging bounded end-to-end acceptance

Staging驗收的目標是覆蓋完整功能，不是累積資料量。任何data-producing測試都必須
符合[staging data policy](deployment.md#staging-data-policy)，並在執行前記錄
image/config SHA、pilot universe、日期或output上限、預估credits，以及相關資料表的
pre-run counts。禁止以smoke test名義執行完整歷史或完整universe導入。

```bash
docker compose -f docker-compose.prod.yml exec -T rabbitmq \
  rabbitmq-diagnostics -q ping
docker compose -f docker-compose.prod.yml exec -T rabbitmq \
  rabbitmqctl list_policies -p /findb
docker compose -f docker-compose.prod.yml exec -T rabbitmq \
  rabbitmqctl list_queues -p /findb name messages durable arguments
docker compose -f docker-compose.prod.yml exec -T worker \
  celery -A app.task_queue inspect ping --timeout=10
docker compose -f docker-compose.prod.yml exec -T ingest \
  python /app/scripts/check_queue_health.py
```

以固定bounded identity送出pilot payload，確認：

1. Provider raw的R2 reference與checksum存在且符合delivery內容。
2. Source回 `202` 與 `attempt_id/run_id`。
3. Run/job進入預期terminal state，outbox已published且沒有active delivery殘留。
4. DQ結果、canonical values與row counts符合pilot範圍。
5. Serve、Admin與Dashboard可查到相同lineage與結果。
6. 相同內容重送不增加canonical row。
7. 相同key不同內容回 `409`。
8. 記錄post-run counts、queue/DLQ狀態與實際provider credits。

## 監控

最低告警：

- RabbitMQ restart/unhealthy與EBS free space
- Dispatcher/worker exit
- Worker heartbeat age
- Unpublished outbox oldest age
- Queued/retrying job oldest age
- Expired execution leases
- Retry exhausted/DLQ
- Missing expected delivery
- DB connection headroom與長transaction

Delivery policy以最近同source、dataset、schema/version的合格delivery建立baseline。
`warn`階段只產生可觀測訊號；切到 `reject`前必須先用真實feed校準count、freshness、
coverage與missing-delivery時間窗。

Scheduler 的執行時間、timezone 與 dataset mapping 以 `scheduler_control` 和
`scheduler_dataset` 為唯一權威；dataset 的 `delivery_expectation` 只描述該 feed 的資料日期
與完整性政策。現行 Fetcher 會把 DB definition 與 reviewed executable workload 嚴格比對，
不一致時 fail closed；調整 definition 必須同步部署相符 workload。Operations 的 ingestion
overview 每個 scheduler 一張卡，應同時核對 DB
設定時間、desired／observed state、heartbeat、provider raw `fetched_at`、normalization
completion 與 feed freshness。看到 raw 已抓取但 normalization 未完成時，應沿 downstream
run／job／outbox／queue 診斷，不應誤判為 provider 沒有送達。

`tw_equity_eod` 的全域 minimum record count 仍為 2,100。FinLab 兩檔 pilot 只透過
source-scoped override 將 minimum 設為 2 並停用 rolling baseline；其它 source 不繼承這個
放寬。擴大 universe 前必須移除或重新校準 override，不能修改全域門檻規避驗證。

### Shioaji 台股分鐘序列

`tw_equity_minute` 與 `tw_etf_minute` 使用 policy-only 的
`delivery_mode=sequenced_snapshot`。這不會擴張 EOD ingress contract 的
`DeliveryMode`；minute contract 自己驗證 `snapshot_id`、`daily_update_id`、`sequence`
與 `sequence_count`。這兩個資料集的預設 freshness 最大抓取年齡為 6 小時、台北時間
13:30 收盤後 210 分鐘（17:00）才算到期，missing delivery 只監控 `shioaji`。

每個 `ingestion_run` 會保存四個 nullable sequence identity 欄位。分鐘 feed 只有在同一
`snapshot_id` + `daily_update_id`、同一資料日期且同一 `sequence_count` 的成功（非 rerun）
run 完成完整 `1..sequence_count` 後才會顯示 fresh 或解除 missing alert；不同 snapshot
不會拼接，部分或失敗群組會維持 partial/failed，補齊後才恢復。排查時直接查
`ingestion_run` 的 identity、terminal status、normalization job/outbox，不依賴 raw JSON
保留期限。f4 migration 會從仍保留且格式有效的 raw batch 回填舊 run；過期、缺 raw 或
格式不完整的歷史 run 保留四欄 NULL，且不會阻塞 upgrade，也不會被視為完整 snapshot。

## 常見診斷

| 現象 | 優先檢查 |
| --- | --- |
| Source 429 | Client quota、IP bucket、`Retry-After`與重試策略 |
| Source 409 | Idempotency key是否對應不同內容或contract |
| Source 422 | Attempt error code與versioned schema |
| Run pending過久 | Outbox是否發布、queue depth、worker heartbeat |
| Run processing過久 | Lease、worker logs、dataset advisory lock、DB pressure |
| Canonical缺資料 | Run terminal state、DQ errors、source precedence |
| Missing delivery | DB scheduler definition/provider是否送達、per-feed expected data date |
| Raw存在但無canonical | Job state、DQ issue、normalizer transaction |

不要只看HTTP `202`判斷資料完成。

## RabbitMQ volume全毀

1. 暫停provider，停止RabbitMQ、dispatcher與worker。
2. 確認PostgreSQL raw/job/outbox完整，不刪除它們。
3. 清空或更換RabbitMQ EBS後，以相同node name與vhost啟動。
4. 啟動worker，重新宣告版本控制中的exchange、queue與DLQ。
5. 啟動dispatcher。
6. Startup reconciliation重設過期processing job並補建缺少的outbox delivery。
7. 觀察queue health直到backlog下降。

不要對運行中的RabbitMQ message directory做檔案snapshot restore。Delivery由DB
reconciliation重建。

## Worker或broker短暫中斷

- Worker使用late ack；未ack訊息會重新delivery。
- Dispatcher定期探測topology；broker恢復後執行reconciliation。
- 超過execution lease仍未消費的job由DB重新建立delivery。
- Normalizer與idempotency必須容忍at-least-once delivery。

## Pause與rollback

1. 暫停所有provider/scheduler。
2. 停止 `ingest`、`dispatcher`、`worker`、`raw-cleanup`。
3. 保留raw、run、job、outbox、broker volume與logs。
4. Serve健康時繼續提供canonical read。
5. 修復後先以smoke client恢復，再逐一解除provider pause。

Schema migration後不直接回退到不認得目前Alembic head的舊image；採forward fix。
若必須回退分鐘序列 migration，只移除 sequence identity 的 schema objects；因為
registry 沒有保存 policy provenance，downgrade 不會刪除 `delivery_expectation`。
