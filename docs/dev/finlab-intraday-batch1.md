# FinLab 盤中資料導入（#564 第 1 批）設計

> 狀態：設計上的決定已由 Owner 於 2026-11-16 確認（見「已定事項」）。實作進度見「工作拆分」。追蹤 issue：
> [#564](https://github.com/FPI-TW/findb/issues/564)。
> 完成後依 `docs/dev/` 的規則，把仍有效的內容整併到 `docs/` 的現行文件並刪除本檔。

## 範圍

第 1 批導入三項台股盤中資料，來源都是 FinLab：

| 項目 | FinLab 名稱 | 這一批的目標 |
| --- | --- | --- |
| 個股 1 分 K | `tw_minute` | 每日更新 |
| 逐筆成交 | `tw_tick` | 每日更新 |
| 盤中 60 分鐘行情 | `tw_price_60min` | 可查詢；由 1 分 K 彙總，不另外抓取（見「60 分鐘行情」） |

Owner 已定的事項：

- 先做每日更新，確認每天能穩定抓到。逐筆成交暫不回補歷史；1 分 K 的回補另行決定。
- 需求表上的群組只是規劃時的分類，實作不以群組為單位。
- 儲存方式參考既有的 K 棒收集文件：行情以 Parquet 檔保存，依標的與月份分檔，先暫存再合併。該文件關於
  Shioaji 的抓取規則不適用。

不在這一批：歷史回補、其他每日更新的資料（第 2 批）、Shioaji 時期分 K 的搬移。

## FinLab 盤中資料的實測行為

2026-11-16 以正式帳號、SDK 2.3.11 實測。除註明外，樣本是 2330 在 2026-11-13 的資料。

| 項目 | 1 分 K（`tw_minute`） | 逐筆成交（`tw_tick`） |
| --- | --- | --- |
| 取得方式 | `data.get("tw_minute:<代號>", start, end)` | `data.get("tw_tick:<代號>", start, end)` |
| 一次請求的範圍 | 一檔標的，最多 31 個日曆日 | 同左 |
| 資料單位 | 標的 × 交易日，一個 Parquet 檔，附列數、大小與 SHA-256 | 同左 |
| 權限 | 現有帳號可取 | 同左 |
| 歷史起點 | 2023-01-03 | 2023-01-03 |
| 一天的列數與大小 | 266 列，約 14 KB | 7,801 列，約 221 KB（0050：20,449 列，約 589 KB） |
| 計入配額 | 約 0.014 MB | 約 0.21 MB |
| 欄位 | `stock_id`、`trade_date`、`timestamp`、`session`、`open`、`high`、`low`、`close`、`volume`、`tick_count`、`vwap` | `stock_id`、`trade_date`、`timestamp`、`sequence`、`ts`、`close`、`volume`、`tick_type`、`bid_price`、`bid_volume`、`ask_price`、`ask_volume`、`session` |
| 時間 | `timestamp` 帶 `Asia/Taipei` 時區，是該分鐘的**開始**時間 | `timestamp` 帶時區，到微秒；`sequence` 是當日序號 |
| 時段 | 只有 `regular` | `regular`，另有盤後定價 `after_hours_fixed`（14:30） |

其他行為：

- **只有有成交的分鐘才有 1 分 K。** 2330 是 09:00–13:24 每分鐘一列，加上 13:30 的收盤撮合；1101B 當天只有 1 列。
- **價格未還原**（`price_adjustment=none`）。
- **成交量單位是張**（回應標為 `provider_native`）。2330 當天 1 分 K 合計 28,790，逐筆一般時段合計相同，盤後定價另有
  25；日 K 的成交股數是 30,973,127 股。差額約 7%，來源未確認（可能是零股與鉅額交易）。
- **回應會標明休市日與當日無成交。** 清單裡每個交易日的狀態是 `ready` 或 `no_trades`，另列 `closed_dates`。
- **有「已驗證日曆」的界線。** 查詢超過 FinLab 已驗證的最新交易日會被拒絕（`not_ready`），只查週末也一樣。
- **不存在的代號與尚未發布回同一種錯誤**（`not_ready`），無法由回應區分。
- **涵蓋上市、上櫃、ETF（含主動式）與特別股。**
- **每次請求的往返**：一檔一天要一次清單請求、一次簽章請求與一次下載，從開發機實測合計約 2.6 秒。

粗估的每日配額（以樣本推算，未取樣全市場）：1 分 K 約 40 MB，逐筆約 230–350 MB；帳號上限是每日 5,000 MB，
staging 與 production 共用。

## 現有分 K 管線的現況

系統有一套為 Shioaji 建的 1 分 K 管線，Shioaji 退役後只供查詢歷史（production 的資料到 2026-10-30）：

- 契約 `market_minute.v1` 以「分批快照」交付：每批最多 50 檔，實際上限 5,000 列與 1 MB，並要求整組快照到齊。
  成交筆數必須是空值，成交量以股為單位，空的成交金額要附異常說明。
- 資料表 `market_data_minute` 在 PostgreSQL，限定每根剛好 1 分鐘；主鍵不含來源，同一根 K 只有一筆，後到的覆蓋先到的。
  表有依交易日分割的設計，但只建了預設分割區。
- 完成度核對對兩條分 K dataset 寫死了「快照組完整才算有資料」。
- Provider catalog 的 feed 只接受日頻。兩條分 K dataset 目前不接受任何 provider。
- 逐筆成交沒有契約、資料表與查詢。
- `findb-<環境>-canonical` 的 R2 bucket 與「publisher 可讀寫、Serve reader 唯讀」的 credential 邊界已建立，但沒有任何
  讀寫它的程式（backlog 的 Canonical R2 項目）。Backend 目前不讀寫任何物件儲存。

## 設計

### 儲存：Canonical R2 上的 Parquet

> 這一節已實作。現行行為以 [`docs/architecture/intraday_storage.md`](../architecture/intraday_storage.md)
> 為準，以下保留設計的理由與估算。

盤中資料不進 PostgreSQL 的行情表，改存 Canonical R2。PostgreSQL 只保存「哪一檔、哪一天、有沒有資料」的索引。

理由：逐筆成交全市場每天粗估數百萬列，一年十億列以上，放進 PostgreSQL 會讓主資料庫的大小與備份時間由它主導；
FinLab 提供的本來就是「標的 × 交易日」的 Parquet 檔，與參考文件「先暫存、再合併成月檔」的做法直接對得上。

**物件配置**（沿用參考文件的分檔方式）：

```text
intraday/v1/<dataset_key>/symbol=<代號>/year=<YYYY>/month=<MM>/part.parquet                 # 已結束的月份
intraday/v1/<dataset_key>/symbol=<代號>/year=<YYYY>/month=<MM>/day=<DD>/part.parquet        # 當月
```

已結束的月份，每個 dataset、每檔標的、每個月一個檔。以 2,300 檔計，一年約 5.5 萬個物件；1 分 K 的月檔約數百 KB，逐筆的月檔
熱門標的約 5–12 MB。

**欄位**：

| 1 分 K | 型別 | 說明 |
| --- | --- | --- |
| `symbol` | string | 保留前導 0 |
| `trade_date` | date | 台北日期 |
| `bar_start_time` | timestamp（UTC） | 該分鐘的開始時間 |
| `session` | string | `regular` |
| `open`、`high`、`low`、`close` | double | 未還原 |
| `volume` | int64 | 張，照 FinLab 的原值 |
| `trade_count` | int64 | FinLab 的 `tick_count` |
| `vwap` | double | FinLab 提供的均價 |

| 逐筆成交 | 型別 | 說明 |
| --- | --- | --- |
| `symbol`、`trade_date` | 同上 | |
| `trade_time` | timestamp（UTC，微秒） | |
| `sequence` | int64 | 當日序號，與 `trade_time` 一起決定順序 |
| `price` | double | FinLab 的 `close` |
| `volume` | int64 | 張，照 FinLab 的原值 |
| `tick_type` | int8 | 照 FinLab 的值保存，語意待確認 |
| `bid_price`、`ask_price` | double | 可為空 |
| `bid_volume`、`ask_volume` | int64 | 張，可為空 |
| `session` | string | `regular`、`after_hours_fixed` |

**寫入流程**（暫存 → 合併）：

1. Fetcher 把 FinLab 回傳的「標的 × 交易日」Parquet 原樣存進 Raw R2 作為證據，另外把它轉成上表的欄位
   （時間轉 UTC、欄位改名），也存進 Raw R2，這就是暫存檔。
2. Backend 的 normalization worker 讀取暫存檔、驗證欄位與內容，寫成該標的該交易日的日檔。重抓同一天時
   **整天替換**。Backend 不解析 FinLab 的原始檔。
3. 寫入成功後，在 PostgreSQL 記錄這個「標的 × 交易日」的索引，同一個 transaction 寫入交付證據。
4. 月份結束後，把該月的日檔合併成月檔並刪除日檔。查詢時當月讀日檔，過去的月份讀月檔。

當月不直接改寫月檔的原因見「硬體需求」：每天重新上傳整個月檔的流量與 worker 時間都不必要。

成交量的單位是張，與參考文件相同，但與系統內其他行情（日 K、舊分 K 以股為單位）不同；查詢的回應要標明單位。

**相對參考文件修正的四點**（都是該文件列為風險的項目）：

| 參考文件的做法 | 這裡的做法 | 原因 |
| --- | --- | --- |
| 時間存成不帶時區的台北時間 | 一律存 UTC，另存台北的 `trade_date` | 原做法的實際值取決於執行環境的時區，換到 UTC 主機會偏移 8 小時並產生重複資料 |
| 以（標的、時間）逐列覆蓋，不刪除舊列 | 以（標的、交易日）整天替換 | 重抓同一天時，新回應裡已不存在的列不會殘留 |
| 空回應一律當成略過 | 只有 FinLab 明確標示 `no_trades` 或休市才記為無資料；其他空回應是失敗 | 被限流或配額用盡時的空回應不會被當成「已補完」 |
| 目錄值會被讀取工具推斷成整數（`0050` 變 `50`） | 檔案內的 `symbol` 是字串，查詢一律經 Serve，不讓下游直接讀目錄 | 避免下游各自踩到型別推斷 |

**PostgreSQL 的索引表**（`intraday_partition`）：每個（dataset、標的、交易日）一列，只有有資料的日子才有列；
當日無成交不建立索引，沿用既有的 no_data 證據。記錄列數、來源、FinLab 原始檔與暫存檔各自的 SHA-256、抓取時間、
所在物件（日檔或月檔）的鍵與版本、對應的 `run_id`。它有三個用途：完成度核對、Serve 決定要讀哪些檔、重抓時判斷
是否已是同一份資料。

**並行**：同一個 dataset 的正規化與月底合併都在既有的 dataset 鎖之下執行，同一時間只有一個寫入者。合併前核對
各日檔的版本與索引記錄一致，不一致時該月份不合併、不覆蓋，下一輪再試。合併由 Worker 在沒有等待中的
normalization 時執行，不需要新的服務或排程。

### 交付契約：以物件參照交付

新增一個 provider-neutral 契約 `intraday_partition.v1`，一個請求代表一檔標的的一個交易日。欄位的現行說明在
[`docs/architecture/ingress_contracts.md`](../architecture/ingress_contracts.md)：

- 內容：dataset、標的與來源代號、交易日、資料種類（1 分 K 或逐筆）、列數、暫存檔的物件參照、大小與 SHA-256、
  FinLab 原始檔的參照與 SHA-256、來源宣告（價格未還原、成交量單位是張）。
- 不在請求裡帶資料列。Backend 依參照讀取 Raw R2 的暫存檔，核對大小、SHA-256、列數與欄位後才處理。
- 暫存檔的欄位由契約定義並隨契約發布，Fetcher 與 Backend 都以它為準。
- 每個 dataset 宣告自己保存的資料種類；1 分 K 的 dataset 不接受逐筆的分區，反之亦然。
- 「當日無成交」沿用現有的 no_data 回報，證據是 FinLab 清單回應的摘要。

不沿用 `market_minute.v1` 的理由：它的分批快照、50 檔一批、成交筆數必須為空、要有成交金額，都與 FinLab 逐檔
交付、有成交筆數、沒有成交金額的資料不合；逐筆成交單檔一天就超過 Source 的 5,000 列與 1 MB 上限。

這個做法需要一項新的權限：**Backend 要能唯讀 Raw R2**。現行邊界是 Backend 不碰物件儲存；Canonical R2 的
publisher 與 Serve reader 兩組 credential 則是原本就規劃好、這次才開始使用。

### Fetcher

已實作（`providers/finlab_intraday.py`、`provider_adapters/finlab.py`），在 catalog 加入 feed 之前不會被執行。

- 取表的子程序新增操作 `finlab.intraday`：對一檔標的、一個交易日取清單並下載，把 FinLab 的欄位轉成契約發布
  的欄位，回傳原始檔、轉換後的暫存檔與清單摘要。不寫 SDK 的本機快取，也不進工作 journal；兩個檔直接存進
  Raw R2。認證與帳號治理沿用取表的做法。
- 轉換只改名稱與型別，不改數值：時間轉成 UTC、交易日轉成日期、代號改用 FinDB 的代號；FinLab 的 `ts` 欄
  與 `timestamp` 重複，不保留。原始檔的欄位、列數、代號或日期與清單不符時不轉換。
- Adapter 沿用現有的「逐成員取得」介面，依 dataset 分流；日 K 的路徑不變。失敗分類：

  | FinLab 的回應 | 處理 |
  | --- | --- |
  | 清單狀態 `ready` | 下載、存 Raw R2（原始檔與暫存檔）、交付 |
  | 清單狀態 `no_trades` | 回報當日無成交，清單摘要存 Raw R2 作為證據 |
  | `not_ready`，且交易日晚於已驗證日曆 | 整個 plan 等待發布，不記在成員身上 |
  | `not_ready`，交易日已在已驗證日曆內 | 只記在該成員：保留有限次數的重試，其他成員繼續；連續多檔如此時整個 plan 暫停一段時間再試 |
  | 認證、SDK 介面不符 | 整個 work 結束 |
  | 配額、網路、逾時 | 整個 plan 稍後重試 |

  第四列的兩種原因無法由回應區分：該檔的分區還沒建好，或 FinLab 沒有這個代號。2026-11-16 實測，日曆在
  16:26–16:36 之間納入當天，分區在 16:59 左右才建好，中間約半小時所有代號都是這個回應；不存在的代號（如 9999）
  與休市日也是同一個回應。

- 名單沿用台股日 K 的官方名單（股票與 ETF），但不做日 K 那種「FinLab 是否有這檔」的檢查：FinLab 沒有盤中
  代號的清單，而且有些代號盤中有資料、日 K 的表還沒有（例如 2026-11-16 新上市的 7848）。所有分類正常的成員
  都會嘗試，FinLab 沒有的代號由它自己的請求發現。
- 每個分區帶有 FinLab 的版本編號 `generation`（看起來是發布時間的微秒數），隨清單摘要保存。
- 耗時與資源（2026-11-16 以真實 API、FinLab 映像檔實測）：每個分區 2.4–2.9 秒（清單、簽章、下載、轉換），
  子程序記憶體尖峰約 160 MB。0050 當天的逐筆 47,380 列，原始檔 1.36 MB，轉換後 0.56 MB。兩千三百檔逐一抓，
  單一 dataset 約需一個半到兩小時；需要時再加並行，並行數是否受 FinLab 限制尚未確認。
- 契約與映像檔的一致性：FinLab 映像檔的 CI 檢查會以映像檔內的 Arrow 把一筆樣本轉成映像檔內契約的欄位，
  型別字串不一致就失敗。

### 排程與完成度

- Provider catalog 放寬 feed 的頻率，新增四條 feed：股票與 ETF 各一條 1 分 K、各一條逐筆。
- 完成度：成員「有資料」的依據改為索引表有該（標的、交易日）的列，不沿用分批快照的判定。
  `expected = data + no_data + missing + blocked` 的定義不變。
- 排程時刻：台北 18:30，完成期限隔日 06:30（所有 feed 都是開始後 12 小時）。2026-11-16 實測 1 分 K 與逐筆約 17:00 發布；排在日 K（18:00）
  之後，因為一個 provider 的工作由同一個 consumer 依序執行，四條盤中 feed 合計要數小時，不能排在日 K 前面。
  同一個交易日的工作依排程時間排入佇列。
- 新的 dataset 另起名稱（暫名 `tw_equity_bar_1m`、`tw_etf_bar_1m`、`tw_equity_tick`、`tw_etf_tick`），不沿用
  `tw_equity_minute`／`tw_etf_minute`。舊的兩條維持現狀，只供查詢 Shioaji 時期的歷史；部署文件已載明替代 provider
  不得沿用舊的收據與凍結計劃作為成功證據。

### Serve

新增唯讀查詢，以 Serve reader 的 credential 讀 Canonical R2：

- 1 分 K：指定標的與日期範圍，分頁回傳；可指定彙總刻度（1 分、60 分）。
- 逐筆成交：指定標的與單一交易日，分頁回傳。
- 先查索引表決定要讀哪些檔；沒有索引的日期不讀檔。
- 單次查詢有列數與日期範圍上限，避免一次讀出整月的逐筆。

既有的 `/minute` 不變，仍只讀 PostgreSQL 的舊資料。兩者要不要合併成一個查詢，見待決定事項。

### 60 分鐘行情

不另外抓 `tw_price_60min`，由 1 分 K 在查詢時彙總。

依據：2026-11-16 以 2026-11-13 的資料比對，FinLab 自己的 60 分鐘行情與由它的 1 分 K 彙總的結果，2330、0050、6488
的開高低收與成交量完全相同；1101B（當天只有一筆成交）價格相同，差別只在 FinLab 對沒有成交的時段另外保留一列
成交量 0。

彙總規則照 FinLab 的定義：以區間結束時間標示，10:00、11:00、12:00、13:00，最後一根是 13:00 到收盤，標為 13:33。

好處：少一條抓取與一套資料表；`tw_price_60min` 的歷史只從 2026-01-02 起，彙總則可回溯到 1 分 K 的起點；整張表下載
一次要約 25 MB 配額。其他刻度（5 分、15 分、30 分）之後可以用同一個方式提供。

## 硬體需求

2026-11-16 唯讀查核的現況：

| | Staging | Production |
| --- | --- | --- |
| Backend 主機 | t3.large：2 vCPU、7.8 GB，可用約 6.3 GB | m7i.large：2 vCPU、7.8 GB，可用約 5.9 GB |
| Normalization worker | 2 個並行 | 2 個並行，使用約 340 MiB |
| RDS | db.t3.micro，20 GB，資料庫 520 MB | db.t4g.medium，100 GB（可自動擴到 500 GB），資料庫 1.6 GB |
| Fetcher 主機 | t3.small：1.9 GB，可用約 1 GB；磁碟可用 7.9 GB | t3.medium：3.8 GB，可用約 2.6 GB；磁碟可用 13 GB |
| FinLab container 記憶體上限 | 沒有上限（原本的 1 GiB 已移除） | 沒有上限 |

照本文件建議的做法，**兩個環境都不需要升級硬體**。估算如下，逐筆的數字由幾檔熱門標的推算，實際應較低：

| 資源 | 每日新增 | 一年 | 說明 |
| --- | --- | --- | --- |
| Canonical R2 | 1 分 K 約 40 MB、逐筆約 300 MB | 約 85 GB | 以 R2 的牌價計約每月 1–2 美元 |
| Raw R2 | 原始檔與暫存檔各一份，約為上列的兩倍 | 保存 90 天，穩定在約 60 GB | 以 R2 的牌價計每月不到 1 美元 |
| RDS：索引表 | 約 4,600 列，約 1 MB | 約 0.3 GB | |
| RDS：每筆交付的流程紀錄 | 約 4,600 筆交付，約 30 MB | 約 7–8 GB | 現有每筆交付在各流程表合計約 6.5 KB，其中 raw 的部分 30 天後刪除 |
| Backend worker | 約 4,600 個工作，每個 1–2 秒，以讀寫 R2 為主 | — | 2 個並行約 40–80 分鐘；讀寫 Parquet 的套件讓每個 worker 多用約 100–150 MB |
| Fetcher | 逐檔執行約每檔 3–4 秒；1 分 K 與逐筆合計每天約 4–5 小時 | — | 記憶體不需載入整張表，逐檔執行時不增加；每檔多上傳一個暫存檔，合計每天約多 12 分鐘（估算，未實測） |

成立的前提（要寫進實作）：

- **Fetcher 不在本機保留盤中資料。** SDK 預設會把下載的檔存進本機快取，工作 journal 也會保存回應；兩者都要關掉或改成
  只留 Raw R2 的參照，否則每天各多約 340 MB，Fetcher 主機的磁碟撐不了多久。
- **當月先存每日一檔，月份結束才合併成月檔。** 每天改寫整個月檔的話，逐筆的月檔每天要從 AWS 重新上傳，合計約 3 GB；
  改成月底合併可以省掉這部分的流量與 worker 時間。查詢時當月讀日檔、過去的月份讀月檔。
- **需要並行抓取時，並行數受記憶體限制。** 每個取得用的子程序約 150 MB；staging 主機平時可用約 0.9 GB，最多約 4 個，
  production 可以到 8 個。

不同決定下的硬體影響：

| 若改成 | 影響 |
| --- | --- |
| 1 分 K 存進 PostgreSQL | 現有分 K 表每列約 340 bytes（含索引）；全市場每天最多約 62 萬列，一年約 30–50 GB |
| 逐筆也存進 PostgreSQL | 每天數百萬列、約 1 GB，一年 250 GB 以上；100 GB 的配置撐不到半年，RDS 的容量與等級都要升 |
| 不讓 Backend 讀 Raw R2，資料列以 JSON 經 Source 送入 | 收到的原文會在 RDS 的 raw 表保存 30 天，逐筆每天約 1 GB，穩定佔用約 30 GB；Backend 的接收與正規化負載同步增加 |
| 回補 1 分 K 到 2023-01-03 | Canonical R2 約 30 GB；若每個「標的 × 交易日」各算一筆交付，RDS 的流程紀錄約增加 10 GB，回補應改成較大的交付單位 |

## 驗收情境

沿用 #564 評估留言列的情境，套用到這一批：

| 情境 | 這一批要驗的內容 |
| --- | --- |
| 部分資料品質失敗 | 一個交易日的檔有不合法的列時，整天不寫入，索引不建立，成員回報失敗 |
| 重複與亂序交付 | 同一份來源檔重送不改寫已有的檔；較舊的來源檔晚到不覆蓋較新的 |
| 修訂覆寫 | FinLab 重新發布某天的檔（清單的版本改變）時整天替換，索引記下新的 SHA-256 |
| stop／resume | 停止後不開始新的抓取；已交付但未合併的項目能完成；恢復後不遺漏、不重複 |
| crash replay | 在存 Raw R2、送出、接受、寫日檔、寫索引、月底合併之間任何一步中斷，都能由持久化狀態恢復；日檔已寫但索引未寫時重做不產生重複資料 |
| daily 與 historical 競爭 | 這一批沒有回補；確認盤中資料的每日工作不延後日 K 的 daily |
| 舊 Fetcher 的相容與回退 | Backend 先部署並接受新契約；Fetcher 回退後不再送新契約，已寫入的檔與索引不受影響 |

另外兩項這一批特有的：

- 月底合併與同月份某一天的重抓同時發生時，月檔不遺失任一天，也不留下已被取代的那一天。
- Serve 查詢與日檔替換或月底合併同時發生時，讀到的是替換前或替換後的完整資料，不是半份。

## 工作拆分

依序進行，每項一個 PR。

1. Backend：Canonical R2 的讀寫元件、Raw R2 的唯讀元件與設定；基礎設施補上對應的 credential。（已完成，
   staging 已驗證真實 R2 的讀寫）
2. Backend，分兩個 PR：
   1. `intraday_partition.v1` 契約、索引表的 migration 與資料模型。（已完成）
   2. Normalizer（驗證、寫日檔、索引與證據）與月底合併。（已完成）
3. Backend 的準備，不改 catalog：catalog 與排程發布接受 `minute`／`tick` 頻率；Serve 的 dataset 檢查認得
   盤中 dataset；plan part 對盤中分區核對幣別與 provider 代號；以整合測試確認 plan 成員由索引表經交付證據
   計為有資料。
4. Fetcher：盤中取得的子程序操作（含欄位轉換）、adapter、Raw R2 的 Parquet 格式、四條 dataset 的名單（沿用
   台股日 K 的官方名單）。合併後不會被執行，直到下一項把 feed 加進 catalog。
5. 切換，Backend 與 Fetcher 同一個 PR：catalog 加入四條 feed、seed 與 registry provisioning 的四個 dataset、
   FinLab Source credential 範圍的 migration、排程的 dataset 對應，以及所有生成產物。限制見下一節。
   （已完成：以真實的 Fetcher 執行迴圈對真實的 Source 與 Worker 的整合測試，確認排程規劃、抓取、交付、寫入
   與計劃計數整條路徑。第一版把四個 dataset 建成未啟用，但系統沒有啟用 dataset 的操作，已由後續的 migration
   改為啟用，改以 baseline 作為逐條開放的關卡。）
6. Backend：Serve 的 1 分 K（含彙總刻度）與逐筆查詢；Dashboard 的查詢頁視需要另開。
7. Staging 驗收：先只開股票的 1 分 K，再開 ETF 與逐筆。（11/16、11/17 兩個交易日已跑完四條 feed，結果見
   下一節；沒有分區的成員改由同日日 K 佐證判定為無成交，見 [營運文件](../operations/full_market.md#finlab-盤中-feed)）
8. Production 上線與文件整併。

### 為什麼不能先把 feed 加進 catalog

原訂第三項是「先在 catalog 加四條 feed」。盤點現有程式後確認不可行，四條 feed 必須與 Fetcher 的支援、
憑證範圍一起切換：

| 現有的檢查 | 只加 feed 的後果 |
| --- | --- |
| 排程啟動時，排程的 dataset 必須等於該 provider 的 Source 範圍（`scheduler_start.py`） | Source 範圍多了四條、排程的 dataset 沒有，FinLab 的排程無法啟動 |
| 排程發布時，憑證的 dataset 必須等於該 provider 要抓取的 feed（`feed_schedule.py`） | 兩邊只要有一邊先變，FinLab 取不到排程（503），daily 停止 |
| Fetcher 拒收含有它不支援的 feed 的整份排程（`execution_config.py`） | Backend 先發布新 feed，現行 Fetcher 的 FinLab daily 停止 |
| Fetcher 啟動時，排程的 dataset 必須等於它安裝的 feed（`full_market_cli.py`） | Fetcher 先換新版，FinLab consumer 無法啟動 |
| 部署不會擴大既有 Source credential 的 `allowed_datasets` | 要有 migration 一併處理，過去兩次範圍變更也是如此 |

所以切換那一步的部署順序固定為：停止 FinLab 的 control → 部署 Backend → 部署 Fetcher → 重新啟動 control。
Fetcher 的部署本來就要求 controls 停止。Staging 平時 controls 是停的，直接合併即可；production 要安排在
沒有排程的時段。

Production 不能只升 Backend：migration 一執行，FinLab 的排程就發布六條 feed，舊版 Fetcher 會拒收整份排程，
FinLab 的日 K 也跟著停止規劃新日期，直到 Fetcher 升到同一版。

Staging 合併時 Backend 與 Fetcher 的部署同時開始。Fetcher 的新 consumer 啟動時要求排程的 dataset 等於它
安裝的 feed，如果它比 Backend 的 migration 早啟動就會失敗並自動回復；等 Backend 部署完成後重跑 Fetcher 部署
即可。

### 啟用步驟（staging 驗收用）

切換部署後四個 dataset 都沒有 baseline，FinLab 的排程行為與部署前相同。每開放一條：

1. Owner 在 Dashboard 的名單頁對該 dataset 按「手動更新」。Control 停止時也會執行；完成後名單直接發布為
   第一份 baseline。確認成員數與對應日 K 的名單一致。
2. 啟動 FinLab 的 control（已在執行就先停止再啟動，讓新的 dataset 納入）。staging 會從首日開始補抓，而且
   已有 baseline 的日 K 也會一起執行；驗收前先把首日調成單日。
3. 觀察計劃的 `data／no_data／missing／blocked`、Raw 與 Canonical R2 的物件、索引表，以及主機的記憶體。

驗收順序：先只開 `tw_equity_bar_1m`，通過後再開 ETF 與逐筆。

### 這次沒有納入的

- 失敗告警：`infra/monitoring/publish_staging_metrics.py` 的拒收／DQ 錯誤計數仍只看五條日線 feed。盤中每天
  約四千六百筆交付，要先在 staging 看到實際的失敗率才能定門檻。
- Native feed evidence（`infra/acceptance/`）的取樣只涵蓋日線 feed；盤中的資料在 Canonical R2，取樣方式不同。

## 已定事項

Owner 於 2026-11-16 確認：

| 項目 | 決定 |
| --- | --- |
| 儲存位置 | 1 分 K 與逐筆都存 Canonical R2 的 Parquet，PostgreSQL 只存索引 |
| Backend 讀 Raw R2 | 同意新增唯讀權限，以物件參照交付 |
| 成交量單位 | 存「張」，照 FinLab 的原值 |
| Shioaji 時期的舊分 K | 第 1 批不動：新資料用新的 dataset 名稱與新的查詢，舊的兩條 dataset 與 `/minute` 維持現狀。是否搬移，第 1 批上線後再決定 |
| 60 分鐘行情 | 由 1 分 K 在查詢時彙總，不另外抓 `tw_price_60min` |
| 1 分 K 回補 | 第 1 批只做每日更新，回補上線後再決定 |
| 原始檔保存期 | 維持 Raw R2 的 90 天 |
| 欄位轉換的位置 | 由 Fetcher 把 FinLab 的欄位轉成契約定義的欄位，原始檔與轉換後的檔都存 Raw R2；Backend 不解析 provider 專屬欄位 |

## Staging 實測（2026-11-16、11-17）

| Feed | 耗時 | 計劃（11/17） | 轉換後檔案 |
| --- | --- | --- | --- |
| 股票 1 分 K | 2 小時 33 分 | 1956 = 1921 有資料 + 35 沒有分區 | 約 10 MB |
| 股票逐筆 | 2 小時 56 分 | 1956 = 1921 + 35 | 24.8 MB，單檔最大 375 KB |
| ETF 1 分 K | 30 分 | 366 = 358 + 8 | 1.9 MB |
| ETF 逐筆 | 29 分 | 366 = 358 + 8 | 7.7 MB，單檔最大 911 KB |

- 四條依序約 6.5 小時，每分鐘 11–14 檔；FinLab 沒有任何配額或限流的拒絕。FinLab 容器 94–260 MiB，沒有重啟。
- 每一筆交付都被接受、沒有 DQ 錯誤。1 分 K 與逐筆有資料的是同一批標的。
- 沒有分區的成員兩天都能以同日日 K 完全解釋：11/17 股票 13 檔無成交、22 檔只有零股；ETF 5 檔無成交、3 檔只有
  零股。沒有一檔是「有整張成交卻沒有分區」。

## 尚未確認

- 盤中下載是否計入 FinLab 每日 5000 MB 的配額。
- FinLab 是否限制並行或請求頻率（依序執行時沒有觸及）。
- 1 分 K 與日 K 成交量約 7% 差額的來源。
- `tick_type` 的語意（樣本只看到 1 與 2）。
- 全市場的實際每日配額用量；上面的數字由幾檔樣本推算。
