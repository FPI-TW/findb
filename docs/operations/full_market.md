# Full market 手動啟動與 runtime enrollment

`FULL_MARKET_ENABLED=false` 是 local、staging、production 的預設值。Backend 與 Fetcher 必須使用
相同 `APP_ENVIRONMENT=local|staging|production` 與旗標。既有 JSON `deployment_target` 仍是環境匹配契約；legacy replay/inspection 窄 bridge 見 deployment runbook。GitHub Environment 的 `FULL_MARKET_ENABLED` 決定一般 deploy 的
`bounded`／`full-market` profile；rollback 從 immutable accepted record 讀取原 profile，不修改 bundle。
旗標為 true 不會啟動排程。缺 runtime、evidence、baseline 或 calendar 時，只阻擋 Full market；
Pilot 保留自身 control 與 Source credential 權限。

## 啟動契約

Owner 先審核並發布 official baseline，再發布完整年度 exchange calendar。管理流程將實際安裝
runtime、Source client UUID、environment、artifact／config、provider evidence、quota、帳號配置與
有效期限綁定。Fetcher 即使停止也可 POST `/api/v1/source/full-market/readiness`，只回報 enrollment
ID、digest、runtime ID 與已登錄 feeds；heartbeat 不能授權或延長 evidence。

Owner 透過既有 `PATCH /api/v1/admin/schedulers/{scheduler_key}`，傳入 `desired_state=running`
與 `expected_revision`。Backend 在 transaction 中重查全部資格，凍結當時 ready feeds 與 capacity
proof。部分 feeds ready 即可啟動；新的 ready feed 須停止後重新啟動，重複 running PATCH 不改 scope。
每 dataset 首次日期由交易所當地今天決定；migration 保留合法 legacy activation_date，malformed
日期阻擋該 feed。停止、重啟與重新 enrollment 均不重設日期。舊 `/admin/feeds/{dataset}/activate`
與 `/deactivate` 在 Owner 認證後回傳 410；沒有 per-feed activation、manual readiness_approved 或五日門檻。

每次 provider call 前立即查 effective Source authorization。旗標關閉、evidence 到期、runtime
替換或 calendar 不完整時，禁止新抓取與 plan。已保存 prepared payload／frozen plan 的 delivery、
normalization、canonical 與稽核可繼續；不重抓已 prepared 資料。關閉旗標由 ingest/all lifespan 或
下述管理命令停止 Full controls 並寫 audit；GET 投影永遠唯讀。重新開旗標後仍需 Owner 手動啟動。

## 共用 account allocation

安裝 Full profile 時，保留 Pilot 與已有 historical workers，使用獨立 container／checkpoint names。
所有 installed consumers 共用 `/var/lib/findb-account/<environment>/governor.sqlite3`；provider account
requests、minute quota、daily bytes 與 conservative reservation 跨 process/restart 保留。Pilot／historical
使用 `.account.json` 的 installed allocation，與 Full readiness TTL 分開；已有安裝即使旗標關閉仍記帳。
普通 bounded/local Pilot 沒安裝 allocation 時維持既有行為。啟用 Full 前 operator 必須將當日其它
consumer 當日既有用量以 `account.usage_day`／`used_requests`／`used_bytes` 登錄（不能高估剩餘額度）；另將可能用量納入 `other_requests_per_day`／`other_bytes_per_day`；此 allowance 由 governor
實際限制。實際 response 超過保守 bound 時保留 overage debt 並停止新 acquisition。

Account 可 exclusive，或有明確 local/staging/production allocation，四個 quota 欄位的 allocation
合計不得超過 verified provider totals。Unknown/unbounded SDK bytes 或無法證明跨 environment
allocation 時不得登錄 Full readiness。Capacity 使用全部 ready feed 成員、report fallback、20% retry
headroom、daily calls/bytes 與 deadline，以及每 provider call 八次 Source checks 的 roundtrip/RPM
成本，保留 20% Source headroom；每個 provider/dataset 另依全 universe 成員計入至少四次 control／authorization／delivery或outcome／terminal Source requests，含 cached FinLab table 與 TAIFEX shared report。所有 opt-in consumers 的每次 Source HTTP request 共用 durable Source-key pacing，含 retries 與 stopped drain；Source authorization 回報 effective interval，由共用 permit 實際限速，等待不重複探測 Source。Declared Source RPM 不得超過 DB client limit；Full profile provision 為 2400 RPM，普通 bounded 為 120，既有 Full scope/budget 在 bounded key rotation 保留供 frozen delivery。Admission deadline 取 declaration 與已支援 feed window 的較小值（US 10800、HK/TW 14400、TAIFEX 18000 秒）；不能截斷 universe。Provider pacing commit-delay race 另列 backlog，並非本功能修復範圍。

