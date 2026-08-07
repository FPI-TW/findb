# FinDB Scheduler DB 控制計畫

## 摘要

三個 scheduler（Twelve Data、FinLab、Shioaji）的執行定義與啟停狀態由 FinDB DB 保存，
Dashboard 只允許 owner 修改 desired state。Fetcher 不直接連 DB，而是使用
provider-scoped Source API 每 30 秒 poll definition 與 desired state，並回報 heartbeat；
Fetcher 的 SQLite 只保存 checkpoint、retry 與 lease。

停止採優雅暫停：完成當前 cycle 後不啟動下一輪。Source API 控制面失聯時 fail-closed，
Fetcher 不開始新 cycle，並持續 retry。

## 已實作範圍

- `scheduler_control` 保存 provider、slot、每日本地時間、IANA timezone、desired／observed
  state、revision、heartbeat 與 cycle 狀態；`desired_state` 是唯一啟停權威。
- `scheduler_dataset` 以 FK 正規化 scheduler 與 dataset 的授權 mapping；舊
  `scheduler_control.dataset_keys` JSON 只保留相容投影，不可擴大 Source credential scope。
- Alembic 以 stopped 狀態建立並驗證三筆初始 definition：
  - `twelve_data_us_common_stocks_daily_v1`
    - `western_markets_window`、`06:30`、`Asia/Taipei`、`us_equity_eod`
  - `finlab_tw_equity_eod_v1`
    - `taiwan_market_window`、`14:30`、`Asia/Taipei`、`tw_equity_eod`
  - `shioaji_tw_pilot_v1`
    - `taiwan_market_window`、`14:30`、`Asia/Taipei`、`tw_equity_minute`、`tw_etf_minute`
- Admin API：`GET /api/v1/admin/schedulers` viewer 以上可讀；
  `PATCH /api/v1/admin/schedulers/{scheduler_key}` owner-only、revision conflict、audit event。
- Source API：`POST /api/v1/source/scheduler-controls/{scheduler_key}/poll`，以 Source
  credential 的 `source_name` 與 `allowed_datasets` 驗證 provider scope，回傳完整
  definition，並更新 observed state、heartbeat、cycle 時間與最近錯誤。未知或不相符的
  provider、mapping、slot、時間或 timezone 一律 fail closed。
- Fetcher 共用 control client/loop：stopped 時保持 container 常駐但不建立 provider
  工作；cycle 中收到停止狀態時完成當前 cycle，再停止下一輪；控制 API 失聯時禁止新 cycle。
- 現行 reviewed pilots 以 poll 回傳的 provider、dataset mapping、slot、時間與 timezone
  嚴格驗證 executable workload；任何不一致都 fail closed。變更 DB definition 時必須同步
  部署相符的 Fetcher workload，不能把這個驗證模式解讀成無需部署即可動態改排程。
- Slot ID 是不可解讀時間、provider 或 dataset 的穩定 window identity；實際本地觸發時間
  由 definition 欄位直接提供。變更時間不得改變 schedule/job identity；Fetcher SQLite
  state v2 會以原子交易升級至 state v3，舊／新 logical identity collision 會 fail closed。
- v2 manifest 另以 nullable `legacy_schedule_id` 明確宣告 v1 SQLite state 的 ownership；
  目前只有 Twelve western feed 可承接 `twelve_data_us_common_stocks_daily_v1`，其它
  provider/slot 不得依 provider、dataset 或時間推測 legacy ownership。
- Admin market freshness 由 `scheduler_control`／`scheduler_dataset` 投影，一個 scheduler
  一張卡；`DatasetRegistry.delivery_expectation` 只負責各 feed 的 expected data date、
  completeness 與 freshness policy，不再是執行時程權威。未停止、未收過資料、heartbeat
  stale、raw 已抓取但 normalization 尚未完成，以及 definition／feed policy 錯誤必須分開顯示。
- Dashboard Operations 統一顯示 definition、desired／observed、heartbeat age、最近 cycle、
  provider `fetched_at`、normalization completion、feed freshness、configuration error 與
  owner-only toggle；queue／worker 指標是 Source commit 後的 downstream pipeline。
- Admin DQ 列表只回傳 bounded run/provider/dataset/schema/raw-reference provenance 與白名單
  policy violation 摘要；不直接回傳 raw payload 或任意 `raw_data`。
- Fetcher CD 三個 scheduler container 一律使用 `--run-forever` 常駐，不再讀取
  `FETCHER_*_SCHEDULER_DESIRED_STATE`。

## 部署與回復

1. 先用既有環境變數機制停止舊版 scheduler container。
2. 部署 backend migration、Admin API 與 Source control API。
3. 確認三筆 control row 存在且 desired state 為 `stopped`。
4. 部署新的 Fetcher image；container 應常駐 idle，不執行資料抓取。
5. 部署 Dashboard。
6. owner 在 Dashboard 逐一啟用 scheduler。
7. 驗證 heartbeat、資料 cycle 與 graceful stop 後，再從 GitHub Environment 移除舊
   desired-state variables。

回復時維持 DB desired state 為 `stopped`，避免舊版 Fetcher 不理解 DB control 而意外
恢復資料抓取。若必須回到舊版，需在受控維運窗口明確設定舊環境變數。

## 預設假設與驗收

- 三個 deployment environment 使用獨立資料庫，因此 table 不增加 environment 欄位。
- 啟用後只執行通過 DB definition 一致性驗證的 slot／時間與既有 checkpoint 邏輯，不新增
  歷史 backfill；本機 manifest 的 `enabled`／`scheduled_time` 不得覆寫 DB 或在 definition
  不一致時繼續執行。
- 30 秒是 control poll 預設值，可由 `FETCHER_SCHEDULER_CONTROL_POLL_SECONDS` 調整，
  但此設定不控制啟停。
- Backend 驗證 migration/backfill、constraint、mapping scope、角色權限、revision conflict、
  audit、heartbeat、per-feed expected date，以及 raw fetched time 與 normalization completion
  分離。
- Fetcher 驗證 disabled idle、enabled cycle、graceful stop、fail-closed 與長 cycle heartbeat。
- Dashboard 驗證 schema、owner/唯讀角色、成功/失敗/stale/loading/pending UI。
- Deployment 驗證 CD 不再依賴 desired-state environment variables，stopped row 只讓常駐
  container idle。
