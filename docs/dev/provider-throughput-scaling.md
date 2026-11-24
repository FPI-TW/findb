# Provider 吞吐擴充：多條執行線、並行與回補效率

狀態：設計草案，尚未實作。Owner 已決定的方向寫在「已定事項」；其餘待決事項列在文末。

## 背景

#564 的 FinLab 擴充有 170 張表、2,517 個 key，其中日更約 1,130 個，部分 key 還要回補長期歷史。
目前每個 provider 只有一個 consumer、一條執行緒，逐成員串行；同一 provider 的所有 dataset、
daily、historical 與名單更新共用一條 FIFO 和一個 owner。#504 已把單一 feed 的逐檔成本壓到約
0.33–1 秒，但 dataset 一多，串行本身就是上限。各 feed 現況與 Backend 上限的推估見
[Performance](../operations/performance.md) 的「2026-11-24 各 feed 瓶頸與 Backend 上限」（#672）。

## 已定事項（Owner，2026-11-24）

- 交付單位維持**逐成員**：每個成員、每個日期一筆 ingest，不改成批次參照交付。
- 同一 provider 改為**多條執行線**（lane），並讓**已到排程時刻的 daily 優先於 historical**，涵蓋 #615。
- 先完成現有 feed 的低風險修正（#668、#669、#672），本文件核准後再分 PR 實作。

## 逐成員模型下的容量推算

以下是推估，用來判斷需要多少並行度，不是承諾。

| 情境 | 單線串行 | 說明 |
| --- | --- | --- |
| 一個 TW 日更 dataset，約 2,300 檔 | 約 13 分鐘／天 | FinLab 日 K production 實測 0.33 秒／檔 |
| 一百個日更 dataset | 約 21–36 小時／天 | 超過一天，必須並行 |
| 一個 dataset 回補 20 年（約 4,900 個交易日、約 1,100 萬筆） | 約 42 天 | 每個日期另外重新載入整表 |
| FinLab 盤中 4 條 | 約 6.5 小時／天 | 每檔 4.7–5.4 秒，多為固定成本 |

並行之後依序會碰到的序列點：

1. **Fetcher 帳號鎖**：`acquisition.py` 的 `account_lock`（`fcntl.flock`＋RLock）從 `Acquisition.__enter__`
   持有到 native 呼叫（含子行程）結束，同一帳號只能一次一個請求。
2. **Fetcher host lock**：`provider_work.py` 的 `host_lock(provider)` 是 provider 層級的非阻塞鎖，同一主機上
   同一 provider 只有一個 consumer 能持有 work。
3. **Backend provider lock**：`app/services/provider_work.py` 的 `lock_provider()`（`pg_advisory_xact_lock`），
   work authorize、prepare 與 ingest 都在鎖內，推估約 550–650 檔／分鐘，與 Fetcher 並行度無關。
4. **Source rate limit**：每個 Source client 2,400／60 秒，daily 與 historical 共用，約 233 檔／分鐘。
5. **Normalization**：dataset advisory lock，worker 只有 2 個 prefork slot；EOD 約 200 runs／分鐘／dataset，
   盤中鎖內含 2 次 R2 往返，約 30–60 筆／分鐘／dataset。
6. **主機與 RDS**：FinDB 主機 `m7i.large`（2 vCPU）同時跑 ingest、worker、serve；RDS `db.t4g.medium`。
   每檔約 64 毫秒 ingest server 時間加 0.2–0.3 秒 normalization，多個 lane 同時跑時，總吞吐的上限可能
   是這一層，**必須先量測**。

即使以上都放寬，逐成員模型下長期回補仍以「天」為單位（例如 1,000 檔／分鐘時，1,100 萬筆約 7.6 天）。
回補範圍與順序需要依實測吞吐排定；若實測後仍不足，再由 Owner 決定是否調整交付單位。

## 設計

### 1. 多條執行線（lane）

- **目標**：同一 provider 的不同工作類型互不阻塞，例如 FinLab 盤中 6.5 小時不擋住表格型 dataset 的 daily。
- **定義**：lane 由 catalog 宣告在 feed 上（例如 FinLab：`table_daily`、`intraday`；historical 自成一條）。
  未宣告的 feed 屬於 provider 的預設 lane，行為與現在相同。
