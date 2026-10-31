# Production 環境維運

Production 固定使用 AWS account `289112218471`、region `ap-southeast-1`，state、KMS、ECR、S3、IAM、DB、host 與 runtime secrets 均與 staging 隔離。
兩環境共用部署契約，以 [部署文件](deployment.md) 為準；本文件保留 production 資源與待處理資料問題。
兩環境的統一版部署基準見 [部署文件](deployment.md)；native 資料與實際 rollback 演練仍見
[待完成項目](../dev/production-foundation-cutover-plan.md)，不得以部署健康驗收代替。

## 其他 provider 複查與控制路徑實測（2026-11-01）

台北 11/01 00:23～00:40 經 Owner 授權，以 PostgreSQL `REPEATABLE READ READ ONLY`、Fetcher
SQLite `mode=ro`／`query_only`、容器日誌與 nginx 存取紀錄複查五個 provider；沒有修改
controls、工作、預算或部署，也沒有 native provider 請求。兩端仍為 v0.1.14、DB revision
`3a80c2e1d5f4`，Full market 首次日期為 10/12～10/13。

| Provider | Full control | 未結案 work | 近 1 小時 receipts | 結論 |
| --- | --- | ---: | ---: | --- |
| Twelve Data | running | 20（10/13 running） | 654 | 受控制路徑放大影響；另有舊週末工作待處理 |
| Shioaji | running | 3 | 61 | 受控制路徑放大影響 |
| TAIFEX | running（cycle 間回報 stopped 屬正常） | 0 | 0 | 10/28～10/30 completed |
| FinLab | 10/28 起 stopped | 8 | 0 | prepared 為空；恢復前須先處理舊週末工作 |
| FactSet | 從未啟動 | 0 | 0 | production 港股目前沒有抓取中的 provider |

**控制路徑放大：** 十分鐘 Source 請求為 enqueue 1,896、authorization 799、prepare 544、
authorize 513、claim 265，實際 ingest 只有 136。Fetcher 每輪從首次日期逐日 enqueue，且
`run_worker` 先 drain 再 cycle，兩者都會重送整個範圍；Backend 每次 enqueue 先取得 provider lock
並讀取完整 work row。`provider_work` 的 TOAST 累計插入 11,951,818 個 chunk，現存僅 45 MB，
表本身 127 列、累計 17,099 次更新，證實 prepared JSONB 被反覆整份重寫。活動 Shioaji work 壓縮後
5.0～5.5 MB、Twelve Data 10/13 為 1.75 MB；FinLab、TAIFEX、FactSet 目前沒有累積 prepared。

**舊休市日工作：** Twelve Data 美股 10/17（六）、10/18（日）、10/24（六）與 FinLab 10/24、
10/25 仍 queued，皆帶 `legacy_id`，frozen decision 為 `planned`；以正式 `current_calendar_status`
唯讀評估七筆均為 `repair_required=true`。SQLite 中 Twelve Data 這三日各有 5,082 個
pending／source_error 成員，attempts 最大值為 7／6／1，依現行程式至少還會再派送約 5 萬次。
Fetcher 原本只在 FinLab 觀測後與 Shioaji 空回應路徑查詢 calendar status。過去 FinLab 10/17、
10/18 全部 1,947 個、Shioaji 週末全部 1,945 個與 TAIFEX 週末全部 63 個成員都已用滿 8 次
一般失敗額度；新日期因五個 provider 的略過非交易日開關皆為 true，已正確記為 `skipped_calendar`。

**其他狀態：** FinLab 帳號 cooldown 標示 `legacy`，另有 change ratio 1.003（1,953 個成員全變）
的 universe candidate；TAIFEX 有 change ratio 0.397 的 candidate。TAIFEX 健康輸出持續回報
10/13～10/27 本地 plan 的 944 個 manual 成員，但對應 Backend work 已 cancelled。
`full_work_diagnostic` 全庫只有一筆 Shioaji `exception:ShioajiSdkError`，Twelve Data 舊 source_error
早於分類診斷，無法回推例外類別。修正與後續項目見
[待辦](../dev/production-foundation-cutover-plan.md)。

SSM：DB 快照 `32176e52-17d5-4705-a4ce-bf2429ccb70e`、決策與開關
`e9e3c385-fa83-46a3-81e7-84805eedfa83`／`52376a5e-779d-47f6-8fea-786b443e8e57`、calendar status
`78504d47-4d44-4cdf-b308-3604ca77b085`、SQLite `6e1c524c-a26c-4e25-a8ee-7cfb7541cce2`、TAIFEX
日誌 `3029a593-7896-42d7-ac9e-5c45f7bb610b`、nginx `0d498e28-1390-45e4-ba18-73c87493f3d8`，均 Success。

## Twelve Data／Shioaji 遺留工作複查（2026-10-31）

台北 21:33～21:36 以 production PostgreSQL `REPEATABLE READ READ ONLY`、Fetcher SQLite
`mode=ro`／`query_only` 與 runtime inventory 複查。以下是活動系統的分時快照，不能當作同一
transaction 的跨主機指紋。本次只調查，沒有變更 controls、工作、預算或部署，也沒有觸發
native provider I/O。

Fetcher 現版為 `fetcher-v0.1.14`，manifest commit
`e710782d59100f403a634977a27a64410df17408`；DB revision 為 `3a80c2e1d5f4`。
兩 provider 的 Pilot／Full／historical 共六個 containers 都 running、零重啟、無 OOM，且
`PROVIDER_WORK_REQUIRED=true`。Full controls 均 desired／observed running，Twelve Data
revision 8、Shioaji revision 16，心跳均新鮮且 last_error 為空；Pilot 均 stopped。
兩 provider 無 recovery_reason 或 acquisition cooldown，不能再沿用 10/29 的 suspended 結論。

### Twelve Data

共 20 筆未結案：19 筆美股 daily（10/12～10/30）與一筆 universe maintenance。
其中 13 筆 daily 帶 legacy_id，另六筆 10/25～10/30 daily 與 maintenance 於本日
11:14 UTC 入列。10/13 work `01a13897-a16e-7c30-8bab-08c2220e66ba` 自
11:14:20 UTC 持有第一輪 open attempt；owner 心跳有效，其餘 19 筆為 `provider_active`。
FIFO 依 queue_order 排序，所以 10/14、10/12 排在其後，不是日期排序錯誤或 owner 失聯。

10/13 的 completed receipts 在 13:34:07 UTC 為 1,855，13:36:27 UTC 增至 1,890，
證明 active work 有實際交付進度。21:35 的本地 checkpoint 為 1,873 prepared、
2,868 pending／source_error、341 manual／retry_exhausted、942 manual／mapping_gap，
合計 6,024 個 frozen members；prepared 的數字與前後 DB 快照不同是取樣時間不同。
942 個 mapping gaps 與其餘 legacy daily 的既有失敗預算仍在；queued 第一輪不代表每個
成員尚未嘗試。舊 HK 的 12 筆 work 仍 failed，沒有復活或進入活動佇列。

目前可確認為「正在消化舊美股工作，後續工作等待同 provider owner」，尚不能宣稱舊錯誤
全數恢復；source_error 未保留精確 provider 例外，不能由此判定為 429、權限或商品無資料。

### Shioaji

活動佇列剩三筆股票 minute daily，均為 `batch_yield`，抓取窗口 open；下次關閉為
台北 11/01 06:00。21:34 的 Backend summary 與收據如下，所有已接受 runs 均 completed：

| 日期 | Work ID | expected | data／completed receipts | missing | blocked | 排程輪次 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 10/28 | `01a187a3-e339-75a0-a8d3-194b76b6a5e4` | 1,953 | 1,142 | 792 | 19 | 211 |
| 10/29 | `01a187a3-e767-7ecf-8b2e-5a67d198b48a` | 1,953 | 766 | 1,177 | 10 | 210 |
| 10/30 | `01a18c40-f5a8-7a2f-ae0c-24ab72edadda` | 1,955 | 553 | 1,394 | 8 | 127 |

