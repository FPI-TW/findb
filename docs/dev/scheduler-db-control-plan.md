# FinDB Scheduler DB 控制計畫

## 摘要

三個 scheduler（Twelve Data、FinLab、Shioaji）的啟停狀態由 FinDB DB 保存，Dashboard
只允許 owner 修改。Fetcher 不直接連 DB，而是使用 provider-scoped Source API 每 30 秒
poll desired state 並回報 heartbeat；Fetcher 的 SQLite 只保存 checkpoint、retry 與 lease。

停止採優雅暫停：完成當前 cycle 後不啟動下一輪。Source API 控制面失聯時 fail-closed，
Fetcher 不開始新 cycle，並持續 retry。

## 已實作範圍

- Alembic 建立 `scheduler_control` registry table，並以 stopped 狀態建立三筆初始資料：
  - `twelve_data_us_common_stocks_daily_v1`
  - `finlab_tw_1430_tw_equity_eod`
  - `shioaji_tw_pilot_v1`
- Admin API：`GET /api/v1/admin/schedulers` viewer 以上可讀；
  `PATCH /api/v1/admin/schedulers/{scheduler_key}` owner-only、revision conflict、audit event。
- Source API：`POST /api/v1/source/scheduler-controls/{scheduler_key}/poll`，以 Source
  credential 的 `source_name` 與 `allowed_datasets` 驗證 provider scope，並更新 observed
  state、heartbeat、cycle 時間與最近錯誤。
- Fetcher 共用 control client/loop：stopped 時保持 container 常駐但不建立 provider
  工作；cycle 中收到停止狀態時完成當前 cycle，再停止下一輪；控制 API 失聯時禁止新 cycle。
- Dashboard Operations 概況提供 desired、observed、heartbeat age、最近 cycle、stale
  heartbeat、loading、pending、error 與 owner-only toggle。
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
- 啟用後只執行既有 scheduler 的 due/checkpoint 邏輯，不新增歷史 backfill。
- 30 秒是 control poll 預設值，可由 `FETCHER_SCHEDULER_CONTROL_POLL_SECONDS` 調整，
  但此設定不控制啟停。
- Backend 驗證 migration、constraint、角色權限、revision conflict、audit、scope 與 heartbeat。
- Fetcher 驗證 disabled idle、enabled cycle、graceful stop、fail-closed 與長 cycle heartbeat。
- Dashboard 驗證 schema、owner/唯讀角色、成功/失敗/stale/loading/pending UI。
- Deployment 驗證 CD 不再依賴 desired-state environment variables，stopped row 只讓常駐
  container idle。