- **Owner 與鎖**
  - Backend：owner 由 `provider` 改為 `(provider, lane)`，`lock_provider()` 改為 lane 層級；FIFO 取用
    （`claim()` 的 `queue_order` 與 `accepted_kinds`）依 lane 分開。
  - Fetcher：`host_lock` 改為 `(provider, lane)`；consumer 拓撲依 lane 產生
    （`infra/deploy/generated/provider_inventory.v1.json`、`release_fetcher_provider.sh`）。
  - 同一 provider 帳號仍由同一個 governor 管理用量與 cooldown；lane 之間只共用帳號，不共用 owner。
- **遷移**：需要 migration（owner／attempt 加上 lane），部署順序 Backend 先、Fetcher 後；rollback 下限
  寫入 `docs/operations/deployment.md`。既有 work 全部屬於預設 lane。

### 2. Daily 優先（#615）

- 取用下一筆工作時，已到排程時刻的 daily 排在同 lane 的 historical 前面。
- historical 要有最低進度保證（例如每完成 N 筆 daily 至少放行一筆 historical，或 historical 自成一條 lane），
  避免一直排不到。
- 一筆 historical request 不一次佔滿整條佇列：依日期區段分批入列。

### 3. Fetcher 並行

- **帳號鎖範圍**：只包 native 呼叫，不包 R2 PUT 與 Source 交付，讓交付和下一檔的抓取可以重疊。
- **抓取與交付重疊**：同一 lane 內最多一檔在途，背景執行緒只負責交付。
- **有限並行**：同一 lane 內 N 檔同時抓取，N 由 catalog 宣告，預設 1。
- **必須保持不變**：prepared／receipt 的順序與 crash replay、stop 掃描、50 毫秒 dispatch freshness
  （`LOCAL_DISPATCH_SECONDS`）。Fetcher 主機是 burstable 的 `t3`，並行前要確認 CPU credit 模式，避免降頻造成
  dispatch 授權連續逾時。

### 4. FinLab 盤中

| 項目 | 內容 | 預估效果 |
| --- | --- | --- |
| 長駐 SDK worker | 一輪只開一個子行程，處理多個 op；每個 op 保留 READY／deadline 握手（`provider_worker.py`）；子行程內用 `requests.Session` 重用連線，比照 `finlab_intraday.py` 既有的 SDK patch 手法 | 每檔省 1–1.5 秒，約 4.5–5 小時 |
| 抓取與交付重疊 | 第 3 節的重疊 | 加上前項約 2.5–3 小時 |
| 有限並行 | N=2–4；前提是 FinLab 允許的並行數，以及 Backend 盤中鎖改為 `(dataset, symbol)`（`store_day` 已有 row lock 與 etag 條件寫入） | 約 1–1.5 小時 |
| 盤中回補 | 一次查一檔 31 天（SDK 上限），一次簽章最多 5 個分區；交付仍一檔一天一筆 | 呼叫次數約少 20 倍 |

長駐 worker 也能讓 FinLab 日 K 的股票與 ETF 共用同一次整表載入：目前子行程只回傳各自名單的欄位，
股票與 ETF 的結果不同，無法在父行程共用（2026-11-24 評估，每天約省 15–50 秒，不單獨做）。

### 5. 回補效率（交付仍逐成員）

- **整表只載入一次**：`member_historical.py` 每個日期結束都 `adapter.reset()`，FinLab 每個日期重新載入
  5 張整表；改為同一個 request 的連續日期共用，搭配第 4 節的長駐 worker。
- **日期區段排程**：historical request 以區段（例如一個月）入列，讓 daily 優先規則有空檔可插入。
- **整表快取**：放在主機 volume，跨 dataset 共用；失效條件為 SDK 版本、表的版本與 `final_after`。

### 6. 配額預算

- FinLab 每天 5,000 MB，staging 與 production 共用同一帳號。依 `finlab_quota` 實測，governor 為 daily 保留
  額度，剩下才給回補；staging 不做第一次的整表下載。