最近一輪分別處理 2／1／1 個成員，elapsed 82.566／86.991／61.452 秒後讓出。
60 秒是成員間檢查的 batch 邊界，不能推論單次操作會在 60 秒內中止；本次沒有足夠分段
計時把耗時定位為 SDK、網路或對帳。高排程輪次不等於 native 呼叫次數或本地失敗預算。
三日 SQLite 仍為 prepared／normalizing 的 2,461 個成員，在對應 DB 快照已有相同數量的
completed receipts；目前沒有 normalization queue 積壓，不能把本地標籤當作 worker 卡住。

21:35 的三個股票 checkpoint 另有 24 個 pending／source_error（attempts=1）、三個
pending／empty_response（attempts=0），以及四個 manual／mapping_gap。Frozen DB scope
尚有六個未被本地遇到的 mapping gaps，所以缺口不只已列出的四個 manual 成員。

另外五筆舊工作已終止為 failed，最後 failure_code 均為 `MANUAL_INTERVENTION_REQUIRED`：

| 日期 | Dataset | complete | mapping_gap | retry_exhausted |
| --- | --- | ---: | ---: | ---: |
| 10/27 | tw_equity_minute | 1,921 | 2 | 24 |
| 10/27 | tw_etf_minute | 355 | 2 | 4 |
| 10/28 | tw_etf_minute | 355 | 4 | 5 |
| 10/29 | tw_etf_minute | 355 | 4 | 5 |
| 10/30 | tw_etf_minute | 360 | 2 | 2 |

這五筆合計 54 個 manual 成員（14 mapping gaps、40 retry exhausted），不會因離開活動
佇列而自動補齊；需按原 work／member 的診斷與 reviewed repair 流程處理，不重設 attempts、
清 cache 或改報 no_data。本次僅確認最新狀態與分類，沒有執行 production 恢復驗收。

Source 當日截至 13:36 UTC 的 Twelve Data／Shioaji attempts 摘要未見 rejected；
normalization_job 當下全部 27,399 筆為 completed。後續應分別追蹤 Twelve Data 的 owner
交付速率、Shioaji 三筆股票的 missing 收斂，以及五筆 failed 的人工恢復，不能只看佇列數下降。
原始 response body／receipt 內容查核被自動審核拒絕，已改用不含內容的狀態彙總；本次沒有
重驗舊空回應 checksum／根因，下方既有證據亦不等於本次新的 native 驗收。

SSM 證據：runtime inventory `83c52a26-5747-483d-b5c8-b93189dd5681`、完整 DB 摘要
`a723538f-3259-4a8d-8a7a-cb18d0b9d67c`、SQLite 狀態彙總
`3991a348-dc7c-44f3-931a-dea1ea1a7d0e`、終態與進度複查
`b5eb62ae-6a6b-4e13-b27b-6e4389e4a4f5`，均 Success。

### 原始 response／receipt 與慢速路徑續查

Owner 隨後明確授權唯讀查核原始 response 與 receipt。台北 23:25～23:38 的續查使用
SQLite `mode=ro`／`query_only`、PostgreSQL READ ONLY 與既有日誌；mapper 在獨立程序
停用 socket 後重播。沒有修改線上工作、controls、cache、預算、runtime 或部署，也沒有
新增 native provider 請求。此前被拒絕的原始內容查核，已在本次授權範圍內完成下述核對。

23:36 的 completed receipts 為 Twelve Data 10/13 共 3,061 筆；Shioaji 股票
10/28、10/29、10/30 分別 1,188／800／585 筆。相較 21:34 快照，Twelve Data
增加 1,206、Shioaji 增加 112，約為每分鐘 9.9／0.9 個成員；不是停止工作。
Shioaji 的 missing 降至 744／1,141／1,359，blocked 為 21／12／11，no_data 均為零。
normalization_job 此時全部 28,717 筆 completed，沒有 normalization 積壓。

**收斂慢的已確認成本與限制：**

- Governor 近兩小時帳本：Twelve Data 1,167 次 acquisition，actual-start 到完成平均
  0.306 秒；Shioaji 110 次 Kbars 平均 0.302 秒、55 次 SDK login 平均 0.794 秒。
  這些只涵蓋原生操作，不涵蓋前後授權、R2 persistence、交付與對帳，不能用作完整成員耗時。
  近半小時 Kbars 的 armed→actual-start 平均另等 1.895 秒，SDK login 為 2.706 秒。
- Backend 將所有 prepared bodies 放在同一 `ProviderWork.prepared_deliveries` JSONB。
  三筆活動 Shioaji work 的 JSON 文字已達 46,772,318／61,084,189／56,387,356 bytes；
  Twelve Data 10/13 為 4,462,809 bytes。`fence()`、active-owner `claim()` 與既有 work
  enqueue 仍讀取完整 ORM；prepared intent／confirm 更新則重寫同一個大 JSONB。
  claim 甚至在判定 kind／profile 不符、回傳無工作之前就載入完整 work。
- 獨立唯讀量測：10/28 Shioaji 完整 ORM 讀取 0.729 秒，metadata 讀取 0.089 秒
  （本次第一個 ORM query，含冷啟成本）；Twelve Data 分別 0.084／0.0014 秒。
  這不是修改過的 endpoint，也不是已實作效能修正的驗收。
- 既有十分鐘 access log 樣本含 427 次 authorize、444 次 prepare、133 次 prepared lookup、
  269 次 claim、1,900 次 enqueue；所選日誌均 HTTP 200。這是跨 consumer 計數，不能
  全部歸給某一 provider。PostgreSQL 八次唯讀採樣均見 work SELECT 或相關交易，亦見
  ClientWrite 及約 0.37～0.52 秒的 provider advisory lock 等待。Ingest 容器累計 NetIO
  接收約 857 GB，為容器生命週期累計值，不能當作本次十分鐘的流量。
- 因此已確認完整 prepared JSON 的重複搬運、解碼、重寫及持鎖是持續放大的控制路徑成本；
  仍缺少 live request 的完整分段耗時，不能將每輪 60～90 秒全部歸因於這一項。
  當下未見主機 memory／IO pressure；此快照也不足以保證其他時段沒有資源或網路問題。

後續效能修正應先將 fence／claim／enqueue 所需 metadata 與 prepared body 讀取分開，
避免同一 work 的每個新成員重寫全部舊 bodies，並減少重複 preparation 往返；不得移除
owner fencing、原子接受、durable intent／confirm、ACK-loss 或原始 bytes 保留。
需以完整成員／回合耗時、實際 Source SQL／傳輸量與原生帳本共同驗收，不只提高 batch 秒數。

**遺留失敗的來源：**

Twelve Data 切換前封存
`/var/backups/findb-provider-work-cutover/production-20261029T1134Z/state.tar`
中的 Full SQLite，原 SHA256 為
`07b50f06be6164fcfa5d89b353e3d1ab12e1c6488b522ddaec312a8933ac0651`，archive WAL 為零。
僅在匿名記憶體讀取其副本，確認 10/13 的 341 個、10/14 的 639 個
manual／retry_exhausted 成員，與目前集合完全一致；兩日各 942 個 mapping_gap 亦已存在。
它們早於本次 Full 啟動，不是本次抓取新耗盡的成員。341 個成員在 current work journal
沒有 response 或 response observation；舊 checkpoint 只留下共用 source_error／耗盡分類，
無法據此補證特定 HTTP code、entitlement 或資料格式根因。不得用重設 attempts 猜測恢復。

目前保存的 3,018 份 Twelve Data 成功 response，以部署版純 mapper 重播全部通過；
部署版確實傳入次日午夜的 end_date 與 `adjust=none`。這支持現行成功路徑，不能反推
上述沒有原始 response 的舊失敗都由同一日期問題造成，也不能令其自動恢復。

Shioaji 五筆 failed 的 40 個 retry_exhausted 成員，逐 work／symbol／date 找到對應的
checksum 正確 cache；七欄向量全空，部署版 mapper 全數拋出
`ShioajiEmptyResponseError: empty kbars requires calendar evidence`，且沒有 prepared body
或 receipt。舊空快取經一般失敗額度反覆拒絕的機制，與既有調查吻合；另外 14 個 manual
則是 frozen mapping_gap。五筆 terminal 時間均早於 10/31 07:36 UTC 的現版 Fetcher 啟動，
所以新版部署不會重新 claim 或自動修復這些 terminal work。

