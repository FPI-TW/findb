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
Fetcher的三個provider images是另一個unit。現行workflow仍以commit SHA **tag**部署；
Phase 3 foundation會在 staging build／reuse 後從 ECR exact SHA tag 取得並嚴格驗證 digest，
產生 unit-scoped、canonical JSON release manifest artifact（FinDB兩張、Fetcher三張完整
`repository@sha256:...` references）。manifest 不含 secret，並記錄 allowlisted deployment
bundle checksum。image 的 `contract_versions` 直接由 selected source root 的
`contracts/manifest.json` descriptor-safe 嚴格讀取；每個 entry 指向的 selected schema 也以同一個 pinned
root FD 讀取。每個 selected relative path 第一次讀取後都固定為同一份 in-memory bytes snapshot，後續
contract parse、schema verify 與 source checksum 都不重新開啟該 pathname；actual SHA-256 必須等於 entry
宣告值，才計算並寫入該 manifest 的 canonical semantic SHA-256。
validate 同時重新計算這些來源並精確比對 workflow 傳入的 unit、selected commit、migration revision、run ID
與完整 unit image-ref map，避免另一份語法正確的 manifest 被替換後通過。
bundle checksum 現命名為 `deployment_source_bundle_sha256`，只描述 selected commit 的 deterministic
source/template inputs（包括 selected `contracts/manifest.json` 與 selected manifest tool 的內容 digest）。
它涵蓋與 staging host contract 相同的 unit-scoped sources，另加這兩個 manifest inputs；不含 current
CI-only ECR build helper、secret、env、production-only serve-key template／renderer。它不是 render 後 host
deploy bundle、accepted replay bundle 或 live deploy evidence。因此實際 versioned/rendered deploy bundle
checklist 仍未完成。

Artifact 名稱為 `findb` →
`staging-findb-release-manifest-${{ github.run_id }}-${{ github.run_attempt }}`、`fetcher` →
`staging-fetcher-release-manifest-${{ github.run_id }}-${{ github.run_attempt }}`；每個 rerun 都會保留獨立
artifact，不覆寫之前的 attempt。

Manifest policy/tool 由 selected SHA own：workflow 只執行 selected source root 中的
`infra/deploy/release_manifest.py`，不使用 current checkout 的工具。materialize 後先以 raw bytes 驗證該
工具的 `protocol` 命令只輸出 `findb-release-manifest-v1` 加一個 LF（stdout/stderr、exit status 或任何額外
bytes 不符即 fail closed），再 generate／validate。Phase 3 foundation 之前、明確
`image_tag` 的 protected-main ancestor rollback 若 selected SHA 不含此工具，會發出 warning 並維持既有
SHA-tag rollback，但不產生 manifest artifact；這個 legacy 分支只適用 `ECR_REUSE_ONLY=true`。foundation
之後的 selected SHA 缺少或無法執行其工具時一律 fail closed；存在但沒有 v1 protocol 的檔案不是 legacy，
不會把其他 manifest error 降級為 legacy。
legacy rollback 的相容性前置檢查只比較實際同步到 staging host、或在 staging 前由 workflow 執行的
unit-scoped runtime inputs：FinDB 的 Compose、三個 staging-rendered nginx inputs（主設定、Source
allowlist、Cloudflare real-IP）、三個非敏感 renderer，以及 loader／command／host render／install／deploy／
catalog；Fetcher 的 loader／command／provider release／catalog。`serve-key.conf` 與
`render_nginx_serve_key.py` 不在 staging 前置路徑，故不在此集合。它刻意不比較純 CI 的 ECR build helper
或 release-manifest tool，因此這些 foundation-only 變更不會阻斷 pre-foundation SHA-tag rollback；任一
上述 host runtime contract 差異仍 fail closed。

workflow 不再額外 `git show` contract manifest；generate 與 validate 都傳入同一個 selected source root 的
`contracts/manifest.json`，避免第二份 materialization 漂移。`fetcher-cd.yml` 的 PR-only policy 變更同時
路由至 FinDB/backend CI，因此完整的 deployment/manifest contract tests 會實際執行；一般 `fetcher/**`
source 變更仍只路由 Fetcher CI。

