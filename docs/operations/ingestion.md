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

## 上線前檢查

- `predeploy_db_check.py`通過。
- RabbitMQ EBS容量與CloudWatch告警正常。
- Queue policy `findb-normalization-consumer-timeout`與Compose設定一致。
- Worker ping成功且heartbeat小於90秒。
- Unpublished outbox、queued oldest age、expired lease沒有持續上升。
- 第一次啟用durable queue期間保持 `RAW_RETENTION_ENABLED=false`。
- Source client已綁定正確source/datasets。

## Smoke test

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

送一筆固定idempotency key的payload，確認：

1. Source回 `202` 與 `attempt_id/run_id`。
2. Run最後進入completed或明確failed terminal state。
3. 相同內容重送不增加canonical row。
4. 相同key不同內容回 `409`。

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

## 常見診斷

| 現象 | 優先檢查 |
| --- | --- |
| Source 429 | Client quota、IP bucket、`Retry-After`與重試策略 |
| Source 409 | Idempotency key是否對應不同內容或contract |
| Source 422 | Attempt error code與versioned schema |
| Run pending過久 | Outbox是否發布、queue depth、worker heartbeat |
| Run processing過久 | Lease、worker logs、dataset advisory lock、DB pressure |
| Canonical缺資料 | Run terminal state、DQ errors、source precedence |
| Missing delivery | Scheduler/provider是否送達、data date與expected source |
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