活動股票工作另有 23 個 pending／source_error 能配對相同空 cache；3492（10/28）與
3498（10/29）則無可重播 response，後者保有 unknown observation，不能當作已確認空值。
新觀測另有九個 pending／empty_response；保存的 native diagnostic 包含 SDK 1.7.9、
symbol／date、七欄長度、request counters 與 usage bytes，但 cause 仍為 unknown。
因此已定位「空回應如何造成舊失敗」，尚未證明供應商為何回傳空值；不可改報 no_data。
既有 terminal／mapping 修復仍須沿用 [受控恢復流程](full_market.md)，另行核准 scope 與額度。

**交付證據：**23:29 範圍內 6,632 筆 receipt 的 raw payload hash 與 idempotency key
均相符（含當下唯一一筆 processing，後續已完成）。兩筆量測 work 共 4,181 份 prepared
body 重算 SHA 全相符，receipt／descriptor hash 無差異；四份最新 raw payload 抽樣重算
hash 亦相符。沒有以清 cache、刪收據或重送新 payload 來取得這些結果。

SSM：原始快取／帳本 `f9900197-fcbd-4cad-beff-81b0ee734931`、純 mapper
`6d953034-8663-4832-9747-64fc13fc7d6b`、ORM／receipt 核對
`058be999-f029-407a-951a-7be0b3ab9ec2`、日誌／資源
`bbc4621b-decf-4ace-8b7c-f06a05996b7d`、DB waits
`53cdb783-d4b0-4a88-a447-1f3d18b71b4a`、封存集合比對
`4ed9ddda-0167-4eaa-b5f5-26210f3485cf`、最新佇列
`e93ec2df-c863-4cdc-af33-473a1568183a`，均 Success。
初次逐 receipt 存取完整 prepared JSON 的診斷 SQL 觸及 25 秒 statement timeout，已改為
每 work 讀取一次後在記憶體比對；沒有提高線上 timeout 或重跑該放大查詢。

## Twelve Data 供應商工作佇列積壓（2026-10-29）

2026-10-29 唯讀查核 production DB、Fetcher checkpoint 與實際 Dashboard
projection，確認 Twelve Data 有 25 筆 Full market daily work，並非人工 historical backfill：

| Dataset | 筆數 | 凍結資料日期 |
| --- | --- | --- |
| `us_equity_eod` | 13 | 2026-10-12 至 2026-10-24 |
| `hk_equity_eod` | 12 | 2026-10-13 至 2026-10-24 |

全部 work 都帶 `scope.legacy_id`，符合 migration `b53da90e7f21` 匯入未完成 daily plan 的規則；
各自只有第一輪 queued attempt，`started_at` 與 `failure_code` 均為空。Dashboard projection 的
25 筆 `wait_reason` 全為 `suspended`，沒有 active owner；`recovery_reason`、帳號 cooldown 亦為空，
查核當下 daily window 為 open。因此目前等待原因是排程停止，並非正在重試或等待失聯 owner。

Backend 與 Twelve Data 的 Pilot／Full／historical 三個 consumers 實際均設定
`PROVIDER_WORK_REQUIRED=false`，仍走 compatibility runtime，不會消費新 FIFO；三個容器雖有
protocol v1 label 且正常 running，不能據此認定已完成 cutover。Pilot／Full controls 的
desired／observed 均 stopped，DB 未查到 cutover audit。Expand migration 建立佇列不等於啟用 consumer，
單純重啟容器不會讓這 25 筆工作執行。

原 Full checkpoint 另保存 98,374 個 `pending / source_error`、12,810 個
`manual / mapping_gap` 與 980 個 `manual / retry_exhausted` 成員。這是舊執行結果，不能與 FIFO
尚未開始的 attempt 混為一談；`source_error` 是多類例外共用分類，未保留足以確認特定 HTTP code、
provider entitlement 或連線故障的診斷，不能直接歸因為 429、金鑰失效或無資料。

