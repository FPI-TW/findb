# Deployment

> 目前本專案實際使用的 EC2 與本次四 feed cutover 對象是 **staging**；下列
> production target 僅保留為專案既有部署能力，不代表目前已有 production EC2。

> Repo內已將CI/CD拆成四個獨立workflow。GitHub Environments、AWS角色與runtime
> secrets仍須在外部管理。Push至`main`會自動部署staging；production只能透過
> `workflow_dispatch`明確選擇，並受production Environment protection約束。

> SSH、GitHub runtime secrets與SHA tag的退場順序，以及staging AWS外部資源清冊與
> 完成定義，見
> [Staging AWS deployment completion plan](../dev/staging-aws-deployment-plan.md)。在各phase
> 驗收前，本文件描述的SSH流程仍是現行runbook，不得把目標架構誤當成已部署。

> 已驗證的staging account、EC2、RDS、R2、GitHub protection、runtime baseline與未完成風險
> 記錄於[Staging AWS resource inventory](staging-aws-inventory.md)。資源或owner變更時必須在
> 同一個PR更新該清冊。

## Workflow 邊界

| Workflow | 責任 | 主要觸發 | Environment / concurrency |
| --- | --- | --- | --- |
| `findb-ci.yml` | Backend、migration、Dashboard與contract acceptance | FinDB或contract相關PR/push、手動 | 不讀部署environment |
| `findb-cd.yml` | 建置並部署backend與Dashboard | `main`的FinDB部署檔案變更、手動 | 自動：`staging-findb`；手動：可選`staging-findb`/`production-findb` |
| `fetcher-ci.yml` | Fetcher lint、test、contract與container build | Fetcher或contract相關PR/push、手動 | 不讀部署environment |
| `fetcher-cd.yml` | 發布Fetcher image並交付獨立目標 | `main`的Fetcher runtime/deploy檔案變更、手動 | 自動：`staging-fetcher`；手動：可選`staging-fetcher`/`production-fetcher` |

兩個CI可同時因 `contracts/**` 或contract source變更而執行。Contract-only變更不會
自動部署Fetcher；跨版本更新必須依下方backend-first順序，由
`workflow_dispatch`明確啟動需要的CD。

自動CD只可部署同一commit已通過對應CI的artifact；不得用另一個SHA或僅憑branch最新
狀態取代該gate。手動 `workflow_dispatch`仍須受environment protection約束，操作者
也必須確認指定revision的CI結果。

兩個CI都會執行 `backend/scripts/check_deployment_artifacts.py`，以無外部副作用的
固定值重跑下列檢查：workflow YAML/action SHA、nginx/infra renderer、GitHub Environment
example contract、local/production Compose schema/render，以及production Compose不得帶入
資料庫或 `latest` fallback。
FinDB CI 另外在 disposable PostgreSQL 上執行 `alembic upgrade head`、`alembic check`
與 revision 查驗；兩個CI都執行 checked-in contract artifact 的 `--check`。因此CD透過
`verify` reusable workflow 自動繼承同一組 gate。

每次CD build只產生一份 immutable release manifest。Build job使用 registry 回傳的
`image@sha256:...` digest；FinDB另將本次解析的Nginx RepoDigest固定在同一份manifest，並把
commit SHA、migration head、contract manifest與 deployment
bundle checksum一併寫入；CD在部署前重新驗證 manifest，Compose缺少完整digest reference
或使用`:latest`都會 fail closed。FinDB部署後的 acceptance 會檢查所有 runtime container
的 exact image reference、Alembic revision、會實際變更一列再明確rollback的UPDATE檢查與
ingest/serve/dashboard health，
並保存 secret-free `target-acceptance.json`；Fetcher三個provider reconciliation先只啟動
candidate container並保存 candidate evidence，三者全部通過後才一次性 promote。FinDB與
Fetcher都將 manifest 放在 `/opt/findb/release-state/candidate`，驗收完成才以暫存檔與
`mv` 原子更新 `accepted`，上一份 accepted manifest 保存在 `previous`；promotion完成前任一
失敗都不會覆寫 accepted metadata，完成後則解除rollback trap並保持新runtime與metadata一致。
FinDB candidate開始後若驗收失敗，新的writer會保持停止，避免未接受
release繼續寫入；不會在migration後盲目回切可能不相容的舊image。SSM/OIDC尚未完成前，這些
檢查仍由現行SSH CD執行。