- 分鐘配額不足時改為等到下一分鐘，不中止整輪（`acquisition.py` 目前直接丟 `QuotaBlockedError`）。
  Twelve Data 並行或回補加速後才會碰到。

### 7. Backend 容量

- 依每檔 RPC 數重新設定 Source `rate_limit_requests`；historical 改用獨立的 Source client。
- worker concurrency 加大，或依 lane／dataset 分 queue。
- ingest 的 uvicorn worker 數與 DB pool（目前 1 個 worker、3+5），避免持鎖等待佔滿連線池。
- 先建立主機與 RDS 的量測方法：同時跑 2–3 條 lane 時的 CPU、DB 連線、`server_ms`、`queue_wait_ms`。

### 8. 共用的本機 SQLite（多條執行線上線前完成）

2026-11-24 production 的 FinLab 1 分 K 第一輪，因為另一個 provider 的每日 journal 清理在寫鎖內掃描整個
共用 journal（約 1 GB），超過當時 5 秒的等鎖上限而結束整輪。lane 會讓寫入者變多，所以以下列為正式設計項目：

| 檔案 | 現況（2026-11-24 production） | 設計 |
| --- | --- | --- |
| provider-work journal | 12 個 consumer 共用一個檔，約 1.0 GB | 每個 provider 一個檔（fetcher-v0.1.51）；舊共用檔只排空。同一 provider 的 lane 共用該 provider 的檔 |
| Full checkpoint `state.sqlite3` | 6 個 daily consumer 共用，約 165 MB | 每個 provider 一個檔；部署停止窗口內由 release helper 搬移該 provider 的列並核對筆數 |
| 帳號 governor | 12 個 consumer 共用，約 430 MB，約一半是未回收空頁 | 依帳號共用，維持一個檔；部署停止窗口內於主機上 VACUUM |

- 所有連線等鎖上限一致為 30 秒；SQLite 鎖逾時只延後該成員，不結束整輪（#698）。
- 每日清理不在寫鎖內掃描整個檔，刪除分批提交（#698）。
- 監控：SQLite 檔大小、Fetcher 主機剩餘磁碟、`local_state_busy` 診斷次數送 CloudWatch 並建告警。

## 新增 worker 的選項與風險評估

現在替同一 provider 多開 consumer 不會變快：Fetcher 的 `host_lock(provider)` 是非阻塞鎖，拿不到的 consumer
只會閒置；Backend 每個 provider 也只有一條 FIFO、一個 owner。不同 provider 之間本來就並行（12 個 consumer），
卡住的是同一 provider 底下有很多 dataset，FinLab 正是如此。

### 選項

| 做法 | 要改的地方 | 效益 | 風險 |
| --- | --- | --- | --- |
| A. 多條執行線（本文件的方向） | Backend owner／FIFO 依 lane（migration）、Fetcher host lock、consumer 拓撲 | FinLab 表格 daily 與盤中各一條，daily 不再被盤中擋住 | 中：改動大但語意清楚 |
| B. 把 FinLab 拆成兩個 provider 名稱 | catalog、credential、governor | 表面上改動較小 | 高：provider 名稱寫進 canonical 的 `source`、source precedence 與 credential scope，等於改資料身分。不採用 |
| C. 只把 normalization worker 從 2 調到 3–4 | `docker-compose.prod.yml` 的 `--concurrency` | 只在多個 dataset 同時 normalize 時有用；目前 worker 跟得上，幾乎沒有效益 | 低：多佔 DB 連線與 CPU |

做法 A 的效益（推估）：帳號鎖不改時 provider 呼叫仍一次一個，但兩條 lane 可以錯開，一條交付給 Source 時另一條在
抓取。交付佔每檔時間的一大半，FinLab 盤中約可從 6.5 小時降到 3.5–4 小時，daily 的完成時間不再受盤中影響。

### 做法 A 的風險