## 管理輸入與輸出

Declaration 的嚴格型別是 `backend/app/schemas/full_market_readiness.py`。使用 UTC-aware
`verified_at`／`expires_at`，安全 HTTPS evidence URL 與 SHA256。`datasets` 可為 provider 的部分 scope。
`requests_per_day`、`requests_per_minute`、`requests_per_second`、`bytes_per_day`、`max_response_bytes`
、`source_requests_per_minute` 以及 `account` 必須反映真實帳號配置。FinLab bound 必須涵蓋 SDK 原始整張 table（上限 2GiB）；HTTP providers 上限 16MiB。`account.consumers` 列出所有實際安裝 consumer，包含
`full_market`、`maintenance`，以及存在的 `pilot`／`historical`。

先將 declaration 的 `provider`、`environment`、上述 provider quota、`source_requests_per_minute` 與 `account` 存成
`<provider>.account.json`，`requests_per_second` 用 JSON float。它是 operator 管理的 installed config，
不含 credential；部署前放到 root-owned `/etc/findb-full-market/readiness/`（檔案 0644、目錄0755）。
Receipt generator 比對實際安裝，不能手工捏造 receipt。Declaration 的 runtime/artifact/config/account
identity 取自該 receipt，verified_at 在 receipt installed_at 之後；account digest 由管理 schema 算出並驗證。

管理命令必須在 Backend management DB scope 執行，不能向 Fetcher 提供 DB 或 Admin credential：

```bash
uv --directory backend run python scripts/enroll_full_market_runtime.py \
  --declaration /secure/finlab.declaration.json --receipt /secure/finlab.receipt.json \
  --artifact /secure/artifact --output-dir /secure/enrolled
```

Deployed 另加 `--manifest /secure/release-manifest.json --accepted /secure/accepted.json`。
命令寫入 enrollment/audit，並輸出 canonical `<provider>.json`、`.account.json` 與 `.enrollment.json`。
將三個檔案複製到 operator-owned readonly readiness directory。Runtime 直接讀 canonical JSON；
不要加 envelope metadata 或自行修改 timestamp/數字型別，否則 digest 不一致。更新 scope、quota、
artifact/config、runtime 或 expiry 要重新管理 enrollment；Source report 不可改它們。Revoked digest
不能透過 idempotent 呼叫復活。

僅同步旗標並停止 Full controls：

```bash
FULL_MARKET_ENABLED=false uv --directory backend run python scripts/enroll_full_market_runtime.py
```

## Local 可執行流程

1. 設定 Backend/Fetcher `APP_ENVIRONMENT=local`、`FULL_MARKET_ENABLED=true`，完成 migration、seed
   與 scoped Source credential。Source/Serve 可以使用 loopback HTTP origin；遠端仍須 HTTPS。
2. 選用 `fetcher/configs/full_market.local.v1.json`。在 repo root 建立 durable governor 並明確設定 runtime env：

```bash
mkdir -p /private/tmp/findb-local-account /private/tmp/findb-local-full /private/tmp/readiness
uv --directory fetcher run python -c 'from pathlib import Path; from findb_fetcher.full_market_state import FullMarketState; FullMarketState(Path("/private/tmp/findb-local-account/governor.sqlite3"))'
export APP_ENVIRONMENT=local FULL_MARKET_ENABLED=true FETCHER_CONSUMER_PROFILE=full_market
export FETCHER_ACCOUNT_STATE_PATH=/private/tmp/findb-local-account/governor.sqlite3
export FETCHER_ACCOUNT_READINESS_FILE=/private/tmp/readiness/finlab.json
export FETCHER_ACCOUNT_ALLOCATION_FILE=/private/tmp/readiness/finlab.account.json
```

   安裝上述 account file；設定 Fetcher 正常的 provider、calendar Serve、Source client key 與 Raw R2 env（見 Fetcher README）。額外 Pilot/historical workers 要使用相同 governor/allocation，並 export 各自 consumer profile。
