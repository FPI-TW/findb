# Current Backlog

> 只保存已核准範圍內的未完成工作。完成後直接移除；歷史由Git追溯。Active feeds
> 範圍見[現行架構](../architecture/overview.md#active-feeds)。

## Pilot profile 退役切換

切換契約、程序與 rollback 下限見 [Pilot 退役](../operations/deployment.md#pilot-profile-退役與-rollback-下限)。

- [ ] Staging 與 production 依序完成切換驗收：停止全部 controls 並排空 historical／maintenance、
  手動停止 `findb-pilot-*`、RDS snapshot 與 SQLite／governor 成套備份、移除 allocation 的 `pilot`、
  migration `a7c3e1f9d2b4`、`pilot-residue-v1` 封存 SHA-256、staging 共用 governor 唯讀核對、
  staging Full 首日驗收（一次只啟動一個 provider、記憶體／OOM，驗收後停止），再以相同 digests promotion
  production。
- [ ] `full_market_environment.enabled` 與 `full_market_enrollment.reported_enabled`（#450）：下一版從 ORM 移除
  （改由 server default 寫入，並調整舊 revision 的 migration 測試種子），再下一版刪欄；production 須依序
  promote，先含 migration `d7a2c4e6f8b1` 的版本，順序見
  [deployment](../operations/deployment.md) 的 Pilot 退役章節。

## Historical Full release cohort（#409、#433）

部署順序見 [Historical Full release cohort 的發布順序](../operations/deployment.md#historical-full-release-cohort-的發布順序409433)。

- [ ] 先在 staging 以一個日期驗收 Twelve Data 與 FinLab（含 `tw_etf_eod`）新 request，再到 production
  回補缺口：Twelve Data 11/02–11/05、FinLab 10/28 之後（範圍見 #409、#433）。
- [ ] 下一版移除舊 historical 路徑（#409、#433）：確認 production 沒有未排空的無 cohort Twelve Data／FinLab
  request 後，移除 `TwelveDataScheduledExecutor`、`FinLabScheduledExecutor`、`scheduler_state.py`、
  `finlab_universe.py`、`universe.py`、靜態清單（`fetcher/configs/twelve_data_*`、`finlab_tw50_*.json`、
  `finlab_tw_review_required.v1.json`）與 catalog 的 `historical_execution`，以及 Backend 的單一 run 相容驗證。

## CI/CD 五分鐘部署驗收

- [ ] Single-pass v4 已部署兩環境，transactions immutable bucket policy 已透過 OpenTofu 套用。
  FinDB／Fetcher × staging／production 各累積五筆
  一般增量成功樣本，每筆部署淨耗時小於 300 秒；保留超標、失敗與特殊情境樣本。
  計時口徑、歷史基準、故障復原與驗收限制見[部署操作](../operations/deployment.md)。
  #422 首輪 staging 已確認兩個 transaction committed：FinDB 347 秒、Fetcher 370 秒
  （已排除 verify/test 與 Fetcher 等待 FinDB 的 172 秒）。五個 images 的 GHA cache 均發生
  HTTP 400，另列快取異常／超標樣本；一般增量合格樣本仍各為 0/5。Cache v2、consumer 內有界平行
  secret 讀取、Fetcher 批次 inventory／平行 pull 與同 deploy role 的編排合併已實作，
  #424 冷快取首輪為 FinDB 322 秒、Fetcher 344 秒（另扣 200 秒 FinDB wait），仍未達標；
  #425 已合併恢復檢查的 SSM 往返，暖快取部署腳本增量為 FinDB 261 秒、Fetcher 236 秒
  （另扣 184 秒 FinDB wait），兩者各取得 1/5 筆合格樣本。仍須累積真實應用程式增量；
  不以空 commit 或 replay 補足。Production 首輪已成功 promotion：FinDB 237 秒、Fetcher 306 秒，
  已扣 verify，Fetcher 在 FinDB 完成後才 dispatch。其後 FinDB 229／245／240 秒、
  Fetcher 280 秒，production 合格樣本為 FinDB 4/5、Fetcher 1/5，Fetcher 首輪超標 6 秒的樣本保留；後續量測 promotion 初始化／digest 複製，不減少健康檢查。
  逐步量測與限制見操作文件。

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

## Provider metadata 與排程統一

追蹤 [#399](https://github.com/FPI-TW/findb/issues/399)／[#368](https://github.com/FPI-TW/findb/issues/368)。
PR1～4 已通過 staging／production 發布相容性驗收；PR5 已實作 DB preflight／migration、
監控期限統一及 Dashboard market 分組。現行契約見 [provider metadata](../architecture/provider_metadata.md)，
發布分組與證據見 [部署](../operations/deployment.md)。PR5 已依 Owner 決定先行發布 production
`findb-v0.1.24`，migration 與 Admin 唯讀驗收通過；staging 驗收仍未執行。

- [ ] PR3＋4 FactSet native 終態驗收依 Owner 指示暫不執行；已建立的 request 保留。Twelve Data、
  FinLab、TAIFEX、planner、daily 動態快照與 bundle 驗收已完成，證據與恢復核對方式見
  [staging 整合驗收](../operations/deployment.md)。
- [ ] PR5 staging migration 已以 Admin API 唯讀驗收（2026-11-10：四個 control 的時鐘、時區、下次執行時間
  與 completion deadline 皆與 catalog 一致）；staging UI 仍待確認。Production 尚需確認 Dashboard 市場分組與無 Slot 標示、
  新 plan 採 catalog deadline 及 slot ingress 相容性。每次發布重跑唯讀 preflight，未知差異中止，
  不自動啟用 stopped controls；controls 由 Owner 恢復。
- [ ] Slot 契約與 DB 欄位另以 expand／contract PR 退役；保留舊 raw／prepared／checkpoint 證據。

## TAIFEX 正式上線

來源行為與排查見 [TAIFEX 期貨日行情](../operations/taifex.md)；native 驗收另見
[未完成驗收](production-foundation-cutover-plan.md)。

- [ ] Full daily 涵蓋 TX／MTX／TMF／TE／TF 全部 published 契約；staging Full 首日起的驗收計畫須逐產品
  明列 sessions 與 canonical／Serve 證據，更早日期以歷史回補驗收。

## Full-market external acceptance

- [ ] TW 分 K 替代 provider 選型與缺口回補：停止抓取後到接手前不更新。Production 10/13～10/26
  無分 K（原 daily work 取消）；10/27～10/30 為部分資料／failed，9/30 equity historical failed。
  Full checkpoint 的 36 個未完成 Shioaji plans、原始 receipts 與 mapping／repair 證據需保留，
  不能直接重新啟動 Shioaji 或將舊失敗改成成功。

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

## Twelve Data 正式上線

現行抓取、價格基準與失敗分類見 [Full market](../operations/full_market.md#twelve-data-日資料與失敗分類)。

- [ ] 部署 `adjust=none`／header 認證／失敗分類後，以 staging 短期啟動 Twelve Data Full（約 6,018 筆目錄）
  驗收 Source → R2 → canonical → Serve，並以 `--health` 的 `source_error_diagnostics` 核對失敗分類；
  兩環境共用帳號，staging 抓取會消耗 production 配額，現行用量下不需事先協調。通過後才由 Owner 啟動 production Full。
- [ ] 向 Twelve Data 書面確認：方案的美股成交量是否為 consolidated、收盤價是否為官方收盤、
  美東 17:00 日線是否定稿及事後修正政策、400 訊息族群。已確認 staging／production 共用同一帳號且無法
  發多組 API key；兩環境 governor 各自計算，現行逐檔串行用量合計遠低於 1597 credits／分鐘。
- [ ] 匯入 Twelve Data splits／dividends 至 `corporate_action`，使 `CORP_ACTION_CONTINUITY` 對美股生效；
  另評估 close-to-close 跳價 warning（現行 `ABNORMAL_RETURN` 只比較同日 open／close）。
- [ ] 供應商 `no_data` 目前一律回報 `source_error` 並消耗重試；評估以停牌公告等交易所證據分流
  `halted`，不可把供應商缺資料直接當作 durable `no_data`。
- [ ] Historical 單一 symbol 失敗會使整個 work item 失敗，且舊 executor 的 idempotency 不含內容；
  評估逐 symbol 隔離與內容識別，避免一檔停牌阻擋 NASDAQ-100 回補。
- [ ] 以 FIGI／ISIN 保存美股商品永久識別，降低代號更名或重用造成的斷鏈與串錯公司。

## FactSet 港股首次環境啟用

實作契約與操作見 [FactSet 港股日行情](../operations/factset_hk.md)；staging ECR、IAM／KMS
及獨立 Source／RDS credentials 已就緒；backend migration、FactSet Source registry、
停止中的 scheduler seed、Fetcher host secret loader 與 Source 契約認證已核對。
10/21 HK 切換時 Backend 為 `fe29ce5`、Fetcher 為 `f7d0d8f`；十五個 consumers 已完成 enforcement。
59 筆 prepared／receipt 與 DB terminal、一致備份、十五 consumer graceful stop、shared-inode
attestation、Owner cutover，以及原 plans／first dates 的保留均已核對。部署與備份證據見
[deployment](../operations/deployment.md#factset-港股版本下限)。
正常日期的 HKEX 第一份 baseline、RDS TLS／catalogue SELECT、兩 owner 排空、四個相關 controls
停止、HK provider cutover 及 rollback 前置核對已完成；現行狀態與證據見
[10/21 切換](../operations/deployment.md#2026-10-21-staging-hk-切換)。
退役 feed 修正已部署至 staging `dd1dece`，當時 Twelve Data Pilot 恢復 US 新抓取與 canonical 入庫；
Pilot 退役後改由 Full daily 承接。證據見 [部署重試與 Pilot 恢復](../operations/deployment.md#部署重試與-twelve-data-pilot-恢復)。

- [ ] 保存另一個正常日期的 FactSet maintenance／release，驗證跨日更新與第一份 FactSet daily
  plan 的成員凍結。既有 20 plans／42 members 的切換前後 fingerprints 已核對不變，不能
  代替尚未建立的 FactSet daily plan 驗收。證據要求見 [FactSet runbook](../operations/factset_hk.md)。
- [ ] 驗證 staging FactSet 原生 prices SELECT，以及異常歷史樣本與全名單 Full daily 的真實
  R2／Source／queue／canonical／Serve 流程，核對 partial／missing／DQ。Daily 須於
  香港時間 18:30–23:00 抓取；本機原生快照重播或歷史回補不能替代 daily 環境驗收。
  FactSet Full control 於 Pilot 退役切換後由 Owner 檢查再啟動。
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
