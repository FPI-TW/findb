# Durable Ingestion Runbook

## Durable承諾與正常路徑

RabbitMQ是單節點、可重建的delivery layer，不是durable truth。Source接受工作時，在同一
PostgreSQL transaction保存：

- `raw.market_payload`
- `ingestion_run`
- `normalization_job`
- `normalization_outbox`

Commit後才回`202`；`ingestion_attempt`另記錄通過auth／rate-limit gate的每次canonical
呼叫，包括contract rejection。

```text
Source commit
  -> dispatcher publishes outbox
  -> RabbitMQ findb.normalize.v1
  -> Celery worker late ack
  -> normalization + DQ + canonical transaction
  -> run/job terminal state
```

同dataset以PostgreSQL advisory lock串行；不同dataset可並行。Consumer timeout必須高於
Celery hard time limit及graceful shutdown所需時間。

## Scheduler operation

`scheduler_control`與`scheduler_dataset`是排程時間、timezone、dataset mapping與desired
state的唯一權威。Fetcher container保持常駐並輪詢Source control endpoint：

- `running`只允許reviewed workload建立新cycle；
- `stopped`完成目前cycle後不啟動下一輪；
- control API失聯、scope／revision／mapping不符時fail closed；
- deployment不得覆寫desired state；Owner透過Dashboard變更。

排程時間調整時，同步檢查scheduler definition、dataset delivery expectation與missing
deadline。Market calendar必須有完整published year；休市、`settlement_only`、缺少年度或
API failure都不得enqueue。

## 上線前檢查

- `predeploy_db_check.py`通過，DB revision、connection headroom與long transaction正常。
- RabbitMQ EBS、topology、DLQ與consumer timeout正常。
- Worker ping成功，dispatcher／worker heartbeat沒有持續惡化。
- Unpublished outbox、queued oldest age與expired lease沒有持續上升。
- Raw retention未在事故、recovery或migration期間誤刪必要payload。
- Source client的provider與dataset scope正確。
- Scheduler definition、published calendar、reviewed universe與desired state一致。

## Bounded end-to-end acceptance

Staging驗收完整功能，不累積資料量。執行前依
[staging data policy](deployment.md#staging-data-policy)記錄image／config、pilot identity、
日期／row上限、credits與pre-run counts。

基礎檢查：

在 staging，以下 queue-health command 必須在 runtime-secret wrapper 的 child command 內執行，且
wrapper 必須以 `--consumer compose` 載入後清理 runtime secrets。不得在 host persistent `.env` 寫入或
`export` `CELERY_BROKER_URL`；以 `docker exec -e` 只把它短暫傳給該次 ingest process。

```bash
docker compose -f docker-compose.prod.yml exec -T rabbitmq rabbitmq-diagnostics -q ping
docker compose -f docker-compose.prod.yml exec -T rabbitmq rabbitmqctl list_queues -p /findb name messages durable arguments
docker compose -f docker-compose.prod.yml exec -T worker celery -A app.task_queue inspect ping --timeout=10
sudo /opt/findb/runtime-secrets/runtime_secret_command.sh \
  --catalog /opt/findb/runtime-secrets/findb.json \
  --region ap-southeast-1 \
  --consumer compose \
  -- docker exec -e CELERY_BROKER_URL findb-ingest \
  python /app/scripts/check_queue_health.py
```

以固定bounded identity送出pilot並確認：

1. Provider raw R2 reference、checksum與delivery內容一致。
2. Source回`202`及attempt／run IDs。
3. Run／job進入預期terminal state；outbox已published且無active delivery殘留。
4. DQ、canonical values與row counts符合pilot。
5. Serve、Admin與Dashboard顯示相同lineage。
6. 相同內容重送不增加canonical row；相同key不同內容回`409`。
7. 保存post-run counts、queue／DLQ及實際credits。

## Delivery completeness

Delivery expectation描述資料日期、mode、count、freshness、coverage及missing policy，
不控制scheduler執行時間。Policy先以`warn`校準；沒有真實baseline前不得切`reject`。

EOD checkpoint catch-up使用`delivery_mode=backfill`及成對coverage dates。Incremental
expectation可由同日incremental或明確覆蓋預期日期的backfill補齊；rerun不是新arrival，
缺少完整coverage的歷史raw也不能推導補齊證據。

Minute feeds使用`sequenced_snapshot`。只有相同`snapshot_id + daily_update_id`、相同
資料日與`sequence_count`的非rerun成功runs完整涵蓋`1..sequence_count`，才算fresh並解除
missing alert。不同snapshot不得拼接；partial、failed或identity缺失都維持未完成。診斷時
查run保存的sequence identity、job、outbox與terminal state，不依賴有限期raw JSON。

## 監控

最低告警：

- RabbitMQ restart／unhealthy、DLQ與EBS free space；
- dispatcher／worker exit及worker heartbeat age；
- unpublished outbox、queued／retrying job oldest age與expired leases；
- retry exhausted、missing expected delivery及schema／provider payload drift；
- DB connection headroom、long transaction與delivery terminal failure；
- scheduler heartbeat、desired／observed mismatch及raw已抓取但normalization未完成。

Market freshness必須分開呈現provider fetch、normalization completion與feed policy。
Raw存在但canonical未完成時沿run／job／outbox／queue診斷，不要誤判為provider未送達。

## 常見診斷

| 現象 | 優先檢查 |
| --- | --- |
| Source 429 | Client quota、IP bucket、`Retry-After`與重試identity |
| Source 409 | Idempotency key是否對應不同內容或contract |
| Source 422 | Attempt error code與published schema |
| Run pending過久 | Outbox、queue depth、worker heartbeat |
| Run processing過久 | Lease、dataset lock、worker logs、DB pressure |
| Canonical缺資料 | Terminal state、DQ error、source precedence |
| Missing delivery | Scheduler definition、calendar、provider fetch與expected date |
| Raw存在但無canonical | Job state、DQ、normalizer transaction |

不要只看HTTP`202`或單一Dashboard card判斷完成。

## RabbitMQ volume全毀

1. 暫停所有schedulers，停止RabbitMQ、dispatcher與worker。
2. 確認PostgreSQL raw／job／outbox完整，不刪除它們。
3. 清空或更換RabbitMQ EBS，以相同node name與vhost啟動。
4. 啟動worker，從版本控制設定重新宣告exchange、queue與DLQ。
5. 啟動dispatcher；startup reconciliation重設expired processing並補建delivery。
6. 觀察DB queue health直到backlog下降，再逐一恢復scheduler。

不要對運行中的RabbitMQ message directory做filesystem snapshot restore。Broker短暫中斷時，
late ack、topology probe、lease與idempotency應讓delivery安全重試。

## Pause與failure recovery

1. 將scheduler desired state切為`stopped`，停止`ingest`、`dispatcher`、`worker`、
   `raw-cleanup`及其他writers。
2. 保留raw、run、job、outbox、SQLite、broker volume與logs。
3. 修復後先恢復queue與單一bounded smoke，再逐一啟用provider。

Schema migration後不直接回退到不認得目前Alembic head的image；採forward fix。禁止以
舊route、舊provider identity、volume deletion或大範圍資料刪除作為recovery。
