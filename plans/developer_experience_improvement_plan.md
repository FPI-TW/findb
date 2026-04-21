# 開發體驗改善計劃（DevEx）

> **建立時間**: 2026-04-21  
> **狀態**: Proposed  
> **目標**: 讓本地開發、提交流程、資料庫演進流程可預期且一致

---

## 一、目標與範圍

本計劃聚焦以下 6 個目標：

1. 本地啟動指令更明確：
- 僅啟動 server
- 僅啟動 db
- 同時啟動 server + db
- 測試可跑「包含 db」的部分
2. 「同時啟動」明確定義為 server image + db image 各一個容器。
3. `git push` 前自動執行測試。
4. `git commit` 前自動執行 format。
5. 提出可落地的 migration 策略（考慮遠端已有資料、各開發者本地不一致）。
6. 保留 `uv` 固定命令作為標準入口，並完成 macOS/Linux 與 Windows 雙平台 wrapper。

非目標（本輪不做）：
- 不改動業務 API 行為。
- 不在本計劃內引入 queue / worker 架構調整。

---

## 二、現況與問題

### 2.1 現況

- 已有 `docker-compose.yml`，但預設包含 `app/db/raw-cleanup/pgadmin`，開發者容易不清楚最小啟動集合。
- README 中有多種啟動方式，但缺少「一眼可用」的命令分組與單一入口。
- CI（GitHub Actions）在 push/PR 會跑 `pytest`，但本地 `commit/push` 前沒有統一 hook。
- 專案依賴已包含 Alembic，但目前尚未完成 migration wiring；啟動仍使用 runtime `create_all()`。
- 目前文件與命令描述偏向 Unix 習慣，Windows 開發者需要額外轉譯 shell 指令。

### 2.2 主要風險

- 本地流程不一致，容易出現「我這邊可以、你那邊不行」。
- 平台差異（shell、路徑、換行）容易讓指令在 Windows 失效。
- schema 變更缺少 migration 管控，遠端與本地 schema 漂移風險高。
- 變更在提交前缺少自動格式化與測試防線，回歸風險提高。

---

## 三、實施方案

## Phase 1：本地啟動與測試命令標準化

### 3.1 固定主命令 + 雙平台 wrapper（建議 `scripts/dev.py`）

建立固定命令，降低心智負擔，並避免依賴 bash / make：

- `uv run python scripts/dev.py up-db`：只啟動 DB 容器。
- `uv run python scripts/dev.py up-server`：只啟動本機 server（預期 db 已可連線）。
- `uv run python scripts/dev.py up`：同時啟動 server image + db image（兩個容器）。
- `uv run python scripts/dev.py test-db`：確保 db 可用後執行 pytest（含 DB 測試）。
- `uv run python scripts/dev.py down`：停止開發容器。

雙平台 wrapper（必做，且內部統一呼叫主命令）：

- macOS/Linux wrapper：`make up-db` / `make up-server` / `make up` / `make test-db` / `make down`
- Windows wrapper：`.\scripts\dev.ps1 up-db` / `.\scripts\dev.ps1 up-server` / `.\scripts\dev.ps1 up` / `.\scripts\dev.ps1 test-db` / `.\scripts\dev.ps1 down`

### 3.2 Compose 啟動邊界

- 保留 `app` 與 `db` 為核心服務。
- 將 `pgadmin`、`raw-cleanup` 視為可選工具（以 profile 或獨立 compose 檔隔離），避免 `up` 時被默認拉起。
- 明確定義「同時啟動」命令只保證 `app + db` 兩個容器。
- Compose 命令統一使用 `docker compose`（避免 `docker-compose` 在部分環境不可用）。

### 3.3 文件更新

在 README 的「快速開始」新增固定區塊：

- `只啟 DB`
- `只啟 Server`
- `啟 Server+DB`
- `跑含 DB 測試`
- `macOS/Linux 與 Windows 對照指令`

並將舊命令改為「進階/補充」而非主路徑。

### 3.4 驗收標準