FinDB 的 nginx templates 永遠保留 checked-in source，不在工作樹上原地 render。部署 job
將四個 target-rendered files 與獨立 attestation 放入 candidate path；attestation 綁定
public host、Source allowlist CIDRs、Cloudflare CIDRs、Serve-key injection 是否存在及
檔案 mode，並以固定 `<redacted>` 正規化 `FINDB_LOOKUP_SERVE_API_KEY`。原始 key、長度與
原始 key digest 都不會寫入 log、manifest、attestation 或 acceptance evidence。EC2 會以
ephemeral `--rm` acceptance container的唯讀bind mount重新驗證轉移後實際檔案，不會把key
複製進長駐app container layer；通過後才切換 nginx，並移除candidate中的重複key檔。切換後若
bounded acceptance失敗，會從 previous bundle 還原 active nginx policy。Target同時核對runner
已驗證candidate manifest的SHA-256與完整schema，防止截短或傳輸替換。

FinDB deployment unit包含backend與Dashboard。FinDB CD會建置兩個image、render nginx
設定、同步remote Compose/infra、執行migration，再啟動與驗證serve、ingest、
dispatcher、worker、RabbitMQ、Dashboard及nginx。

Fetcher CD發布：

```text
ghcr.io/fpi-tw/findb-fetcher@sha256:<digest>
```

並將manifest中的三個 immutable digest 交付給獨立Fetcher target。現有Fetcher程式提供contract
validation、readiness、Source API delivery client、具整體deadline的manual
delivery/wait CLI、versioned小型symbol universe、Twelve Data日線adapter，以及
Fetcher-owned SQLite scheduler、persistent retry、checkpoint與exact-byte raw
Cloudflare R2 persistence。Fetcher CD會用exact SHA image執行無外部呼叫的scheduler
preflight，並讓三個隔離的`--run-forever` container保持常駐。Container只輪詢FinDB
Source control endpoint；DB desired state為`stopped`時不建立新provider cycle，但仍保留
SQLite state並回報heartbeat。Migration在每個environment先建立`stopped` rows，owner
於Dashboard完成驗證後再逐一啟用。它不會建立R2 bucket/API token。

Remote migration期間必須停止 `ingest`、`dispatcher`、`worker`與其他DB writers。
Serve若與新schema相容，可以持續提供查詢。

## Staging data policy

`staging-findb`與`staging-fetcher`只用於驗證部署、完整端到端資料流與故障處理，
不承載完整資料集。預設data-producing acceptance profile固定為committed
`twelve_data_us_common_stocks.v1.json`中的AAPL、MSFT、NVDA，以及scheduler
`outputsize=20`；fresh cycle最多3個symbols、60筆provider rows。Universe內較大的
hard limits只是程式安全上限，不是操作授權。

Staging可以用bounded fixtures或pilot rows驗證：

- provider fetch、R2 raw、Source、outbox、RabbitMQ、worker、DQ、canonical與
  Serve/Admin/Dashboard的完整lineage；
- retry、idempotency、duplicate handling、failure recovery與rollback；
- migration、deployment與cache generation的功能正確性。

Staging禁止完整universe導入、production-scale歷史backfill，以及未經核准擴大
symbol、日期或record caps。例外必須針對具名run另行授權，並事前記錄config/image
SHA、symbol/date/row上限、預估provider credits、rollback/cleanup方案與operator。
Deployment不會改寫DB scheduler desired state，因此staging deploy後仍依資料庫控制
停妥而不需人工SSH關閉。data-producing one-shot acceptance可作為後續受控
operation；在提供前不得藉由CD改成常駐執行。任何具名觀察窗口均不得放寬上述caps。

## Deployment isolation

外部設定需建立四個互相隔離的GitHub Environments：

```text
staging-findb
staging-fetcher
production-findb
production-fetcher
```

每個deployment job只能引用自己的environment。不要在workflow-level或大型job-level
`env:`注入全部secrets，也不要對reusable workflow使用 `secrets: inherit`；逐一傳入
具名secret。

### `{staging|production}-findb`

