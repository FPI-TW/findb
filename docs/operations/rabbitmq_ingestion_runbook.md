# EC2 RabbitMQ 與 Durable Ingestion 維運手冊

## 邊界與承諾

RabbitMQ 是單節點、可重建的 delivery layer，不是 durable truth，也不提供 EC2/node failure HA。已接受工作的唯一真相是 Aurora/RDS 的 `raw.market_payload`、`ingestion_run`、`normalization_job` 與 `normalization_outbox`。API 僅在四者同一 transaction commit 後回 `202`。

## EC2 前置條件

- Deploy workflow 會建立 `/var/lib/findb/rabbitmq` 並設定 RabbitMQ container UID/GID 999 可寫；資料會保留於 EC2 host filesystem，請由磁碟監控與備份政策管理容量及復原需求。
- 5672/15672 不建立 EC2 security-group ingress，也不在 Compose 映射 host port。

Production secrets 必須設定 `CELERY_BROKER_URL`、`RABBITMQ_DEFAULT_USER`、`RABBITMQ_DEFAULT_PASS`、`RABBITMQ_ERLANG_COOKIE`。URL 使用 vhost `/findb`；若以 URI 表示，slash 必須正確 percent-encode 或使用已驗證可連線的完整 secret。

若 RabbitMQ data directory 已初始化，修改 `RABBITMQ_DEFAULT_USER` 或 `RABBITMQ_DEFAULT_PASS` 不會更新既有 broker user。部署前必須以 worker 的 Celery ping 實際驗證 credentials；`CELERY_BROKER_URL` 中的密碼若含特殊字元必須 URL encode。

Normalization queue 透過 `findb-normalization-consumer-timeout` RabbitMQ policy 設定 `consumer-timeout=3600000`（60 分鐘），必須高於 Celery hard time limit（預設 35 分鐘）與正常 graceful shutdown 所需時間。Compose 會先以一次性 `rabbitmq-policy` service 套用 policy，再啟動 dispatcher 與 worker；timeout 不放在 client queue arguments，避免日後調整時因 queue property 不等價而收到 `PRECONDITION_FAILED`。修改 task time limit 時只需調整 `docker-compose.prod.yml` 的 `x-normalization-consumer-timeout`；app 與 policy service 共用同一 YAML anchor，deploy 也會比對 broker 上的實際 policy 值。

## Production Go/No-Go

部署前必須全部成立：

- RDS snapshot 與 point-in-time recovery 已確認，且 production clone 已成功跑到 Alembic head。
- `python /app/scripts/predeploy_db_check.py` 通過：沒有重複 raw `run_id`、超過五分鐘的 transaction，並在扣除 PostgreSQL reserved slots 後保留至少 10 個一般 client connection slots；可用 `PREDEPLOY_MIN_DB_CONNECTION_HEADROOM` 調高門檻。
- data-provider 已暫停排程，或已確認 timeout、429、502、503、504 會使用相同 `Idempotency-Key` 重試。
- 第一次 durable queue 部署維持 `RAW_RETENTION_ENABLED=false`；待 backlog、reconciliation 與監控穩定後才開啟。
- CloudWatch Agent、container monitor 與 queue-health custom metrics 已存在；沒有監控時不得恢復無人值守的 provider 流量。
- 已接受本次 schema 是 forward-only：舊 image 的 `init_db()` 會因 Alembic head 不一致而拒絕啟動，不能作為完整 rollback image。

部署期間 workflow 會先執行 read-only DB preflight，再停止 `ingest`、`dispatcher`、`worker`、`raw-cleanup`，最後才套 migration。若 DB preflight 失敗，既有服務不會被停止。

## 部署與 smoke test

Deploy workflow 會停止所有 DB writer、套 migration、啟動 RabbitMQ/dispatcher/worker/ingest，再依序驗證 queue topology、Celery ping、app health、worker heartbeat、expired lease 與 nginx health。啟動階段不使用全域 Compose health barrier，避免單一服務逾時時遮蔽根因；任何階段失敗都會輸出 Compose 狀態、container exit/OOM 狀態、healthcheck history 與最近 logs。部署後仍應人工執行：