後續處理須先核對原 prepared／receipts／terminal 證據，再依
[provider-work 切換程序](deployment.md#provider-work-切換) 完成 production cutover 與明確的 control 啟用。
現行 HK feed 已由 FactSet 負責，舊 Twelve Data HK work 須走退役 feed 的保留證據／人工處理流程，
不能整批重試、刪除 checkpoint 或改成 completed／no_data。本次僅診斷，未變更 production
旗標、controls、工作狀態或觸發 provider I/O。

SSM 證據：DB／旗標 `7c8bc889-fa9c-43d9-bacb-b8d8b288d847`、SQLite／consumers
`b398ef09-5a3d-4f54-b529-0dfade9069e6`、日期與 Dashboard projection
`088522b4-2b34-4e5b-aff0-e1f54ee78628`。

## 退役後不可重試堆積的結案查核（2026-10-29）

v0.1.12 部署後再次以 Admin projection、Source DB read-only transaction 與原 SQLite
`mode=ro` 查核全部五個 providers。15 個實際 consumers 均使用統一 daily／historical 入口、
`PROVIDER_WORK_REQUIRED=true` 與本次 accepted image；舊 acquiring CLI 已無執行實作。
Shioaji 與 TAIFEX Full controls 已另行啟動，這不是切換驗收時的停止快照。

| 分類 | 數量 | 處理界線 |
| --- | --- | --- |
| FinLab 舊 daily 全成員 complete／manual | 22 | 原成員已無 pending／prepared；可依原預算與交付證據結案 failed |
| Twelve Data 舊 HK daily | 12 | 現行 HK feed 屬 FactSet；只結案退役 work，不重新抓取、不改成 no_data |
| FinLab 仍可重試 daily | 8 | 保留 queued／suspended 與剩餘預算 |
| Twelve Data US 仍可重試 daily | 13 | 保留 queued／suspended 與剩餘預算 |
| Shioaji、TAIFEX、FactSet | 0 筆符合本批清理 | 保留 active／retryable work 與既有 terminal history |

本批 34 筆候選的完整 frozen member 集合及 15,497 筆原始 sequence receipts 已逐筆比對。
Local prepared bytes 的原 SHA 保留；Source raw payload 的 SHA 必須與原 receipt 一致。
跨兩端 payload 比對只將契約預設的 nullable 欄位省略與 `null` 視為等價，避免將 Source
`model_dump` 補上的 null 誤判為內容不同；所有非 null 值、run ID、idempotency key 都須一致。
此查核不更改原 bytes、raw 或 receipts。

清理僅允許將上述 imported daily 的未開始第一輪 queued projection 標記 failed／settled，
逐筆追加具名 AWS IAM operator audit。執行前須持有原 FinLab／Twelve Data flock inodes，
在同一 transaction 依 provider 排序取得 advisory locks，確認兩個 provider 的 controls 仍 stopped、
owner idle、無未回報 source error、未交付 prepared 或 nonterminal run。任一不一致整批 rollback。
Checkpoint、prepared、receipts、raw、frozen plans／members、原預算及其它工作均保留；
stable-key enqueue 再執行不能復活已結案 work。這不是刪除錯誤歷史或重設失敗額度。

Production 已於 2026-10-29 13:09 UTC 完成結案與複查：34 筆 work 為 failed、attempt 為
settled，同一 transaction 追加 34 筆 `reconcile_queue` operator audits。FinLab 使用
`LEGACY_RETRY_EXHAUSTED`，退役 TD HK 使用 `RETIRED_FEED_MANUAL_REVIEW`。
交易前後 13 組 Source DB 保留性雜湊一致；持鎖期間再次讀取原 SQLite，34 筆候選的
plan／member／body／receipt／attempt rows 與原 journal 均未變。兩個原 inode 的 FD 已正常釋放。

執行後 Admin API 顯示 27 筆待處理工作：FinLab 8、Twelve Data US 13、Shioaji 6。
前兩者仍為 queued／suspended；Shioaji 的可執行與 batch-yield 工作照常保留。已結案的 34 個
IDs 全部離開 projection，未清除可重試項目。TAIFEX／FactSet 沒有本批清理項目。

證據保存在兩個 hosts 的 root-only
`/var/backups/findb-provider-work-cleanup/production-20261029T130718Z`：Fetcher 的 fresh lock proof
與保留性結果、Backend 的原 projection 備份、實際程式及 transaction 結果。
SSM：持鎖 `f1b8c5d9-ff4f-4548-a9bd-4aed7d7f2287`、鎖驗證
`d0222e46-2f19-46cc-afd5-d0e0581a5098`、結案
`a54c93a5-c9da-453b-a6ba-0f05c919ff64`、釋放
`b69c8a19-6113-4276-b064-f3cf2a6c0202`，均 Success。
驗證包含 15 項隔離 PostgreSQL 維護 transaction 測試、125 項退役／原預算／錯誤回報相關測試，
以及全 repo `pnpm check`；獨立實作審查 CLEAR，另重跑 15＋46＋39 項測試通過。

歷史 `source_error` 未留下精確 HTTP／SDK 例外，仍不能聲稱已找出每筆來源錯誤的根因，
也不能保證供應商、網路或 mapping 錯誤不再發生；已封住的是退役的雙執行路徑與無 owner
的抓取／接受入口。Mapping gaps 保留人工處理，不因清理改為成功。

## 供應商工作佇列讀取逾時與發布驗收（2026-10-30）

台北時間 2026-10-30 00:32 起的唯讀調查，已重現 Dashboard 的
`供應商佇列更新失敗：FinDB API request timed out`。直接以合法 Admin credential 呼叫
`GET /api/v1/admin/provider-work` 可回傳 HTTP 200、27 筆工作，實測耗時 10.612 秒；
相同回應通過 Dashboard 的 `providerWorkSchema`。Dashboard `fetchTarget` 的
`UPSTREAM_TIMEOUT_MS=10_000` 先取消請求，因此即使 Backend 最後完成，面板仍顯示失敗。

已部署 v0.1.12 的根因在 `backend/app/services/provider_work.py:projection()`：初始 ORM query 將
`ProviderWork` 全欄位與**所有** `ProviderWorkAttempt` join，取得結果後才在 Python 跳過
settled attempts。每輪 join 都重新傳輸／解碼 work 的完整 `prepared_deliveries`；它包含原始
prepared body，而監控列表只需要狀態摘要。當時 Shioaji 已有 183 輪 settled attempts，
current attempts 為第 26～27 輪；六筆工作約 40,436,313 bytes 的 prepared JSON 在該 join
中被放大到約 1,077,959,230 bytes。頁面只回傳約 18.7 KB，並不代表查詢本身很小。

獨立唯讀程序執行 production 原函式，得到 27 筆、耗時 13.655 秒、89 個 SQL calls、
peak RSS 1,106,884 KiB。其中初始 work／attempt join 耗時 13.039 秒；27 次 receipts 查詢
合計 0.124 秒，54 次 previous-attempt 查詢合計 0.061 秒。相同 read-only snapshot 的
查詢對照：在 SQL 先排除 settled、仍選取完整 ORM 的初始查詢為 0.509 秒；只選取未結案
work／attempt metadata 的初始查詢為 0.006 秒。這些是初始查詢對照，尚非修正後完整 endpoint
的效能驗收。

Backend／Dashboard／nginx 均 healthy、零重啟、無 OOM；DB 基本彙總查詢為 2～7 ms，
Shioaji owner heartbeat 正常。前次結案的 22 筆 FinLab、12 筆 TD HK 仍為 failed，
其餘佇列／prepared／receipts 持續保留；診斷階段未清理資料、停止 runtime、調整 timeout 或部署。
修正方向為 SQL 先篩掉 settled、監控只讀摘要／必要欄位，並批次讀取 receipts 與最近 attempt；
驗證需包含大量歷史 rounds、minute prepared bytes 與十秒以內的完整 Dashboard 讀取。
不得以刪除歷史 attempts／prepared 或單純提高 timeout 取代查詢修正。

Repository 已將 projection 改為在 SQL 排除 settled，僅選取必要 metadata；prepared 有無、
未接受交付與 nonterminal receipt 由 SQL 計算布林摘要，最近批次與失敗代碼以同一查詢的
相關子查詢取得。Historical eligibility 批次讀取 request／item，HK cutover 沿用正式 eligibility
判定。一般 projection 為固定三次 SELECT；同時需要 historical／HK eligibility 時最多五次，
不隨工作數或歷史輪次新增 SQL round trips。原工作、receipts、prepared bytes 與 budgets 不改寫。

候選函式於 production host 的**獨立程序**、READ ONLY transaction 中量測，未替換 module、
未更新服務或部署：當下 26 筆未結案工作全部回傳，完整 projection 0.240 秒、三次 SELECT、
peak RSS 81,504 KiB、回應約 19.3 KB。當時原服務仍使用舊版，這不是已部署 endpoint 的驗收。
本機六筆 minute fixture 保存 42,062,340 bytes prepared 與各 28 輪歷史，projection 為三次 SELECT、
0.028 秒；測試以禁止選取完整 body／manifest／proof／token 與固定 SQL 次數防回歸，
不以依賴機器負載的毫秒門檻判定正確性；正式部署與 Dashboard 驗收見下方紀錄。

PR #376 已合併至 `c1c4282795c9ee16ea0e36f418baec898760ec74`，
[staging CD](https://github.com/FPI-TW/findb/actions/runs/40918842768) 的 immutable acceptance
與 activation 均通過。已核對主機 manifest、Backend／Dashboard 映像與合併版本一致，
健康檢查、未授權請求拒絕、連續三次佇列 API 讀取及前端 schema 驗證均通過。
登入後的 Dashboard「供應商工作佇列」正常顯示最後成功時間與五個 provider 分頁。
當時 staging 沒有未結案工作；大量 retained rounds 的讀取已驗證，含實際待處理工作的
面板則由下方 production 發布後驗收補足。

同日 [production promotion](https://github.com/FPI-TW/findb/actions/runs/40922214003)
已成功啟用 `findb-v0.1.13`，指向相同合併 commit。Release binding、完整 CI、主機 preflight、
candidate acceptance、immutable acceptance 保存與 activation 均通過；已核對 production
manifest 與 staging accepted record 的 Backend／Dashboard digests 完全一致。

Production 健康檢查、連續三次完整佇列 API 讀取、前端 schema 與未授權拒絕驗證均通過。
實際待處理工作可於既有十秒上限內完整回傳；登入後的 Dashboard 五個 provider 分頁均可讀取，
顯示新的成功時間，未出現原佇列逾時訊息。主機獨立唯讀檢查確認正式函式為三次 SELECT，
Backend／Dashboard 與基礎服務健康，無 OOM 或新重啟。

本次只發布 Backend／Dashboard；原 Fetcher 容器 ID、映像、provider-work protocol 與持久化
mount 身分保留，scheduler desired states 與 revisions 未變更，未重設 checkpoint、預算或
cooldown。所有既有 work rows 與 failed 證據仍保留；既有排程持續運行期間，部分 Shioaji
prepared 增加，receipts 與 attempts 亦增加，不能宣稱整個活動資料庫指紋完全不變。
Backend 停機窗口內觀察到原 Fetcher consumers 自動重啟；發布後全部恢復 running，
兩次 inventory 比對的重啟計數已穩定。這次沒有發布 Fetcher，也未調整其連線失敗處理。

另曾觀察公網 `/health` 單次 Cloudflare 525、耗時 43.787 秒。Origin certificate 有效至
2041-09-13，當下沒有足夠證據定位該次 TLS handshake failure；不能將它與已獨立重現的
佇列查詢膨脹混為同一根因。

SSM：健康與規模 `7fc31a56-dea4-4283-b622-d2d78ec663f1`、實際 profiler 與對照
`88bf68d2-c39a-4095-b0e4-98d0ef906009`。所有診斷為 read-only，沒有改寫 production 程式或資料。
候選 projection 唯讀量測：`475de304-2002-4a81-843e-8effec56bb30`。

## Backend 部署期間 Fetcher 重啟調查（2026-10-30）

台北時間 01:40～01:43 的 Backend candidate／activation 窗口，部署器停止公開 nginx 與
Source，Fetcher 仍以公開 Source URL 輪詢。保留日誌顯示 historical worker 的
`FullMarketProtocolError: Source governance returned HTTP 521`；同期 Pilot／Full 顯示固定的
startup／operation blocked 訊息。Docker 保留事件有 Pilot／Full exit 2、historical exit 1，
原 consumers 的 restart policy 為 `unless-stopped`。保留事件支持暫時 Source 不可用後的
程序退出；未見部署器替換 Fetcher、OOM 或重新啟用已退役 CLI 的證據。部分日誌已輪替，
Docker events 只保留局部，不能重建每一次退出的完整堆疊。

`full_market_client.py` 與 `scheduler_control.py` 的 bounded status retry 清單都是
429／502／503／504，未包含實際遇到的 521；本機 MockTransport 重現 521 只送一次且沒有
退避，503 則按設定送三次並等待。Pilot／Full 在 initial poll、idle drain／poll 外層缺少
長期等待處理，例外會到 CLI 最外層並退出；historical `run_worker()` 同樣未處理 claim 的
暫時 Source 失敗。現有 `SchedulerControlLoop` 的容錯測試不代表這些現行 CLI 使用了該 loop。

必須區分 idle 控制失敗與 active owner 不確定。`ProviderWorkRecoveryBlockedError` 刻意繼承
`BaseException`，會穿過一般 SDK error sanitizer，保留 DB owner、隔離 host FD，並要求原
shared inode 與子程序退出證據才能 recovery。修正不可用廣泛 catch／繼續舊 session 的方式
繞過此安全邊界；heartbeat／generation 不可確認時仍須停止新 provider I/O。

本次保留證據與影響：

- 既有 work rows、failed 證據、prepared、receipts、原容器與持久化 mounts 保留；deployment
  前後沒有改 desired states 或 control revisions。部署窗口的 receipts 全部完成 normalization，
  目前 DB 沒有同 work／member 的重複 receipt，沒有卡在 recovery 的 owner。
- Governor 與 work journal 的唯讀 integrity check 均正常；原歷史 quota／acquisition 證據仍在。
  部署窗口未找到新的 unknown acquisition，Shioaji SDK 用量樣本持續累加，沒有重設。
  Source poll／claim 失敗不會直接呼叫 provider acquisition；重新連線的 SDK login 與實際
  抓取仍依原 governor 記帳。已保存的 response／prepared 可重播，但不能據此承諾中斷的
  未持久化回應或 SDK 重連完全沒有額外用量；尚未以供應商帳單量化額外費用。
- 另重現工作重試額度風險：Twelve Data、FinLab、Shioaji、TAIFEX 的 canonical daily 路徑在
  `FullMarketState.claim()` 先增加 attempts，才呼叫 `owner.authorize()`。Mock Source 521
  使授權 fail closed、provider 呼叫為零時，attempts 仍保留為 1。這與 provider 帳號 quota
  是不同帳本；不能因帳號用量未重設而宣稱工作重試額度完全不受影響。多次此類中斷可能
  侵蝕原 failure budget；本次 production 窗口未觀察到因此新增 terminal failed 工作。
  Backend `attempt_number` 是排程回合，不是這個本地 failure budget，不能以高回合數推論
  provider 請求或工作失敗次數。

需要修正：補齊現行 CLI 在尚無 active owner 時的可中止退避／恢復迴圈與安全錯誤分類，
統一處理暫時 Source status（含實際 521）；active owner 仍遵守停止、reap 與重新取 fence 的
恢復規則；對可證明未派送的純 Source 等待保留工作 failure budget，已派送／不確定操作的
quota、unknown debt 與 cooldown 不得退還。另補 startup、idle、active heartbeat／ACK-loss、
prepared replay 與重試額度的回歸測試。修正已由 PR #379 實作並於 v0.1.13 發布，驗收見
[部署文件](deployment.md#source-暫停修正的部署驗收)。
本次調查只有唯讀 production 查核與本機 mock 重現，未修改線上 runtime、controls 或預算。

## Shioaji 工作佇列調查（2026-10-30）

以下為當時的 production 唯讀快照；本次 staging 結清及文件提交未重新查核或操作 production。
修復程式已合併 #383／#385，staging 部署及 10/23 兩筆休市結清已完成，收據與資料保留核對見
[Full market runbook](full_market.md)。#378 保持 open，production 待驗收範圍見
[驗收矩陣](../dev/production-foundation-cutover-plan.md#issue-378-production-驗收範圍)。

台北時間約 09:10～09:18，production 有五筆 Shioaji Full market daily 工作未結案。
全部為 queued，等待原因為 `acquisition_window_closed`；目前抓取窗口為台北 18:00～隔日
06:00，下次開放為 10/30 18:00。Full control desired／observed 均為 running、revision 12，
heartbeat 約十秒前更新、last_error 為空；provider owner 無 active attempt、recovery_reason
或 acquisition cooldown。這個等待狀態符合時段設定，沒有卡住舊 owner 的證據。

以下是同批工作的本地 checkpoint 成員數；pending 包含尚未嘗試與可重試失敗，manual
包含 mapping gap 與重試耗盡，不能將整筆 queued 工作視為全部成員失敗。

| 日期 | Dataset | complete | prepared | pending | manual |
| --- | --- | ---: | ---: | ---: | ---: |
| 10/27 | tw_equity_minute | 1,921 | 0 | 24 | 2 |
| 10/28 | tw_equity_minute | 0 | 635 | 1,317 | 1 |
| 10/28 | tw_etf_minute | 355 | 0 | 4 | 5 |
| 10/29 | tw_equity_minute | 0 | 428 | 1,524 | 1 |
| 10/29 | tw_etf_minute | 355 | 0 | 5 | 4 |

Backend 唯讀 SQL 確認這五筆工作的已接收 receipts 全部為 completed，沒有未接收的
prepared member；其中本地仍標示 `prepared/normalizing` 的 1,063 個成員，其 run 也都已
completed。因此這些本地標籤不能當成 Backend normalization 仍在排隊的證據；待後續
合法回合對帳，不需重送新 payload。兩筆 equity 日期各仍有大量未嘗試成員，近期回合
因 60 秒 batch 上限讓出，亦不能只用整筆工作的高排程回合數判定失敗。

另外確認兩種會阻擋缺漏收斂的問題：

- 五筆工作共有 44 個 `source_error` pending 成員及一個 `retry_exhausted` manual 成員。
  唯讀檢查全部 45 份對應 work／symbol／date 的 response journal，checksum 均相符，
  Kbars 七個向量皆為空。依正式 decoder 還原 tuple 後，離線 mapper 全部拋出
  `ShioajiPayloadError: empty kbars requires calendar evidence`。`fetch_kbars()` 使用的
  `durable_response` 未設定 target validator，空回應會成為該 work 的持久快取；同一成員
  重試仍讀取相同 bytes，mapper 再次拒絕，外層統一記為 `source_error`。
  本機合成資料重現一次 native fixture 呼叫、八次 mapper 失敗，最後 checkpoint 為
  attempts 8／manual／retry_exhausted。這是實際 provider acquisition 與本地失敗額度
  不同的情況；不能把八次失敗解讀成八次 SDK Kbars 呼叫，也不能靠重設 attempts 解決。
- 另有 12 個已標記 manual／mapping_gap 的成員。Backend frozen universe 本身就是
  `mapping_status=gap`，包括 equity 的 1589／4804，以及 ETF 的 00416A／00838B／009831／
  009832（依日期不同）；這些成員沒有 Kbars cache。未嘗試的 scope 亦可能還有其他
  frozen mapping gap，以上數量只包含目前 checkpoint 已遇到的缺口。

與 Backend 部署中斷的關係：00647L（10/28 ETF）首次 `source_error` 已在台北 10/29
21:26 的排程 proof 記為 local attempts 1，早於 10/30 01:40～01:43 的部署窗口；
部署後首次增加至 2 是 02:29 的回合，05:56 達到 8。部署前保存的 queue projection
亦已有相同 equity／ETF 的 source_error、mapping_gap，以及另一日期的 retry_exhausted。
因此本次可定位的空回應快取與 frozen mapping 缺口是獨立的既有問題，沒有證據將它們
歸因於 HTTP 521 或退役 CLI 復活。前節授權前增加 attempts 的風險仍需修正，但單獨
修復 Source 重連／純等待額度，不會解決相同空快取反覆轉換失敗。

Shioaji 空回應分類、可恢復的 response 驗證／更新及 frozen mapping 修復流程已實作並完成
staging 部署；production 部署與原生恢復尚待另行核准及驗收。空資料不能直接改報 no_data：當時仍缺少可支持 calendar／no-trade
的 durable evidence，也未確認 SDK 為何對這些商品日期回傳空值。保留既有快取作為
證據，任何受控重新抓取須沿用 owner fencing、原 quota／cooldown、prepared 與 receipts，
避免盲目刪 cache 或重設預算。此次沒有發出 native provider 請求、修改線上狀態或清空佇列。
剩餘 production 驗收項目見 [待辦](../dev/production-foundation-cutover-plan.md#issue-378-production-驗收範圍)。

## 五 provider 舊流程退役評估（2026-10-29）

同日 03:53 UTC 全 provider 唯讀查核：Backend 與全部 15 個 consumers 的
`PROVIDER_WORK_REQUIRED` 仍為 false；Twelve Data 25 筆與 FinLab 30 筆 daily work 均 suspended。
Shioaji Full 的 desired／observed 仍 running，其餘九個現行 Pilot／Full controls 均 stopped。
所有既有 provider owner slots 均未 attested，未查到 cutover audit；零 nonterminal ingestion run
只代表當下狀態，不能當作停止新 acquisition 後的 drain 證據。

全 provider 程式碼、production 函式雜湊與隔離重現結果支持退役舊 acquiring／無 work fence
acceptance，但必須先完成五 provider cutover、原交付保留與環境驗收。各 provider 差異與已實作的
退役邊界見 [provider-work 架構](../architecture/provider_work.md)。Repository 已移除舊執行入口，
當時 production cutover、native 驗收與部署尚未執行；沒有停止 Shioaji、改 flag 或清理 queue。
後續已授權的切換紀錄見下節；native 資料驗收仍獨立列管。

## Provider-work 切換與舊流程退役發布（2026-10-29）

本次目標版本為 staging 已 accepted 的 `f514cf32185d8d3b76784c062ec171a37665a381`
（PR #375），使用 annotated immutable tags `findb-v0.1.12`、`fetcher-v0.1.12`。
兩個 tags 的物件分別為 `0e4ab79fa02a178e9a04ce9f11ee108d9fa74678`、
`ce10157602bd0bcf0d153f1501ad57293cc750bf`；發布依既有 Backend-first promotion，沿用
staging digests，不重建 images。Backend／Fetcher 的 CI、acceptance、activation 均已成功。

停機前再次確認十個 Pilot／Full controls 的 desired／observed 全為 stopped、25 個 historical
requests 全為 terminal、沒有 running historical item 或 nonterminal ingestion run。Full／Pilot／
governor 沒有 body 已保存但 receipt 缺失的 work；13 份 Shioaji historical checkpoint 的
prepared-without-receipt、accepted-nonterminal 均為零。TAIFEX historical 兩筆 work 均保留原
prepared／receipt。原 failed Shioaji request 的一筆 `raw_uploading` 仍原樣保留，不重試或猜測交付。

全部十五個 consumers graceful stop，均 exit 0、無 OOM／Docker error。Fetcher host
`i-0e94c09b2af831808` 的 `/var/backups/findb-provider-work-cutover/production-20261029T1134Z`
保存完整原 container inspect、59 個持久化檔案、17 個 SQLite 的逐表 rows／SHA256 與
`state.tar`（486,963,200 bytes，SHA256
`a8223f4674605c1777aba769a30aa3426308be5ebb9d58063fce39b0b72c29c8`）。原 Full、Pilot、
governor、五 provider historical 目錄及 sidecars、prepared、receipts、account pins／budget／
cooldown 全部保留。RDS snapshot `findb-provider-work-production-20261029-1134` 已 available；
加密 EBS snapshot `snap-0e76651e38bfe7101` 已 completed。

離線以原 accepted protocol-v1 images 初始化五個共享鎖，再按原 image／argv／HostConfig／bind
mounts 重建 enforced consumers，僅將 `PROVIDER_WORK_REQUIRED` 設為 true；此階段不啟動。
原 17 個 SQLite 的所有既有 tables／rows 與備份指紋完全一致。原版 preflight 對真實 container
inspection、唯一 Fetcher host 與五個 flock 驗證成功；11:44:03 UTC 產生 proof，隨即以具名
Owner machine key 經正式 cutover API 提交，回覆成功。具名 audit actor 為
`Production_admin_owner_key`（machine），event ID `01a18779-b6aa-7b61-be03-6c5148015bfc`，
保存時間 11:44:23 UTC；Inventory SHA256 為
`ec58b2d89525ec41d2634a1a5e320a3c66b5a12c488620f6bb37676336679f79`。

| Provider | Attested shared lock identity |
| --- | --- |
| Twelve Data | `production:66305:302843` |
| FinLab | `production:66305:302860` |
| Shioaji | `production:66305:302872` |
| TAIFEX | `production:66305:302880` |
| FactSet | `production:66305:302881` |

兩組 deployment SSM parameter 均由 version 1 更新為 version 2，唯一變更為
`PROVIDER_WORK_REQUIRED=true`。兩組 runtime configuration 同步新增此值，其餘欄位不變；
Backend／Fetcher 新 versions 分別為 `8da4ea8d-8891-4934-8bbc-ec2e99105453`、
`b752d41b-d3d0-44be-b587-a961fca19c57`。

新建但未啟動的容器實際為 `created`，cutover helper 接受此準備狀態，installed-host guard
則要求 `running` 或 graceful `exited`。因此須在 Backend promotion 成功後、controls 仍停止且沒有
可 acquisition work／maintenance／rerun 時，啟動這批 enforced accepted consumers，確認真實
running／零重啟並執行完整 installed-host guard，再 dispatch Fetcher promotion；不偽造 Docker
State、不跳過 guard。本次這項啟動檢查已通過：15 個 consumers 均真實 running、零重啟，完整
guard 回報 `fetcher_host_eligibility=running`，五組 inode 與 Owner attestation 相同。

Backend 的 schema-v3 production accepted bundle 為
`findb/production/accepted/f514cf32185d8d3b76784c062ec171a37665a381/28244fc878a64439f1c879d27bf7107b019d11f228f8ad69897833467381526f.tar`。
Fetcher accepted bundle 為
`fetcher/production/accepted/f514cf32185d8d3b76784c062ec171a37665a381/3971df592c38b2465f7abed0679442c5f80cf5bb63520b10da81a2c722db3cd8.tar`；
兩份 accepted records 皆為 schema-v3／production／accepted，綁定同一 commit 與各自的 tag，
Fetcher 為 `runtime_profile=unified`。六個 digests 與 staging 完全相同：

| Image | SHA256 digest |
| --- | --- |
| Backend | `2db1da26a45542e0fb255d6f53c41ec879ff9751892693cddec5ca9454bb35d5` |
| Dashboard | `397166c8926c160fc6caed75d8c42bc7b8d309f63877b43a9ea0ea9b8436cbc3` |
| Twelve Data／TAIFEX | `30bd93485ff4d210aa9f264ae3476f0878ec4eb299af4bcfa92f7f48a761d99e` |
| FinLab | `05b8da4c683a12de3d9c2dd53dd9c87111800b78aa9a1764a76ebb870e14a1d0` |
| Shioaji | `705717720905898479aa5e49c70ec769d6f4331f5f6d5eac679b0a504217ec3b` |
| FactSet | `63ef339a2803732246ca8b8d0ee7d25e406fe0f8390bb7ea6b8c8f2619623ec2` |

DB revision 由 `0d57f9b8a2c1` 升至 `1e68a0c9b3d2`；此 migration 僅調整 constraint 名稱／日曆
CHECK／時間預設值，沒有改寫既有 frozen rows。部署後八個表的 rows 與完整內容指紋一致：
7 first-date states、101 plans、101 decisions、3,699 parts、182,413 members、25 historical
requests、32 historical items、17,772 provider-work receipts。舊 revision 的 accepted bundle
仍須通過現行 DB compatibility gate，不能直接啟動舊 image 或 downgrade 交付證據。

Enforced runtime 的既有 pending-plan projection 為 Shioaji 2026-10-27 的 equity／ETF minute
各補上一筆 FIFO work；兩筆原 plan 均於 10/28 建立，原 plan／members／checkpoint 未改寫。
原 Twelve Data 25 筆、FinLab 30 筆 work 保留，目前 57 筆 projection 全為 suspended、沒有 active
owner 或 acquisition／待 drain 工作；不是重新執行全部 backlog。原 Shioaji 28 筆、TAIFEX 15 筆
cancelled work 及兩筆 TAIFEX historical 終態也保留。公開 health 正常，normalization queue
沒有 queued／processing、unpublished outbox、expired lease 或 retry exhausted。部署不啟動既有
停止的 controls、不觸發 provider 資料抓取，也不宣稱 native 驗收完成。

最後一輪唯讀檢查確認六個 Backend／Dashboard services 與十五個 Fetcher consumers 均 running、
exit 0、零重啟、無 OOM／Docker error，實際 images／argv 與 installed release manifest 一致；
五個 Backend roles 與全部 Fetcher consumers 的 `PROVIDER_WORK_REQUIRED=true`。
完整 installed-host guard 再次通過，五組 lock inode 未變。十七個原 SQLite 的所有既有 tables／rows
與備份 SHA256 完全一致，包含原 prepared bytes、20,102 個 Full checkpoint receipts、帳號 pins、
acquisition intents、operation attempts、quota／pacing／cooldown；八個 DB frozen tables 指紋也再次
一致。`pnpm check` 與 `git diff --check` 通過。本次沒有執行 native canonical／recovery 或實際 rollback
演練，相關待辦繼續列於 [native 與回復驗收](../dev/production-foundation-cutover-plan.md)。

證據：停機／state 備份 `1a791b83-37d5-4dd5-910a-f300aaac5777`、DB frozen scope 基準
`57427f8b-9d67-41de-86b0-0ac128cf38d7`、enforced containers／原 rows 比對
`4a18c241-691c-4967-947f-777446c2e6dd`、host proof
`9279b1c1-4cee-43f6-a114-040a452987db`；Backend production workflow
[40867773717](https://github.com/FPI-TW/findb/actions/runs/40867773717) 已成功，Fetcher workflow
[40869478807](https://github.com/FPI-TW/findb/actions/runs/40869478807) 亦已成功。
Backend 後 frozen rows gate `795d241e-1e77-491b-b242-496f0b3ea6f8`、受控啟動／完整 host guard
`04ea1e7a-6992-4497-8e29-e3eea7d5b38b`、新增投影來源／具名 audit／原 frozen rows 比對
`7e36eada-2a8c-4b2b-aace-df902f522728`。
最終 Fetcher 保留性／images／完整 guard `79293c41-5839-4c31-82e0-0f7754d072da`、
Backend／Dashboard images／服務健康 `945729ee-678b-4517-a9c2-b78e80461ed9`、
DB frozen rows／controls／queue／Owner audit `ef962abb-4b81-4ed5-8e70-ac0d892539d8`。

## Shioaji 缺口與 DQ 調查（2026-10-05）

Production 2026-10-01、10-02、10-05 共六筆未結案 missing alerts，分別對應每日的
`tw_equity_minute` 與 `tw_etf_minute`。三日的 SQLite checkpoint 均顯示 equity sequence 1
（`2330`）成功，sequence 2（`2454`）在第一次 attempt 以 `KBARS_PAYLOAD` /
`contract_access` 終止；其餘 48 檔股票與 3 檔 ETF 尚未嘗試，排程窗口已到 cutoff。
這是 provider contract access 階段失敗，不能把未嘗試序列判定為商品無資料。

調查當時 runtime 固定使用 Shioaji `1.7.1`。隔離的 bounded simulation 驗證確認 login 成功後，
`api.contracts` 實際為 `ContractsApi`；讀取 `.stocks` 立即拋出 `AttributeError`。
直接呼叫 `api.contracts.get("2454")` 則回傳 `BaseContract`，identity 為
`code=2454`、`exchange=TSE`、`security_type=STK`、`region=TW`，並成功 logout。
失敗根因是 runtime binding 的公開介面與已安裝 `_core.pyi` 中的 legacy `stocks`
宣告不一致，不是商品無資料或已確認的 catalog timeout。唯讀 direct lookup 另確認 frozen
53 商品全部可取得：code 與 manifest 一致、security type 均為 STK、region 均為 TW，
其中 52 檔為 TSE、1 檔為 OTC，無需推測 exchange。

Provider fallback 已改為 [官方合約文件](https://sinotrade.github.io/tutor/contract/)
公開的 `api.contracts.get(code)`；維持 missing contract 分類、credential-free error 封裝，
以及 `2330`、`0050`、`0056`、`006201` 四個已驗證 `BaseContract` identity shortcut。
Production 53 商品 manifest、sequence 順序與 cutoff／terminal 政策維持原設定。

修復 provider 不會自動重設既有 terminal checkpoint 或補送上述缺口。Production 現可透過
[Shioaji 歷史回補](data_maintenance.md#shioaji-股票etf-分鐘回補) 分別申請股票／ETF minute，
以 reviewed production 50／3 成員 manifest 建立新 request，保留 daily terminal checkpoint。
上述六筆 alerts 是當次調查紀錄；實際缺口須重新查證並完成回補驗收後，再人工處理對應 alerts／DQ。
不可刪除 SQLite state、鬆綁 daily cutoff 或把 terminal error 自動轉為 retry。
部署驗證與歷史缺口復原須各自留下驗收證據。

同次唯讀調查的 26 筆未解決 DQ issues 均為 `INGRESS_DELIVERY_POLICY_WARNING`，severity
為 `warning`。其中 16 筆為 Shioaji 09-21 至 09-24 的 historical payload 在 09-30 送達，
觸發 `LATEST_DATE_MISSING`（expected latest date 為 09-29）；另 10 筆為 FinLab snapshot
低於 delivery policy 最低 2,100 筆，五次各 50 筆、五次各 2 筆，其中四次同時缺 latest date。
Bounded FinLab production manifest `fetcher/configs/finlab_tw50_2026_09_21.v2.json`
只選 50 檔，與 full snapshot 的 2,100 筆 threshold
不相符，須獨立檢視政策與執行範圍。這 26 個 ingestion runs 均為 completed，合計成功
寫入 3,668 筆，`failed_records` 合計為 0；warning 不阻擋 canonical 寫入。
DQ 問題需逐筆經管理流程人工結案；provider 修復或部署不可自動 resolve，也未授權修改
DQ／delivery policy。

## Foundation apply

1. 使用 `findb-production` profile 驗證 caller identity，先在
   `infra/tofu/production-bootstrap` 執行 `fmt`、`validate`、refresh plan。Bootstrap plan 只能
   建立 state KMS 與 `findb-production-tofu-state-289112218471`，不可出現 delete。
2. 經核准後 apply bootstrap，將其 local state 遷移至獨立的
   `production/bootstrap.tfstate`；確認 versioning、public access block、SSE-KMS 與 lockfile。
   遷移前先將 `production-bootstrap/backend.s3.tf.example` 複製為 ignored `backend.tf`，再使用
   `bootstrap_backend_migrate_command` output；確認 remote state 完整後才安全刪除 local state。
3. 以 bootstrap output 的 KMS ARN初始化 `infra/tofu/production`。保存 refresh plan，確認
   account output 為 `289112218471` 且沒有 delete，再由受信任 operator apply。
4. 同一 PR 對 `infra/tofu/staging` 執行 refresh plan，新增的兩個 promotion-reader role 之外
   必須 zero-delete。Reader 只可讀自己 unit 的 `*/accepted/`、acceptance record 與 ECR layers。

禁止從 PR runner apply；禁止在 state bucket 存 application secret；禁止把 bootstrap saved plan
拿來 apply foundation。任何 account、region、resource prefix 或 delete 不符即停止。

## Live foundation acceptance

- VPC CIDR 必須是 `10.20.0.0/16`，無 NAT；RDS subnets 無 internet route。
- FinDB `t3.large`、Fetcher `t3.medium`，IMDSv2 required、hop limit 2、無 key pair、無 TCP 22。
  FinDB 443 只接受 Cloudflare published CIDRs；Fetcher security group 無 ingress。兩台host都必須安裝
  Docker Engine與Compose v2；`docker compose version`是部署前必要檢查。
- RDS 必須為 private PostgreSQL 16、`db.t4g.medium`、100 GiB gp3、KMS、Single-AZ、14 日
  backup、deletion protection，5432 僅接受 FinDB security group。
- 五個 ECR repository 必須 immutable 且 scan-on-push；instance role 只 pull 自己 unit，deploy
  role不能讀 Secrets Manager values，promotion role 只寫自己 unit 的 ECR 與 bundle metadata。
- 兩台 host 必須出現在 SSM managed nodes；使用 bounded SSM command 驗證 instance role、unit
  ECR pull、unit bundle read、cross-unit denial、runtime secret isolation與 RDS connectivity。
- 驗證 CloudTrail multi-region/log validation、GuardDuty、DLM recovery point、RDS automated backup、
  CloudWatch alarms 與寄至 `tyler.ho@fpitw.com` 的 SNS subscription。Custom metrics 在 collector
  尚未健康發布時必須因 missing data 告警，不得把 `INSUFFICIENT_DATA` 當通過。
- 所有資源必須有 `Project=findb`、`Environment=production`、`DeploymentUnit`、`Owner`、
  `BackupOwner`，並在 Cost Explorer 啟用這些 cost allocation tags；不建立固定 Budget。

## External resources 與 secrets

Cloudflare 帳號中建立 private `findb-production-raw` 與 `findb-production-canonical` R2 buckets。
Raw lifecycle 30 日並保護前 7 日；Fetcher raw credential只可讀寫 raw。Canonical publisher只可
讀寫 canonical，Serve reader只可讀 canonical，三組 access key/secret不得相同。管理 credential
不得注入 runtime。

建立 proxied `findb.tingfong.com` A record 指向 FinDB EIP，origin 使用獨立 Cloudflare origin
certificate。Fetcher EIP 的 `/32` 必須成為 `SOURCE_ALLOWLIST_CIDRS`。憑證、R2、DB、RabbitMQ、
四類 API key 與三個 provider credential 全部重新產生，分別寫入 OpenTofu 已建立的
`findb/production/findb/*` 或 `findb/production/fetcher/*` metadata；不得重用 staging 值。
Origin certificate secret固定使用單行 base64 欄位
`FINDB_ORIGIN_CERTIFICATE_PEM_B64`與`FINDB_ORIGIN_PRIVATE_KEY_PEM_B64`；部署只在instance上解碼、
驗證hostname、至少30日效期與keypair一致性，再原子寫入`/run/findb-runtime-secrets/nginx/`。
憑證與私鑰不得持久化到`/home/ubuntu`或進入GitHub Environment。部署產生的三個非秘密nginx
設定檔必須在驗證前正規化為root-owned `0644`；tmpfs中的憑證、私鑰與lookup key仍維持`0600`。

## 共用 credentials 與初始化

五個 provider Source scopes 在 staging／production 相同，只有 credentials values 不同：

| Source | Dataset allowlist |
| --- | --- |
| `twelve_data` | `us_equity_eod`、`hk_equity_eod` |
| `finlab` | `tw_equity_eod`、`tw_etf_eod` |
| `shioaji` | `tw_equity_minute`、`tw_etf_minute` |
| `taifex` | `tw_futures_eod` |
| `factset` | `hk_equity_eod` |

`reconcile_fetcher_credentials.py --runtime-profile unified` 只校準 allowlist／hash 並留下 audit，
不啟動 scheduler。五把 Source keys 相互隔離；現行execution manifest的HK feed使用FactSet，
Twelve Data保留HK allowlist僅供既有或已凍結工作的相容交付。有效日曆與執行政策由Source提供，
不再注入calendar Serve key。
跨 unit bridge 只傳 SHA-256，
instance roles 不放寬跨 unit Secrets Manager 讀取。
TAIFEX 公開 API 不需要 provider credential，但需要獨立 Source key；IaC 固定宣告此 metadata。
S3 state backend 必須使用既有 KMS key ARN，不能任意替換 encryption 設定。

## DB 與 calendar

兩環境共用 migration、application role 最小權限校準、registry 與 credential 初始化；
部署不執行 calendar seed，也不要求日曆完整。Published 日期優先，缺少日期採 weekday default。
新 controls 均 stopped；不得複製 staging canonical／raw／workflow data 至 production。
Candidate 必須先通過 migration 與內部健康驗證，才保存 acceptance；失敗保持停止，不讓舊程式操作新 schema。
詳見 [統一切換與回退程序](deployment.md)。

`backend/configs/calendars/` 的 2026 US v2、HK v1、CN v1、TAIFEX v1 JSON 各有
365 個明確日期、session 時間、來源 URL／原始檔 SHA-256 與 2026-10-02 review 註記。
TW 2026 沿用既有 immutable `holiday_schedule_115.csv`，2026-10-02 核對
[TWSE 官方開休市頁](https://www.twse.com.tw/zh/trading/holiday.html)及其
[2026 年度 JSON](https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20260101&response=json)
的 27 筆日期、名稱與開休市分類完全一致（換行格式不同）；原始 JSON SHA-256 為
`7fefe785ea7155a5004a2eb74486ad865ea5c4b5f02ee0cffbbdacb1ca2ea390`。
保留 2/12、2/13 無交易但可結算交割的 `settlement_only`，不以 TAIFEX 的休市分類取代。
HK 使用 HKEX 年度通告校正國票 HTML 遺漏的 6/19，2/16、12/24、12/31 為半日開市，
採 closing auction 最晚界線 12:10，正常日 16:10。US 依 NYSE 保留 10/12、11/11 開市，
11/27、12/24 於 New York 13:00 提前收市；其餘 open rows 為 09:30–16:00。
CN 取國票 PDF「上海及深圳」本地市場列並核對上交所年度安排，不以北向／南向通休市
代替本地股市；4/3、4/7、7/1、10/19 維持開市，例假補班週末仍休市，收盤 15:00。
TAIFEX 僅取官方年度 PDF 的 regular 台灣指數期貨交易日期，2/12、2/13 是休市，
不能複用 TWSE settlement-only 狀態；regular 收盤 13:45，彩色到期／掛牌標記不視為休市。
個別契約最後交易日、國外商品與臨時停市不由此 fixture 推測；臨時異動需 Owner 新 revision。
After-hours 仍保留交易所 attributed trade_date，不能轉成民曆日期或建立 continuous futures。

`seed_production_calendars.py --deployment-target production` 預設只驗證檔案；
`--apply` 才使用既有 preview → apply → publish 流程發布缺少的完整年度。
任何未知來源、operator override、draft 或不同 rows 均拒絕覆寫。
已存在且 source metadata、checksum、365 筆 rows 全部符合舊版 NYSE bootstrap 的 US 2026，
一般 seed 保留 revision 並回報 `action=pending_reviewed_update, upgrade_required=true`，
不阻塞一般部署，也不宣稱新 session 已套用。Owner 審核後才以 `--apply --upgrade-reviewed-2026`
發布 successor，透過 expected revision 的 CAS 防止競爭，原 revision 保留為 superseded。
HK／CN／TAIFEX 初次套用與 US successor 均不改動 scheduler admission、首次日期、
readiness 或 scheduler desired state；本 repo fixture 的更新不等於 live DB 已調整。