| 類型 | Environment設定名稱 |
| --- | --- |
| Secrets | `FINDB_EC2_HOST`、`FINDB_EC2_USER`、`FINDB_EC2_SSH_KEY` |
| Secrets | `DATABASE_URL`、`CELERY_BROKER_URL`、`RABBITMQ_DEFAULT_USER`、`RABBITMQ_DEFAULT_PASS`、`RABBITMQ_ERLANG_COOKIE` |
| Secrets | `CLOUDFLARE_R2_CANONICAL_PUBLISHER_ACCESS_KEY_ID`、`CLOUDFLARE_R2_CANONICAL_PUBLISHER_SECRET_ACCESS_KEY`（Worker限定、Object Read & Write） |
| Secrets | `CLOUDFLARE_R2_CANONICAL_READER_ACCESS_KEY_ID`、`CLOUDFLARE_R2_CANONICAL_READER_SECRET_ACCESS_KEY`（Serve限定、Object Read） |
| Secrets | `ADMIN_BREAK_GLASS_API_KEY` |
| Secrets | `FINDB_LOOKUP_SERVE_API_KEY`、`FINDB_STATIC_CACHE_SERVE_API_KEY`（`SERVE_REQUIRE_AUTH=true`時必填且必須為不同的DB-backed keys） |
| Queue health secret | `FINDB_QUEUE_HEALTH_ADMIN_API_KEY`（DB-backed Admin viewer key） |
| Variables | `APP_NAME`、`APP_VERSION`、`DEBUG`、`PORT`、`DATABASE_POOL_SIZE`、`DATABASE_MAX_OVERFLOW` |
| Variables | `API_V1_PREFIX`、`API_KEY_HEADER`、`SOURCE_ALLOWLIST_CIDRS`、`SOURCE_TRUST_PROXY_HEADERS`、`SERVE_REQUIRE_AUTH` |
| Variables | `RATE_LIMIT_REQUESTS`、`RATE_LIMIT_WINDOW`、`RAW_RETENTION_ENABLED`、`RAW_RETENTION_DAYS`、`FINDB_STATIC_CACHE_BASE_URL`、`FINDB_LATEST_PRICE_WORKERS`、`FINDB_PUBLIC_HOST`（此 target 對外的 DNS hostname）、`CLOUDFLARE_R2_ACCOUNT_ID`、`CLOUDFLARE_R2_CANONICAL_BUCKET` |

### `{staging|production}-fetcher`

| 類型 | Environment設定名稱 |
| --- | --- |
| Secrets | `FETCHER_EC2_HOST`、`FETCHER_EC2_USER`、`FETCHER_EC2_SSH_KEY` |
| Variables | `FETCHER_SOURCE_API_URL`、`CLOUDFLARE_R2_ACCOUNT_ID`、`CLOUDFLARE_R2_RAW_BUCKET` |
| Variables | `FINDB_SERVE_BASE_URL`、`FETCHER_CALENDAR_TIMEOUT_SECONDS`、`FETCHER_CALENDAR_CACHE_TTL_SECONDS` |
| Variables | `FETCHER_SCHEDULER_CONTROL_POLL_SECONDS`（預設`30`，限制`1`–`30`秒）、`CLOUDFLARE_R2_MAX_OBJECT_BYTES` |
| Variables | `TWELVE_DATA_BASE_URL`、`TWELVE_DATA_TIMEOUT_SECONDS`、`TWELVE_DATA_MAX_RESPONSE_BYTES` |
| Variables | `FETCHER_REQUEST_TIMEOUT_SECONDS`、`FETCHER_MAX_ATTEMPTS`、`FETCHER_MAX_RETRY_AFTER_SECONDS` |
| Secrets | `FETCHER_TWELVE_DATA_SOURCE_CLIENT_KEY`、`TWELVE_DATA_API_KEY` |
| Secrets | `FETCHER_CALENDAR_SERVE_API_KEY`（必填，且不得與Source key相同） |
| Optional secrets | `FETCHER_FINLAB_SOURCE_CLIENT_KEY`（FinLab consumer上線前只保存於Environment，不注入Twelve Data container） |
| Optional secrets | `FINLAB_API_TOKEN`（僅手動 staging acquisition smoke 注入隔離FinLab container；不注入Twelve scheduler） |
| Secrets | `CLOUDFLARE_R2_RAW_ACCESS_KEY_ID`、`CLOUDFLARE_R2_RAW_SECRET_ACCESS_KEY`、選用的`CLOUDFLARE_R2_RAW_SESSION_TOKEN` |