```bash
docker compose -f docker-compose.prod.yml exec -T rabbitmq rabbitmq-diagnostics -q ping
docker compose -f docker-compose.prod.yml exec -T rabbitmq rabbitmqctl list_policies -p /findb
docker compose -f docker-compose.prod.yml exec -T rabbitmq rabbitmqctl list_queues -p /findb name messages durable arguments
docker compose -f docker-compose.prod.yml exec -T worker celery -A app.task_queue inspect ping --timeout=10
docker compose -f docker-compose.prod.yml exec -T ingest python /app/scripts/check_queue_health.py
docker compose -f docker-compose.prod.yml ps
```

送一筆帶固定 idempotency key 的 smoke payload，確認 response 為 202；以 run endpoint 確認最後進入 terminal state。重啟 RabbitMQ 與 kill worker child 後用同 key 重送，run 不應重複，canonical 結果不應增加重複列。

### Source client 切換

第一次 deployment 可暫時保留 legacy `SOURCE_API_KEY`，但 provider-specific identity 必須透過 `POST /api/v1/admin/source-clients` 建立。建立時固定 `source_name`、`allowed_datasets` 與 quota；plaintext key 只回傳一次，交付 provider 並完成 smoke test 後，才安排撤換 legacy key。

恢復 provider 流量前確認 Admin queue health：worker heartbeat 小於 90 秒、expired lease 為 0、unpublished outbox 與 queued oldest age 沒有持續上升。若歷史 pending run 在 migration 後產生 backlog，先等待它下降再解除 provider pause。

## Broker volume 全毀恢復

1. 停止 RabbitMQ、dispatcher 與 worker。
2. 確認 Aurora/RDS 可用，不刪除 raw/job/outbox。
3. 清空或更換 RabbitMQ EBS 後，以相同 hostname `findb-rabbitmq`、node name `rabbit@findb-rabbitmq` 啟動。
4. 啟動 worker，使版本控制中的 exchange/queue/DLQ topology 重新宣告。
5. 啟動 dispatcher。startup reconciliation 會重設 lease 過期的 processing job，並為沒有 pending delivery 的非 terminal job建立新 outbox event。
6. 觀察 `/api/v1/admin/queue/health`，直到 unpublished 與 queued oldest age 恢復正常。

Dispatcher 每 30 秒主動探測 topology。若觀察到 broker 中斷後恢復，會立即執行全量 DB reconciliation；即使中斷短到未被探測，超過 execution lease（預設 40 分鐘）仍未被消費的 queued delivery 也會由 RDS 重新建立 delivery，作為最終安全網。

不對運行中的 RabbitMQ message directory做檔案 snapshot restore；已 publish 但未處理的訊息由 DB reconciliation 重建。

## CloudWatch 告警最低集合

EC2/CloudWatch Agent 必須涵蓋 CPU、memory、root disk 與 RabbitMQ EBS free space。Container health monitor 必須涵蓋 RabbitMQ restart/unhealthy、dispatcher exit、worker exit。另由排程以 Admin queue health endpoint送出 custom metrics：unpublished outbox oldest age、queued/retrying oldest age、expired leases、retry exhausted、worker heartbeat age。建議 worker heartbeat age超過 90 秒即告警。

## Rollback

不 downgrade schema、不刪除 raw/job/outbox。發現問題時先暫停 provider，並停止 `ingest`、`dispatcher`、`worker`、`raw-cleanup`；這會保留所有已接受工作，serve 若健康可繼續提供 canonical data。

本次 migration 後不能直接回退到 migration 前 image：舊 image 內的 Alembic head 不同，`init_db()` 會拒絕啟動，而且舊 ingest 不會填入新的 raw primary key。只有包含目前 migration chain、並經過新 schema 測試的 image 才可作為 rollback image；否則採 forward fix。部署前應記錄本次 image SHA，失敗時保留它供診斷，不執行 schema downgrade 或刪除 outbox/raw。