3. 啟動停止狀態 worker，記下 PID；此時缺 enrollment 不抓 provider：

```bash
fetcher/.venv/bin/findb-fetch-full-market --provider finlab \
  --config "$PWD/fetcher/configs/full_market.local.v1.json" \
  --state-path /private/tmp/findb-local-full/state.sqlite3 \
  --readiness-file /private/tmp/readiness/finlab.json --run-forever &
FULL_PID=$!
```

4. 另個 terminal 執行實際 process inspection；額外已安裝 Pilot/historical process 以
   `--consumer-pid pilot=<PID>`／`--consumer-pid historical=<PID>` 登錄：

```bash
uv --directory fetcher run python -m findb_fetcher.installation_receipt \
  --pid "$FULL_PID" --config "$PWD/fetcher/configs/full_market.local.v1.json" \
  --governor /private/tmp/findb-local-account/governor.sqlite3 \
  --allocation /private/tmp/readiness/finlab.account.json --output /private/tmp/finlab.receipt.json
```

5. 用 receipt 填好 declaration，`--artifact` 指向安裝的
   `fetcher/src/findb_fetcher/`（整個已安裝 Python package），執行 management enrollment 並複製 canonical outputs。
   Worker 停止狀態 report 後，必要時用 `--sync-universes` 提交候選 baseline，再由 Owner publish。
   完成完整 calendar 後在 Full market tab 手動啟動。Local runtime identity 由 artifact/config/governor
   hash 決定，普通 restart 穩定；generator 比對 PID 的實際 env/allocation 路徑與啟動時間，source/config 修改後需先 restart 再 inspection。實際配置變更使舊 evidence 失效。

## Deployed 可執行流程與 drain

一般部署先完成 verified candidate、immutable acceptance 與 activation。從 release coordinator 的
accepted object 複製完整 `accepted.json` 至 Fetcher host，與已驗證 release root `release-manifest.json` 一起保存。
**完成 activation 後**在 host 執行 bundle 內 generator；它實際讀 Docker container ID、image、
environment、running state、RW governor mount、每個容器的 actual allocation digest 與 Full config digest：

```bash
python3 <release-root>/infra/deploy/inspect_full_market_installation.py \
  --manifest <release-root>/release-manifest.json --accepted /secure/accepted.json \
  --config <release-root>/fetcher/configs/full_market.staging.v1.json \
  --provider finlab --governor /var/lib/findb-account/staging/governor.sqlite3 \
  --allocation /etc/findb-full-market/readiness/finlab.account.json \
  --output /secure/finlab.receipt.json
```

Production 使用 `full_market.production.v1.json` 與 production governor path。Receipt 連同 immutable
manifest/accepted record 複製至 management host，artifact 為同一 manifest，再執行 enrollment。
單有 accepted bundle 不代表已安裝。停止 worker 仍每 30 秒 ACK 已登錄 installation；ACK 超過 90 秒或 runtime flag off／identity/config/account 不符即阻擋新 admission/acquisition。此 ACK 不能延長 evidence TTL 或創造 quota authority。Backend/Fetcher flag mismatch 是 blocker；prepared drain 仍可完成。Docker ordinary restart 保留 ID；recreate/upgrade 的新 ID 要 fresh
inspection/enrollment。四個 Full workers 為 `findb-full-market-{twelve-data,finlab,shioaji,taifex}`，
Pilot 保留原 names；production Full checkpoint 保留既有 `/var/lib/findb-full-market/state.sqlite3`。

關閉旗標的一般 bounded deployment 保留已安裝 Full workers/checkpoint，讓 prepared backlog drain，
新 false install 不新增它們。不要刪除 state、container 或改用 Pilot names。待 durable pending plans
皆完成後 operator 可另行退役。Rollback profile 使用 accepted record；bounded replay 保留已安裝 compatible Full image 供 drain，不能用舊 bounded image 降級 drainer；Owner 先在兩個 tabs 停止
Pilot 與 Full，確認 desired/observed stopped，再依既有 rollback 流程恢復。旗標重新開啟不自動 start。

