# Deployment Runbook

> 目前實際target是staging；production workflow capability存在，但production EC2與外部資源
> 尚未完成。SSH、GitHub runtime secrets與tag-only image部署仍是現況；退場計畫見
> [Staging AWS Deployment Completion Plan](../dev/staging-aws-deployment-plan.md)。

## Workflow與release units

| Workflow | Unit | Trigger | Environment／concurrency |
| --- | --- | --- | --- |
| `findb-ci.yml` | Backend、migration、Dashboard、contracts | PR、`workflow_call`、manual | 不讀deployment Environment |
| `findb-cd.yml` | Backend＋Dashboard | FinDB paths合入`main`或manual | `staging-findb`／`staging-findb` |
| `fetcher-ci.yml` | Fetcher、contracts、images | PR、`workflow_call`、manual | 不讀deployment Environment |
| `fetcher-cd.yml` | Generic＋FinLab＋Shioaji Fetcher | Fetcher paths合入`main`或manual | `staging-fetcher`／`staging-fetcher` |

Deployment concurrency一律`cancel-in-progress: false`。CD直接呼叫同revision reusable CI；
不可用branch最近一次成功取代。Contract-only變更會執行兩個CI，但不自動部署Fetcher，
需要依backend-first順序manual dispatch。

FinDB deployment unit包含backend、Dashboard、nginx、RabbitMQ及Compose services。
Fetcher的三個provider images是另一個unit。現行workflow以commit SHA tag部署；完成digest
與release manifest前，不得宣稱可把staging artifact原樣promotion到production。

## Staging data policy

