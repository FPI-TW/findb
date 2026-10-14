# Current Backlog

> 只保存已核准範圍內的未完成工作。完成後直接移除；歷史由Git追溯。Active feeds與pilot
> 範圍見[現行架構](../architecture/overview.md#active-feeds)。

## 延後：平台強化

Production foundation與live promotion／rollback acceptance由
[production foundation與cutover計畫](production-foundation-cutover-plan.md)單獨追蹤，不在此重複列項。

- [ ] 移除 `fetcher-deploy.yml` 的 `FETCHER_RUNTIME_PROFILE` replay shim 與 `release_manifest.py`
  的 fetcher bundle layout 1：條件是 rollback 視窗內所有 production accepted records 皆由統一管線
  （無 `runtime_profile`、layout 2）產生。移除前 `fetcher-v0.1.7` rollback 演練仍需此 shim。
- [ ] Canonical R2 publish／read／sign runtime完成後，補資料面驗收；目前只完成bucket與credential邊界。
- [ ] 當單機故障開始阻塞release，或production topology需要先演練時，重新評估staging多EC2／ASG／
  blue-green、private subnet＋ALB，以及RDS Multi-AZ／read replica／proxy。
- [ ] Production SLA、法遵或客戶稽核需求確立時，建立正式24/7 on-call與企業稽核控制。
- [ ] Production環境複製或drift治理需求確立時，重新評估既有AWS data-plane資源的全面IaC import；
  目前OpenTofu只納管新增控制面並引用既有EC2／RDS／VPC。
- [ ] 本次Serve breaking replacement部署至staging後，重生instrument cache，對四個active feeds
  執行required Serve evidence、bounded latency smoke與Dashboard人工驗收；部署前不得沿用舊
  minute `not_applicable` evidence宣稱完成。

- [ ] 若將 `release_fetcher_provider.sh` 開放為 standalone operator 介面，先補獨立 transaction hardening：
  previous-only stopped original 的 rename/start 必須在 recovery trap 後；stable accepted label 須明確驗證；
  state mkdir/chown/chmod 必須在完整可信 inventory 後。目前只支援 verified coordinator child，
  上述 standalone 邊界依賴 parent inventory／journal gate，尚未提供獨立介面保證。

- [ ] 補完整 frozen legacy coordinator 經實際 trusted transport 的離線 replay harness，保留原 flags、
  traps 與 pointer 流程，僅替換 Docker／secret wrapper 等外部介面；驗證 invalid Cmd 在任何 effects 前
  拒絕、原 container IDs／狀態與 pointer 不變，並加入合法 accepted bundle 成功 replay control。
  現有 transported excerpt harness 的 no-op registration 不代表完整 legacy transaction 覆蓋。

## Full-market external acceptance

- [ ] Account actual-start pacing v2 已在工作分支實作，待 independent Validator／Reviewer 與正式 rollout：
  舊 `BEGIN IMMEDIATE` 已先於 grant clock，但 commit、最後 Source check、SDK preparation／IPC
  仍可延後 actual invocation，使 grant-based interval／minute-day attribution 失真。新 protocol
  使用全 consumer account lock、pre-auth armed intent、bounded immutable auth origin、actual-start
  charging、completion floor、durable 429 與 conservative crash liability；須驗收 real process race／kill、
  historical lease/cancel、credential aliases、SDK counter reset 與 mixed-protocol drain／rollback。
- [ ] 以各 provider 的實際帳號確認 US 普通股／ADR、HK 主板／GEM、TW 上市／上櫃普通股與
  各類 ETF、Shioaji 分鐘資料 entitlement，並保存完整 official universe mapping、各平台已確認
  限制的 API 用量證據與治理資料；受限項保持 blocked。
- [ ] Production opt-in full-market runtime：長期 HK／FinLab ETF scopes、TAIFEX 獨立 Source credential
  與雙向 secret isolation 已配置查核；仍須正式安裝四個 Full workers，驗證 durable quota/checkpoint
  與 rollback、七個 native provider feeds；staging／production baseline 與 2026 calendar 已備齊，現況見 Full market runbook。
- [ ] TW、HK、US、futures 實際 rollout 驗證 open-day 準時 complete，驗證
  expected 分母、durable no_data、partial Serve、gap catch-up、session 與 nullable futures fields。

## 不變約束

- 新資料來源只走provider-neutral Source contract；不恢復legacy direct routes。
- Serve不寫DB；canonical read model不代表active provider feed。
- Schema只經Alembic改變；Fetcher不連FinDB DB、不import backend runtime。
- Contract升級採backend-first expand/migrate/contract。
- 新增資料域前，先核准provider entitlement、bounded universe、versioned contract、registry、
  normalizer、DQ、Serve shaping、monitoring與staging acceptance。
