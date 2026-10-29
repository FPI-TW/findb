# Production 環境維運

Production 固定使用 AWS account `289112218471`、region `ap-southeast-1`，state、KMS、ECR、S3、IAM、DB、host 與 runtime secrets 均與 staging 隔離。
兩環境共用部署契約，以 [部署文件](deployment.md) 為準；本文件保留 production 資源與待處理資料問題。
兩環境的統一版部署基準見 [部署文件](deployment.md)；native 資料與實際 rollback 演練仍見
[待完成項目](../dev/production-foundation-cutover-plan.md)，不得以部署健康驗收代替。

## Shioaji 缺口與 DQ 調查（2026-10-05）

Production 2026-10-01、10-02、10-05 共六筆未結案 missing alerts，分別對應每日的
`tw_equity_minute` 與 `tw_etf_minute`。三日的 SQLite checkpoint 均顯示 equity sequence 1
（`2330`）成功，sequence 2（`2454`）在第一次 attempt 以 `KBARS_PAYLOAD` /
`contract_access` 終止；其餘 48 檔股票與 3 檔 ETF 尚未嘗試，排程窗口已到 cutoff。
這是 provider contract access 階段失敗，不能把未嘗試序列判定為商品無資料。

調查當時 runtime 固定使用 Shioaji `1.7.1`。隔離的 bounded simulation 驗證確認 login 成功後，
`api.contracts` 實際為 `ContractsApi`；讀取 `.stocks` 立即拋出 `AttributeError`。
直接呼叫 `api.contracts.get("2454")` 則回傳 `BaseContract`，identity 為
`code=2454`、`exchange=TSE`、`security_type=STK`、`region=TW`，並成功 logout。
失敗根因是 runtime binding 的公開介面與已安裝 `_core.pyi` 中的 legacy `stocks`
宣告不一致，不是商品無資料或已確認的 catalog timeout。唯讀 direct lookup 另確認 frozen
53 商品全部可取得：code 與 manifest 一致、security type 均為 STK、region 均為 TW，
其中 52 檔為 TSE、1 檔為 OTC，無需推測 exchange。

