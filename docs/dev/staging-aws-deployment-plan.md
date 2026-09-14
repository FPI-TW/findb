# Staging AWS Deployment Completion Plan

## CI/CD convergence follow-up（未完成）

Staging 現在以 protected `main` 上 shared change policy 所選的 unit **自動** rollout；manual
dispatch 僅用於精確 accepted bundle replay，不能選擇 production。contract-only change 會執行兩個
unit CI、但不自動部署；unit-specific staging caller或reusable deploy workflow變更則必須rollout對應
unit，確保deployment path本身可被staging驗證。新的 accepted manifest 為 v2，必須帶 target/account/registry、release tag
與 deterministic bundle identity；candidate 成功後才持久化 accepted record，再 activate/health gate。

既有 staging v1 accepted bundle 保留為 read-only replay 相容路徑，直到所有仍可能需要的 recycle
record 超過保留期；它不得 promotion 至 production。完成 v2 轉換前仍須：

- [x] 兩個 staging caller 已只負責 policy、exact bundle preparation 與 target-scoped credential
  isolation；candidate／accepted replay、immutable acceptance 與 activation SSM transaction 已收斂到
  reusable target-aware workflows，並保留 bounded marker protocol。
- [ ] 以 staging accepted v2 record 驗證 production digest copy、production v2 accepted record與
  production-only rollback replay；production 資源尚未建立時不得執行。
- [ ] 在 v1 replay/recycle horizon 結束後，另行授權移除 v1 accepted compatibility reader。

Daily DLM recovery-point observation已於2026-09-14完成；以上不改變GHCR metadata
recycle/retirement工作的既有範圍與狀態。

> 狀態：Phase 0–1完成；Phase 2A的runtime-secret／ECR cutover、GHCR metadata retirement apply與
> DB-backed runtime credential rotation已完成；accepted SHA後四個active feeds的完整原生provider
> cycle已於2026-09-09驗收完成。Phase 2B的provider與R2 acceptance-criterion scope項目、RabbitMQ
> rotation及GitHub runtime copies移除均已完成。Phase 3的兩個unit normal accepted deployment與
> accepted replay live gate均已通過，文件closeout已由PR #203完成。FinDB Phase 4已完成兩次正常
> SSM deployment、一次accepted replay與三個FinDB deploy SSH secrets移除，exit gate已完成。
> Fetcher Phase 5已完成兩次正常SSM deployment、一次accepted replay與FinLab smoke、scheduler
> 恢復及三個Fetcher deploy SSH secrets移除，exit gate已完成。
> Phase 6已完成native/custom CloudWatch alarms、SNS通知與synthetic驗證、RDS PITR restore、兩台encrypted
> root replacement、兩個unit的Session Manager recovery、SSH ingress與host recovery key退場、generated cache重生、
> different-digest rollback、schema-incompatibility rejection、SQLite recovery及RabbitMQ rebuild。Current-volume
> daily DLM policy與即時encrypted snapshots已建立；2026-09-08已修正重複`Purpose` tag並以fresh
> zero-delete plan原地apply，AWS回讀policy為`ENABLED`。2026-09-09已上線DLM policy health custom
> metric與alarm且43個alarms全為`OK`；2026-09-14回讀確認至少五個雙volume排程週期，首兩個不同
> 週期驗收完成。
> 本文是staging
> AWS控制面、部署身分與驗收的核心
> 成熟化計畫；現行可操作 runbook 仍以
> [`../operations/deployment.md`](../operations/deployment.md) 為準。
>
> 最後盤點：2026-09-08。Phase 1 evidence 的 protected `main` merge SHA
> `77212ce47b3138c2e21c6984e989b66239fd3cce` 已由兩個 GitHub Environments 完成 OIDC／SSM
> preflight及既有SSH deployment，OpenTofu remote state、OIDC/IAM、instance profiles、required
> tags、SSM managed nodes、Session Manager與unit-specific SSM logs均有live evidence。Phase 0與
> Phase 1已通過exit gate；「服務可用」仍不等同於Phase 2–6的secret遷移、digest／manifest、
> SSM deployment cutover、監控與災難復原已完成。Staging 已完成 Amazon ECR foundation、publisher
> roles、workflow cutover與live deployment驗收；Phase 3的五枚digest、兩個unit-specific accepted
> bundles、normal deployment及accepted replay均已完成live驗收。**目前 FinDB accepted commit為**
> `a6ed544a809e64ba98477e4d671c489acce42a20`；**目前 Fetcher accepted commit為**
> `f5eec7903f3a0f63a1cbff6da656dbbec17b70eb`。Phase 2A的完整四feed原生provider cycle已通過，
> Phase 2B的GitHub runtime copies亦已移除。Phase 2B的provider與R2
> acceptance-criterion scope項目已依使用者
> 核准完成，RabbitMQ rotation已有live evidence。GHCR metadata retirement apply已完成，兩筆實體metadata待2026-09-27
> recovery window
> 結束後刪除。首次共同自動化preflight acceptance為兩個unit的protected `main` merge SHA
> `228989afe857c82d619cd53d15dbb29873d6710a`。

## 2026-09-08 staging驗收紀錄

