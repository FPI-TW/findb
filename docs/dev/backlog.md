# Current Backlog

> 只保存已核准範圍內的未完成工作。完成後直接移除；歷史由Git追溯。Active feeds與pilot
> 範圍見[現行架構](../architecture/overview.md#active-feeds)。

## 延後：平台強化

Production native 資料與實際 rollback 演練由
[未完成驗收](production-foundation-cutover-plan.md)單獨追蹤，不在此重複列項。

- [ ] 修正已失敗且不再重試的任務仍堆積於供應商工作佇列：同步任務／attempt 終態，
  保留仍可重試與待排空交付的工作，提供既有殘留的安全修復與回歸測試。追蹤 [#373](https://github.com/FPI-TW/findb/issues/373)。

- [ ] 修正 production `DeploymentFailure` control-plane/all 測點缺測：告警自 2026-09-17 因
  `TreatMissingData=breaching` 維持 ALARM。需補足 publisher／告警契約與 live 後驗；不得以靜默重設
  ALARM 或忽略 missing data 代替修正。v0.1.6 部署與查核證據見 deployment runbook。

- [ ] Canonical R2 publish／read／sign runtime完成後，補資料面驗收；目前只完成bucket與credential邊界。
- [ ] 當單機故障開始阻塞release，或production topology需要先演練時，重新評估staging多EC2／ASG／
  blue-green、private subnet＋ALB，以及RDS Multi-AZ／read replica／proxy。
- [ ] Production SLA、法遵或客戶稽核需求確立時，建立正式24/7 on-call與企業稽核控制。
- [ ] Production環境複製或drift治理需求確立時，重新評估既有AWS data-plane資源的全面IaC import；
  目前OpenTofu只納管新增控制面並引用既有EC2／RDS／VPC。
- [ ] 本次Serve breaking replacement部署至staging後，重生instrument cache，對七條 feeds
  執行required Serve evidence、bounded latency smoke與Dashboard人工驗收；部署前不得沿用舊
  minute `not_applicable` evidence宣稱完成。

- [ ] 若將 `release_fetcher_provider.sh` 開放為 standalone operator 介面，先補獨立 transaction hardening：
  previous-only stopped original 的 rename/start 必須在 recovery trap 後；stable accepted label 須明確驗證；
  state mkdir/chown/chmod 必須在完整可信 inventory 後。目前只支援 verified coordinator child，
  上述 standalone 邊界依賴 parent inventory／journal gate，尚未提供獨立介面保證。

## TAIFEX 正式上線

來源行為與排查見 [TAIFEX 期貨日行情](../operations/taifex.md)；native 驗收另見
[未完成驗收](production-foundation-cutover-plan.md)。

- [ ] Pilot 只取排序前兩個契約（實際皆為 MTX），TX／TMF／TE／TF 只能由 Full 或歷史回補驗收；
  驗收計畫須明列。

## Full-market external acceptance

- [ ] 將 TAIFEX dated-universe 專用告警、Admin candidates／member-diff 唯讀入口與 Dashboard
  提示部署至 staging，配合已完成的 bounded rollover／semantic universe 流程驗證後再 promotion
  至 production；production 不得套用 staging 的候選或操作紀錄，並須以自身 candidate、audit 與
  Source→raw→normalization→canonical→Serve 證據驗收。

- [ ] Account actual-start pacing v2 已在工作分支實作，待 independent Validator／Reviewer 與正式 rollout：
  舊 `BEGIN IMMEDIATE` 已先於 grant clock，但 commit、最後 Source check、SDK preparation／IPC
  仍可延後 actual invocation，使 grant-based interval／minute-day attribution 失真。新 protocol
  使用全 consumer account lock、pre-auth armed intent、bounded immutable auth origin、actual-start
  charging、completion floor、durable 429 與 conservative crash liability；須驗收 real process race／kill、
  historical lease/cancel、credential aliases、SDK counter reset 與 mixed-protocol drain／rollback。
- [ ] 以各 provider 的實際帳號確認 US 普通股／ADR、HK 主板／GEM、TW 上市／上櫃普通股與
  各類 ETF、Shioaji 分鐘資料 entitlement，並保存完整 official universe mapping、各平台已確認
  限制的 API 用量證據與治理資料；受限項保持 blocked。
- [ ] TW、HK、US、futures 實際 rollout 驗證 open-day 準時 complete，驗證
  expected 分母、durable no_data、partial Serve、gap catch-up、session 與 nullable futures fields。

## FactSet 港股首次環境啟用

實作契約與操作見 [FactSet 港股日行情](../operations/factset_hk.md)；staging ECR、IAM／KMS
及獨立 Source／RDS credentials 已就緒；backend migration、FactSet Source registry、
停止中的 Pilot／Full scheduler seed、Fetcher host secret loader 與 Source 契約認證已核對。
10/21 HK 切換時 Backend 為 `fe29ce5`、Fetcher 為 `f7d0d8f`；十五個 consumers 已完成 enforcement。
59 筆 prepared／receipt 與 DB terminal、一致備份、十五 consumer graceful stop、shared-inode
attestation、Owner cutover，以及原 plans／first dates 的保留均已核對。部署與備份證據見
[deployment](../operations/deployment.md#factset-港股版本下限)。
正常日期的 HKEX 第一份 baseline、RDS TLS／catalogue SELECT、兩 owner 排空、四個相關 controls
停止、HK provider cutover 及 rollback 前置核對已完成；現行狀態與證據見
[10/21 切換](../operations/deployment.md#2026-10-21-staging-hk-切換)。
退役 feed 修正已部署至 staging `dd1dece`，Twelve Data Pilot 已恢復 US 新抓取與 canonical 入庫，
Full 保持停止；證據見 [部署重試與 Pilot 恢復](../operations/deployment.md#部署重試與-twelve-data-pilot-恢復)。

- [ ] 保存另一個正常日期的 FactSet maintenance／release，驗證跨日更新與第一份 FactSet daily
  plan 的成員凍結。既有 20 plans／42 members 的切換前後 fingerprints 已核對不變，不能
  代替尚未建立的 FactSet daily plan 驗收。證據要求見 [FactSet runbook](../operations/factset_hk.md)。
- [ ] 驗證 staging FactSet 原生 prices SELECT，以及兩檔單日、異常歷史樣本、全名單的真實
  R2／Source／queue／canonical／Serve 流程，核對 partial／missing／DQ。兩檔 daily 須於
  香港時間 18:30–23:00 抓取；本機原生快照重播或歷史回補不能替代 daily 環境驗收。
  Twelve Data Pilot 已恢復 US 每日兩檔；FactSet Pilot 與全部 Full controls 保持停止，
  FactSet 驗收後維持 Pilot 每日兩檔、Full 停止。
- [ ] Staging 驗收後，套用 production 的新增基礎設施與 secrets，再依既有 production promotion
  與 Owner control 流程啟用；維持目前上市
  cohort 回補範圍、18:30–23:00 新 I/O 窗口、明確 partial／missing／DQ 證據，不自動 fallback。

## 不變約束

- 新資料來源只走provider-neutral Source contract；不恢復legacy direct routes。
- Serve不寫DB；canonical read model不代表active provider feed。
- Schema只經Alembic改變；Fetcher不連FinDB DB、不import backend runtime。
- Contract升級採backend-first expand/migrate/contract。
- 新增資料域前，先核准provider entitlement、bounded universe、versioned contract、registry、
  normalizer、DQ、Serve shaping、monitoring與staging acceptance。