`GITHUB_TOKEN`由GitHub針對workflow run提供，只用於拉取GHCR image，絕不傳入runtime
container。部署層使用provider-specific的
`FETCHER_TWELVE_DATA_SOURCE_CLIENT_KEY`，再映射為container內共用的
`SOURCE_CLIENT_KEY`。現階段Source、Twelve Data與R2 secrets由對應的`{target}-fetcher` Environment
逐一傳到遠端程序，再用Docker `--env NAME`注入；這是遷移到instance role加
Secrets Manager/Parameter Store前的明確過渡機制，不可使用
`--env NAME=value`出現在command line。Fetcher Raw R2 credentials與未來OIDC/SSM
設定必須維持Fetcher專屬，不能複製到FinDB。FinDB Canonical R2 credentials僅由Worker
publisher及Serve reader各自持有，不能複製到ingest、dispatcher或raw-cleanup。FinDB TLS private key若未來由workflow管理，只能加入
對應的`{target}-findb`，不能跨環境或服務共用。

Fetcher 一律使用 FinDB complete published calendar；部署前必須先在 FinDB 發布完整
目標市場年度，建立專用 DB-backed Serve key，並於 staging 驗證休市、僅結算與 API
失敗都不會 enqueue。Calendar preflight key只授權
唯讀 Serve API，不可重用 Source client key。

Fetcher只持有Raw bucket的Object Read & Write credential；raw object key固定從
`{provider}/{dataset}/...`開始，沒有可設定prefix，也不支援舊bucket相容。FinDB Canonical
bucket使用完全獨立的兩套credential：Worker publisher為Object Read & Write，Serve reader為
Object Read；R2 lifecycle、bucket lock或configuration稽核必須使用application runtime以外的
獨立短效管理credential。

### Staging R2 bucket split（部署前必做）

建立兩個不同的staging bucket後，將raw bucket填入`staging-fetcher`的
`CLOUDFLARE_R2_RAW_BUCKET`，將canonical bucket填入`staging-findb`的
`CLOUDFLARE_R2_CANONICAL_BUCKET`。Fetcher Environment不得保存canonical bucket或其
credentials。FinDB CD會要求publisher與reader兩套canonical credentials皆存在且access key
ID與secret都不同，並只注入Worker或Serve。

更新staging raw bucket後，必須同步換成僅對新raw bucket具Object Read & Write權限的
`CLOUDFLARE_R2_RAW_ACCESS_KEY_ID`與`CLOUDFLARE_R2_RAW_SECRET_ACCESS_KEY`。執行
`uv --directory backend run python ../infra/env/sync_github_environment.py staging fetcher`
確認契約；再以
`uv --directory backend run python ../infra/env/sync_github_environment.py staging fetcher --apply`
發布；同樣執行`staging findb`發布canonical bucket與兩套FinDB credentials。此腳本只新增或
更新，**不會刪除**GitHub Environment既有變數，operator應自行移除退休的prefix、legacy和
跨服務R2設定。Fetcher與FinDB分屬不同GitHub Environment，系統無法程式驗證兩邊token
是否重複；operator必須確保Fetcher Raw token不與任何FinDB Canonical token重用。

Raw bucket切換**不遷移也不fallback**既有object或scheduler state。部署在重用
`/var/lib/findb-fetcher/state.sqlite3`前會驗證同目錄的`raw-bucket.sha256` account+bucket binding marker；
marker遺失或與新bucket不符會在container reconciliation與preflight前中止，避免送出指向舊
bucket的prepared/raw reference。可回復的staging cutover程序為：

1. 將scheduler desired state設為`stopped`並確認stable、candidate、previous containers已停止後移除。
2. 在主機上建立受限權限的archive目錄，將`state.sqlite3`、存在時的`state.sqlite3-wal`與
   `state.sqlite3-shm`移入該目錄；不要複製舊Raw objects。
   同時 archive 或 reset `FETCHER_SHIOAJI_STAGING_STATE_PATH`及其`-wal`/`-shm`檔案；它的
   persisted raw/prepared delivery refs同樣不可跨Raw account+bucket重用。
3. 移除舊的`raw-bucket.sha256`，以UID/GID `10001:10001`、mode `0600`寫入新account+bucket SHA-256
   binding；或保留state目錄空白讓下一次部署原子建立marker。若首次部署在建立marker後、建立
   SQLite state前失敗，下一次部署會先驗證marker的owner/mode，並安全重用或原子更新它。重新驗證
   state目錄為`0700`且同一owner。
4. 以新bucket及新Raw credential redeploy，先保持`stopped`完成preflight，再視需要切為`running`。

此程序刻意丟棄舊的prepared/raw refs；archive只提供人工復原或稽核，不得被新bucket runtime使用。

Canonical R2目前僅完成bucket與credential wiring：Worker publisher R/W與Serve reader R/O
credentials已分權注入，但Canonical publish、read與sign runtime尚未實作，因此本階段不能以
staging進行Canonical R2資料流程實測。

