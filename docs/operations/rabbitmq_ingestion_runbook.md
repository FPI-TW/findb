# EC2 RabbitMQ 與 Durable Ingestion 維運手冊

## 邊界與承諾

RabbitMQ 是單節點、可重建的 delivery layer，不是 durable truth，也不提供 EC2/node failure HA。已接受工作的唯一真相是 Aurora/RDS 的 `raw.market_payload`、`ingestion_run`、`normalization_job` 與 `normalization_outbox`。API 僅在四者同一 transaction commit 後回 `202`。

## EC2 與 EBS 前置條件

- Instance 至少 4 vCPU、8 GiB RAM；deploy workflow 會硬性檢查。
- 建立 20 GiB encrypted gp3 EBS，設定 `DeleteOnTermination=false`。
- 格式化後以 filesystem UUID 寫入 `/etc/fstab`，掛載點固定為 `/var/lib/findb/rabbitmq`。
- 執行 `findmnt /var/lib/findb/rabbitmq`、`lsblk -f`、`df -h /var/lib/findb/rabbitmq` 驗證；RabbitMQ container UID/GID 999 必須可寫。
- 5672/15672 不建立 EC2 security-group ingress，也不在 Compose 映射 host port。

Production secrets 必須設定 `CELERY_BROKER_URL`、`RABBITMQ_DEFAULT_USER`、`RABBITMQ_DEFAULT_PASS`、`RABBITMQ_ERLANG_COOKIE`。URL 使用 vhost `/findb`；若以 URI 表示，slash 必須正確 percent-encode 或使用已驗證可連線的完整 secret。

## 部署與 smoke test

Deploy workflow 會停止舊 ingest、套 migration、啟動 RabbitMQ/dispatcher/worker/ingest，再檢查 broker 與 container。部署後執行：

```bash
docker compose -f docker-compose.prod.yml exec -T rabbitmq rabbitmq-diagnostics -q ping
docker compose -f docker-compose.prod.yml exec -T rabbitmq rabbitmqctl list_queues -p /findb name messages durable arguments
docker compose -f docker-compose.prod.yml ps
```

送一筆帶固定 idempotency key 的 smoke payload，確認 response 為 202；以 run endpoint 確認最後進入 terminal state。重啟 RabbitMQ 與 kill worker child 後用同 key 重送，run 不應重複，canonical 結果不應增加重複列。

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

不 downgrade schema、不刪除 raw/job/outbox。停止 dispatcher 與 worker即可安全暫停；回退 application image後仍保留所有 durable work。舊版 ingest 不得在 durable migration 後重新開放 provider 流量，除非確認它相容新的 raw primary key 與 202/outbox 契約。