| 風險 | 說明 | 緩解 |
| --- | --- | --- |
| Provider 帳號限制 | FinLab 並行限制未知；FinLab 與 Twelve Data 帳號 staging 與 production 共用 | 帳號鎖先維持一次一個請求，對 provider 的請求頻率不變；確認 FinLab 限制後再放寬 |
| Source rate limit | 同一把 key 2,400／60 秒，兩條 lane 一起跑會先撞到；429 帶 `Retry-After: 60`，整條 lane 會停住 | 依實際每檔 RPC 數調高 `rate_limit_requests`，或 historical 改用另一把 key |
| Backend provider lock | 不改的話所有 lane 在 Backend 仍排成一列，約 550–650 檔／分鐘 | 同一次改為 lane 層級的鎖 |
| FinDB 主機與 RDS | 主機 2 vCPU 同時跑 ingest、worker、serve；RDS `db.t4g.medium` 有 CPU credit，額度用完會降速；Serve 延遲可能一起變差 | 先在 staging 量兩條 lane 同時跑時的 CPU、DB 連線數與 Serve 延遲 |
| Fetcher 主機記憶體 | `t3.medium` 4 GiB、容器沒有記憶體上限；FinLab 日 K 整表載入子行程峰值約 940 MiB，與盤中同時跑可能用到 swap | 量測峰值；必要時讓兩條 lane 避開同時載入整表，或換機型 |
| 共用的本機 SQLite | governor、journal、Full 狀態由多個 consumer 共用，寫入者變多會互等鎖；consumer 的 `/tmp` 只有 16 MiB（#572） | 見第 8 節，多條執行線上線前完成；大量刪除與排序維持不使用 `/tmp` |
| 正確性 | 每條 lane 的 prepared／receipt、crash replay、stop 掃描、daily 結算都要維持；`expected = data + no_data + missing + blocked` 不能錯 | 依本文件驗收矩陣；staging 一次只啟動一個 provider |
| 部署 | 需要 migration 與 rollback 下限，Backend 先、Fetcher 後；consumer 變多，告警與 host lock 登記要跟著加（#642） | 分 PR 上線，每一步先在 staging 驗收 |

### 建議

1. 最小的一步：FinLab 拆成表格 daily 與盤中兩條 lane。帳號鎖不動；同一次調高 Source rate limit、Backend 鎖改為
   lane 層級；在 staging 量 FinDB 主機、RDS 與 Fetcher 記憶體。
2. 數字可接受後，再考慮更多 lane 與縮小帳號鎖範圍。
3. 做法 C 等量測顯示 normalization 跟不上時再調。

## 待 Owner 決定

1. **授權類 RPC 合併或快取**：先前決定暫不做。每檔約 5 次（control poll、full-market authorization、
   work authorize），約 0.3–0.6 秒，佔逐檔成本的一半左右；擴充後這部分成本按成員數放大。
2. **lane 的切分方式**：依工作類型（daily／intraday／historical），還是依 feed 宣告。
3. **盤中並行數**：需向 FinLab 確認並行限制與盤中下載是否計入每日配額。
4. **配額分配**：回補的每日 MB 預算，以及 staging 的上限。

## 驗收矩陣

每個實作 PR 都要涵蓋：

- stop／resume、crash replay、prepared drain、no-data 輪次、DQ 失敗只擋該成員。
- `expected = data + no_data + missing + blocked`，summary 與完整 `reconcile_plan` 一致。
- lane 之間互不阻塞；daily 優先；historical 不會一直排不到。
- staging 一次只啟動一個 provider，首日先調成單日；以 `delivery_timing` 比較每檔耗時與 RPC 數。
- 多條 lane 同時跑時，各 SQLite 檔的等鎖時間與 `local_state_busy` 次數；不得出現因本機鎖結束整輪。

## 建議的 PR 順序

0. 共用的本機 SQLite（第 8 節）：journal 分檔、Full checkpoint 分檔、監控與 governor VACUUM。
1. Backend 容量量測（不改行為）：兩條 lane 同時跑的模擬負載與指標。
2. Lane 資料模型與 Backend owner／FIFO（migration），含 daily 優先。
3. Fetcher host lock 與 consumer 拓撲依 lane 產生。
4. FinLab 盤中長駐 SDK worker。
5. 帳號鎖範圍縮小與抓取、交付重疊。
6. 有限並行與 Backend 盤中鎖改為 `(dataset, symbol)`、worker 與 rate limit 調整。
7. 回補日期區段與整表快取、配額預算。
