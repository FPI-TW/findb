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

## RabbitMQ runtime credential rotation

此程序只輪替`findb/staging/findb/rabbitmq/runtime`，不輸出secret值，也不以刪除
`AWSPREVIOUS`取代舊值失效驗證。先停止三個provider stable schedulers，避免rotation期間建立新
delivery；保留RabbitMQ persistent volume、node name、vhost與topology。持久化volume存在時，
`RABBITMQ_DEFAULT_PASS`環境變數只用於首次bootstrap，**不會**更新既有internal user的password。

1. 記錄目前Secrets Manager version IDs與stages；停止三個provider stable schedulers後，先確認queue／DLQ、
   unpublished outbox與active delivery處於安全狀態，並確認既有broker container/node仍在運行、管理context持有
   舊cookie。若任一前提不成立，立即中止，保持schedulers停止，不得刪除或改動persistent volume。
2. 僅在仍運行、持有舊cookie的既有broker container/node context內，安全地只將新的`AWSCURRENT` password
   傳給既有internal user的in-broker `rabbitmqctl change_password`（或同等、可稽核的broker管理操作）。此步驟
   不得source、替換或注入新的`RABBITMQ_ERLANG_COOKIE`；立即以新password驗證auth成功，並確認舊password被
   broker拒絕。若舊cookie precondition或管理命令失敗，立即中止，保持schedulers停止，不得刪除或改動persistent volume。
3. 完成password變更與新auth驗證後，才以protected `main` deploy重新載入runtime-secret consumer，重建rabbit、policy、dispatcher與worker；
   不刪volume、不重建node identity，也不在persistent host env寫入broker credential。official RabbitMQ
   container在此重建時以新的`RABBITMQ_ERLANG_COOKIE`更新cookie。
4. 在相同image、network、指定node `rabbit@findb-rabbitmq`與完全相同的probe command下，以current與
   previous cookie各執行兩次differential probe。current必須兩次成功；previous必須兩次明確非零，且診斷
   證明為authentication／cookie rejection。不得把任何固定exit code視為可重用runbook契約。
5. 驗證rabbit healthy、policy exit code為0、worker healthy，並確認新password auth成功、舊password收到
   broker拒絕。驗證queue與DLQ為0，且DB-authoritative queue health的expired leases、missing deliveries、
   unpublished outbox均為0，worker heartbeat正常；確認volume、node、vhost及topology存在。
6. 使用protected `main`的既有accepted image執行一次FinDB manual deployment／health驗收，保存workflow
   SHA、image SHA及上述結果。未通過時保持provider schedulers停止，收集不含secret的診斷後forward fix。
7. 所有broker驗收通過後才恢復三個provider stable schedulers，記錄running、restart count與image SHA。
   scheduler恢復本身不等同於完成原生provider cycle；該時間gate須另有cycle evidence。

2026-08-29的已完成紀錄：舊version `65fbff30-c417-54b5-b582-0b0fc0f89bc4`為`AWSPREVIOUS`，新version
`1b2a8db6-6c97-44c0-a21f-9cf384c2571d`為`AWSCURRENT`。先以新password對`rabbit@findb-rabbitmq`的
既有internal user完成in-broker password更新並確認新auth成功，再於05:04Z完成重建及broker／queue驗收。
official container重建後，在相同image、network、node與probe command下，current cookie兩次結果為
`[0,0]`，previous cookie兩次結果為`[69,69]`；後者的診斷為cookie authentication rejection，該`69`
僅為本次歷史結果，不是runbook契約。05:06:47Z三個scheduler恢復running、restart count 0；它們仍使用
accepted ECR SHA `0e2e28089498237b4261169aa0c6885f215a1d9e`。舊version未刪除，僅保留為`AWSPREVIOUS`。

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

2026-09-03 live rehearsal使用SSM command `055aee8e-f2a6-4fdb-b43e-6232dfb11d48`：三個scheduler
先切為desired stopped，FinDB writers與broker全部停止，再把live bind path換成不同inode的空目錄；舊資料
保留於`/var/lib/findb/rabbitmq.phase6-pre-rebuild-20260903T091531Z`，沒有直接刪除。Compose依版本控制
重新建立broker與policy（exit 0），兩個durable queues回復且depth均為0，worker heartbeat為3.7秒。
PostgreSQL前後的normalization job counts、unpublished outbox、expired leases、retry exhausted與missing
deliveries完全一致。這是無active backlog的control-plane rebuild基線；active delivery、worker kill與DB短暫
中斷下的duplicate-delivery驗證仍須依bounded feed acceptance另行執行。

## Pause與failure recovery

1. 將scheduler desired state切為`stopped`，停止`ingest`、`dispatcher`、`worker`、
   `raw-cleanup`及其他writers。
2. 保留raw、run、job、outbox、SQLite、broker volume與logs。
3. 修復後先恢復queue與單一bounded smoke，再逐一啟用provider。

Schema migration後不直接回退到不認得目前Alembic head的image；採forward fix。禁止以
舊route、舊provider identity、volume deletion或大範圍資料刪除作為recovery。