不要複製或「升級」既有raw objects；本次工作不做資料遷移。production尚未部署，
因此沒有legacy migration或fallback；日後首次建立production環境時直接分別填入Fetcher Raw
與FinDB Canonical bucket及其分離credentials。
此程式碼變更本身沒有建立bucket、複製object、修改GitHub Environment或執行部署。

Fetcher CD會建立並驗證`/var/lib/findb-fetcher`（numeric owner `10001:10001`、mode
`0700`），以bind mount提供給preflight與scheduler。SQLite state保存schedule job、
retry lease與逐symbol checkpoint；container writable layer、FinDB RDS與RabbitMQ都
不能替代此volume。Rollout在替換stable前先以exact SHA image對實際mount執行
`--check`。`running`模式以candidate通過bounded存活與restart-count檢查後rename
promote，維持單一writer；`stopped`模式建立但不啟動同樣image/config的candidate，再
promote並驗證running count為零。失敗會移除candidate並把先前container收斂回宣告
狀態；首次部署失敗則不留下service。State不隨rollback備份或還原。

每次release在pull新image前先reconcile `stable`、`candidate`與`previous`名稱：先移除
競爭中的candidate，保留stable或在其不存在時恢復previous，最後將保留container收斂
為Environment宣告的狀態。Reconciliation失敗會在preflight與停止舊服務之前以generic
error終止；`running`避免留下兩個scheduler，`stopped`不會呼叫`docker start`。
Scheduler preflight亦會驗證SQLite quick-check、完整v1 table/column/index/uniqueness，
不接受只偽造`user_version=1`的空或不相容資料庫。

Container停止給予30秒grace period，但scheduler目前不攔截SIGTERM來主動釋放執行中
lease；若在job中途被終止，重新啟動後須等該lease到期才會恢復為retry，這是現有
rollback的已知延遲。

Fetcher raw R2 bucket不得綁定public development URL或custom domain；R2 API token只授權
該bucket的Object Read & Write，並以Fetcher專屬secret注入。Cloudflare R2會自動以
AES-256加密所有object及metadata，因此PutObject不得傳入R2不支援的AWS SSE/KMS headers。
R2 raw data lifecycle為30天，bucket lock為7天；規則由Cloudflare R2管理。
Application只傳credential-free `r2://account-id/bucket/key` reference，不產生
presigned URL，也不把Fetcher的R2 credentials送入FinDB。

FinDB job不得讀provider credentials、Fetcher Source client key、raw storage或Fetcher
部署credential。Fetcher job不得讀 `DATABASE_URL`、RabbitMQ、Admin、Dashboard、TLS、
FinDB Source shared key或FinDB部署credential。

非敏感值，如API URL、port、image name與retention days，放Environment variables。

GitHub Environment只會限制存放在該environment內的secrets/variables；repository-level
secrets仍可能被repository內其他workflow引用。因此上表的deployment secrets必須實際
搬入對應environment，確認workflow已切換後再從repository scope移除。建立environment、
設定branch policy與搬移secret都是GitHub外部作業，repo檔案不會自動完成。

## GitHub protection

- 四個Environment都只允許`main`部署。
- Production只允許手動選擇，不接受push事件自動部署。
- 專案目前採單人維護模式，不增加強制審核規則；代理人只在主要維護者請假時介入。若增加
  固定維護者、production風險提高或稽核要求變更，再重新評估。
- Branch protection要求所有`main`更新經PR進入，並禁止刪除與force-push。
- 第三方Actions固定到完整commit SHA；升級由獨立PR審查。
- 每個服務使用獨立concurrency group且 `cancel-in-progress: false`。
- workflow、infra、Compose、migration與contract的確定性檢查應持續收斂進CI/CD pipeline，
  以降低人工步驟、漏檢與重複操作。

Environment只隔離job，不能取代branch與pipeline控制。任何workflow變更仍須經PR、同revision
CI與target-specific Environment執行；具破壞性或尚未自動化的外部操作需先列出內容並人工確認。

## AWS credentials與runtime secrets

目標架構不在GitHub保存長效AWS access key或SSH private key：

```text
GitHub Actions
  -> OIDC short-lived credentials
  -> service-specific AWS deploy role
  -> SSM / deployment service
  -> EC2 instance role
  -> service-specific Secrets Manager path
```

角色：

