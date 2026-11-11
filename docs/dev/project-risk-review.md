# 全專案風險與孤兒盤點

2026-11-11；檢查基準為 `79361d7` 的本機工作樹。這是待分流、待修正的 review 清單，
不代表已核准的新功能 backlog。處理完成後，將仍有效的規則整併至現行文件並移除此清單。

範圍涵蓋 Backend HTTP／auth／ingest／normalization／Serve、Fetcher runtime／historical／
checkpoint、Dashboard server functions／routes、Nginx、Compose、CI／CD、OpenTofu 與文件引用。
方法是資料流程追查、跨層設定核對、AST／全 repo 引用搜尋、完整既有測試與隔離重現；
不是逐行形式驗證，也未查詢 production／staging DB、AWS／R2 實際資源或供應商帳號。
因此下列「資料殘留風險」不能解讀為已確認線上有多少孤兒資料。

## Issue 追蹤

2026-11-11 寫入前已比對全部 open issues，並以風險關鍵字補查既有 issues。
R3 補入既有 #485；全部孤兒候選、保留邊界與文件漂移集中補入既有 #489，保留原 account
governor 調查，不另開重複的孤兒 issue。其餘五項各自建立追蹤：

| 項目 | Issue |
| --- | --- |
| R1：並行 EOD 修正 | [#490](https://github.com/FPI-TW/findb/issues/490) |
| R2：manifest 大小限制 | [#491](https://github.com/FPI-TW/findb/issues/491) |
| R3：限流鍵不回收（補充既有 issue） | [#485](https://github.com/FPI-TW/findb/issues/485) |
| R4：Dashboard 共用登入來源 | [#492](https://github.com/FPI-TW/findb/issues/492) |
| R5：Production IaC 驗證缺口 | [#493](https://github.com/FPI-TW/findb/issues/493) |
| R6：PostgreSQL prepared body／過期暫存保存 | [#494](https://github.com/FPI-TW/findb/issues/494) |
| 孤兒候選集中追蹤（擴充既有 issue） | [#489](https://github.com/FPI-TW/findb/issues/489) |

R5 與 #484 的 plan JSON／bootstrap 指令問題不同；R6 與 #410 的 R2 lifecycle、
#401 的 Fetcher SQLite 清理範圍不同。建立 issue 不代表已修正或完成線上驗收。

## 可重現問題與設定缺口

### R1 — P1：並行 EOD 修正可寫入違反 DQ 的行情

位置：`backend/app/services/admin.py:193–219`，入口為
`PATCH /api/v1/admin/eod/{instrument_id}/{trade_date}`。

讀取行情沒有 row lock 或 optimistic revision；兩個 transaction 可從同一份舊值各自通過
`_validate_blocking_dq_rules()`，再以不同欄位 UPDATE 合成不合法的最終值。
行情表也沒有阻擋此 OHLC 組合的 DB constraint。

隔離 PostgreSQL 重現已成功：

1. 初始 `open=100, high=120, low=80, close=100`。
2. 兩個獨立 session 同時修正：A 設 `low=95`，B 設 `close=90`。
3. 在既有 DQ 驗證後、stats 寫入前用 barrier 固定交錯順序，未替換查詢或 DQ 邏輯。
4. 兩筆修正均 commit；重新查詢得到 `low=95.00000000, close=90.00000000`。

影響：canonical 行情失去 OHLC 一致性；同欄位競寫時，稽核 before snapshot 也可能不是
實際被覆寫的版本。建議在讀取前取得同一行情的 row lock，或使用版本條件更新，並補上
兩個獨立 transaction 的回歸測試；同時檢查與 normalizer upsert 的交錯行為。

### R2 — P2：manifest 的 Backend／Nginx 大小契約不一致

位置：`infra/nginx/nginx.conf:42–45`、`backend/app/main.py:77–85`。

Backend 對 `POST /source/provider-work/{id}/manifest` 放寬到 16 MiB，但 Nginx 只有
`/source/universes` 的 exact location 放寬；manifest 仍進入 `^~ /api/v1/source/` 的 1 MiB
限制。Fetcher 的 `Owner.freeze()` 一次提交整份 manifest，沒有分頁或分塊協定。

合成驗證：16,000 個 `SYM00000` 格式成員、每筆包含 `key`／`source_symbol`／
`mapping_status`，連同 fence 的 JSON 為 1,152,164 bytes，通過 `ManifestRequest` 形狀驗證；
此成員數亦低於 universe 的 30,000 上限，但超過 Nginx 的 1,048,576 bytes。
經目前代理設定會在抵達 Backend 前被 413 拒絕。這是設定／合成 payload 驗證，
未宣稱目前線上 cohort 已達此大小，也未實際對線上 Nginx 發送大請求。

建議讓此特定 route 的代理上限與 Backend 一致，並加入經 Nginx 的邊界驗收；
保留其他 ingest route 原本的限制。

### R3 — P2：登入限流的鍵數無上限，過期後也不回收

位置：`backend/app/api/deps.py:133–146`、`backend/app/api/v1/admin.py:668–674`。

公開登入端點在認證前以 `IP:username` 建立 dict entry。程式只過濾本次命中鍵的時間戳，
不刪其他過期鍵，也沒有總鍵數上限或獨立 IP admission limit；改用新的 username 即可新增鍵。
Source／Serve limiter 同樣沒有全域過期鍵回收，但登入端點不需要有效 credential，影響較直接。

以假的 request／clock 呼叫既有 limiter：建立 1,000 個不同帳號鍵，將時鐘推進超過全部
window 後再登入一個帳號，dict 仍有 1,001 entries。未發送攻擊流量。
長期運作或大量不同帳號嘗試會讓 process memory 持續增加。

建議採有容量上限與 TTL eviction 的儲存方式，並加入按可信 client IP 的整體登入速率限制；
測試過期鍵回收及大量不同 username 的記憶體上界。

### R4 — P2：Dashboard 登入共用代理 IP，任一瀏覽器可耗盡其他人的同帳號限額

位置：`dashboard/src/lib/auth.server.ts:109–116`、`backend/app/api/deps.py:136`、
`docker-compose.prod.yml` 的 Dashboard upstream 設定。

Dashboard 以 server-side fetch 直接呼叫 ingest，headers 只有 Accept／Content-Type，
沒有傳遞由入口 Nginx 確認的 client identity。因此 Backend 看見的是同一 Dashboard
container 的 IP；外部不同使用者對同一 username 共用一個登入桶。
在預設設定下，任一未登入使用者五次嘗試該帳號，即可使其他瀏覽器的下一次正確登入
得到 429，直到五分鐘 window 釋放。既有 limiter 的六次同來源／同帳號呼叫已驗證此結果；
跨瀏覽器身份折疊由上述 proxy 路徑確認，未執行線上封鎖。

建議明確設計「可信來源 IP」與「帳號」兩層限制，將 Nginx 驗證後的來源傳遞至 ingest；
不可直接信任瀏覽器可任意填寫的 forwarding header。若刻意採全帳號共用限制，需明確記錄
此封鎖取捨與復原方式。

## 需要決定政策的風險

### R5 — Production IaC 的 CI 缺少自身 validate／plan

`infra/deploy/change_policy.py` 會讓 production `.tf` 變更觸發 infra-plan，
但 `.github/workflows/staging-infra-plan.yml:134–147` 的 init／validate，以及後續 plan，
都固定執行 `infra/tofu/staging`。Production 只得到遞迴 fmt 與 Python 文字斷言測試。

因此 production 的型別錯誤、無效 resource 引用或 replacement，可能在 Required CI 綠燈後
才於人工 production plan 被發現。現行 README 已提供 production 人工 plan／apply 指令，
這不是「完全沒有驗證」。建議至少為 production／bootstrap 增加隔離的 validate，
並決定是否提供使用獨立唯讀權限的 production plan gate；不需要自動 apply。

### R6 — Raw TTL 沒有涵蓋 server-side prepared body 副本

`backend/app/services/provider_work.py:200–209` 將完整交付 body 存入
`provider_work_prepared.body`。`backend/scripts/cleanup_raw.py` 只清理
`raw.market_payload`；未找到定期清除已結案 work 的 PostgreSQL prepared body 的路徑。
目前七天清理機制是 Fetcher 本機 journal／checkpoint，並不清理這張 server-side table。

結果是 raw 表雖有 30 天 TTL，完整交付內容仍可長期留在 PostgreSQL 的另一份副本。
這是容量與保存政策缺口，不能只看 raw 表大小估計完整 payload 的儲存量，也不能直接把
prepared rows 當作孤兒刪除：未結案、待排空、待 repair 的 bytes 是恢復證據。

建議先唯讀盤點 terminal work 的 body bytes／年齡／receipt 與非終態 run，決定
metadata／checksum 永久保存、body 保留或封存的期限，再訂安全清理條件。

其他生命週期候選：`calendar_import_batch` 預覽在 30 分鐘後拒絕套用，但未找到過期
candidate_rows 的清理；`admin_session` 到期／撤銷後也未找到定期清理。兩者需先區分
操作稽核需求與可回收內容，尚未量測線上累積量。

## 孤兒與可清理候選

以下以 tracked source、測試、scripts、CLI entrypoints、文件、workflow 和設定的全 repo
引用搜尋判定；不把「沒有 import」直接等同可刪除，也不能排除 repo 外的 operator 使用。

### Repo 內沒有消費者的定義

| 位置 | 定義 | 判定 |
| --- | --- | --- |
| `backend/app/models/base.py:144` | `get_session` | 只有定義；正式 DI 使用 `app.dependencies.get_db` |
| `backend/app/vocabulary.py:41` | `sql_in` | 只有定義 |
| `backend/app/schemas/full_market.py:268` | `FeedActivateRequest`／`FeedDeactivateRequest` | 無 route／service 使用 |
| `backend/app/services/full_market.py:1572` | `activate_feed`／`deactivate_feed` | 只有回 410 的內部函式，沒有掛載的 endpoint 或呼叫者 |
| `fetcher/src/findb_fetcher/finlab_scheduler.py:55` | `FinLabSchedulerError` | 只有定義 |
| `fetcher/src/findb_fetcher/finlab_universe.py:338` | `_exact_int` | 只有定義 |
| `fetcher/src/findb_fetcher/schedule.py:177` | `load_schedule_config` | 只有定義；保留它也不構成 runtime 使用舊 schedule 的證據 |
| `fetcher/src/findb_fetcher/schedule.py:429` | `_optional_date` | 只有定義 |

### 只剩測試引用，沒有正式 runtime 消費者

- `backend/app/services/full_market_admission.py:252` 的 `capacity_proof`。
- `fetcher/src/findb_fetcher/full_market_runtime.py:145` 的 `validate_capacity`，以及 `:159` 的 `split_rows`。
- `fetcher/src/findb_fetcher/account_governor.py:71` 的 `load_declaration`：測試仍 monkeypatch 它，
  但正式程式沒有呼叫；這類 patch 已不能證明對應路徑被隔離或覆蓋。
- `fetcher/src/findb_fetcher/daily_runtime.py:8` 的 `validate_policies`。
- `fetcher/src/findb_fetcher/scheduler_control.py:128` 的 `require_scheduler_stopped`。
- `fetcher/src/findb_fetcher/scheduler_state.py:712` 的 `validate_scheduler_state_read_only`。
- `fetcher/src/findb_fetcher/schedule.py:185` 的 `load_schedule_manifest` 在正式程式只由無消費者的
  `load_schedule_config` 呼叫；測試仍大量使用。不能據此刪整個 `schedule.py`，
  因為 `ScheduleConfig`／`ScheduleError` 仍由舊 historical executor 使用。
- `dashboard/src/features/operations/OperationsConsole.tsx` 是舊 re-export barrel，
  唯一外部引用為 `OperationsConsole.test.tsx`；正式 routes 已直接載入拆分模組。

上述測試不等於沒有價值，但應決定保留為明確相容契約，或與死程式一起移除／改為測試
實際 runtime，避免以測試通過推斷退役 gate 仍有效。

### 不應直接當孤兒刪除

- 退役 standalone CLI：仍在 `fetcher/pyproject.toml` 註冊，主動回報 retired，避免舊指令重新抓取。
- `legacy_checkpoints.read_taifex_pilot_checkpoint`：README／provider-work 文件明定的唯讀證據工具。
- `TwelveDataScheduledExecutor`、`FinLabScheduledExecutor`、靜態 universe 與舊 scheduler state：
  historical runtime 仍有無 cohort request 的相容分支，`docs/dev/backlog.md` 已追蹤排空後移除。
- `readiness_probe.py`：有可用的 `python -m` 入口，但只找到測試、沒有正式 runbook／部署使用；
  應先確認 operator 是否仍用，不列為可立即刪除的死模組。
- `backend/scripts/dev.ps1`：沒有 repo 內呼叫者，但本身是可獨立執行的 Windows wrapper。
- Shioaji 既有 canonical、prepared、receipts、repair 與 archive models：退役政策要求保留證據，
  未查線上資料前不能宣告為孤兒。
- `prepared_map`、`reset_source_rate_limit_state` 等測試輔助函式有明確測試用途，
  不以 production 無呼叫作為刪除理由。

## 文件漂移

- Root `AGENTS.md:154` 仍說 `DEBUG=false` 時 FastAPI 要求 `SOURCE_ALLOWLIST_CIDRS` 並拒絕啟動。
  現行 `Settings` 沒有此欄位／驗證器，API auth 也不做 CIDR 比對；部署入口由 Nginx allowlist
  負責。維運者不能依此舊文字假設直連 ingest 時仍有相同 IP 保護。
- `docs/operations/testing.md:64` 指向已不存在的 `test_shioaji_daily_runtime.py`，
  應改成現行相容／退役測試的實際入口。

## 已執行驗證

`UV_CACHE_DIR=/tmp/findb-review-uv-cache pnpm check` 通過全 repo 配置的 format、lint、type checks。
原預設 uv cache 因 sandbox 寫入限制失敗，改用可寫入的暫存 cache 後完成；不視為專案缺陷。

`UV_CACHE_DIR=/tmp/findb-review-uv-cache pnpm test` 完成：

| Suite | 結果 |
| --- | --- |
| Contracts／generated artifacts | 通過 |
| Backend 一般測試 | 2,233 passed、3 skipped |
| Backend migrations | 44 passed |
| Infra | 977 passed、3 skipped，另 203 subtests passed |
| Fetcher | 1,817 passed |
| Dashboard unit | 206 passed |
| Dashboard browser | 9 passed |
| Dashboard e2e | 15 passed |

總計 5,301 passed、6 skipped，另 203 subtests passed。
Backend 跳過的三項是缺少 `FINDB_FACTSET_NATIVE_SNAPSHOT_DIR` 的 sample／anomaly／full
原生快照驗收；Infra 跳過的三項需要指定已建置 image 的 `FINDB_TEST_BACKEND_IMAGE`。
未建置 production images、未執行真實 AWS/OpenTofu plan、未查核依賴 CVE，也未做供應商抓取。
測試 PostgreSQL 使用 repo fixture 的獨立暫存 database；競態重現也使用相同隔離機制，結束後刪除。

優先處理順序：R1 的 canonical 一致性、R3／R4 的登入邊界、R2 的跨層上限；
R5／R6 先確認驗證與保存政策。孤兒清理應獨立成小型 PR，保留相容性與證據用途的項目。
