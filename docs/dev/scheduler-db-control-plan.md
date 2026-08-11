# FinDB Scheduler DB 控制計畫

## 摘要

Staging 的三個 provider scheduler（Twelve Data、FinLab、Shioaji）由 FinDB DB 保存
執行定義與啟停狀態，Dashboard 只允許 owner 修改 desired state。Fetcher 不直接連 DB，
而是使用 provider-scoped Source API poll definition 並回報 heartbeat；Fetcher SQLite
只保存 checkpoint、retry 與 lease。

目前 active provider/dataset 配對固定為：

- `twelve_data/us_equity_eod`
- `finlab/tw_equity_eod`
- `shioaji/tw_equity_minute`
- `shioaji/tw_etf_minute`

Canonical/Serve read model 可保留歷史資料域，但不會因 scheduler card 出現就自動取得
其他 provider feed。需要新增資料域時，另案建立完整新版 contract、dataset mapping、
normalizer、DQ 與 Serve 驗收。

停止採優雅暫停：完成當前 cycle 後不啟動下一輪。Source API 控制面失聯時 fail-closed，
Fetcher 不開始新 cycle，並持續 retry。

## 已實作範圍

- `scheduler_control` 保存 provider、slot、每日本地時間、IANA timezone、desired／observed
  state、revision、heartbeat 與 cycle 狀態；`desired_state` 是唯一啟停權威。
- `scheduler_dataset` 以 FK 正規化 scheduler 與 dataset 的治理 mapping；mapping 不得
  超出 Source credential scope。
- Alembic 以 stopped 狀態建立並驗證三筆初始 definition：
  - Twelve Data：`us_equity_eod`
  - FinLab：`tw_equity_eod`
  - Shioaji：`tw_equity_minute`、`tw_etf_minute`
- Admin API：`GET /api/v1/admin/schedulers` viewer 以上可讀；
  `PATCH /api/v1/admin/schedulers/{scheduler_key}` owner-only、revision conflict、audit event。
- Source API：`POST /api/v1/source/scheduler-controls/{scheduler_key}/poll`，以 Source
  credential 的 `source_name` 與 `allowed_datasets` 驗證 provider scope，回傳完整
  definition，並更新 observed state、heartbeat、cycle 時間與最近錯誤。未知或不相符的
  provider、mapping、slot、時間或 timezone 一律 fail closed。
- Fetcher 共用 control client/loop：stopped 時保持 container 常駐但不建立 provider
  工作；cycle 中收到停止狀態時完成當前 cycle，再停止下一輪；控制 API 失聯時禁止新 cycle。
- reviewed workloads 以 poll 回傳的 provider、dataset mapping、slot、時間與 timezone
  嚴格驗證；任何不一致都 fail closed。變更 DB definition 時必須同步部署相符的 Fetcher
  workload，不能把此驗證模式解讀成可動態擴大 active feed。
- Slot ID 是不可解讀時間、provider 或 dataset 的穩定 window identity；實際本地觸發時間
  由 definition 欄位直接提供。變更時間不得改變 schedule/job identity。
- Admin market freshness 由 `scheduler_control`／`scheduler_dataset` 投影，一個 scheduler
  一張卡；`DatasetRegistry.delivery_expectation` 只負責 expected data date、completeness
  與 freshness policy，不是執行時程權威。未停止、未收過資料、heartbeat stale、raw 已抓取
  但 normalization 尚未完成，以及 definition/feed policy 錯誤必須分開顯示。
- Dashboard Operations 顯示 definition、desired／observed、heartbeat age、最近 cycle、
  provider `fetched_at`、normalization completion、feed freshness、configuration error 與
  owner-only toggle；queue／worker 指標是 Source commit 後的 downstream pipeline。
- Admin DQ 列表只回傳 bounded run/provider/dataset/schema/raw-reference provenance 與白名單
  policy violation 摘要；不直接回傳 raw payload 或任意 `raw_data`。
- Fetcher CD 的三個 scheduler container 一律使用 `--run-forever` 常駐，不讀取環境變數
  作為啟停權威。

## 部署與回復

1. 先停止既有 scheduler container。
2. 部署 backend migration、Admin API 與 Source control API。
3. 確認三筆 control row 存在且 desired state 為 `stopped`。
4. 部署新的 Fetcher image；container 應常駐 idle，不執行資料抓取。
5. 部署 Dashboard。
6. owner 在 Dashboard 逐一啟用 staging scheduler。
7. 驗證 heartbeat、資料 cycle 與 graceful stop 後，再清理不再使用的環境變數。

回復時維持 DB desired state 為 `stopped`，避免舊版 Fetcher 不理解 DB control 而意外恢復
資料抓取。不得以舊 route、舊 provider 或 scheduler identity 作為 fallback。

## 預設假設與驗收

- staging 使用獨立資料庫，因此 table 不增加 environment 欄位。
- 啟用後只執行通過 DB definition 一致性驗證的 slot／時間與既有 checkpoint 邏輯，不新增
  歷史 backfill；本機 manifest 的 `enabled`／`scheduled_time` 不得覆寫 DB。
- 30 秒是 control poll 預設值，可由 `FETCHER_SCHEDULER_CONTROL_POLL_SECONDS` 調整，
  但此設定不控制啟停。
- Backend 驗證 migration、constraint、mapping scope、角色權限、revision conflict、audit、
  heartbeat、per-feed expected date，以及 raw fetched time 與 normalization completion 分離。
- Fetcher 驗證 disabled idle、enabled cycle、graceful stop、fail-closed 與長 cycle heartbeat。
- Dashboard 驗證 schema、owner/唯讀角色、成功/失敗/stale/loading/pending UI。
- Deployment 驗證 CD 不依賴 desired-state environment variables，stopped row 只讓常駐
  container idle。