Provider fallback 已改為 [官方合約文件](https://sinotrade.github.io/tutor/contract/)
公開的 `api.contracts.get(code)`；維持 missing contract 分類、credential-free error 封裝，
以及 `2330`、`0050`、`0056`、`006201` 四個已驗證 `BaseContract` identity shortcut。
Production 53 商品 manifest、sequence 順序與 cutoff／terminal 政策維持原設定。

修復 provider 不會自動重設既有 terminal checkpoint 或補送上述缺口。Production 現可透過
[Shioaji 歷史回補](data_maintenance.md#shioaji-股票etf-分鐘回補) 分別申請股票／ETF minute，
以 reviewed production 50／3 成員 manifest 建立新 request，保留 daily terminal checkpoint。
上述六筆 alerts 是當次調查紀錄；實際缺口須重新查證並完成回補驗收後，再人工處理對應 alerts／DQ。
不可刪除 SQLite state、鬆綁 daily cutoff 或把 terminal error 自動轉為 retry。
部署驗證與歷史缺口復原須各自留下驗收證據。

同次唯讀調查的 26 筆未解決 DQ issues 均為 `INGRESS_DELIVERY_POLICY_WARNING`，severity
為 `warning`。其中 16 筆為 Shioaji 09-21 至 09-24 的 historical payload 在 09-30 送達，
觸發 `LATEST_DATE_MISSING`（expected latest date 為 09-29）；另 10 筆為 FinLab snapshot
低於 delivery policy 最低 2,100 筆，五次各 50 筆、五次各 2 筆，其中四次同時缺 latest date。
Bounded FinLab production manifest `fetcher/configs/finlab_tw50_2026_09_21.v2.json`
只選 50 檔，與 full snapshot 的 2,100 筆 threshold
不相符，須獨立檢視政策與執行範圍。這 26 個 ingestion runs 均為 completed，合計成功
寫入 3,668 筆，`failed_records` 合計為 0；warning 不阻擋 canonical 寫入。
DQ 問題需逐筆經管理流程人工結案；provider 修復或部署不可自動 resolve，也未授權修改
DQ／delivery policy。

## Foundation apply

1. 使用 `findb-production` profile 驗證 caller identity，先在
   `infra/tofu/production-bootstrap` 執行 `fmt`、`validate`、refresh plan。Bootstrap plan 只能
   建立 state KMS 與 `findb-production-tofu-state-289112218471`，不可出現 delete。
2. 經核准後 apply bootstrap，將其 local state 遷移至獨立的
   `production/bootstrap.tfstate`；確認 versioning、public access block、SSE-KMS 與 lockfile。
   遷移前先將 `production-bootstrap/backend.s3.tf.example` 複製為 ignored `backend.tf`，再使用
   `bootstrap_backend_migrate_command` output；確認 remote state 完整後才安全刪除 local state。
3. 以 bootstrap output 的 KMS ARN初始化 `infra/tofu/production`。保存 refresh plan，確認
   account output 為 `289112218471` 且沒有 delete，再由受信任 operator apply。
4. 同一 PR 對 `infra/tofu/staging` 執行 refresh plan，新增的兩個 promotion-reader role 之外
   必須 zero-delete。Reader 只可讀自己 unit 的 `*/accepted/`、acceptance record 與 ECR layers。

禁止從 PR runner apply；禁止在 state bucket 存 application secret；禁止把 bootstrap saved plan
拿來 apply foundation。任何 account、region、resource prefix 或 delete 不符即停止。

## Live foundation acceptance

- VPC CIDR 必須是 `10.20.0.0/16`，無 NAT；RDS subnets 無 internet route。
- FinDB `t3.large`、Fetcher `t3.medium`，IMDSv2 required、hop limit 2、無 key pair、無 TCP 22。
  FinDB 443 只接受 Cloudflare published CIDRs；Fetcher security group 無 ingress。兩台host都必須安裝
  Docker Engine與Compose v2；`docker compose version`是部署前必要檢查。
- RDS 必須為 private PostgreSQL 16、`db.t4g.medium`、100 GiB gp3、KMS、Single-AZ、14 日
  backup、deletion protection，5432 僅接受 FinDB security group。
- 五個 ECR repository 必須 immutable 且 scan-on-push；instance role 只 pull 自己 unit，deploy
  role不能讀 Secrets Manager values，promotion role 只寫自己 unit 的 ECR 與 bundle metadata。
- 兩台 host 必須出現在 SSM managed nodes；使用 bounded SSM command 驗證 instance role、unit
  ECR pull、unit bundle read、cross-unit denial、runtime secret isolation與 RDS connectivity。
- 驗證 CloudTrail multi-region/log validation、GuardDuty、DLM recovery point、RDS automated backup、
  CloudWatch alarms 與寄至 `tyler.ho@fpitw.com` 的 SNS subscription。Custom metrics 在 collector
  尚未健康發布時必須因 missing data 告警，不得把 `INSUFFICIENT_DATA` 當通過。
- 所有資源必須有 `Project=findb`、`Environment=production`、`DeploymentUnit`、`Owner`、
  `BackupOwner`，並在 Cost Explorer 啟用這些 cost allocation tags；不建立固定 Budget。

## External resources 與 secrets

Cloudflare 帳號中建立 private `findb-production-raw` 與 `findb-production-canonical` R2 buckets。
Raw lifecycle 30 日並保護前 7 日；Fetcher raw credential只可讀寫 raw。Canonical publisher只可
讀寫 canonical，Serve reader只可讀 canonical，三組 access key/secret不得相同。管理 credential
不得注入 runtime。

建立 proxied `findb.tingfong.com` A record 指向 FinDB EIP，origin 使用獨立 Cloudflare origin
certificate。Fetcher EIP 的 `/32` 必須成為 `SOURCE_ALLOWLIST_CIDRS`。憑證、R2、DB、RabbitMQ、
四類 API key 與三個 provider credential 全部重新產生，分別寫入 OpenTofu 已建立的
`findb/production/findb/*` 或 `findb/production/fetcher/*` metadata；不得重用 staging 值。
Origin certificate secret固定使用單行 base64 欄位
`FINDB_ORIGIN_CERTIFICATE_PEM_B64`與`FINDB_ORIGIN_PRIVATE_KEY_PEM_B64`；部署只在instance上解碼、
驗證hostname、至少30日效期與keypair一致性，再原子寫入`/run/findb-runtime-secrets/nginx/`。
憑證與私鑰不得持久化到`/home/ubuntu`或進入GitHub Environment。部署產生的三個非秘密nginx
設定檔必須在驗證前正規化為root-owned `0644`；tmpfs中的憑證、私鑰與lookup key仍維持`0600`。

## 共用 credentials 與初始化

五個 provider Source scopes 在 staging／production 相同，只有 credentials values 不同：

| Source | Dataset allowlist |
| --- | --- |
| `twelve_data` | `us_equity_eod`、`hk_equity_eod` |
| `finlab` | `tw_equity_eod`、`tw_etf_eod` |
| `shioaji` | `tw_equity_minute`、`tw_etf_minute` |
| `taifex` | `tw_futures_eod` |
| `factset` | `hk_equity_eod` |

`reconcile_fetcher_credentials.py --runtime-profile unified` 只校準 allowlist／hash 並留下 audit，
不啟動 scheduler。五把 Source keys 相互隔離；現行execution manifest的HK feed使用FactSet，
Twelve Data保留HK allowlist僅供既有或已凍結工作的相容交付。有效日曆與執行政策由Source提供，
不再注入calendar Serve key。
跨 unit bridge 只傳 SHA-256，
instance roles 不放寬跨 unit Secrets Manager 讀取。
TAIFEX 公開 API 不需要 provider credential，但需要獨立 Source key；IaC 固定宣告此 metadata。
S3 state backend 必須使用既有 KMS key ARN，不能任意替換 encryption 設定。

## DB 與 calendar

兩環境共用 migration、application role 最小權限校準、registry 與 credential 初始化；
部署不執行 calendar seed，也不要求日曆完整。Published 日期優先，缺少日期採 weekday default。
新 controls 均 stopped；不得複製 staging canonical／raw／workflow data 至 production。
Candidate 必須先通過 migration 與內部健康驗證，才保存 acceptance；失敗保持停止，不讓舊程式操作新 schema。
詳見 [統一切換與回退程序](deployment.md)。

`backend/configs/calendars/` 的 2026 US v2、HK v1、CN v1、TAIFEX v1 JSON 各有
365 個明確日期、session 時間、來源 URL／原始檔 SHA-256 與 2026-10-02 review 註記。
TW 2026 沿用既有 immutable `holiday_schedule_115.csv`，2026-10-02 核對
[TWSE 官方開休市頁](https://www.twse.com.tw/zh/trading/holiday.html)及其
[2026 年度 JSON](https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20260101&response=json)
的 27 筆日期、名稱與開休市分類完全一致（換行格式不同）；原始 JSON SHA-256 為
`7fefe785ea7155a5004a2eb74486ad865ea5c4b5f02ee0cffbbdacb1ca2ea390`。
保留 2/12、2/13 無交易但可結算交割的 `settlement_only`，不以 TAIFEX 的休市分類取代。
HK 使用 HKEX 年度通告校正國票 HTML 遺漏的 6/19，2/16、12/24、12/31 為半日開市，
採 closing auction 最晚界線 12:10，正常日 16:10。US 依 NYSE 保留 10/12、11/11 開市，
11/27、12/24 於 New York 13:00 提前收市；其餘 open rows 為 09:30–16:00。
CN 取國票 PDF「上海及深圳」本地市場列並核對上交所年度安排，不以北向／南向通休市
代替本地股市；4/3、4/7、7/1、10/19 維持開市，例假補班週末仍休市，收盤 15:00。
TAIFEX 僅取官方年度 PDF 的 regular 台灣指數期貨交易日期，2/12、2/13 是休市，
不能複用 TWSE settlement-only 狀態；regular 收盤 13:45，彩色到期／掛牌標記不視為休市。
個別契約最後交易日、國外商品與臨時停市不由此 fixture 推測；臨時異動需 Owner 新 revision。
After-hours 仍保留交易所 attributed trade_date，不能轉成民曆日期或建立 continuous futures。

`seed_production_calendars.py --deployment-target production` 預設只驗證檔案；
`--apply` 才使用既有 preview → apply → publish 流程發布缺少的完整年度。
任何未知來源、operator override、draft 或不同 rows 均拒絕覆寫。
已存在且 source metadata、checksum、365 筆 rows 全部符合舊版 NYSE bootstrap 的 US 2026，
一般 seed 保留 revision 並回報 `action=pending_reviewed_update, upgrade_required=true`，
不阻塞一般部署，也不宣稱新 session 已套用。Owner 審核後才以 `--apply --upgrade-reviewed-2026`
發布 successor，透過 expected revision 的 CAS 防止競爭，原 revision 保留為 superseded。
HK／CN／TAIFEX 初次套用與 US successor 均不改動 scheduler admission、首次日期、
readiness 或 scheduler desired state；本 repo fixture 的更新不等於 live DB 已調整。