## Dashboard

導入／排程預設 Pilot，`?profile=full_market` 選 Full market；切換只導覽，不寫 control。每 tab
只呈現該 profile 與各自 bulk controls，標籤顯示 running 數量。Full 顯示有效旗標、readiness blockers、
凍結 scope 與 first dates。Overlap confirmation 使用所有 controls（包含另一個隱藏 tab 的 desired
或 observed running）；Owner、CAS、skip/error UI 與部署前停止兩邊提醒維持。

## 容量違規與可信復原

Provider response 或官方 universe snapshot 超出 installed `max_response_bytes` 時，共用 governor
會原子保存實際接收的額外 bytes 與 account-level capacity violation；所有 Full、Pilot、historical
provider acquisition 都停止。Content-Length 超限即使尚未接收 body 也保存 declared witness；不把
未接收 bytes 當 actual usage。SQLite `full_capacity_violation` 的 `received_bytes`／`declared_bytes`
分別顯示證據，`observed_bytes` 是修復時新 bound 必須涵蓋的最大 witness。每日 quota 保留 conservative
reservation 與 actual overage debt。普通 restart、隔日、TTL 更新或 heartbeat 都不解除違規；Source
pacing 與 prepared delivery/normalization 可繼續。不要刪除 governor 或手改 usage/violation table。