- `GitHubDeployFinDBRole`：只能部署FinDB資源。
- `GitHubDeployFetcherRole`：只能部署Fetcher資源。
- `FinDBInstanceRole`：只能讀 `/findb/staging/*`。
- `FetcherInstanceRole`：只能讀 `/fetcher/staging/*`。

Application secrets由runtime instance role讀取Secrets Manager/Parameter Store；GitHub
Actions只取得部署權限。需要更強隔離時，兩個secret path使用不同KMS key與key policy。

OIDC trust policy必須限制repository與GitHub Environment subject，不能只信任整個
organization。

## Source client key

Fetcher使用Admin API簽發的DB-backed source client key：

- 每個provider/client一把key。
- 綁定固定 `source_name`、`allowed_datasets`與rate limit。
- FinDB只保存hash；plaintext只在簽發時回傳一次。
- Plaintext存入Fetcher的Secrets Manager path，不放FinDB runtime。

## Credential bootstrap與分階段退場

部署與日常運作均使用 DB-backed credentials：

1. 設定一把高強度 `ADMIN_BREAK_GLASS_API_KEY`，只透過
   `POST /api/v1/admin/auth/bootstrap`建立第一位Owner；bootstrap完成後不得注入
   Dashboard或一般automation。
2. Owner在Credentials頁分別簽發lookup、static cache、各Fetcher與machine Admin key。
   Plaintext只顯示一次，立即存入對應deployment secret。
3. `FINDB_LOOKUP_SERVE_API_KEY`只供nginx同源lookup Referer注入；
   `FINDB_STATIC_CACHE_SERVE_API_KEY`只供cache generator。兩者不可共用。
4. `SERVE_REQUIRE_AUTH=true`時，FinDB CD會要求上述兩把專用DB-backed Serve key；
   部署前先確保兩把key已在DB建立且未撤銷。Serve不支援環境變數fallback。
5. 後續逐一輪替 Source/Admin consumer 並觀察 Credentials 頁的 last-used/usage
   freshness至少一個完整排程週期。

Dashboard以DB-backed Admin user登入，後端簽發的opaque session只存在
`HttpOnly + Secure + SameSite=Strict` cookie；不再讀取共享Dashboard帳密或Admin key。

Break-glass輪替時先更新runtime secret並重新部署，再以新key執行受控復原檢查；不要把
break-glass key用於一般健康檢查。若所有Owner無法登入，使用break-glass credential
建立或恢復Owner後，立即撤銷臨時session並記錄稽核事件。

## Network boundaries

- Fetch EC2只透過HTTPS呼叫Source API。
- 優先使用private networking或security-group-to-security-group規則。
- Fetch EC2不得取得RDS與RabbitMQ ingress。
- Source nginx allowlist在Cloudflare後必須先以受信任的Cloudflare CIDR還原
  `CF-Connecting-IP`，再對真實client IP執行allowlist。
- RabbitMQ 5672/15672不對EC2 host或Internet公開。
- TLS private key只存在FinDB主機，不經Fetcher。

## Release順序

Contract變更使用backend-first：

1. Contract PR必須同時通過FinDB CI與Fetcher CI。
2. 以FinDB CD的 `workflow_dispatch`（或符合其FinDB path trigger的main更新）先部署
   同時接受舊版與新版contract的backend。
3. 驗證schema endpoint與shadow delivery。
4. 待Fetcher runtime能力完成後，以Fetcher CD的 `workflow_dispatch`部署切換pin版本；
   contract-only merge不會自動執行此步。
5. 觀察attempt、queue、DQ與canonical結果。
6. 經過保留期後才停止舊版。

環境需記錄：

```text
findb_image_sha
dashboard_image_sha
fetcher_image_sha
accepted_contract_versions
```

## Production go/no-go

- RDS snapshot/PITR可用，production clone已演練migration。
- DB preflight通過且保留足夠connection headroom。
- Provider已暫停，或確認retry會沿用同一idempotency key。
- Queue、worker、disk、DB與delivery monitor均有告警。
- 新image與schema的rollback/forward-fix策略已確認。
- 部署後以固定key做smoke delivery，確認run進入terminal state。

## Rollback

- 發現寫入問題時先暫停provider，停止ingest/dispatcher/worker。
- 不刪raw、job、outbox或RabbitMQ volume來「清除錯誤」。
- 不對production schema做即席downgrade。
- 舊image若Alembic revision不同，不能直接作為rollback image。
- 優先部署包含目前migration chain的forward fix；Serve健康時維持唯讀服務。