- GitHub repository variable `STAGING_ECR_CUTOVER_ENABLED`為`true`；`staging-findb`與
  `staging-fetcher`只允許`main`部署。Default branch由active rulesets `Main protection`及
  `FinDB required CI`保護，後者要求`Required CI`；傳統branch-protection endpoint回覆404是因目前
  使用ruleset，不代表default branch未受保護。文件merge SHA
  `fa1d5c7f1cc0d5475303913290874c63c10b12c4`的兩個CD run
  [34197329827](https://github.com/FPI-TW/findb/actions/runs/34197329827)與
  [34197329911](https://github.com/FPI-TW/findb/actions/runs/34197329911)均只有policy成功，其餘
  verify／select／publish／prepare／deploy跳過，符合docs-only change policy。
- S3 v2 accepted records與host current pointers一致：FinDB為commit
  `a6ed544a809e64ba98477e4d671c489acce42a20`、bundle
  `a20fbabf99144721c0e0a67f5632d2b5832835b5d57062dc6f218b534c03e76e`、run
  [34122194372](https://github.com/FPI-TW/findb/actions/runs/34122194372)；Fetcher為commit
  `f5eec7903f3a0f63a1cbff6da656dbbec17b70eb`、bundle
  `9e56954b0460380b75ea7d2b5768040f01fa08e25914bb4b1efd1d1bf1a83b9b`、run
  [34087810284](https://github.com/FPI-TW/findb/actions/runs/34087810284) attempt 2。五枚running
  image digest與records一致；FinDB八個containers健康，三個active provider schedulers running且
  restart count為0。三個另行隔離的historical workers不列為active scheduler single-writer。
- SSM command `73a4359c-4d75-494f-b986-d8604ad571c5`確認queue健康：unpublished outbox、DLQ、
  expired leases及missing deliveries均為0，worker heartbeat約24秒；歷史累計保留一筆failed及一筆
  retry-exhausted，不解讀為目前backlog。兩台EC2均running且SSM Online；42個CloudWatch alarms全為
  `OK`，但這組alarms未揭露下述DLM policy execution error。
- SSM command `8bf7e518-3521-43fc-80d1-4007474845cd`後驗最近十日非rerun資料：FinLab
  `tw_equity_eod`在2026-08-31至09-07的六個交易日各2筆；Shioaji `tw_equity_minute`與
  `tw_etf_minute`在2026-09-01至09-08各有效日分別270及810筆；Twelve Data
  `us_equity_eod`在2026-08-31至09-04各有效日3筆。所列有效日均raw present、run/job completed、
  outbox published、DQ error 0、canonical count一致且attempt 1。Fetcher accepted record建立後，
  FinLab與兩個Shioaji feeds已有原生成功週期；Twelve Data尚須等待下一個eligible schedule，因此完整
  post-accepted-SHA gate仍不關閉。
- 2026-09-09 Twelve Data的下一個eligible native schedule已補齊最後一個時間門檻。Fetcher SSM
  command `4aac2ffd-bcd7-4d95-8806-db095c71bdb0`確認AAPL、MSFT、NVDA三個SQLite jobs均在attempt 1
  completed，checkpoint由2026-09-04前進至2026-09-08，universe SHA
  `60558e7a73a3d0622463ab2ef686b7ac95ec11c4fda01bc170e0a48bea12f6a9`、schedule SHA
  `d2bbb6ce5ae2c3f2d1a6d3d8a0d8d4f73d5f462d0830bf14d564cb7d4b93bfa5`，使用3／3 credits；三個Raw R2
  objects的內容與metadata checksum均通過。Nginx在00:15:27–00:16:28 UTC保存三個Source POST `202`；
  FinDB SSM command `48711d23-46c1-4cd5-a670-5baab115a14b`確認三個非rerun run／job均completed、outbox
  published、attempt 1、DQ error 0，canonical對應同一run且active jobs與unpublished outbox均為0。
  公開Serve精確回傳2026-09-08的AAPL、MSFT、NVDA三筆，Dashboard lookup最終HTTP 200。因此FinLab、
  兩個Shioaji feeds及Twelve Data都已有目前accepted SHA後的原生成功週期，完整四feed gate關閉。
- SSM command `504da39c-583f-4233-a34b-9c93f4280353`從Raw R2唯讀下載上述四feed各最近兩個
  有效交易日的16個objects（126,918 bytes），內容SHA-256、object metadata與DB contract checksum
  全部一致。Command `ce400d4a-3202-40f0-b014-b3df9a4f3188`保存2026-09-08四個Shioaji Source
  ingest的HTTP `202`。四個代表性run的Admin raw endpoint均為HTTP 200；公開Dashboard lookup最終
  HTTP 200，其exact Referer注入的Serve freshness HTTP 200；Serve EOD精確回傳2026-09-07的FinLab
  `2317`／`2330`兩筆及2026-09-04的Twelve Data `AAPL`／`MSFT`／`NVDA`三筆。FinLab／Twelve Data
  所選歷史run的原始`202` access log未跨部署保留，minute亦無Serve read model；完整lineage backlog
  因此只記為部分通過。
- DLM policy `policy-0d0a29c9e19f6323e`的重複tag根因已修復：保留`CopyTags=true`，schedule-only
  tag由`Purpose`改為不與volume tags重疊的`BackupPurpose`。OpenTofu saved plan SHA-256
  `49fb3fe75512a8d97388daf312b8d0f541bc9935aef48f7d150a36f0eb0dba46`通過plan guard，只有
  policy原地update，apply結果為`0 added, 1 changed, 0 destroyed`。AWS回讀為`ENABLED`、每日
  `09:00 UTC`、保留7份並精確選取兩個current volumes。2026-09-10補查確認首個scheduled cycle已於
  2026-09-09 09:41 UTC完成：Fetcher `snap-01ebe8336b7ced28c`與FinDB
  `snap-0396321f0243a9ed1`均`completed`、encrypted，policy／schedule／managed／BackupPurpose tags正確，
  每volume各1/7份；manual snapshots未計入。`CopyTags`亦帶入來源volume舊`Retention`標籤，但policy
  `RetainRule.Count=7`才是保存權威。2026-09-14回讀確認第二個週期已於2026-09-10完成：Fetcher
  `snap-0576caba14540b2ac`與FinDB `snap-0df1a639ad70bd5fc`均`completed`、encrypted且exact tags正確；
  至2026-09-13已連續完成五個雙volume週期，每volume各5/7份，recurring chain驗收完成。即時snapshots
  `snap-0fd82febf04befa97`及`snap-03efabf42ff2b398c`仍為completed、encrypted且保留至2026-10-03。
- 固定idempotency以FinLab raw `01a08027-37fb-737d-affd-0072b52eb2a8`驗收：相同內容重送
  回`202`、重用run `01a08027-37fb-7f52-a9ed-0921ace147a9`，attempt
  `01a08058-3777-74cb-8c14-f3e6b3a48260`為`duplicate`；同key修改內容回
  `409/IDEMPOTENCY_PAYLOAD_MISMATCH`，attempt `01a08058-3bfa-765a-aeef-caf294a77701`
  為`rejected`。FinLab raw count前後均為22。
- Active-backlog故障演練使用同一retained raw建立contract-only rerun
  `01a08060-1268-7919-8ba6-81310564a372`。三個producer先暫停，worker停止且暫時設為
  `restart=no`；主queue確認`ready=1`後，短暫撤銷RDS SG對FinDB EC2 SG的TCP/5432規則，使新啟動
  worker無法建立DB連線。SSM command `39431d4d-df7a-4fe1-900e-7eb1078f28b1`在delivery為
  `unacked=1`時SIGKILL worker，message隨即回到`ready=1/unacked=0`。規則以相同source、port及
  description恢復為`sgr-0da1da94a95406703`；worker恢復`unless-stopped`後queue歸零、ping healthy。
  Rerun最終2/2 completed、job completed、outbox published、publish attempts 1、job attempt 1；SSM
  command `95f01d30-7e5f-446d-986d-f66434f22a97`確認2317與2330兩筆canonical均唯一指向該rerun。
  Producers恢復running且restart count 0；42個alarms皆為`OK`，public health及Dashboard為HTTP 200。
- 2026-09-09集中重跑P0故障與時間邊界自動驗收：Fetcher 164項、Backend 65項，共229項全數通過，
  覆蓋bounded retry、expired lease reclaim、graceful stop、holiday、DST及late delivery resolution。
  結合前述live worker-kill／RabbitMQ redelivery證據，該P0項目已關閉；多交易日完整live evidence
  package仍獨立保留在資料面backlog。
- 同日的live policy calibration以SSM commands `d6225912-98bd-4d98-87e6-5eecdc71bcde`及
  `cf7b2818-4ebd-4022-a3aa-6e7f550e1219`確認四個feed均`ready`／`fresh`且無open missing alert；
  Twelve Data每symbol最低1筆、FinLab override最低2筆符合bounded universe，兩個minute feeds則依
  sequenced-snapshot語意維持record-count disabled。
- 2026-09-09 `pre` evidence package由FinDB command
  `5b2a4bf4-18cf-4961-8df2-ad7a2a82007c`與Fetcher commands
  `8cf5c17e-fb02-44ac-94a4-6c0bf27c5267`、`a1abe722-0c99-4284-aaa2-5f8567f4db5e`、
  `e6ba00ea-b4f2-4e4b-a0ab-dee399b5734d`建立。四feed均有兩個有效交易日；Twelve Data config／
  universe SHA分別為`d2bbb6ce5ae2c3f2d1a6d3d8a0d8d4f73d5f462d0830bf14d564cb7d4b93bfa5`／
  `60558e7a73a3d0622463ab2ef686b7ac95ec11c4fda01bc170e0a48bea12f6a9`，3 estimated credits、cap 5；
  FinLab兩symbols、cap 5且provider不採credit meter；Shioaji固定四sequences並受50 requests／60秒治理。
  DB audit確認FinLab／Twelve Data所選runs的`ingestion_attempt.http_status=202`，不再依賴已輪替的Nginx
  logs。Minute Serve明確為`not_applicable/market_minute_read_model_not_exposed`，以Admin raw、Admin
  freshness與Dashboard作營運讀邊界。去敏manifest SHA-256
  `7c234156bdf18964c7f1dc5d164a208a00c42db7458ee35fb30fffc0ec0e8e68`已用KMS存入versioned S3 key
  `evidence/staging/active-feeds/2026-09-09/pre-7c234156bdf18964.json`，version
  `Pw6cXtCwp7juQkCuI4AGvFxhulk4R03D`。下一個新交易日後仍須產生鏈結此SHA的自然`post`；不回填。
- 同日以retained real Twelve Data request做受控異常校準。Fixture/export與submit commands分別為
  `b266ec88-6e6a-4708-a5ba-531590dcf021`及`7ccc01e6-4123-491d-a2ff-49c30a24b455`。缺少必要`close`
  得到`422/INGRESS_SCHEMA_INVALID`，attempt `01a0856d-74f8-7bb4-8263-24177f7641b6`；空snapshot依warn
  policy得到`202`，attempt `01a0856d-752e-72a4-b009-be7c384d2fc4`、run
  `01a0856d-7537-7e9e-8f5f-07177ec67898`。OpenTofu apply新增3個alarms、原地更新4個監控資源、刪除0；
  collector command `35c65ea7-0dc2-47cc-aa26-416cdfe624c0`觀察rejected／empty metrics均`0 -> 1`，
  兩個alarm均`OK -> ALARM`，DQ維持`0/OK`；恢復publisher command
  `15019842-0adc-4ea4-bd15-7cadf57f04e6`在17:20發布0，兩alarm於17:24自然回到`OK`。因此欄位消失／
  空snapshot告警校準完成；policy暫維持
  `warn`，待實際自然週期觀察再另案升級，不以本次單一受控樣本直接改成reject。

## ECR foundation 與啟用契約（已完成；持續驗收）

Staging ECR foundation已建立並完成live驗收：五個private repositories、main-only publisher roles、
instance pull權限與read-only infra plan refresh權限均有AWS及workflow evidence。後續任何ECR resource、
IAM role、GitHub variable、OpenTofu apply或deployment變更仍須各自授權與驗收。

`STAGING_ECR_CUTOVER_ENABLED` 是唯一的 repository variable activation gate。啟用順序如下：

1. gate 維持未設定或 `false`；在受保護 `main` 上，另行授權執行 fresh zero-delete plan 與 foundation
   apply，建立 ECR/IAM resources。
2. 先完成 foundation-level acceptance：確認五個 repositories 與 exact settings、publisher/deploy/
   instance role boundaries，以及安全的 authentication／authorization checks（包括 instance role
   只能取得 authorization token 並 pull 自身允許的 images）。此階段不要求 gated application rollout。
3. foundation acceptance 完成後，operator 才可把 repository variable 設為**精確字串** `true`，並手動
   trigger staging workflows。
4. triggered workflows publish immutable commit-SHA images並執行host rollout；其後完成cutover的
   live acceptance與observation。若acceptance失敗，立即將gate設回`false`，這只會freeze並阻擋
   新的staging rollout，**不是**GHCR fallback。此流程曾以當時accepted SHA
   `570e3c1e935210c7f084c9d8b5a7f711cdf9300b`完成live deployment驗收。

任何其他值一律 fail closed，staging 不可 build/push/deploy 到 ECR。

啟用後 staging 僅使用 `439622209937.dkr.ecr.ap-southeast-1.amazonaws.com` 和 AWS Secrets Manager
runtime mode；EC2 不接收 `GITHUB_TOKEN`、PAT、`GHCR_USERNAME` 或 `GHCR_TOKEN`。Production 採
獨立 account／ECR 與 instance-role runtime secret path；promotion 僅讀取 accepted staging digest，
不會 assume staging publisher role 或拉取 mutable image tag。
Phase 3 wave 1的staging exact-digest bundle與accepted replay已通過merged protected-main live
acceptance：SHA tag僅是ECR build/reuse的selected commit索引，staging deploy identity是validated
bundle中的`repository@sha256`。兩個unit的normal accepted deployment及replay均已成功；Phase 3目前
僅保留文件整併與closeout，不把這些部署證據延伸解讀為Phase 2A原生provider cycle已通過。

受控 rollback 不會恢復 transitional GHCR access，也不再有current-checkout generation或相容性橋接。
gate維持精確`true`；operator只能在protected `main`手動dispatch對應workflow並選擇
`deployment_target=staging`。正常部署的`image_tag`與`accepted_bundle_key`都必須留白；任何歷史
`image_tag`必須同時提供該unit、該commit的accepted bundle key。workflow從strict acceptance record取得
歷史accepted bytes、bundle／validator SHA、migration與完整`repository@sha256` image map，再由host驗證及
materialize；不得重建manifest或混用current checkout。健康成功後才activate該immutable release；replay不重寫
accepted bundle或record。candidate、cross-unit、mutable／unsafe key、缺image tag、production及record不匹配
一律fail closed。

## 決策背景

本計畫依下列已確認前提收斂，不以完整企業級治理作為 staging 完成條件：

- staging 是 production 的前置驗證站，預期三個月內開始建立 production；
- staging 由小團隊共同維運，不要求正式 24/7 on-call；
- protected `main` 合併後，shared change policy命中的unit會在cutover gate啟用時自動
  rollout至staging；manual dispatch只用於exact accepted bundle replay。Production採
  unit-specific Git tag加手動artifact promotion，不另設Environment人工核准；
- 本輪採核心安全與可復原基線，不一次導入 HA、全面 IaC import 或完整企業稽核；
- IaC 只先管理新控制面資源，既有 EC2、RDS 與網路先盤點及引用，不因全面 import 阻塞 cutover。

因此，本計畫不是讓現有 staging 繼續運作的前置條件，而是讓 staging 能安全承擔 production
promotion、交接、重播與復原驗證的完成條件。

## 目標與範圍

把原先可運作但依賴 SSH、GitHub Environment runtime secrets 與 tag-only image identity 的
staging 部署，收斂為下列架構。目前runtime-secret transport與exact-digest accepted release已完成；
FinDB與Fetcher local workflow已分別完成Phase 4／5 SSM transport切換；兩個Environment的deploy SSH
secrets均已刪除。不同digest rollback與schema拒絕演練屬Phase 6復原基線，不影響Phase 4／5完成判定：

```text
protected main
  -> reusable CI for the same commit
  -> unit-specific OIDC publisher role
  -> build images once, push to staging ECR and record exact digests
  -> GitHub Environment: staging-findb / staging-fetcher
  -> GitHub OIDC
  -> service-specific AWS deploy role
  -> SSM candidate command to one tagged EC2 target (bounded checks, then fail-stop)
  -> immutable accepted record commit point
  -> separate bounded SSM activation command
  -> EC2 instance role reads service-specific runtime secrets
  -> FinDB EC2 -> private RDS + Canonical R2 credential boundary
  -> Fetcher EC2 -> FinDB HTTPS + Raw R2
  -> accepted release manifest for later production promotion
```

本計畫包含：

- staging 的 GitHub protection、AWS IAM、SSM、runtime secrets 與 immutable release；
- FinDB migration、Fetcher single-writer、健康檢查與 bounded acceptance；
- 必要的 EC2、RDS、EBS、RabbitMQ 與 scheduler 監控及復原演練；
- 日常 SSH 退場與可稽核的 SSM recovery path。

本計畫不包含：

- 建立production雲端資源、Environment或執行live promotion；production promotion workflow已由
  後續工作實作並完成dry-run契約驗證，但不代表production foundation已存在；
- staging HA、Auto Scaling、多 EC2、blue/green 或 Multi-AZ 強制要求；
- 全面 import 既有 AWS 資源到 IaC；
- 擴大 active feed universe、production-scale backfill 或新增資料來源；
- Canonical R2 publish/read runtime；
- 正式 24/7 on-call、完整企業稽核或 staging 每次部署的人工核准。

## 各階段必要性

| 階段 | 判定 | 原因與調整 |
| --- | --- | --- |
| Phase 0：盤點與保護 | 必要 | 確認實際 target、資料與復原 owner；不要求全面 IaC import，也不對 staging 加Environment人工核准 |
| Phase 1：OIDC、SSM、instance role | 必要 | 建立service-specific AWS trust boundary與可稽核的SSM recovery，作為後續移除長效SSH deployment identity的前置條件 |
| Phase 2A：Runtime-secret transport與ECR registry cutover | 必要 | 避免runtime secrets經GitHub runner與遠端shell傳遞；改由instance role讀取Secrets Manager、取得ECR短效token，並完成DB-backed credential rotation與GHCR credential退役 |
| Phase 2B：External credential hygiene | 必要但獨立收尾 | Raw／Canonical R2與三個provider scope項目均依使用者核准變更acceptance criterion而完成，既有值維持；R2未建立新key、未輪替、未替換、未撤銷舊key，故不構成rotation或old-value invalidation evidence，也不代表曾執行Cloudflare操作。RabbitMQ已完成rotation與舊值拒絕驗證。GitHub Environment的23枚舊runtime copies已在完整原生provider cycle、last-used與health前置檢查後依明確授權移除，刪後驗收通過 |
| Phase 3：Digest、manifest、CI gate | 必要 | staging 必須能重播並把相同 artifact promotion 到 production，不能依賴 `latest` 或可漂移 tag |
| Phase 4：FinDB SSM cutover | 必要 | 保留既有 migration 與 queue safety gate，只替換部署傳輸與secret來源 |
| Phase 5：Fetcher SSM cutover | 必要 | 保留 SQLite、Raw bucket binding 與 single-writer gate，只替換部署傳輸與secret來源 |
| Phase 6：必要維運與復原基線 | 部分必要 | 完成關鍵告警、restore、broker rebuild、different-digest rollback與schema-incompatibility rejection；HA與完整on-call另案處理 |

## Repo 可證實的現況

| 項目 | 現況 | 核心基線仍缺少 |
| --- | --- | --- |
| Release units | FinDB 與 Fetcher 已有獨立 CI/CD、Environment 與 concurrency group | 將 AWS target、role、secret path 與 acceptance 寫入可稽核清冊 |
| CI gate | CD以`workflow_call`執行同一revision的CI；FinDB migration tests已拆為獨立job，並以session-scoped PostgreSQL templates重用historical revisions | 定義支援revision、以實際staging predecessor／restore clone驗證upgrade、image runtime security與deploy bundle deterministic check |
| Image identity | Staging五個target-specific ECR images均已由unit-specific accepted bundle固定為完整`repository@sha256`，normal deployment與accepted replay均以digest部署；SHA tag只作build/reuse索引，production 使用 target-specific ECR promotion | Phase 3文件closeout後持續保護accepted bundle／record與manifest-aware retention；production promotion workflow已實作並完成dry-run契約驗證，AWS foundation與live acceptance仍待完成 |
| EC2 transport | FinDB與Fetcher staging workflow均已改為OIDC＋SSM bounded candidate／accepted-record／activation；兩個unit各完成兩次normal deployment、accepted replay與Session Manager recovery。兩個Environment的`*_EC2_*` deploy secrets均已刪除，三個staging SG也已無TCP/22 ingress，EC2 key-pair resources與host `authorized_keys`中的對應key亦已退役；production 使用同樣的 OIDC＋SSM bounded transport | FinDB different-digest previous-release rollback與不同Alembic revision schema拒絕已於Phase 6完成；production transport另案 |
| Runtime secrets | Staging已由instance role讀取Secrets Manager，host loader只在`/run` tmpfs建立allowlisted bundle並於使用後清理；四feed accepted-SHA原生週期已通過。GitHub Environment的23枚舊application runtime copies已依授權移除。FinDB／Fetcher deploy SSH secrets、TCP/22 ingress及host recovery key material均已退場 | 持續保護Secrets Manager consumer boundary與不落地契約；無本階段blocker |
| RDS rollout | 有predeploy DB check、writer pause、單一Alembic upgrade與revision check；已確認private、encryption、deletion protection、10-day automated backup與PITR，並於2026-09-01完成一次PITR restore、revision／row count／connectivity驗證 | migration credential分權與RDS tags仍未納入本Phase；RDS backup-lag custom alarm已上線 |
| Queue | RabbitMQ在FinDB encrypted root EBS path保存；PostgreSQL是durable truth；2026-09-03已從空broker目錄重建policy、queue、DLQ並驗證DB計數不變 | 舊broker目錄暫留供稽核；active-feed負載下的重複delivery/worker-kill演練仍屬資料面backlog |
| Fetcher state | 三個provider runtime隔離；container已採non-root、read-only、drop capabilities與no-new-privileges，SQLite與Raw bucket binding只接受current state並fail closed；部署helper固定安全旗標並自動檢查image、user與restart policy，Phase 5 SSM rollout、bounded FinLab terminal delivery、三個SQLite online backup/restore、accepted-SHA後四feed原生週期及DLM off-host排程證據均已完成 | 實際runtime已有人工驗證，但尚未自動反查運行中container的read-only root、capabilities、no-new-privileges、privileged與mount邊界，因此完整runtime security防退化驗證仍待完成；此項留作已知強化缺口，不阻塞本次staging closeout |
| Legacy removal | 舊public routes、舊skill、Fetcher scheduler／SQLite compatibility及DB dataset projection已移除；predeploy仍拒絕非canonical state | 保存staging實際revision及legacy predeploy gates為零的外部證據；不得在新deploy helper恢復compatibility |
| R2 | Raw與Canonical bucket／credential契約已拆分；2026-08-21已人工確認Raw lifecycle 30天與bucket lock 7天 | Canonical runtime不得宣稱已通過資料面驗收 |
| Protection | workflow固定第三方Action SHA；GitHub組織ruleset `Main protection` 要求PR、禁止force-push與限制deletion；repo ruleset `FinDB required CI` 對default branch／`main`強制 `Required CI`；兩個Environment各只允許`main`且無reviewer／wait-timer；兩台EC2已有unit-specific required tags；protected `main`已實證兩個CD不受人工核准阻塞且target count各為1 | 持續維持always-created `Required CI`、Environment branch policy與unit-specific target tags一致；無Phase 1 blocker |
| Observability | 兩個KMS-encrypted、30-day SSM log groups、六個native與37個custom alarms已live apply；35組bounded metrics、兩個association、periodic/sparse synthetic transitions及SNS delivery均有證據，43 alarms後驗OK；DLM policy disabled／error／missing已有fail-closed監控 | Queue-depth trend、market freshness、ingestion/DQ、TLS certificate與custom synthetic inbox獨立收件確認不在本輪完成證據 |
| IaC | Repo已有OpenTofu bootstrap與staging control-plane stacks；encrypted、versioned S3 remote state使用native lockfile與指定KMS key，Tyler (`tylercore`)為state owner；IaC plan gate、獨立OIDC plan role、完整Phase 6 alarms、daily current-root DLM policy、policy health alarm與首兩個不同排程週期均已live驗收 | 既有EC2／RDS／VPC等data-plane資源只引用與加required tags，不在本計畫全面import |

`.github/workflows/required-ci.yml`、兩份unit CI、兩份unit CD與
`docker-compose.prod.yml` 是現行行為的 source of truth。本文不把尚未查證的 AWS console
設定當作事實。

## Workflow / release unit matrix

| Unit | CI / CD boundary | Images | Environment | Concurrency | Deploy order |
| --- | --- | --- | --- | --- | --- |
| FinDB | Backend、Dashboard、contracts、Compose、nginx與FinDB workflows | Backend、Dashboard | `staging-findb` | `staging-findb`，`cancel-in-progress: false` | 先部署；contract變更維持backend-first |
| Fetcher | Fetcher、contracts、contract generation依賴與Fetcher workflows | Generic、FinLab、Shioaji | `staging-fetcher` | `staging-fetcher`，`cancel-in-progress: false` | FinDB acceptance後部署 |

所有targeting `main`的PR都建立aggregate `Required CI`；classifier依上述unit邊界呼叫可重用的
`FinDB CI`／`Fetcher CI`，未命中unit時明確接受skipped，classifier失敗、routed job非success或
unrouted job非skipped時fail closed。兩個unit CI不再直接接收`pull_request`，但保留
`workflow_call`與`workflow_dispatch`。

兩個 CD 的 protected `main` push由shared change policy判定unit邊界；命中的unit在
`STAGING_ECR_CUTOVER_ENABLED=true`時，會執行同revision CI、ECR build／publish與host rollout。
Contract-only變更只執行兩個CI，不自動部署；manual dispatch只接受exact accepted bundle
replay。CD必須直接呼叫同revision CI。
Staging Environment只允許protected branch且不設人工核准；
production建立後使用`findb-vMAJOR.MINOR.PATCH`或`fetcher-vMAJOR.MINOR.PATCH`宣告對應unit的
release，再由operator手動dispatch artifact promotion；同樣不配置Environment人工核准。

FinDB仍是一個deployment unit，backend與Dashboard各自記錄digest。Fetcher三個image同屬一個
deployment unit，但release manifest必須列出三個digest，不能只用共同SHA tag代替。

## Staging 資源清冊

清冊只記錄target-specific、非敏感的實際識別資料。Secret只記錄ARN或name，不得貼值、hash或
可比較片段。SSH recovery path依Phase 1／6 exit condition管理，不因SSM首次上線立即移除。

| 類別 | `staging-findb` | `staging-fetcher` | 驗收要求 |
| --- | --- | --- | --- |
| AWS account / Region | `439622209937` / `ap-southeast-1`（SSH + IMDSv2、AWS Console 已驗證） | `439622209937` / `ap-southeast-1`（SSH + IMDSv2、AWS Console 已驗證） | Workflow明確檢查STS account與region |
| VPC / subnet | `vpc-0865afcf10442bf4d` / `subnet-0aba2a175b5912c24` | `vpc-0865afcf10442bf4d` / `subnet-0cde9e8dc33bec41e` | RDS private；public EC2若保留須記錄例外與production前檢查點 |
| EC2 target / tags | `i-0942016913367a8b2`（`findb-staging`、`m7i.large`、`ap-southeast-1c`；ARN `arn:aws:ec2:ap-southeast-1:439622209937:instance/i-0942016913367a8b2`）；required tags為`Project=findb`、`Environment=staging`、`DeploymentUnit=findb`、`Owner=tylercore`、`BackupOwner=tylercore` | `i-05f518ef183bc31a9`（`findb-fetcher-staging`、`t3.small`、`ap-southeast-1a`；ARN `arn:aws:ec2:ap-southeast-1:439622209937:instance/i-05f518ef183bc31a9`）；required tags與FinDB相同但`DeploymentUnit=fetcher` | IAM simulator已驗證各deploy role只允許本單元target，跨單元為`implicitDeny`；protected `main` live workflows已分別驗證exact-tag selector只命中1台target |
| EC2 security group / root EBS | `sg-0194615fe18784889`（`ec2-rds-1`）、`sg-070694f093a25cb31`（`launch-wizard-1`）；public inbound HTTP 80、HTTPS 443，另有8080 `/32`，無TCP/22；`vol-071822e2fe38c3991`（50 GiB）in-use、encrypted、delete-on-termination | `sg-0c98f59c6961a00d2`無inbound rules；`vol-07a726b6215c34c20`（30 GiB）in-use、encrypted、delete-on-termination | Encrypted replacement與SSH ingress退場已完成；`fb-db-key`／`findb-fetcher-key` resources已刪除，四個host `authorized_keys`的對應key均已移除。Daily DLM policy `policy-0d0a29c9e19f6323e`已啟用並精確標記current volumes；首兩個不同雙volume排程recovery points已驗證 |
| Instance role | `findb-staging-instance` profile／role已關聯；只允許FinDB future secret／parameter path、`findb/` deploy bundle、SSM core與FinDB log group | `fetcher-staging-instance` profile／role已關聯；只允許Fetcher future secret／parameter path、`fetcher/` deploy bundle、SSM core與Fetcher log group | Live IMDSv2 preflight已確認兩台profile exact match；不共用runtime path或bundle prefix |
| GitHub deploy role | `arn:aws:iam::439622209937:role/findb-staging-deploy` | `arn:aws:iam::439622209937:role/fetcher-staging-deploy` | OIDC trust的`aud=sts.amazonaws.com`且`sub`精確綁定對應Environment；IAM simulator已驗證cross-unit target/log拒絕，protected `main` workflows已實證各自OIDC assume成功且deploy-role secret read為`AccessDenied` |
| SSM managed node | Online；agent `3.3.4793.0`；Session document `SSM-SessionManagerRunShell-findb-staging` | Online；agent `3.3.4793.0`；Session document `SSM-SessionManagerRunShell-fetcher-staging` | 兩台bounded command與unit-specific Session Manager recovery均成功；command output送各自CloudWatch log group |
| RDS / security group | `fin-db`；ARN `arn:aws:rds:ap-southeast-1:439622209937:db:fin-db`、resource ID `db-ENXUOKJALHEX5BZJ3NVXKN4I7I`；PostgreSQL 16.13、available、`db.t3.micro`、`ap-southeast-1c`、VPC `vpc-0865afcf10442bf4d`、Multi-AZ No、private；`rds-ec2-1` `sg-0faf978bb6c67ca20` 僅允許 5432 from FinDB SG `sg-0194615fe18784889`；connected compute 僅 FinDB；encryption enabled with `aws/rds` KMS、deletion protection enabled、gp3 20 GiB（autoscaling max 1000 GiB）；RDS tags count 0 | RDS不適用；RDS SG沒有Fetcher inbound rule，connected compute也只有FinDB instance | `PubliclyAccessible=false`；只允許FinDB EC2 SG；backup/PITR啟用 |
| Data classification | Staging raw／canonical market data與workflow registry；RDS是durable truth，RabbitMQ可由PostgreSQL重建，generated cache不是source of truth | Provider scheduler／checkpoint SQLite與Raw R2 market payload；不持有RDS、RabbitMQ、Admin或Canonical R2資料 | 資料與credential依unit、target及durability分級，不因同VPC而共用權限 |
| Secret prefix | `findb/staging/findb/`；staging runtime由FinDB instance role依consumer allowlist讀取，GitHub舊copies已移除 | `findb/staging/fetcher/`；staging runtime由Fetcher instance role依consumer allowlist讀取，GitHub舊copies已移除 | Tyler 是 resource owner；兩個unit不共用DB、R2、provider、Source/Admin或deploy credential。Phase 2A時間gate與Phase 2B runtime-copy retirement均已通過 |
| Owner | Tyler（GitHub `tylercore`；Phase 1 apply evidence `AdministratorAccess/Tyler`）同時擔任 resource、backup、SSH recovery、alert 與 OpenTofu/IaC remote-state owner | Tyler（GitHub `tylercore`；Phase 1 apply evidence `AdministratorAccess/Tyler`）同時擔任 resource、backup、SSH recovery、alert 與 OpenTofu/IaC remote-state owner | 具名email channel與synthetic收件驗證已完成；正式24/7 on-call仍不屬staging exit gate |
| Deploy artifact | Private、versioned、KMS-encrypted bucket `findb-staging-deploy-bundle-439622209937`的`findb/` prefix | 同bucket的`fetcher/` prefix | exact SHA key、checksum、versioning與exact CMK header policy；不使用application R2 bucket |
| CloudWatch | `/findb/staging/findb/ssm`，KMS encryption、retention 30 days；FinDB EC2/RDS native alarms及FinDB custom alarms使用encrypted SNS topic | `/findb/staging/fetcher/ssm`，KMS encryption、retention 30 days；Fetcher EC2 native alarm及Fetcher custom alarms使用相同topic | 43個alarms後驗皆為OK；35組bounded custom metrics；兩個association成功；DLM policy health、native inbox synthetic與custom periodic/sparse transitions均有證據 |
| Backup | RDS automated backups enabled 10 days、PITR、encryption與deletion protection；2026-09-01 restore到`05:40:43Z`資料點，約9分18秒available，revision與instrument `8/8`比對成功。FinDB root由daily DLM policy精確選取，立即encrypted snapshot已完成 | Fetcher root由相同policy精確選取，立即encrypted snapshot及SQLite online backup/restore已完成 | RDS restore、SQLite recovery及RabbitMQ rebuild gate已完成。DLM policy為enabled且保留7份；至2026-09-13已有五個不同雙volume排程週期，每volume各5/7份，recurring execution evidence已關閉 |
| Public endpoint | `/dashboard/`、`/dashboard/lookup` public HTTPS read-only acceptance passed；nginx直接使用host提供的certificate/key，現有container health check以`--no-check-certificate`驗證服務可達，不會偵測憑證即將到期 | 無public inbound endpoint | Certificate expiry monitoring未完成：需定期讀取公開端點憑證的`notAfter`、發布剩餘天數並對低門檻與missing data告警；此項留作已知監控缺口，不阻塞本次staging closeout |

## Phase 0 evidence（2026-08-25）

本節是本次 Phase 0 完成紀錄。證據來源為 signed-in GitHub settings／Deployments
UI 與 AWS Console 的 read-only 檢視、兩台 EC2 的 SSH + IMDSv2 與 host runtime 檢查、staging
public HTTPS read-only acceptance，以及 repo/local source 檢查；未輸出 secret 值、主機名稱或 IP。
FinDB Alembic inspection使用`uv run`時在現有`ingest` container writable layer同步了25個development
packages；未重啟service或修改database，該暫時layer會由下次normal deployment替換。後續唯讀盤點
不得再用會同步dependency的命令，應使用image內已安裝的entrypoint或immutable probe。

### 本次 Phase 0 work record

| Action | Evidence | Result |
| --- | --- | --- |
| 指派兩個 staging unit 的 owner | GitHub `tylercore` 與 AWS `PowerUserAccess/Tyler` 是本次唯一驗證的 operator identity；兩台 EC2、RDS／資料復原、SSH recovery、alert 與 IaC state ownership 已一併記錄 | Tyler 現為兩個 unit 的 resource、backup、SSH recovery、alert 與 OpenTofu/IaC remote-state owner；這是使用者授權的正式 assignment，不僅是 provenance |
| 設定 Phase 0 interim alert path | GitHub Actions／Deployments 與 AWS Console 可由 `tylercore` 手動查看；CloudWatch inventory 仍為 alarms 0、log groups 0 | Phase 6 前只採 manual monitoring；未宣稱 GitHub notification delivery 或 CloudWatch alarms 已存在，Phase 6 必須建立並測試 automated notification channel |
| 確認 IaC 邊界與 state owner | Repo/local 檢查未找到 Terraform、OpenTofu、CloudFormation、Pulumi、CDK 或 external owner evidence | 新 control-plane resources 選用 OpenTofu，由 Tyler 持有 state；encrypted remote backend／locking 尚未建立，留待 Phase 1/IaC implementation |
| 記錄 SSH／EBS exceptions 與補償控制 | 兩個 EC2 SG 仍允許 public TCP/22；兩個 current root volumes 均未加密、delete-on-termination，且無 current-volume snapshot／DLM policy | Tyler 接受 residual risk。現有控制限於 environment-scoped SSH keys、protected-main audited deployments、recovery reachability、不得擴大 SSH 使用與保留 recovery path；storage-destructive maintenance 必須先做 current-volume manual snapshot，Fetcher 另須 stopped-writer 或 SQLite online backup，否則 no-go |
| 清理 Fetcher local ignored env | `infra/env/staging/fetcher/.env.remote` precondition `CLOUDFLARE_R2_ACCOUNT_ID` count 2、values distinct、mode `0600`；postcondition count 1、remaining nonempty、mode `0600`；equality-only evidence matched GitHub Environment | stale first declaration 已安全移除且未輸出值；其它 intentionally duplicated equal declarations 未變更；ignored file 不應出現在 Git diff |
| 實作並啟用 required CI | 新增always-created `.github/workflows/required-ci.yml`，以fail-closed path classifier呼叫兩份可重用unit CI，最後產生固定名稱 `Required CI`；unit CI移除直接`pull_request` path filters但保留`workflow_call`／`workflow_dispatch`；獨立validator為PASS | 已建立Active repo ruleset `FinDB required CI`（ID `21393782`），只套用default branch／`main`，required context為`Required CI`／Any source，無bypass、不要求branch up-to-date；組織ruleset未變更。Aggregate job名稱與ruleset context是同一契約，後續變更必須同步驗證 |

### GitHub control plane

| 檢查 | 已驗證結果 | 未完成／限制 |
| --- | --- | --- |
| Repository protection | 組織 ruleset `Main protection` 套用 `ai-stock`、`ai-stock-frontend` 與 `findb`：要求 PR、禁止 force-push、限制 deletion；repo ruleset `FinDB required CI`（ID `21393782`）Active且只套用default branch／`main`，強制`Required CI`（Any source），無bypass、不要求branch up-to-date；classic branch protection absent | aggregate workflow已通過YAML、routing、truth-table與獨立驗證；workflow名稱、always-created行為與ruleset context若發生drift會fail closed，任何後續修改都必須在同一變更驗證兩者一致 |
| Deployment Environments | `staging-findb`、`staging-fetcher` 各只允許 selected branch `main`；各有恰一條 deployment branch rule，無 reviewer／wait-timer protection | required target tags／SSM selector 與 environment owner 尚未完成確認 |
| Latest FinDB deployment | FinDB CD #59 成功；active deployment application SHA `394bbd9784367ed190584d3439efa23424d9b1fc` | 這是 SHA tag／deployment evidence，不是 accepted digest manifest |
| Latest Fetcher deployment | Fetcher CD #39；running images application SHA `80333806212c70281e47d9dd837c3642dc4c48f1` | 這是 SHA tag／deployment evidence，不是 accepted digest manifest |

### AWS identity and host evidence

| Unit | EC2／network／storage | Recovery／control-plane 狀態 |
| --- | --- | --- |
| FinDB | `i-0942016913367a8b2`、`findb-staging`、`m7i.large`、AZ `ap-southeast-1c`；ARN `arn:aws:ec2:ap-southeast-1:439622209937:instance/i-0942016913367a8b2`；VPC `vpc-0865afcf10442bf4d`、subnet `subnet-0aba2a175b5912c24`；SG `sg-0194615fe18784889`、`sg-070694f093a25cb31`；root EBS `vol-0784675e2ada6642e`、50 GiB | running、3/3 status checks；IMDSv2 required；僅 `Name` tag；required target tags 尚未建立；`Managed=false`、無 IAM role／instance profile；`amazon-ssm-agent` inactive；SSH recovery reachable with existing environment-scoped SSH keys |
| Fetcher | `i-05f518ef183bc31a9`、`findb-fetcher-staging`、`t3.small`、AZ `ap-southeast-1a`；ARN `arn:aws:ec2:ap-southeast-1:439622209937:instance/i-05f518ef183bc31a9`；VPC `vpc-0865afcf10442bf4d`、subnet `subnet-0cde9e8dc33bec41e`；SG `sg-0c98f59c6961a00d2`；root EBS `vol-0201fa5249fe44c39`、30 GiB | running、3/3 status checks；IMDSv2 required；僅 `Name` tag；required target tags 尚未建立；`Managed=false`、無 IAM role／instance profile；`amazon-ssm-agent` inactive；SSH recovery reachable with existing environment-scoped SSH keys |

Signed-in AWS Console session was `financial_db_dev` in account `439622209937`, with
`PowerUserAccess/Tyler`, region `ap-southeast-1`; together with GitHub `tylercore`, this is the
verified operator identity and Tyler is now the named resource, backup, SSH recovery, alert and
OpenTofu/IaC remote-state owner for both units. Console verified exactly two running EC2 instances in the
account／region, both with 3/3 status checks, but both are public EC2 with unrestricted SSH and no IAM
role／instance profile. Required target tags are absent, so a future SSM tag selector cannot yet be
accepted. RabbitMQ bind path `/var/lib/findb/rabbitmq` is on FinDB root EBS, not a dedicated EBS
volume; all Fetcher SQLite／marker state is on Fetcher root EBS. Current volumes沒有snapshot或DLM
automated policy；these are retained EBS exceptions with Tyler as owner. Existing SSH recovery is reachable, but public SSH from `0.0.0.0/0`
is a recorded Phase 0 exception and is not an acceptable mature baseline; removal remains a later
Phase 6 action after SSM recovery is established.

### FinDB runtime evidence

| 項目 | 已驗證結果 |
| --- | --- |
| Running application | SHA tag `394bbd9784367ed190584d3439efa23424d9b1fc`；`serve`、`ingest`、`dispatcher`、`worker`、`raw-cleanup`、`dashboard`、`nginx`、`rabbitmq` 均 running，已定義 health 者為 healthy |
| Host-local images | Backend `sha256:bd73bdbf5208131041c902a99dd2efefd00aeff33fff5f43e6e6c0e1f27fd6fa`；Dashboard `sha256:52d18fefeeff0efb880804ddee0cc30815e38f3531c9eac84d2bb5df9d853ba3`。兩者皆明確是 host-local image ID，不是 registry digest，也不是 accepted manifest artifact |
| Database / predeploy gates | Alembic `d6e7f8a9b0c1`（head）；`noncanonical_scheduler_control_slots=0`、`noncanonical_dataset_delivery_schedule_slots=0`、`legacy_finlab_scheduler_keys=0`、`dataset_keys_projection_mismatches=0`；dataset projection status `not_applicable`；SSH check passed |

### Fetcher runtime evidence

| 項目 | 已驗證結果 |
| --- | --- |
| Containers / application | 僅三個 stable containers，無 candidate／previous containers；全部以 user `10001:10001` running，root filesystem read-only；SHA tag `80333806212c70281e47d9dd837c3642dc4c48f1` |
| Host-local images | Generic `sha256:109c8963d5792f0848261bbf1726053cbf06bd5069a5a472382cd685aa51467b`；FinLab `sha256:103f570f50c4818b7aa0aabaa384e00c652c6a3f8b27c85668fc7c0df0b3dd4a`；Shioaji `sha256:c4b73a2323e2971bec5d979133b1499012429fdb066ef1d40ef7f30f54ae0b1c`。皆是 host-local image ID，不是 registry digest，也不是 accepted manifest artifact |
| State checks | Generic／FinLab SQLite `user_version=3`，table set 含 `scheduled_job`、`symbol_checkpoint`；Shioaji 為 current provider table set、`user_version=0`、`quick_check=ok`；三者 `quick_check=ok` |
| Filesystem / storage | State dirs UID/GID `10001:10001`、mode `0700`；raw-bucket marker mode `0600`；全部 state 在 root EBS，非 dedicated volume；current-volume snapshot／DLM policy 不存在，此staging例外已有紀錄 |

### Acceptance and repository evidence

- Configured staging host 的 public HTTPS read-only acceptance 已通過 `/dashboard/` 與
  `/dashboard/lookup`；certificate expiry monitoring 仍待完成，未新增或恢復 legacy routes。
- Repo/local 檢查未發現 Terraform、OpenTofu、CloudFormation、Pulumi、CDK 或 remote-state
  ownership evidence；因此新 control plane 選用 OpenTofu，Tyler 擔任 state owner。encrypted remote
  backend／locking 尚未建立，留在 Phase 1/IaC implementation work。
- ignored `infra/env/staging/fetcher/.env.remote` mode `0600` 的 stale first
  `CLOUDFLARE_R2_ACCOUNT_ID` declaration 已安全移除，未輸出 values。Precondition count 為 2 且
  values distinct；postcondition count 為 1、remaining value nonempty、mode 仍為 `0600`。先前
  equality-only evidence 已確認 remaining declaration 與 GitHub Environment value 相符；其它
  intentionally duplicated equal declarations 未變更。該 ignored file 不應出現在 Git diff。
- 該次GitHub Environment盤點的secret names與契約相符：當時FinDB 16個、Fetcher 13個；Phase 4移除
  三個FinDB deploy SSH secrets後，FinDB目前為13個。本紀錄不列secret values。
- Current operator／deployment actor 為 `tylercore`；Tyler 已被正式指派為兩個 unit 的 resource、
  backup、alert、SSH recovery 與 OpenTofu/IaC remote-state owner。Phase 6 前的 alert channel 是
  `tylercore` 手動監看 GitHub Actions／Deployments 與 AWS Console；GitHub notification delivery
  與 CloudWatch alarms/log groups 均未宣稱已存在，Phase 6 必須建立並測試 automated notification channel。

## 身分、secret 與 release 設計

### GitHub OIDC 與 AWS roles

兩個deploy role的trust policy只接受GitHub OIDC provider，並同時限制：

```text
repository: FPI-TW/findb
aud: sts.amazonaws.com
sub (FinDB):  repo:FPI-TW/findb:environment:staging-findb
sub (Fetcher): repo:FPI-TW/findb:environment:staging-fetcher
```

CI jobs不取得AWS身分；deploy與ECR build/push jobs只在各自職責需要時取得`id-token: write`。
FinDB與Fetcher build/push jobs各自使用獨立、main-only的publisher role；trust限定
`repo:FPI-TW/findb:ref:refs/heads/main`與`aud=sts.amazonaws.com`，且只可publish／inspect自身ECR
repositories。Publisher、deploy與instance roles不得共用：deploy role只可查驗target、上傳release
artifact及對對應tag的唯一EC2執行SSM command，不得讀Secrets Manager value、RDS data、R2
credential、另一unit的target或publish image。

Staging caller必須維持三段身分邊界：Environment-bound `select`只做gate、exact revision與replay
selection；無Environment的`publish`才可取得main-ref publisher OIDC並只操作ECR；Environment-bound
`prepare`改用unit deploy role建立及上傳candidate。不得把publisher assume與bundle upload重新合併到
同一Environment job，否則OIDC subject會從`ref:refs/heads/main`變成`environment:staging-*`且應
fail closed。Accepted replay會skip `publish`，直接由已選定的accepted bundle進入共用deploy流程。
Caller的`staging-<unit>`／`production-<unit>` concurrency與reusable job的
`<target>-<unit>-deploy`必須保持不同名稱；後者仍提供跨caller的target/unit host-mutation serialization，
但不可與持有caller workflow的lease同名，避免reusable job在runner啟動前自我競爭而失敗。

EC2 instance role負責讀取自身runtime secrets與deploy bundle。FinDB與Fetcher的deploy role、
instance role、secret path及KMS policy scope不得重用。

### Staging ECR registry

Staging已在帳號`439622209937`、區域`ap-southeast-1`建立並驗收下列Amazon ECR repositories：

- `findb/staging/backend`；
- `findb/staging/dashboard`；
- `findb/staging/fetcher/twelve-data`；
- `findb/staging/fetcher/finlab`；
- `findb/staging/fetcher/shioaji`。

五個repositories皆採private、immutable tags、AES-256與basic scan-on-push；`force_delete=false`，
IaC以`prevent_destroy`保護。Lifecycle只自動清理超過7天的untagged images，不得使用會刪除
accepted digest的blind tagged-image rule；後續如需控制tagged image成本，必須先有manifest-aware
retention並保護所有仍可rollback／promotion的digests。

FinDB與Fetcher各自使用main-only publisher role push自身repositories。兩台EC2只以unit-specific
instance role取得`ecr:GetAuthorizationToken`所需的12小時authorization token，並只pull自身exact
repository ARNs；token只寫入`/run` tmpfs Docker config，命令結束即logout及清理，不寫入Secrets
Manager、GitHub或persistent host設定。Deploy role不得取得ECR publish或runtime pull權限。

Staging完成cutover後不再dual-publish GHCR；既有 GHCR metadata recycle 保留至既定 recovery window，直到正式
production ECR repositories與promotion workflow另案完成。本計畫不將該相容路徑誤列為最終
production artifact契約。

### Runtime secrets

預設使用Secrets Manager保存需要版本與輪替紀錄的RDS、provider、R2、DB-backed API key與
RabbitMQ credentials；簡單且低頻變更的敏感值可使用SSM SecureString。ECR authorization token
由instance role即時取得，不是runtime secret。非敏感設定留在GitHub Environment variables或
Parameter Store一般參數。

目前active runtime catalog與IaC集合均為17筆。兩筆空過渡資源
`findb/staging/findb/registry/ghcr-pull`與`findb/staging/fetcher/registry/ghcr-pull`已在
2026-08-28以retirement apply排程30天刪除；包含planned-deletion metadata時清冊仍顯示19筆。
兩個已建立但未使用的package-read-only PAT已撤銷，文件與操作紀錄均不得保存token內容。

Retirement PR 的 CI 僅是 preflight，不能作為 apply authority。實際刪除只能在 reviewed retirement
PR 合併到 protected `main` 後進行：operator 必須以乾淨 checkout 讓 `HEAD` 精確等於合併後的
`origin/main` SHA，live 驗證兩個 exact Secret ID 都沒有 versions 或 values，從該 SHA 產生 fresh
saved plan，並以 immutable guard 加上兩個 exact allow addresses 驗證 `delete_count=2` 且沒有其他
delete。取得 action-time user confirmation 後，獨立 apply identity 才可 apply 那份 exact saved
plan；隨後驗證兩筆皆為30天 scheduled deletion、active catalog 為17，並重跑 fresh zero-delete plan。

至少維持下列邊界：

- RDS migration credential不得常駐注入application containers；
- RDS application、Canonical publisher與Canonical reader依用途分離；
- Fetcher shared、各provider與Raw R2 credential只供對應consumer；
- Staging EC2不得接收`GITHUB_TOKEN`、PAT或GHCR credential；ECR pull只使用instance role短效token；
- Host-side loader只以allowlist取值，寫入`tmpfs`上的`0600`檔案並在結束後清理；不得echo、寫入
  persistent `.env`、放入SSM command參數或diagnostic output。

### Immutable release 與 promotion

每個CD run只build一次，並產生versioned release manifest：

```json
{
  "schema_version": 1,
  "deployment_target": "staging",
  "unit": "findb-or-fetcher",
  "commit_sha": "40-char SHA",
  "images": {"name": "439622209937.dkr.ecr.ap-southeast-1.amazonaws.com/repository@sha256:..."},
  "migration_revision": "alembic revision or none",
  "contract_versions": ["versioned contracts"],
  "contract_manifest_sha256": "selected contract manifest canonical semantic SHA-256",
  "deployment_source_bundle_sha256": "...",
  "created_by_run_id": "..."
}
```

selected bundle的validator先執行`protocol`，raw stdout必須精確為
`findb-release-manifest-v1\n`（stderr、extra bytes或失敗皆fail closed）；workflow與host均以strict
validator SHA trust anchor驗證其bytes後才執行。沒有legacy SHA-tag rollback或current-checkout compatibility
branch可繞過此驗證。

同一份 selected source root 由 descriptor-safe root FD pin 住；每個 allowlisted path 的首次 bytes 讀取會
固定於單一 snapshot，contract parser、schema checksum 與 source checksum 都重用該 bytes，避免重開
pathname 導致的 source drift。

validate 還會精確綁定 workflow 的 unit、selected commit、migration revision、run ID 與每個 expected image
digest ref。manifest output 只寫入預先存在的 job-private runner temp parent；工具從 filesystem root 以
`O_NOFOLLOW` 逐段走訪並 pin parent directory FD，降低 ancestor/path/symlink swap，但不宣稱能防禦同 UID
的任意持續寫入者。

- Build job從ECR取得每個image digest；SHA tag只作索引，不作部署或rollback identity；
- Compose與Fetcher deploy helper只接受完整`image@sha256:...`，缺值或`:latest`一律fail closed；
- Deploy bundle使用private、versioned的AWS S3 control-plane bucket，與Raw／Canonical R2分離；
- Host在停止writer前先驗證manifest、bundle checksum、target、pull exact digests並render config；
- 成功後保存accepted manifest、Alembic revision、SSM command ID與acceptance結果；
- 未來production promotion只能使用staging ECR已接受的相同OCI artifacts與digests，不重新build。

未來production採unit-specific tag加手動promotion：

- FinDB Git tag使用`findb-vMAJOR.MINOR.PATCH`，Fetcher使用`fetcher-vMAJOR.MINOR.PATCH`；tag必須
  指向protected `main`上已有對應accepted staging manifest的commit；
- operator建立Git tag後再manual dispatch對應unit的promotion，workflow必須驗證tag、unit、commit
  與accepted manifest一致；Git tag與manual dispatch共同構成發布授權，不另設Environment reviewer；
- promotion將accepted staging OCI artifact複製到target-specific production ECR repository，
  驗證digest不變後才新增Docker `vMAJOR.MINOR.PATCH` tag；FinDB同時涵蓋backend與Dashboard，
  Fetcher同時涵蓋generic、FinLab與Shioaji；
- Docker release tag已指向相同digest時允許冪等重播，若已指向不同digest則fail closed且不得覆寫；
- Git tag、SHA tag與Docker release tag都只作索引；production manifest從accepted staging
  manifest衍生，實際deploy與rollback仍使用完整digest，不以tag取代image identity。

## IaC 邊界

本repo採OpenTofu管理本計畫新建或為cutover明確修改的控制面資源。Bootstrap stack建立
`findb-staging-tofu-state-439622209937` private、versioned S3 bucket與
`alias/findb-staging-tofu-state` KMS key；bootstrap與control-plane state分別使用
`staging/bootstrap.tfstate`與`staging/control-plane.tfstate`，並以S3 native lockfile鎖定。兩份
state object皆已驗證使用exact state CMK與bucket key，操作期backend設定由ignored
`bootstrap/backend.tf`依tracked template產生，不進入Git history。

本次只納管下列控制面資源：

- GitHub OIDC provider引用、FinDB／Fetcher deploy roles與instance roles；
- 五個staging ECR repositories、FinDB／Fetcher publisher roles、instance pull policies與安全的
  image lifecycle；
- SSM、KMS／secret policies、CloudWatch log groups及必要alarms；
- private、versioned deploy bundle／release manifest S3 bucket與權限。

既有EC2、RDS、VPC、subnet、security groups、R2與DNS先用read-only data source或資源清冊引用。
2026-08-26套用前已排除同名OIDC provider、roles、profiles、documents、KMS aliases、log groups與
buckets碰撞；套用後bootstrap與staging stacks均為`No changes`。全面納管既有AWS data plane
另案進行，不阻塞本次staging hardening；若後續發現外部owner，先import或交由原owner修改，
禁止建立第二套同名資源。

IaC原始碼同步與plan採以下過渡及長期契約：

- IaC專用workflow完成前，CloudShell只接受由受信任operator從指定commit建立、僅含
  `infra/tofu/`的單次archive。上傳前後都驗證SHA-256，從`/tmp`解壓，執行完即刪除source、
  tfvars、plan與archive；source commit變更或合併到`main`後必須以新commit重新產生及驗證，
  不把CloudShell副本視為可持續同步來源；
- 長期由GitHub Actions checkout觸發workflow所屬的exact commit，不再把CloudShell上傳作為日常
  同步方式。`infra/tofu/**`變更的PR必須執行recursive `fmt -check`、backend `init`、`validate`
  與refresh-enabled `plan`，provider及OpenTofu版本依repo lockfile／workflow固定；
- plan job使用獨立`staging-infra-plan` OIDC role。該role只允許讀取既有AWS資源、讀取及解密
  staging state，並只為S3 native lockfile取得必要的建立／刪除鎖權限；不得修改受管AWS資源、
  讀取runtime secret value或取得apply role；
- workflow輸出綁定commit SHA與configuration／lockfile checksum的bounded plan摘要；完整plan不得
  公開或跨commit重用。預設任何delete／replace action、初始化或驗證錯誤皆fail closed；retirement
  PR 的唯一例外只允許兩個 exact metadata deletes，且仍僅為 preflight；
- apply不由PR plan role執行。retirement apply 必須在 reviewed PR 合併後，以 `HEAD` 精確等於
  `origin/main` merged SHA 的乾淨 checkout、live empty-secret 驗證、fresh saved plan、immutable
  guard 的 exact two-delete proof 與 action-time user confirmation 為前提，再由獨立 apply identity
  apply exact saved plan；之後驗證30天 scheduled deletion、17筆 active catalog及fresh zero-delete
  plan。PR plan只作 gate與預覽，不構成apply授權。

## 實作波次

每一波各自提PR；先完成read-only或dual-path驗證，再移除舊路徑。不得在同一個deployment同時
切換OIDC、secret來源、image identity與migration流程。

### Phase 0：盤點與變更保護

- [x] 填完staging資源清冊，記錄owner、AWS account/region、resource ARN/ID、資料分類、backup
  policy、告警接收者與公開網路例外。
- [x] 驗證`main`的required CI與PR protection；`staging-findb`、`staging-fetcher`只允許protected
  branch且不設Environment人工核准；目前 staging 由protected `main` push依shared change policy
  自動rollout命中的unit，manual dispatch只用於accepted replay。
- [x] 依IaC邊界確認團隊既有工具與state owner；沒有既有標準時採OpenTofu，只納管新控制面；Tyler
  (`tylercore`) 擔任 OpenTofu/IaC remote-state owner，encrypted backend／locking 留待 Phase 1。
- [x] 記錄目前accepted SHA、實際image identity、Alembic revision、running containers、RDS
  snapshot／backup狀態與SSH recovery owner；同時保存noncanonical scheduler、legacy FinLab key與
  dataset projection等predeploy gates為零的結果。

Phase 0 執行註記：四項 checklist 均完成。文件只保存長期有效的控制契約與外部設定證據，
不保存一次性整合狀態。

| Checklist | 已完成的子結果 | 阻塞／未完成 |
| --- | --- | --- |
| 資源清冊 | 已記錄 account、region、VPC、subnet、恰兩台 running EC2 與 3/3 checks、SG、root EBS、RDS private／SG／encryption／backup-PITR state、runtime、公網例外與 public HTTPS 結果；Tyler 已是兩個 unit 的 resource、backup、SSH recovery、alert owner | Required EC2 tags、SSM、CloudWatch alarms/log groups 與 automated notification 仍留在 Phase 1/6；public SSH 與未加密 EBS 是已記錄、由 Tyler 承擔的 residual exceptions |
| `main` 與 Environment protection | 已驗證組織PR／force-push／deletion rules、兩個Environment各僅允許`main`且無reviewer／wait-timer；另建立Active、findb-only `FinDB required CI` ruleset，default branch只要求aggregate `Required CI`。Aggregate與兩份unit workflow變更已通過獨立驗證 | 無Phase 0 blocker；持續條件是每個targeting `main`的PR都產生`Required CI`，且不得以放寬ruleset或恢復path-filtered required contexts繞過 |
| IaC 與 state owner | repo/local 未找到 Terraform、OpenTofu、CloudFormation、Pulumi、CDK 或 remote-state ownership evidence；因此選用 OpenTofu，Tyler (`tylercore`) 擔任 state owner | encrypted remote backend／locking 尚未建立，屬 Phase 1/IaC implementation work；不得宣稱 state backend 已存在 |
| release／復原紀錄 | 已記錄 FinDB／Fetcher SHA、host-local image IDs（非 registry digests）、Alembic head、containers、Fetcher SQLite／filesystem checks、predeploy gates、SSH recovery 可達、RDS automated backup／PITR inventory，以及 `.env.remote` stale declaration 的安全清理結果 | accepted manifest／完整 registry digests、RDS restore rehearsal、current-volume EBS backup與SQLite／RabbitMQ recovery仍屬後續 Phase 3/6 驗收，不是本次已存在的證據 |

Phase 0 exit gate **已達成**：target、database、secret／backup owner與residual exceptions都有具名
紀錄；repo required CI enforcement與既有PR protection已由GitHub設定頁外部驗證；SSH recovery
仍可用，且staging自動rollout與accepted replay均不會被Environment人工核准阻塞。後續變更必須維持always-created
aggregate workflow、`Required CI` context與ruleset一致，不得倒退為path-filtered required contexts。

### Phase 1：OIDC、SSM與instance role基礎

- [x] 建立GitHub OIDC provider與兩個staging deploy roles，trust精確綁定各自Environment。
- [x] 為兩台EC2建立獨立instance profile、必要target tags、SSM agent與Session Manager設定。
- [x] SSM command output送到unit-specific CloudWatch log group，設定30-day retention且禁止secret輸出。
- [x] 以OIDC執行無副作用preflight：STS account／role、target count恰為1、environment marker、
  Docker／Compose版本、disk／inode、time sync、DNS與instance profile。
- [x] SSM首次live acceptance與兩個unit-specific Session Manager recovery成功；依既定範圍保留
  SSH recovery與TCP/22，不在本Phase提前移除。

#### Phase 1 work record（2026-08-26）

| Action | Evidence | Result |
| --- | --- | --- |
| 建立與遷移OpenTofu state | Bootstrap apply `8 added / 0 changed / 0 destroyed`；state bucket `findb-staging-tofu-state-439622209937`啟用versioning、public access block、native lockfile與KMS；local bootstrap state經驗證後遷移到`staging/bootstrap.tfstate` | Bootstrap與control-plane state objects均有version ID，SSE為`aws:kms`，exact key為`arn:aws:kms:ap-southeast-1:439622209937:key/776159fc-3251-4cd0-98b0-24dfa9e9701d`；兩個stack最終plan均為`No changes` |
| 建立Phase 1 AWS foundation | Staging apply `38 added / 0 changed / 0 destroyed`；建立GitHub OIDC provider、兩個deploy roles、兩個instance roles/profiles、required EC2 tags、兩份Session documents、兩個KMS-encrypted SSM log groups與private deploy-bundle bucket | OIDC issuer `token.actions.githubusercontent.com`、audience `sts.amazonaws.com`；trust subject分別精確綁定`staging-findb`／`staging-fetcher`，deploy bundle以unit prefix與exact CMK policy隔離 |
| 關聯instance profiles並啟用SSM | FinDB association `iip-assoc-025dc561d8df3ea85`；Fetcher association `iip-assoc-09519a3019afd0407`；兩台snap agent均為`3.3.4793.0` | `i-0942016913367a8b2`與`i-05f518ef183bc31a9`均為SSM `Online`，IMDSv2回報的profile與unit exact match |
| Live bounded host acceptance | Run Command驗證instance ID/profile、Docker/Compose、root disk、inode、UTC與`findb-staging.tingfong.com` DNS；兩台response code 0、stderr空白 | FinDB root disk/inode使用18%／4%，Fetcher為24%／7%；stdout已寫入各自CloudWatch log group，不讀取env或runtime config |
| 修正live acceptance發現的最小IAM缺口 | 首次command成功但agent log證明CloudWatch publisher缺`DescribeLogGroups`／unit-scoped`CreateLogGroup`，association loop缺`ListInstanceAssociations`；同時補齊`UpdateInstanceAssociationStatus` | 精確plan為`0 added / 4 changed / 0 destroyed`；修正後兩個stdout streams建立成功，recent agent check無實際AccessDenied／ERROR |
| 驗證recovery與least privilege | 兩份unit-specific Session Manager documents各建立一次session並正常退出；IAM simulator以實際role、target tags與ARN測試 | 本單元`ssm:SendCommand`與log read為`allowed`；cross-unit target/log與deploy-role `secretsmanager:GetSecretValue`為`implicitDeny` |
| 發布GitHub Environment variables | `staging-findb`與`staging-fetcher`各發布`AWS_REGION`、`AWS_ACCOUNT_ID`、unit-specific deploy role ARN、instance profile、SSM log group與DNS check name；透過GitHub API回讀鍵值。2026-09-04 target-aware reusable deploy live run補齊兩個Environment的exact `ECR_REGISTRY` | 初始12個非敏感AWS/SSM值與accepted OpenTofu outputs一致；後續兩個registry值精確綁定相同account與region。未變更Secrets、branch policy或reviewer設定 |
| Target-aware v2 rollout follow-up | FinDB runs [33859408068](https://github.com/FPI-TW/findb/actions/runs/33859408068)／[33862590269](https://github.com/FPI-TW/findb/actions/runs/33862590269)逐步通過same-revision CI、ECR digest、v2 candidate preparation、runner-side exact bundle validation、Bash wrapper、bundle materialization與image pull／inspect。後者的preflight SSM command `4a11caf6-4d75-4e71-8443-22a996283971`以`output_outside_run`拒絕；以同一exact v2 candidate執行的bounded probe `55eae9d3-2923-42b6-b409-b53d696bc534`為Success／exit 0、stderr空白，loader回報`output=removed`且Compose structure valid。PR #248合併後的FinDB run [33864465828](https://github.com/FPI-TW/findb/actions/runs/33864465828)之preflight command `3ed2795c-d891-4f09-a0d8-f2009ce950e0`正式Success，但candidate command `61e51fa6-3d14-46a8-b111-36131582db02`因reusable workflow漏傳staging `PORT`等non-secret runtime variables而被Compose interpolation拒絕；同revision Fetcher workflow [33864465856](https://github.com/FPI-TW/findb/actions/runs/33864465856)依change policy成功跳過deploy。PR #249合併後的FinDB run [34025994515](https://github.com/FPI-TW/findb/actions/runs/34025994515)之preflight command `54623223-76c4-4393-b8a9-1ab331668a60`與candidate command `c587bee7-7c00-49e5-86aa-d00b39b08b40`均Success，並已持久化exact v2 accepted bundle；activation command `c63ea779-e0d8-4740-9988-26cdfae9a2c3`在pull exact images後因Nginx catalog allowlist仍要求舊`-findb`尾碼而以`catalog_path_invalid`拒絕。只讀SSM command `e3bdbe80-b75d-4d8a-a586-5e8f624effc3`確認`current`仍指向run `33736955949`的舊accepted release，全部containers持續Up 3 days，未發生writer stop、migration、Compose up或pointer切換。 | `--check-only`仍要求output位於`/run/findb-runtime-secrets/<scope>/runtime.env`並在驗證後刪除，不能使用release的`/tmp` work path；preflight Compose只做`--no-interpolate`結構驗證。FinDB 18項與Fetcher 15項staging runtime variable allowlist由candidate／activation共用exact shell-quoted contract，production仍由target-scoped Secrets Manager載入。Nginx catalog gate須接受目前的`<bundle-sha>-<run-id>-<attempt>` v2 identity；尾碼`-findb`只對staging v1 accepted replay保留，production仍fail closed。修正合併後仍須以新main SHA完成FinDB normal deployment、Fetcher normal deployment與各自accepted replay，才可關閉follow-up。 |
| Fetcher v2 publisher follow-up | PR #250 merge SHA `3de4d332bdddd33f5a14374f443208ac530b6d99`的FinDB run [34070894738](https://github.com/FPI-TW/findb/actions/runs/34070894738)已完成同revision normal rollout；Fetcher run [34070894743](https://github.com/FPI-TW/findb/actions/runs/34070894743)的same-revision CI與FinDB dependency gate均Success，但`publish`在第一張Twelve Data image因傳入`./fetcher` build context而無法取得Dockerfile要求的`fetcher/`與`contracts/`，FinLab／Shioaji、prepare與deploy均未執行。只讀SSM command `309d7e89-ebc0-4c5b-9468-c40e7da56155`確認Fetcher `current`仍指向run `33718656063`的既有accepted release，三個長駐scheduler均Up 3 days。 | Fetcher三份Dockerfile與reusable CI皆以monorepo root為build context；staging publisher必須使用相同root context及`fetcher/Dockerfile*`，並以靜態契約測試拒絕退回`./fetcher`。本次失敗發生於ECR build，未建立candidate、acceptance record或SSM deployment；修正合併後仍須完成Fetcher v2 normal deployment與accepted replay。 |
| Fetcher v2 preflight follow-up | PR #251 merge SHA `ec56b725acc4725b7aeb7f8627a64b0127df7712`的Fetcher run [34077854079](https://github.com/FPI-TW/findb/actions/runs/34077854079)已通過same-revision CI、三張ECR image publish與v2 candidate bundle preparation；read-only preflight command `711aa6c6-15eb-494c-8451-fb4931826bd6`亦成功驗證bundle、exact image digests及runtime secret catalog，但最後嘗試讀取不屬於Fetcher bundle的`docker-compose.prod.yml`而exit 1。 | Fetcher unit以三個transactional runtime helpers直接管理providers，bundle刻意不含FinDB Compose。preflight須對materialized `runtime_secret_command.sh`、`deploy_fetcher_aws.sh`與`release_fetcher_provider.sh`執行`bash -n`，不得要求cross-unit artifact。本次未執行candidate、persist acceptance或activation；修正合併後仍須完成Fetcher v2 normal deployment與accepted replay。 |
| Unit workflow rollout routing follow-up | PR #252 merge SHA `285fc331146337c9daa75607d942e6702a014001`已修正Fetcher preflight，但Fetcher run [34081287247](https://github.com/FPI-TW/findb/actions/runs/34081287247)只執行same-revision CI，`select`／`publish`／`deploy`全部skipped；同PR因修改`backend/tests/`而由FinDB run [34081287256](https://github.com/FPI-TW/findb/actions/runs/34081287256)完成一次normal rollout。 | Shared policy原本把所有workflow-only變更視為non-deploying，導致unit deploy path修正無法自行觸發staging驗證。unit-specific `findb-cd.yml`／`findb-deploy.yml`與`fetcher-cd.yml`／`fetcher-deploy.yml`須分別觸發對應unit rollout；CI與production workflow仍只跑CI。Fetcher v2 normal deployment與accepted replay仍待完成。 |
| 保留既有recovery與secret邊界 | 未修改security groups、SSH keys、TCP/22、runtime secrets、RDS或application containers | SSH與未加密／無backup EBS仍依Phase 0紀錄、補償控制與Phase 6條件管理；secret migration只在Phase 2執行 |
| Protected `main` OIDC／SSM preflight | PR [#171](https://github.com/FPI-TW/findb/pull/171)合併為SHA `77212ce47b3138c2e21c6984e989b66239fd3cce`；[FinDB CD 32930274496](https://github.com/FPI-TW/findb/actions/runs/32930274496)與[Fetcher CD 32930274472](https://github.com/FPI-TW/findb/actions/runs/32930274472)均由`push`自動觸發並完成same-revision CI、build與deploy | 兩個Environment均通過STS account／role guard、exact-tag target count 1、exact instance profile、SSM `Online`及deploy-role secret read `AccessDenied`；無reviewer／wait-timer阻塞 |
| 驗證本次bounded command與marker | FinDB command `da88fea4-789f-4e5e-9ead-bea64b9a9480`寫入`/findb/staging/findb/ssm`；Fetcher command `9a5eb744-c6f9-4846-8b85-185cf6a9fe39`寫入`/findb/staging/fetcher/ssm` | 兩個invocation皆為Success、response code 0；以command／instance-specific log stream prefix查得success marker 1、failure marker 0，證實stream建立競態修正後仍維持bounded且fail-closed的marker契約 |
| 部署後read-only acceptance | FinDB與Fetcher runtime皆使用SHA `77212ce47b3138c2e21c6984e989b66239fd3cce`且restart count為0；FinDB Alembic為`d6e7f8a9b0c1`（head），DB-authoritative queue health無DLQ、expired lease、missing delivery或unpublished outbox；公開`/dashboard/`與`/dashboard/lookup`最終HTTPS 200 | Fetcher三個scheduler均以`10001:10001` running；FinDB container未指定non-root user、實際deployment仍使用SSH／SCP、image仍以SHA tag引用，分別保留在Phase 3、Phase 4／5，不誤列為Phase 1完成項目 |

Phase 1 checklist已全部完成。Protected `main`的兩個GitHub Environments已各自取得OIDC token並
跑完workflow bounded preflight；驗收同時以GitHub Actions、AWS SSM invocation與CloudWatch
marker查詢交叉確認，不以AWS administrator session代替GitHub OIDC證據。

Phase 1 exit gate **已達成**：兩個Environment各自只命中一台EC2；cross-unit SSM／log access由
IAM simulator維持`implicitDeny`，live deploy role secret read為`AccessDenied`；兩個unit-specific
Session Manager recovery均可用。

### Phase 2：Runtime secrets與ECR registry cutover

FinDB與Fetcher staging hosts均已安裝AWS CLI v2.36.31，各自以unit-specific instance role完成STS
identity驗證，並通過`/run` open-file-descriptor tmpfs檢查。Staging ECR foundation、publisher roles、
workflow cutover與live deployment 已完成。Secrets Manager active catalog 已收斂為17筆；兩筆空
GHCR transitional metadata已在2026-08-28排程30天刪除，且均無secret version。兩枚staging GHCR
pull PAT已刪除；已知legacy `/opt/findb/.env`與`/opt/findb-fetcher/.env`均不存在；FinDB的
`/run/findb-runtime-secrets`只保留nginx運行所需的`serve-key.conf`，兩台host皆無暫存bundle殘留。
GitHub Environment的23枚runtime copies已在完整原生provider cycle、last-used與health確認後依明確授權移除。
七枚DB-backed runtime
credentials已完成輪替、部署、使用驗證與舊值撤銷；三個provider scope項目依2026-08-29使用者核准變更
acceptance criterion而視為完成，既有值維持，實際未輪替、未替換、未撤銷，且不構成rotation evidence；
RabbitMQ已完成rotation及舊password拒絕驗證。Raw與Canonical R2 scope亦依本次使用者核准的
acceptance criterion變更而完成；既有值維持，未建立新key、未輪替、未替換、未撤銷舊key，因此不構成
rotation或old-value invalidation evidence，也不代表曾執行Cloudflare操作。

為避免ECR cutover被不同權限、停機窗口與外部服務流程無限延長，Phase 2自2026-08-29起依
責任邊界收斂為兩段：**2A ECR／runtime-secret transport**包含accepted SHA後的原生排程週期
觀察，已於2026-09-09完成；**2B credential hygiene**承接R2與provider scope decision、RabbitMQ rotation及GitHub
Environment runtime copies移除。provider與R2 scope項目已依使用者核准變更acceptance criterion而完成，並非
rotation evidence；RabbitMQ驗收及GitHub copies移除均已完成。

- [x] 建立IaC專用GitHub Actions PR plan gate與`staging-infra-plan` OIDC role，驗證 exact commit、
  bounded plan與plan／apply身分分離；PR plan只作preflight。Retirement另以protected-`main`
  fresh saved plan、immutable guard、獨立apply身分與action-time confirmation完成。
- [x] 依consumer邊界建立target-specific secrets與KMS policy；17筆 active runtime entries已建立，
  23枚GitHub runtime copies已移除。
- [x] 新增host-side secret loader，以allowlist取值、寫入tmpfs、驗證owner／mode並於結束後清理。
- [x] 完成runtime-secret deployment驗收；每個container僅取得必要credential。
- [x] Foundation 已建立五個ECR repositories、兩個main-only publisher roles及unit-specific instance
  pull policies，並完成 immutable tags、AES-256、basic scan-on-push 與 untagged 7天 lifecycle 驗收。
- [x] staging build/push/pull 已切換到ECR，FinDB與Fetcher完成ECR登入、pull與live deployment驗收；
  staging不dual-publish且不把`GITHUB_TOKEN`／PAT傳到EC2。
- [x] 七枚DB-backed keys已逐一輪替、驗證last-used並撤銷舊值。
- [x] 在accepted SHA後觀察一次完整原生排程週期；FinLab、兩個Shioaji feeds與Twelve Data均有native
  acquisition、delivery與terminal lineage證據，未以repair rerun、deployment replay或skipped acquisition smoke取代。
- [x] Phase 2B provider scope change：Twelve Data、FinLab、Shioaji provider scope項目依2026-08-29使用者
  核准變更acceptance criterion而視為完成；既有值維持，實際未輪替、未替換、未撤銷，且不構成rotation evidence。
- [x] Phase 2B RabbitMQ rotation：依安全停機順序重建broker相關服務，確認新值生效、舊password被拒絕、
  舊cookie失效、queue／DLQ與DB-authoritative health正常；詳見[Durable Ingestion Runbook](../operations/ingestion.md#rabbitmq-runtime-credential-rotation)。
- [x] Phase 2B R2 scope change：Raw與Canonical R2依2026-08-29使用者核准變更acceptance criterion而視為完成；
  既有值維持，未建立新key、未輪替、未替換、未撤銷舊key，故不構成rotation或old-value invalidation evidence，
  也不代表曾執行Cloudflare操作。
- [x] Phase 2B在完整原生provider cycle gate通過後，另行取得移除授權並確認last-used與health，再撤銷GitHub
  Environment中的runtime copies；此項不與R2實際rotation綁定。
  2026-09-09在目前及accepted commits的CD／reusable workflows均無`${{ secrets.* }}`runtime引用、accepted
  deployments與四feed cycle通過後，依使用者明確授權刪除`staging-findb`13枚及`staging-fetcher`10枚copies。
  兩個Environment secret清單回讀皆為空；FinDB command `fb74877d-3829-4260-9d4b-4465cb2f6f4d`與Fetcher
  command `84bb294b-91d8-46b3-ac70-71e091e0629e`分別驗證完整canary及三個provider catalog皆可由instance
  role載入且`output=removed`。Queue gauges為0、三個scheduler fresh／ready、public health與Dashboard為200、
  兩台SSM Online且43個alarms皆為`OK`。GitHub只保留role ARN、region、target selector、public host與secret
  identifier等非敏感值。
- [x] 兩枚 staging GHCR pull PAT 已刪除；不記錄token值。
- [x] 以retirement PR把active runtime-secret集合收斂為17筆；apply只刪除兩筆空GHCR metadata，
  使用30天recovery window，並符合本節的protected-`main`、live verification、saved plan、
  immutable guard與action-time confirmation邊界。

#### Phase 2 work record（2026-08-28–29）

| Action | Evidence | Result |
| --- | --- | --- |
| ECR foundation 與 cutover | Staging ECR foundation、publisher roles、workflow cutover及FinDB／Fetcher live deployments已完成驗收 | staging 僅由instance role取得ECR短效token，不保留GHCR credential |
| Runtime secret retirement | Retirement apply由protected `main` SHA `e995fa251f86627982d8be292905bc933ac776f6`產生fresh saved plan，guard精確證明`0 add / 1 change / 2 destroy`且僅含兩個核准地址 | CloudTrail兩筆`DeleteSecret`均於2026-08-28 08:09:03Z成功、`recoveryWindowInDays=30`、無`forceDeleteWithoutRecovery`；預定2026-09-27刪除 |
| Runtime secret inventory | `list-secrets --include-planned-deletion`回報17筆無`DeletedDate`的active entries加兩筆planned-deletion GHCR metadata；兩筆GHCR各為0個version | 17筆active secret各有且僅有一個`AWSCURRENT`；application DB及七筆已輪替DB-backed secret保留`AWSPREVIOUS`供版本稽核；provider與Raw／Canonical R2 scope項目依使用者核准變更acceptance criterion而完成。R2既有值維持，未建立新key、未輪替、未替換、未撤銷舊key，非rotation或old-value invalidation evidence，亦非Cloudflare操作證據；RabbitMQ已完成本輪rotation |
| Legacy GHCR／host material | 兩枚staging GHCR pull PAT、六枚deploy SSH secrets及23枚application runtime copies均已依各自授權刪除；兩台host均無legacy persistent `.env`；FinDB tmpfs只保留nginx運行所需檔案，其餘暫存bundle已清理 | 兩個Environment secret清單皆為空；AWS Secrets Manager catalogs、instance-role loader、queue、feeds、public routes、SSM與alarms刪後驗收通過。本次不涵蓋production |
| 合併版本部署與EOD repair | Manual FinDB run [33161513278](https://github.com/FPI-TW/findb/actions/runs/33161513278)成功部署merge SHA `0e2e28089498237b4261169aa0c6885f215a1d9e`；六個FinDB service使用該ECR SHA，queue／DLQ active gauges為0 | 原FinLab run因application role嘗試partition DDL而失敗；修正後精確rerun `01a047d7-d0f4-7477-9d9f-ceb0eb36959a`一次完成、attempt 1、2/2 rows、failure為null，application role仍無`public CREATE` |
| Scheduler observation | Shioaji兩個feed在2026-08-28 fresh；Twelve Data在2026-08-27 fresh；2026-08-31目前accepted Fetcher commit `c4c827a96c538d8be65787dcff0cb3492506098f`完成一次FinLab原生週期，run `01a0572e-64d6-73a3-8d1c-0055c0eec96e`為attempt 1、2/2 records、policy `pass`、durable terminal success | Phase 5 bounded FinLab gate已完成，但四個active feeds的完整post-deploy原生排程與多交易日觀察仍須依各feed eligible schedule驗收；repair rerun、deployment replay或skipped smoke不得取代此gate |
| Fetcher DB-backed credential rotation | Manual Fetcher run [33163966538](https://github.com/FPI-TW/findb/actions/runs/33163966538)成功部署accepted SHA；calendar Serve與Twelve Data、FinLab、Shioaji Source consumer fingerprint均精確對應四枚新credential，且有持續last-used／usage evidence | 四枚被取代credential及FinLab自2026-07-30後未使用的更舊前身共五枚均已撤銷；三個scheduler維持ECR accepted SHA運行 |
| FinDB DB-backed credential rotation | Lookup Serve、static-cache Serve與queue-health Admin三枚credential已輪替至各自Secrets Manager secret；Manual FinDB run [33164657004](https://github.com/FPI-TW/findb/actions/runs/33164657004)通過CI、runtime-secret canary、部署、queue health、public routes與cache生成 | serve／ingest／nginx實際載入fingerprint均精確對應新credential；三枚新key各有200 response與durable usage evidence後，三枚被取代credential均已撤銷 |
| Provider acceptance scope change | 2026-08-29使用者明確核准變更Twelve Data、FinLab、Shioaji的acceptance criterion | 此provider scope項目視為完成；既有值維持，實際未輪替、未替換、未撤銷，且不構成rotation evidence，也不以此宣告原生provider cycle gate通過 |
| R2 acceptance scope change | 2026-08-29使用者明確核准Raw與Canonical R2維持目前值，並變更本PR的acceptance criterion | 此R2 scope項目視為完成；既有值維持，未建立新key、未輪替、未替換、未撤銷舊key，故不構成rotation或old-value invalidation evidence，也不代表曾執行Cloudflare操作 |
| RabbitMQ runtime credential rotation | 先停止三個provider stable schedulers；持久化volume下`RABBITMQ_DEFAULT_PASS`不會更新既有internal user，故先以新`AWSCURRENT` password對`rabbit@findb-rabbitmq`執行in-broker password更新並驗證新auth成功，再於05:04Z由protected `main` deploy重建rabbit、policy、dispatcher與worker。Manual FinDB run [33235103928](https://github.com/FPI-TW/findb/actions/runs/33235103928)在workflow SHA `945abef69aa149da3460f5ce772c3022264bd904`重用accepted image `570e3c1e935210c7f084c9d8b5a7f711cdf9300b`，全綠 | `findb/staging/findb/rabbitmq/runtime`舊version `65fbff30-c417-54b5-b582-0b0fc0f89bc4`仍為`AWSPREVIOUS`，新version `1b2a8db6-6c97-44c0-a21f-9cf384c2571d`為`AWSCURRENT`。official container以新cookie重建後，在相同image、network、node `rabbit@findb-rabbitmq`與相同probe command下，current兩次為`[0,0]`，previous兩次為`[69,69]`，並診斷為cookie authentication rejection；`69`只屬本次歷史結果。舊password被broker拒絕；rabbit healthy、policy exit 0、worker healthy，queue／DLQ、expired leases、missing deliveries與unpublished outbox皆為0，worker heartbeat正常；persistent volume、node、vhost與topology仍存在 |
| Provider scheduler recovery after RabbitMQ rotation | 三個provider stable containers於05:06:47Z恢復running、restart count 0，仍使用accepted ECR SHA `0e2e28089498237b4261169aa0c6885f215a1d9e` | 僅記錄scheduler恢復；沒有原生provider cycle完成的實證，Phase 2A時間gate仍保持未完成 |
| Nginx lookup key重新掛載修復 | 舊lookup credential撤銷後，同源Referer的public Serve request回403；host tmpfs檔已是新fingerprint，但nginx對外仍注入已撤銷的舊key。PR #188與#189雖全綠，live ID／events證明nginx未replace；根因是`bash -s`內的main Compose up繼承stdin並消耗其後script。PR #190將不需stdin的Docker／Compose commands全部隔離為`</dev/null`，合併SHA `570e3c1e935210c7f084c9d8b5a7f711cdf9300b`後manual run [33193026381](https://github.com/FPI-TW/findb/actions/runs/33193026381)完成 | Deploy log在新nginx create／start後輸出`findb_aws_deploy=ready`；live events精確證明舊ID `8f16b2e98ffa`已kill／stop／die／destroy、新ID `c1c2ac638a05`已create／start且healthy。新container具正確Compose labels及唯讀`/run/findb-runtime-secrets/nginx/serve-key.conf` mount，internal exact-Referer與public lookup皆200，六個application containers均為accepted SHA；獨立Validator判定PASS |

Phase 2A exit gate：deploy role無法讀secret value；workflow log與SSM command不含runtime
secret；七枚DB-backed舊credential已撤銷而非只複製；FinDB與Fetcher無cross-secret read；兩個
unit皆以instance role取得ECR短效token，staging無GHCR credential，active runtime-secret catalog
恰為17筆。以上項目均已通過；accepted SHA後四個active feeds的完整原生排程已於2026-09-09完成驗收。
多交易日完整evidence package仍屬資料面backlog，不回頭開啟此單次原生週期gate。

Phase 2B exit gate：provider與Raw／Canonical R2 scope項目均依使用者核准變更acceptance criterion而視為完成。
R2既有值維持，未建立新key、未輪替、未替換、未撤銷舊key，故不構成rotation或old-value invalidation evidence，
也不代表曾執行Cloudflare操作。RabbitMQ已完成新舊值overlap、consumer reload、health及舊值失效驗證
（`AWSPREVIOUS`保留供稽核，非已刪除）。GitHub Environment runtime copies已在完整原生provider cycle、
last-used與health確認後依明確授權移除；此完成項不與R2實際rotation綁定。

### Phase 3：Digest、release manifest與CI gate

Wave 1 已實作並live驗收 deterministic、unit-scoped versioned bundle。每個bundle含selected SHA own
validator、canonical manifest及allowlisted deployment sources；manifest將FinDB的backend／Dashboard及
Fetcher的三個provider固定為 staging ECR `repository@sha256:...`。SSM在任何SSH、SCP或writer
interruption之前，以instance role取得同一份bundle，驗證外部bundle及validator SHA、manifest、commit、
migration和全部image digest，然後只materialize至root-owned immutable release root。後續staging helper、
Compose、nginx render與provider/smoke均由該release root執行；preflight不覆寫active path。
FinDB staging的三份非secret nginx rendered config只寫入root-owned、非group/world-writable的
`/etc/findb/nginx`，並由Compose的`FINDB_NGINX_CONFIG_DIR` mount；TLS檔及tmpfs serve key維持各自
既有路徑，其中TLS路徑僅作read-only prerequisite，staging workflow不得在其下mkdir或chown。
Production不沿用任何 staging host path、GHCR 或 SSH/SCP 相容路徑；其獨立帳戶的 target-aware
runtime path 屬於 production foundation/cutover work，且目前尚未建立 production resources。

正常staging部署只接受空白`image_tag`與空白`accepted_bundle_key`，為當前commit建立candidate bundle。
health成功並完成release activation後，workflow才以conditional writes持久化同一bundle及嚴格acceptance
record；record是accepted replay的唯一commit point。bundle單獨存在不代表已accepted。

受控staging rollback是accepted replay，而不是SHA-tag compatibility bridge：僅protected `main`的manual
dispatch可用，任何歷史`image_tag`必須同時提供匹配該unit accepted prefix、同一commit的
`accepted_bundle_key`；key沒有`image_tag`、candidate／cross-unit／mutable key、production及不匹配的
record一律拒絕。replay下載歷史accepted bytes與strict record，重驗證其bundle SHA、validator SHA、commit、
migration及完整digest image map，不重新生成manifest或使用current checkout。健康檢查成功後才切換current
release；replay不覆寫既有accepted bundle／record。

#### Phase 3 live work record（2026-08-30）

| Action | Evidence | Result |
| --- | --- | --- |
| 建立兩個unit的accepted release | FinDB normal run [33260508254](https://github.com/FPI-TW/findb/actions/runs/33260508254)與Fetcher normal run [33261139792](https://github.com/FPI-TW/findb/actions/runs/33261139792)均部署protected-main commit `b499869c8ff86e09232c1b55516787ae7ed5d2f0` | FinDB accepted bundle為`findb/accepted/b499869c8ff86e09232c1b55516787ae7ed5d2f0/0f90cbe570e34bc8bebe5c9a1737edfd881a7bc4e9d83ca4103a79dd884986cf.tar`；Fetcher accepted bundle為`fetcher/accepted/b499869c8ff86e09232c1b55516787ae7ed5d2f0/7513a17912111e0a1f5f34fa344ba5847a26eb64cf66e976d993273c75b1af15.tar` |
| 驗證五枚exact image digests | FinDB backend `ad08a5eb4b607ea219ac1714bda23f749bb1a1d2e993592b8c785d43c99b18ad`、Dashboard `cf1edf0afa0d3493c3044240959aae602acbb9be4ffaa7e53d8611788e212a2b`；Fetcher Twelve Data `b7b7d933686918bfc0d4af195eece2ff89774911f6a6b340fd02ebd56674982a`、FinLab `e42873931fb6fcaa4d1ef269ffaa85c5084e0a79a0791e9777f82f7631d3827f`、Shioaji `dce5ed79dc1d160b84e91b6fcc11c1147e555ac534dfe0229aef9bc9c1474d04` | Normal deployment與replay的live containers均使用相同完整digest；restart count為0並通過各unit既定health acceptance |
| FinDB accepted replay | Corrected replay run [33293482050](https://github.com/FPI-TW/findb/actions/runs/33293482050)以paired `image_tag`與accepted bundle key執行 | `/opt/findb/current`切換至`0f90cbe570e34bc8bebe5c9a1737edfd881a7bc4e9d83ca4103a79dd884986cf-33293482050-1-findb`；accepted tar／record VersionId仍為`GODQPpq2DScuruR9tUhcdyvB0iCgn89v`／`T2t2ZnrFJK33.PPEEEnpU6lvwSU2dCd_`，replay未重寫accepted evidence |
| Fetcher accepted replay | Test-only日期隔離修正合併後，run [33294103564](https://github.com/FPI-TW/findb/actions/runs/33294103564)以相同paired identity執行 | `/opt/fetcher/current`切換至`7513a17912111e0a1f5f34fa344ba5847a26eb64cf66e976d993273c75b1af15-33294103564-1-fetcher`；accepted tar／record VersionId仍為`4N1u770mjP1aVsl7TfcZeVC64JYIpjeE`／`o7f2BAqmJdoBhJZUuaPlaQSDqrdZbbDR`，tar SHA-256仍為`7513a17912111e0a1f5f34fa344ba5847a26eb64cf66e976d993273c75b1af15` |
| 獨立接受判斷 | 兩個replay均完成Manager read-only spot-check；Fetcher另經獨立Validator PASS與Reviewer CLEAR | Phase 3 normal accepted deployment、immutable replay、current release activation及不覆寫accepted record的技術gate已通過。FinLab acquisition smoke在Fetcher replay中明確skipped，不是Phase 2A原生provider cycle evidence |

Phase 3文件closeout已由PR #203完成。Production資源及workflow實作不是這個staging階段的完成條件。

### Phase 4：FinDB改走SSM（已完成）

- [x] FinDB staging deploy job改成OIDC＋SSM；workflow只傳non-secret deployment configuration，runtime
  secrets仍由instance role／tmpfs allowlisted loader取得，staging不再使用SSH／SCP action。
- [x] Preflight確認PostgreSQL TLS、current／target revision相容性、connection headroom、long transaction、
  RDS automated backup／PITR與host磁碟／inode headroom。
- [x] 先停`ingest`、`dispatcher`、`worker`、`raw-cleanup`及所有DB writers，再以migration credential
  執行單一Alembic job；`serve`只在schema相容時保留。
- [x] 啟動candidate後驗證container、internal health、public TLS／readiness、queue topology、
  worker ping、DB queue health與bounded DB transaction；Dashboard public route只驗證
  `/dashboard/`與`/dashboard/lookup`，不得恢復已移除的legacy public routes或skill入口。
- [x] 兩次normal run [33303655357](https://github.com/FPI-TW/findb/actions/runs/33303655357)／
  [33304135877](https://github.com/FPI-TW/findb/actions/runs/33304135877)已保存accepted evidence並完成
  SSM activation；accepted replay [33304630225](https://github.com/FPI-TW/findb/actions/runs/33304630225)
  使用既有record，candidate與persist均skipped，activation SSM
  `3899bcd3-32a9-444c-8402-5ce6ae30ac50`為`Success`／exit 0、success marker存在，health為200，且accepted
  tar／record VersionId維持`.rTPHJxsyKs0cxDGa.EQtVObbtd8A6M6`／`Loij0QSs05Iz.uCPojZV93w7Cl42JgqP`。
- [x] 依使用者授權精確刪除`staging-findb`的`FINDB_EC2_HOST`、`FINDB_EC2_USER`、
  `FINDB_EC2_SSH_KEY`：刪前16筆、刪後13筆，Environment API回讀`findb_ec2=[]`；repository Actions
  scope與staging variables均無同名項。刪後public
  health為HTTP 200（`{"status":"healthy","version":"0.1.0"}`），SSM target
  `i-0942016913367a8b2`仍為Online、agent `3.3.4793.0`。這不移除 staging TCP/22、SSH recovery
  ingress／keys、Fetcher 或 GitHub runtime copies；production infrastructure 尚不存在，不能把它的
  SSH、GHCR 或 runtime-secret retirement 宣告為既有完成事項。

Phase 4 exit gate已完成：FinDB連續兩次正常SSM deployment、一次accepted replay與deploy SSH secret
retirement均有live evidence，migration／candidate／activation維持fail closed且不使用SSH。Owner決定將
different-digest rollback與不同Alembic revision的schema-incompatibility live rejection移至Phase 6；此
scope調整不把同digest replay誤稱為rollback，也不改寫兩項尚未執行的事實。

### Phase 6：必要監控、復原、rollback與SSH退場

- [x] CloudWatch涵蓋EC2 status、disk／inode、Docker restart、RabbitMQ disk／memory、scheduler
  heartbeat、DLM policy health與deployment failure；RDS涵蓋free storage、connections、latency與backup lag。
  六個native加37個custom alarms均已live apply，35組bounded metrics及兩個collector associations成功，
  periodic／sparse synthetic alarm完成`OK -> ALARM -> OK`且43 alarms後驗為OK。
- [x] 告警接入具名小團隊owner與通知channel，設定log retention並演練一個synthetic alarm；不以
  建立正式24/7 on-call作為完成條件。
- [x] 驗證RDS encryption、automated backup、PITR與deletion protection，完成一次staging restore
  rehearsal並保存實測恢復時間與資料點。
- [x] Fetcher三個SQLite均以online backup建立一致性副本，再從隔離restore檔完成integrity／checksum／
  table-count驗證；RabbitMQ以保留舊目錄、清空live bind path的方式，從版本控制topology與PostgreSQL
  durable state完成queue／DLQ重建，DB前後計數一致。SQLite本機副本仍需DLM提供off-host保護；
  active-feed負載下的重複delivery另列資料面backlog。
- [x] 驗證generated instrument／macro cache可由canonical data重生；cache volume不列入durable
  backup或restore來源。
- [x] 當存在與目前deployment contract相容、但image digests不同的accepted predecessor時，演練
  application-only previous-release rollback；必須使用該release的exact digests、不重跑migration，並保存
  activation、health與immutable accepted evidence。Run `33734709446`成功切至不同backend/dashboard
  digests且Alembic維持`a8b9c0d1e2f3`；run `33736955949`再恢復protected-main accepted bundle。
- [x] 以不同Alembic revision的accepted release演練schema-incompatibility rejection。Workflow run
  `33736392355`先在deployment-contract compatibility gate fail closed；SSM command
  `4dd6baba-7427-4b59-a597-613409c35737`再證明current `a8b9c0d1e2f3`不相容且不等於target
  `d6e7f8a9b0c1`，四個writers保持停止、current pointer未改變。
- [x] 兩個unit各自完成成功的Session Manager recovery並移除兩台EC2的SSH ingress；兩個Environment的
  deploy SSH secrets已在Phase 4／5移除。
- [x] 移除`fb-db-key`／`findb-fetcher-key`與host `authorized_keys`中的recovery／unused keys，保留SSM
  break-glass流程與audit trail。2026-09-03兩台host的`root`／`ubuntu`各移除一筆精確指紋匹配，後驗
  target count均為0；兩個custom Session Manager documents均成功開啟並正常退出，之後兩個EC2
  key-pair resources刪除成功且回讀為空。
- [x] 為兩個current root volumes完成encrypted replacement；post-replacement services與schedulers健康。
- [x] 為兩個current root volumes完成recurring backup chain執行驗證。Daily DLM policy
  `policy-0d0a29c9e19f6323e`原設計為保留7份，兩個current-volume即時encrypted snapshots已completed；
  2026-09-08已將schedule-only tag由重複的`Purpose`改為`BackupPurpose`，fresh zero-delete plan
  apply後policy回到`ENABLED`。2026-09-09首個雙volume排程recovery point已驗證；2026-09-14回讀
  確認第二個不同週期於2026-09-10完成，且至2026-09-13已有五個不同雙volume週期，每volume各5/7份。
  所有計入snapshot均為`completed`、encrypted且exact policy／schedule／managed tags正確；manual
  snapshots未計入。

2026-09-01至2026-09-03的永久證據已整併至
[`operations/monitoring.md`](../operations/monitoring.md#live-acceptance-record-2026-09-01-to-2026-09-03)
與
[`operations/deployment.md`](../operations/deployment.md#phase-6-live-recovery-record-2026-09-01-to-2026-09-03)。
未完成項目的風險與不得宣稱邊界同步列於`docs/dev/backlog.md`；本次DLM驗收只採實際scheduled
snapshots，不以policy存在、資源已加密或manual snapshots替代。

Exit gate：SSM是唯一日常部署與管理路徑；關鍵alarm、RDS restore、SQLite recovery與broker
rebuild都有最近一次成功紀錄；different-digest application rollback成功，schema-incompatibility rejection
fail closed且writers保持停止；兩個 unit 各有兩次成功 SSM deploy 與 Session Manager recovery；current
EBS 已有encrypted replacement及即時recovery snapshots；automated DLM policy已修復為`ENABLED`且
首兩個不同雙volume排程週期均已通過，recurring backup chain驗收完成。SSH deployment
identity已撤銷。

## 每次 cutover 的 go/no-go

只有全部為Yes才執行會停止writer的步驟：

- manifest commit與當次CI revision一致，所有image使用digest；
- target account、region、Environment與唯一EC2 tag match正確；
- previous accepted manifest與schema相容性已判定；
- RDS backup／PITR健康，migration lock/time與connection headroom在門檻內；
- Fetcher scheduler desired state與data caps已記錄，沒有未結束的provider cycle；
- RabbitMQ／Fetcher state volume空間正常，SSM command logs可用；
- 具名operator、觀察窗口、停止條件與forward-fix owner已確認。

No-go時不得用`docker compose down -v`、刪volume、`alembic downgrade`、`stamp head`、mass
delete或R2 object搬移來強迫部署。Schema已改且舊image不相容時保持writers停止，部署包含目前
migration chain的forward fix。

## 完成定義

### Phase 3 wave 1 實作狀態（live gate已通過；PR #203已完成文件closeout）

Protected `main` 已具備 selected-SHA deterministic deployment bundle、private KMS S3
candidate/accepted records、SSM host-side bundle/digest preflight，以及 FinDB/Fetcher staging exact
digest deployment boundary。兩個unit均已完成normal accepted deployment、live SSM/health acceptance
與accepted replay rehearsal。Production promotion workflow已實作並完成dry-run契約驗證；
production resources與live acceptance仍不是目前staging completion condition。Phase 4與Phase 5均已完成兩次normal SSM deployment、accepted replay及
各自三個deploy SSH secret移除；Fetcher另完成bounded FinLab smoke。兩個exit gate均已關閉；
different-digest rollback與schema拒絕演練已於Phase 6完成。

Staging AWS deployment只有在以下全部有可查證evidence時才算完成：

- [x] aggregate加四個unit GitHub workflows的path、CI、Environment、concurrency與release unit matrix一致。
- [x] Protected `main` 的CD push path會先執行同revision CI，並在shared change policy命中unit且
  cutover gate啟用時自動rollout至staging；contract-only變更只跑兩個CI，manual dispatch只用於
  exact accepted bundle replay。兩種路徑均不受Environment人工核准阻塞，required CI與PR
  protection已完成外部驗證。
- [x] `infra/tofu/**`變更由IaC專用GitHub Actions在exact PR commit執行`fmt/init/validate/plan`；
  `staging-infra-plan` OIDC role不能修改受管資源或讀取runtime secret value，delete／replace
  fail closed，apply只可由protected `main`的fresh plan與獨立身分人工執行。
- [x] `staging-findb`與`staging-fetcher`有獨立OIDC deploy role、EC2 instance role與secret path。
- [x] 五個private staging ECR repositories均為immutable、AES-256、basic scan-on-push，且只有
  對應unit的publisher role可push、instance role可pull；兩個unit均以instance role取得短效token，
  staging不保存或傳送GHCR credential。
- [x] Staging日常deploy不使用SSH／SCP，cross-unit IAM測試fail closed。
- [x] EC2不開放SSH ingress；三個staging security groups均無TCP/22 rule。
- [x] Host recovery／unused EC2 key pairs與`authorized_keys`依Phase 6退場；SSM break-glass後驗成功。
- [x] 所有application image以digest部署，accepted release manifest包含五個完整ECR digests、可重播
  並供production promotion。
- [x] Accepted release manifest具備未來promotion所需的unit、commit與完整digests；契約已固定為
  `findb-vMAJOR.MINOR.PATCH`／`fetcher-vMAJOR.MINOR.PATCH`加manual dispatch，Docker
  `vMAJOR.MINOR.PATCH` tag只能在target-specific production ECR repository附加到相同OCI digest，
  碰撞時fail closed。Production不得重新build或依tag部署；production promotion workflow已實作，
  其AWS foundation與live acceptance不是本staging計畫的完成條件。
- [x] Runtime secrets不在persistent host env file，active catalog恰為17筆，兩筆空GHCR
  metadata已排程退役、兩個未使用PAT已撤銷；DB-backed rotation與RabbitMQ rotation均有evidence；provider
  與Raw／Canonical R2 scope項目依使用者核准變更acceptance criterion而完成，非rotation evidence；完整原生
  provider cycle、GitHub runtime copies移除及刪後health均有evidence。
- [x] RDS private、backup／PITR／deletion protection與一次restore rehearsal有紀錄。
- [x] Different-digest application rollback成功，schema-incompatible rollback fail closed且writers保持停止。
- [x] FinDB與Fetcher各完成兩次SSM deploy並通過bounded deployment acceptance。
- [x] 四個active feeds完成accepted SHA後的完整原生排程週期觀察；未以replay或bounded FinLab smoke取代。
- [x] 關鍵EC2／RDS／EBS／application告警、log retention、owner與synthetic alarm有紀錄；六個native、
  37個custom alarms、35組metrics、兩個associations與periodic／sparse synthetic transitions均有live evidence。
- [x] Fetcher SQLite recovery與RabbitMQ由PostgreSQL outbox重建均已演練。
- [x] Staging例外、相關風險、補償控制與重新評估條件均留有紀錄；staging例外不要求逐項登記具名owner。
- [ ] 現行操作移入`docs/operations/deployment.md`，migration／recovery規則移入對應operations
  runbook，target設定契約移入`infra/env/`，未完成項目只保留在`docs/dev/backlog.md`。
- [ ] 上述證據已有永久、可查驗的位置；完成此確認的同一PR刪除本暫時性計畫，並更新README、
  文件索引、backlog及其他連結。

## 明確延後與重新評估條件

| 項目 | 本次處理 | 重新評估條件 |
| --- | --- | --- |
| Staging HA／多EC2／ASG／blue-green | 延後；維持單EC2且不宣稱HA | 單機故障開始阻塞release，或production topology需要先演練 |
| RDS Multi-AZ／read replica／proxy | 不作staging完成條件 | Production SLA、連線或讀取負載需求確立 |
| Private subnet＋ALB | 若public EC2移除SSH且只保留必要HTTPS，可暫緩 | Production network設計完成前，或public EC2風險不可接受 |
| 全面AWS IaC import | 延後；本次只納管新控制面 | 既有resource inventory／drift responsibility 已由 Tyler 持有，待 production 環境複製或 drift治理需求確立 |
| Canonical R2資料面驗收 | 延後；只完成bucket與credential邊界 | Canonical publish/read/sign runtime完成 |
| Production資源與live promotion | Promotion workflow已實作並完成dry-run契約驗證；production Environment、ECR、EC2、RDS、R2與live acceptance仍延後 | 開始建立production foundation或執行正式promotion／rollback live acceptance |
| 正式24/7 on-call與企業稽核 | 延後；使用具名owner與通知channel | Production SLA、法遵或客戶稽核需求確立 |

這份檔案是暫時性執行計畫，不是永久runbook。不得在尚有未完成Phase、未搬移的操作知識或
只存在本文的驗收證據時提前刪除；全部完成並完成文件搬移後，也不應繼續保留本計畫。