首次 Full checkpoint bootstrap 僅傳入與 config 一致的 `APP_ENVIRONMENT`，在 local／staging／production
以離線 `--initialize-state` 建立 SQLite；不載入 Source/provider secrets。已有 checkpoint 跳過初始化。
Full profile 的此步驟僅套用 `full_market` consumer 與 `findb-fetch-full-market` CLI；
Twelve Data／FinLab Pilot 與 historical 保留各自 startup/state 邏輯，不能收到 Full 初始化參數。
未指定 consumer 維持 Pilot 預設，Full CLI／consumer 不匹配一律拒絕。Shioaji Pilot 的既有專用
CLI bootstrap 與 versioned-state guards 保留。Coordinator 保留用於 compatible image／secret selection 的
effective Full profile，另將已驗證 manifest 的 recorded release profile 傳給 bootstrap dispatch。
Full command 明確傳入同一 mount 的 `--state-path`：production 保留
`/var/lib/findb-full-market/state.sqlite3`，staging 使用 `/var/lib/findb-full-market/staging/state.sqlite3`，
initializer／control probe／worker 都依此定位，不以 CLI default 代替 checkpoint。
已有 named／legacy Full worker（包含僅剩 accepted `-previous` 的 recovery container）時，無論 ordinary
upgrade 或 accepted replay、bounded 或 Full profile，checkpoint 必須先存在、符合既有 nonsymlink path 規則，並通過 read-only 最小穩定 core-table
檢查（不要求新的 capacity／repair tables）。以 read-only `PRAGMA integrity_check` 驗證實際 pages，
並確認 `full_work`、`full_plan`、`full_quota`、`full_cursor` 的完整舊 core 欄位可讀；僅有同名空表不算合法
checkpoint。Zero-byte、physical corruption、缺少必要欄位、非 Full 或 malformed SQLite 同樣拒絕，
即使 retained helper／image 較舊也不得先補建 schema。此唯讀 inventory gate 在 historical worker
retirement、其他 stop／rename／start、helper、runtime secret loading 或 control probe 前執行；不建立
空白替代 state，也不嘗試恢復遺失 prepared work。Valid previous-only recovery 保留原 container identity、
image 與 checkpoint，由既有 recovery journal／helper 接續恢復。Pilot、Full 與 historical 共用完整交易 snapshot；journal 記錄原始
stable／previous 名稱、ID、image 與 running 狀態；失敗或 signal 會回復原始狀態，原本 stopped 不會被
自動啟動。若 stable 另有 accepted stale previous，先以獨立 journal 移至 transaction backup；失敗先回復
current original，再回復 stale backup 的原始名稱與狀態。只有成功 commit 後，且 backup identity 相符、
graceful stop 與 strict retirement 均通過，才以非 force removal 清除 stale backup。若 Docker 持續拒絕
stop，交易明確回報 rollback failure 並保留原始 identity，不 force-remove；此時不能宣稱已恢復原始
stopped 狀態，須 operator 排除外部 Docker 問題。
Bounded accepted replay 的 drainer image 取自完整 registered accepted original snapshot；unaccepted
stable interrupted candidate 不參與 image 選擇。Legacy 名稱的 migration 也先選 accepted original，
再依該 original 的 command 判斷 Full 角色；未 accepted 的 Pilot／Full stable 不會遮蔽 accepted Full previous，
也不會將此 previous 誤判為 stale backup。Previous-only historical 的 accepted original 在建立
replacement 前停止並嚴格退役，失敗／signal 依 journal 回復原 previous 名稱與原 running 狀態。
各 profile 的 registration 先完整讀取 stable／previous 的 accepted metadata、ID、image、running 狀態，
並檢查 transaction-backup collision，全部成功後才登記 recovery journal 與 staged rename。Inspect／
collision 失敗不會啟用 generic rollback；每個完整記錄同時保護兩個 accepted initial IDs（不將未 accepted 的 interrupted candidate 當 original），因此 journal 已登記、
backup 尚未 rename 時收到 signal，也不會將 accepted stale previous 誤刪為 candidate。舊 accepted
runtime 沒有 label 的 Docker `<no value>` 表示仍相容；inspect command failure 或未知 metadata fail closed。
Coordinator 初始 inventory 同時涵蓋所有將操作的 Pilot／Full／historical stable、previous、candidate、
backup 與 preflight names，並驗證完整 identity/state 欄位；任何 unknown 在 journal、current pointer、
secrets 或 runtime mutation 前拒絕。Normal helper retirement 與 commit preparation 都要求成功且合法的
Running=false、ExitCode=0、OOMKilled=false 與空 State.Error；僅保留既有精確匹配舊 staging historical
137 的退役例外。所有可失敗的 original／backup identity、state 驗證先於 commit；成功後僅以
已驗證 ID 做 non-force removal，避免 name reuse，亦拒絕已重啟的 runtime。刪除前再次依 ID
驗證 image 與 strict retirement 欄位；postcommit 查詢／狀態失敗會回報 `committed_cleanup_failed`（`committed=true`），保留已 committed replacement／checkpoint 與尚存舊 IDs，不回報 activated success、
不宣稱 rollback，須先排除 Docker／舊 runtime 狀態問題再退役。
Trusted external guard 與 coordinator 使用相同 managed inventory：Full／Pilot 及兩者 historical base，
各自涵蓋 stable、previous、candidate、transaction-backup、legacy-previous、preflight、preflight-state、
preflight-control。Standalone guard 使用一次成功 exact-name inventory snapshot，每個列出項目仍須成功 inspect；
衍生／historical 名稱也必須在 immutable coordinator 前通過 command 與 Full checkpoint gate。
Current external gate 與 coordinator 的 `{{json .Config.Cmd}}` 必須是非空 JSON argv array、每項為字串，
並以已知 managed worker executable（Full、Pilot、historical）或確切 `python -m findb_fetcher` readiness
command 辨識角色。Blank／non-JSON／null／未知 command 均在 journal、pointer、secret 或 runtime effects
之前拒絕；argument 內的 Full 字樣不決定角色。查詢或 decoding 失敗只回報固定安全 reason，不輸出 Cmd。
Current external gate／coordinator／helper 只將成功 `docker container ls --all` 的 exact-name inventory
視為不存在的證據；列出的 runtime 還須成功 inspect。Docker rc 1 同時可能表示 daemon／permission
failure，不能以 inspect 非零推定 absence。Unknown query 在 journal／runtime mutation 前拒絕；已開始
交易則明確進入 rollback，rollback／recovery 本身無法確認 inventory 時回報失敗並保留未知 identity。
Recovery 的 `.State.Running` 查詢須同時成功且回傳 `true`／`false`；rc 1／81、空字串或未知值均不得
視為 stopped 或用來猜測 start／stop。Named／legacy／generic cleanup 與 strict retirement 欄位查詢
不依賴 Bash errexit，conditional caller 也必須明確回報 `transaction_rollback_failed`／`recovery_failed`。
查詢無法驗證時保留 original name／ID／image，不宣稱已還原 running 狀態。Identity／image 與
舊 staging historical 137 例外的 image／Path／Args／Cmd／accepted 查詢也必須先驗證 exit status；
即使 stdout 符合已記錄值，非零 status 仍拒絕 restore、退役例外或 commit cleanup。
Genuine first install 與 previous-only recovery 仍支援。Helper entry 的 interrupted candidate／unaccepted
stable／bootstrap／historical 清理也使用 graceful strict retirement，絕不 force-remove；所有支援 consumer 的 stable 與
accepted previous 並存時，parent 必須先 journal/stage backup，helper 拒絕未 staged 的 accepted backup。
`release_fetcher_provider.sh` 是 verified coordinator 的內部 child；operator 必須使用已驗證的 coordinator／workflow，
直接呼叫 helper 不屬支援的維運介面。Parent 的 initial inventory、完整 journal 與 checkpoint gate 是部署前提。
實際 provider helper recovery trap 也採 graceful stop、stopped／exit 0／無 OOM／無 Docker error 的 strict
retirement，再以非 force removal 清理新 candidate／replacement／historical worker。Known original ID
不進入此清理流程；retirement 拒絕則保留該 runtime 並明確回報 recovery failure，不能由 child force-remove
後再期待 coordinator 還原。