Staging用來驗證部署、bounded端到端資料流與故障處理，不承載完整universe或
production-scale歷史資料。Active feeds與pilot範圍見
[現行架構](../architecture/overview.md#active-feeds)。

任何data-producing操作事前記錄：

- image／config SHA與operator；
- provider、dataset、symbols、日期、row上限及預估credits；
- pre-run counts、停止條件與cleanup／rollback方式。

驗收可覆蓋provider fetch、Raw R2、Source、outbox、RabbitMQ、normalization、DQ、
canonical及Serve／Admin／Dashboard lineage。不得以smoke名義擴張universe或執行歷史
backfill。Deployment不改寫DB scheduler desired state；啟用或停止由Dashboard Owner控制。

## Environment與設定

Deployment使用四個互相隔離的GitHub Environments：

```text
staging-findb
staging-fetcher
production-findb
production-fetcher
```

Push至`main`自動部署staging；production只允許manual dispatch。兩者都不配置GitHub
Environment人工核准，仍以PR、required CI、protected branch及target隔離控制變更。每個job
只能取得自己unit與target的設定；branch與Environment protection的實際外部設定需另行驗證。

完整變數與secret名稱不在本runbook重複維護，以
[infra/env契約](../../infra/env/README.md)、各target的`remote.env.example`、workflow及
`app.config.Settings`為準。Sync script只新增或更新GitHub Environment，不會刪除退休值；
operator必須另行移除不用的repository／Environment設定。

目前runtime secrets由target-scoped GitHub Environment傳到remote process，屬明確過渡。
目標是GitHub OIDC＋service-specific deploy role＋SSM，並由EC2 instance role讀取
Secrets Manager／Parameter Store。完成對應plan phase前，不得把目標架構寫成現況。

## Credential與storage邊界

- FinDB與Fetcher使用不同deployment target、credential與secret scope。
- 每個provider/client使用獨立DB-backed Source key與dataset allowlist。
- Fetcher calendar使用獨立read-only Serve key，不得與Source key共用。
- Break-glass key只供bootstrap／recovery；日常Dashboard使用具名session。
- Fetcher只持有Raw R2 Object Read & Write；不得取得Canonical R2、RDS、RabbitMQ或Admin。
- Canonical publisher與reader credentials分離，且只注入worker與serve。
- R2 management credential不得注入application runtime。

Raw與Canonical使用不同private buckets。Raw object key由provider／dataset開始，contract
只帶credential-free `r2://` reference與checksum。Raw R2 lifecycle為30天、bucket lock
為7天；此Cloudflare設定已於2026-08-21人工確認。Canonical runtime尚未實作，不得做
資料面acceptance聲明。

Fetcher SQLite位於provider-specific EBS paths，owner為runtime UID，目錄`0700`、檔案
`0600`。Raw bucket binding marker不符時deployment fail closed；不同bucket的state與
prepared refs不得混用。Candidate通過preflight與single-writer檢查後才能取代stable。

## Network與GitHub protection

Repository可證實的application／Compose邊界與需要外部核對的target policy必須分開看待：

- **Operator pre-deploy**：確認RDS不公開且只接受FinDB EC2 security group，Fetcher沒有RDS
  route；未保存外部設定證據前不得宣稱已驗證。
- **Workflow**：RabbitMQ ports不對host公開；第三方Actions固定完整commit SHA；不同unit
  使用獨立deployment concurrency。
- **Operator pre-deploy**：確認Source經Cloudflare/nginx還原可信client IP後執行allowlist，
  TLS private key只存在FinDB target；外部Cloudflare、DNS與security group設定另行留證。
- **Operator pre-deploy**：確認repository政策與外部設定要求PR、required CI及protected
  branch。Staging由protected `main`自動部署，production只接受manual dispatch；兩者均不設
  GitHub Environment人工核准。

## Release順序

一般release：

1. **Workflow**：對同revision執行對應CI，build並push明確image identity，再pull candidate。
2. **Operator pre-deploy**：確認target、backup／PITR、disk／inode、container state、觀察窗口及
   必要external dependencies；現行workflow未自動涵蓋的項目必須人工留證。
3. **Workflow**：執行remote與DB preflight；FinDB migration前停止本unit所有DB writers，
   由單一migration job升級。
4. **Workflow**：啟動candidate，檢查container、internal health、nginx、RabbitMQ topology、
   worker ping及DB-authoritative queue health。
5. **Operator acceptance**：從外部驗證public TLS／routing，執行bounded fixed-idempotency
   smoke並確認terminal state。
6. **Operator acceptance**：記錄實際image、Alembic revision、設定checksum及驗收結果。

Contract upgrade固定backend-first：**Workflow**先驗證Backend同時接受新舊版本並部署；
**Operator acceptance**完成schema與shadow delivery驗證後，再manual deploy Fetcher切換pin；
經過保留期才停止舊版。

Fetcher deploy保留DB desired state，不把deployment當成啟用授權。三個scheduler分別
執行offline preflight、SQLite quick-check、bucket binding與single-writer reconciliation。
常駐CLI將`SIGTERM`／`SIGINT`轉為shared stop event：idle時立即退出，final running preflight
後收到stop也不得啟動新provider cycle；已開始的cycle則完成terminal report後退出。Workflow
以30秒grace period停止stable container並要求exit code為`0`，否則fail closed並恢復原stable，
不得把exit `137`或其它非零退出視為成功promotion。首次signal-handler bootstrap已完成且
temporary allowlist已移除；後續所有provider與image一律套用相同的strict exit-`0` gate。

## Alembic migration

ORM與migration必須在同一PR；runtime只驗證revision，不執行`create_all()`。Staging採
forward-only；downgrade只供本機round-trip測試。

本機：

```bash
uv --directory backend run alembic current
uv --directory backend run alembic upgrade head
uv --directory backend run alembic revision --autogenerate -m "describe change"
uv --directory backend run alembic downgrade -1
uv --directory backend run alembic upgrade head
```

Autogenerate後人工審查schema/table、enum、constraint、index、data backfill、lock／rewrite、
partition及可逆性。涉及既有資料時在clone或partial dump驗證，不只測空DB。

Staging rollout：

1. **Operator pre-deploy**：確認backup／PITR與可用的restore紀錄，暫停provider並確認沒有
   未結束的delivery cycle。
2. **Workflow**：執行read-only DB preflight；失敗時不停止既有服務。
3. **Workflow**：停止`ingest`、`dispatcher`、`worker`、`raw-cleanup`及本unit所有writers。
4. **Workflow**：由candidate image執行單一`alembic upgrade head`與`alembic current`。
5. **Workflow**：啟動queue、worker、ingest及相容的serve，執行container、health與queue
   checks。
6. **Operator acceptance**：執行fixed-idempotency smoke，驗證terminal state後才恢復producer。

`alembic stamp`只可在schema已人工證明與revision完全一致時使用，不能掩蓋drift。

## Acceptance與rollback

Deploy後至少完成：

- **Workflow**：container與restart count、process health、Alembic head、RDS connectivity、
  RabbitMQ topology、worker ping及DB-authoritative queue health。
- **Operator acceptance**：public TLS／routing、bounded API smoke、terminal state與lineage。
- **Operator acceptance**：Fetcher另核對heartbeat、desired／observed state及terminal
  delivery。

失敗時：

1. 暫停provider並停止所有writers，保留raw、run、job、outbox、volumes與logs。
2. 收集bounded diagnostics，不輸出secret。
3. 只有schema相容時才能恢復previous image。
4. Schema已改且舊image不相容時保持writers停止，部署包含目前migration chain的forward fix。

禁止使用`docker compose down -v`、刪資料、`stamp head`、即席production downgrade或R2
mass delete強迫恢復。Serve健康且schema相容時可維持唯讀服務。