artifact output 寫入只接受預先存在的 job-private runner temp parent，從 filesystem root 以 `O_NOFOLLOW`
逐段 pin 到 parent directory FD，再用 single-link temporary file 與 atomic replace；這可避免祖先／輸出
parent path 的 symlink swap 影響本次寫入。它不是、也不宣稱是對同 UID
任意持續寫入者的完整防護；GitHub job-private temp 與 workflow expected-input binding 是此 foundation 的威脅
邊界。

Phase 3 foundation artifact 已有兩個 unit 的 live acceptance。Fetcher manual run
[33244760600](https://github.com/FPI-TW/findb/actions/runs/33244760600) 產生三枚 digest manifest，
獨立 SSM command `56127a67-2eed-4535-a670-86faad915e21` 驗證三枚 host-local RepoDigest 完全一致，
且三個 scheduler 均為 running、restart count 0。FinDB manual run
[33245539837](https://github.com/FPI-TW/findb/actions/runs/33245539837) 產生兩枚 digest manifest並完成
部署；獨立 SSM command `b6cb3d90-61c3-4a9f-bd80-3ad854dadd69` 驗證 backend／Dashboard
RepoDigest、Alembic `d6e7f8a9b0c1`、RabbitMQ與所有public/internal health checks。這些證據只證明
artifact內容、ECR digest與當次SHA-tag deployment一致。

這份 manifest **尚未**成為 deploy 或 rollback identity，host Compose／helper 尚未切換 digest，
因此不得把 artifact 當成 accepted release 或 production promotion evidence。staging build bridge現在會
把已驗證的unit-specific digest refs傳給deploy job，bounded SSM preflight在任何SSH或writer interruption
前以instance role、tmpfs Docker config pull並逐一inspect exact RepoDigest。此路徑已由共同merge SHA
`228989afe857c82d619cd53d15dbb29873d6710a`完成live驗收：FinDB run
[33246701516](https://github.com/FPI-TW/findb/actions/runs/33246701516)／SSM command
`d6437246-f584-4545-9224-88867b8bdef9`輸出`ecr_digest_pull_inspect=ok images=2`，Fetcher run
[33246700446](https://github.com/FPI-TW/findb/actions/runs/33246700446)／SSM command
`9e4596ca-1888-44bf-acef-89858f9b35d4`輸出`ecr_digest_pull_inspect=ok images=3`；兩者均為
`Success`／exit 0且後續部署健康。Compose/helper digest cutover、private S3 accepted manifest與production promotion仍未完成。

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

Workflow保留`staging`與`production` target及production manual dispatch能力；這只是預留的
workflow capability，不代表對應GitHub Environment或AWS資源已建立。現況如下：

| Target／resource | Current state |
| --- | --- |
| `staging-findb` GitHub Environment | 已建立，為目前FinDB staging target |
| `staging-fetcher` GitHub Environment | 已建立，為目前Fetcher staging target |
| `production-findb` GitHub Environment | N/A（尚未建立） |
| `production-fetcher` GitHub Environment | N/A（尚未建立） |
| Production FinDB EC2 | N/A（尚未建立） |
| Production FinDB RDS | N/A（尚未建立） |
| Production promotion snapshot／dump／seed | N/A（尚未建立） |

Push至`main`自動部署staging；production僅在上述Environment與target資源建立後允許
manual dispatch。兩者都不配置GitHub Environment人工核准，仍以PR、required CI、protected
branch及target隔離控制變更。每個job只能取得自己unit與target的設定；branch與Environment
protection的實際外部設定需另行驗證。

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

`market_data_eod`的年度partition屬migration-owned schema state。只有使用
`MIGRATION_DATABASE_URL`的migration job可以建立或附掛partition；application runtime只以
PostgreSQL catalog唯讀確認目標年度的exact partition已附掛，不持有schema DDL權限。若缺少
partition，normalization以`EOD_PARTITION_UNAVAILABLE`永久失敗，operator必須先由Alembic
補齊schema再重送；不得為了恢復ingest而授予application role `CREATE`。年度邊界前應在
migration測試與staging preflight確認下一年度partition已存在。

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