GitHub replay 的外層 checkpoint gate 使用 `infra/deploy/check_full_market_checkpoint.sh`，另從同 repo 的
`github.workflow_sha` checkout 取得；必須驗證 checkout SHA 與 guard 存在，缺檔或不符即停止，不能 fallback
到 `inputs.revision` 或舊 accepted bundle。SSM 對 base64 傳輸解碼後再次校驗 SHA-256，先執行此唯讀 gate，
再進入 immutable coordinator 與原本已驗證的 legacy environment bridge。因此 pre-marker accepted bundle
同樣受保護，仍使用其記錄的 profile，manifest／bundle 內容不改寫。既有 accepted bundle／image 可正常 replay，
checkpoint 遺失或不合法時需 operator 處理 durable state，部署不會重建未知 prepared work。
目前 transported 離線測試使用 frozen coordinator／helper／CLI excerpts 與 no-op registration，證明
拒絕順序及舊 bootstrap 邊界，未完整覆蓋舊 coordinator 的 flags／traps／pointer 與成功 replay。
另有完整 HEAD coordinator 的 unknown Cmd proof，確認 current guard 拒絕時零 effects、IDs／pointer 保留；
此證據不等同完整 accepted bundle 成功 replay。完整離線 harness 見 backlog，實際成功 replay 仍屬外部驗收。
Docker `--check`／`--require-stopped` probes 可使用 `cache_dir=-`，空 cache mount 在 Bash 3.2 下同樣安全。
`--require-stopped` 僅以 read-only SQLite 驗證舊 compatible checkpoint 也具備的 core tables，再查詢
Source desired state；缺檔／不合法 state 會在載入 credential 與 Source 呼叫前拒絕，既有 prepared
bodies、debt、cursor 不改寫。只有沒有既有 Full worker 的真正首次 Full 安裝可執行明確 initializer。

所有 HTTP status 都先做 bounded read 與 byte/witness 記帳，包括 Twelve Data daily、官方 universe、TAIFEX fallback
report 的非 200 回應與中途 transport failure；error body 不解析為 provider data/universe。
已知 429 在讀取 body 前就將 60 秒 cooldown 持久化至 active Permit 的實際 shared account/governor，
即使 body 超限、encoding／transport／observer 失敗也保留 cooldown。Twelve Data 的 Pilot／historical
client 使用自己的 shared Permit，即使未提供 Full callback 也遵守同一帳號 cooldown。所有非 200
（包含成功形狀的 201／206）一律拒絕；錯誤 JSON 只供遮蔽 secrets 的診斷，不成為成功 payload。
中途 transport error 保留實際已讀 bytes，不能把未接收 body 當 debt。其他 consumers 與重啟程序遵守同一帳號 cooldown；
未安裝共用 allocation 的既有 runtime 保留自身 provider checkpoint fallback。保留 conservative
reservation，僅計入實際已接收的 overage，宣告長度不冒充 actual debt；prepared Source drain 不受影響。
已知超限 Content-Length witness 先於 unsupported encoding 拒絕保存；buffered 非 identity content
可能已解碼，其 decoded 長度不作為 actual wire debt；只有可取得的 raw-byte counter 才計 actual，
無可觀測 wire counter 時保留未知為 0 與 conservative reservation。