- 新人依 README 於 10 分鐘內可完成 `up-db -> up-server -> test-db`。
- `uv run python scripts/dev.py up` 僅啟動 `app/db` 兩容器。
- 測試命令在乾淨環境可重現。
- macOS 與 Windows 各至少一台環境可按同一主命令成功執行。
- wrapper 與主命令行為一致（同參數、同結果、同退出碼）。

---

## Phase 2：commit / push 自動守門

### 4.1 導入 Git Hooks（建議使用 `pre-commit`）

理由：跨平台、版本化管理、團隊一致性高。

建議規則：

- `pre-commit`（commit 前）：
- 執行 format（`black`，可選加 `ruff --fix`）。
- 若格式被修正，要求開發者重新 `git add` 後 commit。
- `pre-push`（push 前）：
- 執行 `uv run pytest --tb=short -q`。

跨平台約束：

- hook `entry` 不使用 bash 專屬語法，統一走 `uv run ...` 命令。
- 補上 `.gitattributes`（至少 `* text=auto`）降低 CRLF/LF 差異造成的格式噪音。

### 4.2 與 CI 的關係

- 本地 hook 是第一道防線，CI 保留為第二道防線。
- CI 不變更為「僅信任本地 hook」；避免繞過 hook 時失守。

### 4.3 驗收標準

- 未格式化程式碼無法直接 commit 成功。
- 測試失敗時無法 push。
- 團隊文件可完成一次性安裝：`pre-commit install --hook-type pre-commit --hook-type pre-push`。

---

## Phase 3：Migration 導入策略（遠端有資料 / 本地不一致）

### 5.1 原則

- 避免直接在已有資料的遠端環境執行高風險 destructive DDL。
- 先建立「可追蹤版本基線」，再開始增量 migration。
- 將 schema 變更責任從 `create_all()` 轉移到 Alembic。

### 5.2 建議路線

1. **凍結基線（Baseline）**
- 以目前遠端實際 schema 建立 `baseline` revision。
- 對於遠端既有資料庫：先備份，再使用 `alembic stamp <baseline_rev>` 對齊版本號（不重建資料）。

2. **建立乾淨環境驗證**
- 在空資料庫執行 `alembic upgrade head`，確認可完整建庫。
- 在 CI 新增 migration smoke test（至少驗證 migrate up）。

3. **本地不一致收斂策略**
- 開發者本地資料「可捨棄」：提供 reset 指令（drop/recreate + `upgrade head`）。
- 開發者本地資料「需保留」：先備份，再手動對齊後 `stamp` 到 baseline，再跑後續 migration。

4. **移除 runtime schema 管理依賴**
- 將 `init_db()` 的 `create_all()` 逐步降級為開發專用，最終由 migration 接管。
- 生產環境啟動流程改為「先 migration，再起 app」。

### 5.3 風險控管

- 每次 migration 都需提供 rollback 說明（至少 downgrade 路徑或資料回復策略）。
- 涉及欄位型別/約束變更時，要求分批 migration（expand -> migrate data -> contract）。

### 5.4 驗收標準

- 遠端既有 DB 可以透過 `stamp + incremental migrations` 納入版本管理。
- 新環境可僅透過 migration 建立完整 schema。
- 新增/修改 ORM model 時，團隊遵循 migration 流程，不再依賴 `create_all()`。

---

## 四、交付清單（建議）

1. `scripts/dev.py`（`uv` 固定主入口）+ 雙平台 wrapper（`Makefile`、`scripts/dev.ps1`）。
2. `docker-compose` 調整（核心服務與工具服務分離）。
3. `.pre-commit-config.yaml` + README 安裝與故障排查段落。
4. Alembic 初始化與 baseline revision。
5. CI 增加 migration smoke test（可與現有 pytest 併行）。

---

## 五、執行順序與預估

1. **P1 命令標準化 + README 更新**（0.5~1 天）
2. **P2 Git hooks 導入**（0.5 天）
3. **P3 Migration baseline + CI 驗證**（1~2 天，視遠端 schema 差異）

總計：約 2~3.5 天（不含生產變更審批窗口）。

---

## 六、完成定義（Definition of Done）

- 開發者有單一路徑可完成：啟動、測試、commit、push。
- 主要守門前移到本地（format/test），CI 持續作為保底。
- schema 演進從「啟動自動建表」轉為「migration 可審計、可重放」。