先停止兩個 profiles，調查完整 response 大小、重試與 deadline/capacity，設定足夠的 finite bound、
daily budget 及各環境 allocation，更新實際安裝的 `.account.json`。依上方流程 restart、fresh
installation inspection、trusted management reenrollment，將新的 canonical `.json`、`.account.json`、
`.enrollment.json` 安裝至同一 governor 的 consumers。從該 SQLite 唯讀查得目前 account 的
`violation_id`，再由可信 host operator 明確執行（deployed 可在既有 Full container 內執行，local 使用 uv）：

```bash
python -m findb_fetcher.account_governor repair-capacity \
  --provider finlab --environment staging \
  --governor /var/lib/findb-account/staging/governor.sqlite3 \
  --readiness /var/lib/findb-account/readiness/finlab.json \
  --allocation /var/lib/findb-account/readiness/finlab.account.json \
  --enrollment /var/lib/findb-account/readiness/finlab.enrollment.json \
  --violation-id <current-violation-id>
```

命令必須使用支援 `GET /source/full-market/enrollment-verification` 的目前 Backend 與有效 Source key；
不能靠檔案中的 UUID/hash 自行解除違規。該唯讀接口核對目前管理 enrollment、Source client/scope、
canonical declaration digest/runtime/artifact/config/account allocation/expiry，以及 90 秒內的 matching
installation ACK；即使 Owner stopped、flag=false 或容量違規仍可驗證，且不回傳 acquisition 授權。
Worker 必須先持續上報新的 enrollment；CLI 本身不代替 worker 上報，也不延長 TTL。

CLI 在 Full worker 的環境執行，核對 configured readiness/allocation/governor 路徑與實際 config digest/runtime。
Local 在 repo root 另設 `export FETCHER_FULL_MARKET_CONFIG="$PWD/fetcher/configs/full_market.local.v1.json"`，
沿用前述安裝 env。Deployed 使用實際 Full container 的 config 路徑，例如
`docker exec -e FETCHER_FULL_MARKET_CONFIG=/app/configs/full_market.staging.v1.json findb-full-market-finlab python -m findb_fetcher.account_governor repair-capacity ...`
（後方使用上例的完整參數；production 換成實際 production config/path）。Readiness/allocation/governor
必須是該 container 已安裝的同一組檔案；新 config/allocation 必須先經 fresh inspection/management enrollment，
再由 worker matching ACK 確認。Source 驗證有 bounded timeout，失敗一律保留 violation。

全部 authoritative/installed bindings、新 allocation digest 與足夠的 bound 符合，才以 violation ID CAS 復原。結果保存至
`full_capacity_repair`；先前 usage/debt、pacing 與 prepared bodies 不重設。若新增 witness 與復原同時
發生，CAS 拒絕，須重新讀取最新違規。復原後 Full 仍需有效 readiness 與 Owner 手動 start。

舊 production Full runtime 曾使用 Pilot 的 `findb-fetcher-*-scheduler` names。升級或 flag-off bounded
部署會先 graceful stop，嚴格驗證非 OOM、無 Docker error 與合法 retirement exit，再搬至獨立 Full
names；既有 accepted image/identity/checkpoint 不刪除。每次 stop/rename 前登記交易 recovery entry，
失敗或 INT/TERM/HUP 時先倒序恢復 provider，再恢復原 names 與原 running/stopped 狀態。若有 accepted `-previous` 殘留，先停止／嚴格驗證並搬至獨立 Full `-legacy-previous` backup，避免 Pilot 復原誤選舊容器；失敗先恢復 current original，再恢復舊 previous，成功才清除這個非 current backup。退役失敗
會中止交易，不能透過強制刪容器跳過。Owner 應先停止兩個 profiles；idle `--run-forever` process
仍 running 是受支援的升級情境，deploy helper 負責優雅停止。
