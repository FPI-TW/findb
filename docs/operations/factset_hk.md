# FactSet 港股日行情

FactSet 負責切換後的新港股 EOD 抓取；Twelve Data 繼續負責美股。港股不自動 fallback。
程式更新不等於環境已切換：正式抓取仍需要 provider-work enforcement、已發布的
HKEX baseline、明確的 HK provider cutover，以及 Owner 啟動相應 Pilot／Full control。

## 清單與歷史範圍

清單以[港交所股本證券](https://www.hkex.com.hk/market-data/securities-prices/equities?sc_lang=zh-hk)
及官方 `ListOfSecurities.xlsx` 為主，每天由 FactSet maintenance work 更新一次。
只納入主板／GEM 普通股；分類不明、找不到 listing ID、幣別不符或 mapping 歧義均保留 gap，
不得從 expected 成員中刪除。[TradingView 現行股票清單](https://tw.tradingview.com/markets/stocks-hong-kong/market-movers-all-stocks/)
只作輔助核對。

Daily plan 凍結其 published release；歷史 request 在建立時凍結當下 published release、
member checksum 及完整成員 mapping，所有日期共用這份快照。清單刷新不會重寫原 request。
回補對象是申請當時的現行股票，不包含已不在清單中的下市商品，具有存活者偏差，
不得標示為歷史全市場。Production 最長 366 calendar days，local/staging 最長 31 天。

首次 baseline 尚未發布時，使用已部署 FactSet image 的正常
`findb-fetch-runtime --execution-profile pilot --provider factset --sync-universes`，並帶入該環境
原有 config、Pilot checkpoint 與 shared account／provider-work mounts。維護使用 Source 的
maintenance 授權，不必先啟動 daily control；不得以獨立無 owner 的 MySQL probe 代替。
CLI 回傳 `queued` 只表示排隊，須追到 maintenance terminal、原始證據與 release 才算成功。
工作 key 由 CLI 依 UTC 日期產生，同日重跑不另造 identity；HKEX 檔案日期則按香港日期驗證。
Future-dated 失敗保留原 journal 與用量，待正常日期建立新工作，不改時鐘或重置預算。

首份 release 由 Owner 呼叫 `POST /api/v1/admin/universes/{release_id}/publish`，提供
`first_baseline_approved=true` 與可追溯的 `evidence_note`。發布前核對官方檔案日期／SHA-256、
raw reference、成員總數／checksum、mapping gaps 與 maintenance 結算；gaps 不得刪除。
每日刷新驗收須保存不同正常日期的 work／release 與原始證據，並核對既有 daily plan 的
release ID、members、parts 及 checksum 未變。單日重跑或本機 fixture 通過不代表已驗證跨日更新。
自動 maintenance 由 running cycle 在 refresh due 時入列，controls stopped 時不保證每日刷新；
需明確執行上述維護入口。既有發布差異門檻與人工例外規則維持。

Daily 新 I/O 僅於香港時間 18:30（含）至 23:00（不含）進行；已準備好的交付仍須排空。
Pilot 按 canonical 識別排序取前兩檔。`FACTSET_SKIP_NON_TRADING_DAYS=true` 是預設值；
歷史回補略過休市日。缺少已發布日曆時沿用既有週一至週五規則。

## MySQL 與價格映射

Fetcher 使用獨立 FactSet image 與 Source credential，不連 FinDB DB。設定：

| 設定 | 意義 |
| --- | --- |
| `FACTSET_HOST`、`FACTSET_PORT` | RDS hostname 與 port，預設 3306 |
| `FACTSET_USER`、`FACTSET_PASSWORD` | 來源唯讀帳號，僅提供給 FactSet consumer |
| `FACTSET_SSL_CA_FILE` | 必須存在的 CA bundle；image 預設 `/app/certs/ap-southeast-1-bundle.pem` |
| `FACTSET_QUERY_TIMEOUT_SECONDS` | 1–60 秒，預設 30 |

部署 secret catalog 使用 `provider/factset` 與 `api/source/factset`，後者包含
`FETCHER_FACTSET_SOURCE_CLIENT_KEY`。密碼不進入 argv、錯誤訊息、工作紀錄或 account identity；
account identity 使用 host／port／user，密碼輪替不會重置用量紀錄。

連線驗證 TLS CA 及 hostname，採 read-only transaction、參數化 SELECT、server query timeout、
最多 10,000 個 listing／16 MiB 回應。每個 frozen cohort／日期只查詢一次。
Connect 與 SELECT 前分別重驗授權；阻塞呼叫在繼承 whole-work FD 的子程序執行，
timeout 必須 terminate／kill／reap 後才能釋放 owner。

來源表為 `factset.fgp_v1_fgp_global_prices`，主鍵 `fsym_id`／`price_date`：

| 原生欄位 | 契約欄位 |
| --- | --- |
| `price_open`／`price_high`／`price_low`／`price` | open／high／low／close |
| `volume` | 可無損轉為非負 bigint 的 volume |
| `currency` | 與 frozen HKEX member 一致的幣別 |

映射以 HKEX symbols 限定 `sym_v1_sym_ticker_exchange_hist` 的 ticker 主鍵查詢範圍，
再核對 `sym_v1_sym_ticker_exchange` 的相同 ticker／listing 當前關聯，以及
`sym_v1_sym_coverage` 的 HKG／SHARE／listing 記錄。固定 join 順序避免掃描整張 coverage；
缺少歷史索引候選或當前關聯時保留 mapping gap。
只接受 listing `*-L`，不以 regional ID 替代。`first_price_date` 與 ticker history 起訖
不作上市、下市或停復牌證據。Turnover 單位尚未核定，僅保留 native raw，不寫 canonical turnover。

## 資料錯誤、結算與修補

輸出 `market_eod.v2`：僅 OHLC 大小關係移至逐列 DQ，其餘入口限制保持。
Native bytes 先持久保存，再逐一處理成員；結構錯誤與缺列保留 raw 證據並繼續其他成員。
缺列是 missing，不是 no_data；目前來源沒有可用的每日停牌／無交易證據，adapter 不造 no_data。

EOD v2 daily/historical 結算核對完整 frozen member 集、所有永久 receipts、實際寫入證據及
DQ／raw 缺漏證據。所有 accepted runs 必須 terminal，原 prepared 必須已交付或有合法
終結證據。存在資料缺漏時結果為 `partial`；historical 繼續下一日期，最終 request 仍標示
partial。暫時來源／Source／raw storage 故障停止新 I/O，close、drain 後 FIFO tail retry，
不能用 partial 掩蓋。Native response、prepared bytes、receipts 與 operation budgets 跨 retry 保留。
窗口／control 停止也走安全 tail retry；只有該操作尚未 dispatch 時退還預留次數，已開始的
來源 I/O 仍計入原預算。已知永久來源、raw storage 或 Source 拒絕會 close／drain 後結算
failed，保留尚未交付 prepared 的人工處理證據；fence 或 ownership 不確定則仍停止並要求復原。
Production planner 的 `--deliver --wait` 遇到 partial／failed／cancelled 立即輸出終態並回傳
exit code 3；只有 completed 回傳 0。未指定 `--deliver` 仍只是 preview。

每日告警依 expected trading days 計數；`EOD_LONG_GAP_TRADING_DAYS` 預設 5，可設 1–366。
有效 data 或持久 no_data 中斷該成員的 streak；休市不計。沒有 frozen plan 時記錄 feed 缺漏，
不推測成員範圍。

來源修正後，可建立新的歷史回補 request。相同 provider／dataset／日期、相同 frozen
上市識別碼的 historical 實際寫入，可解除 daily monitoring 的缺漏；原 daily work 的 partial
結算與原始證據仍保留。其他 provider、不同上市識別碼、單純成功筆數或既有價格不能補足證據。
任一 work 的 completed 結算仍只採認自己的 receipts，不能借用另一工作的寫入證據。

## Backend-first 切換

1. 更新 backend、執行 Alembic head 與 seed。HK registry 同時保留 v1、v2 和
   `twelve_data`、`factset` acceptance，舊 prepared／raw rerun 按原 identity 路由。
2. 安裝 FactSet image／credentials；所有新 controls 預設 stopped。Fetcher bundle v2
   包含四個 images、五個 providers、十五個 Pilot／Full／Historical consumers。
   舊 bundle v1 只供既有安裝辨識；新 release 與 promotion 必須包含 FactSet digest。
3. 依 [provider-work 維運](../architecture/provider_work.md) 完成單 host／shared inode／所有 consumers
   停止及排空的 preflight attestation，再開啟 `PROVIDER_WORK_REQUIRED`。
4. 使用 FactSet Pilot maintenance 同步 HKEX 清單，由 Admin 確認並發布第一份 baseline。
   之後沿用既有差異門檻與人工例外核准規則。
5. 停止 Twelve Data 與 FactSet 的 Pilot／Full controls，排空 active owner。以香港當日日期
   執行 `uv --directory backend run python scripts/cutover_hk_factset.py --effective-date YYYY-MM-DD --actor OPERATOR`。
   預設驗證後 rollback；加入 `--apply` 才提交。切換決定不可改寫或回溯日期。
6. 切換 transaction 保留原 dataset/profile first dates 與 plans，記錄 audit，設定 FactSet
   priority -1、Twelve Data priority 0，並使舊 HK Twelve Data stored priority 一致，保留價格與 lineage。
   FactSet 新 daily 日期從切換日開始；更早日期使用 historical request。
7. 完成兩檔單日、異常歷史樣本、全市場 native acceptance 後，再由 Owner 啟動所需 control，
   依既有 staging acceptance／production promotion 流程交付。

不得在仍有 cohort、partial、v2 run 或切換紀錄時 downgrade 此 migration。Rollback 亦須保留
支援 v2 acceptance、FactSet scopes 與五 provider inventory 的版本；不以刪除證據方式降版。

### Cutover 與原生連線驗收證據

切換前保存當前 accepted Backend／Fetcher bundle、Alembic revision、DB／checkpoint 備份、
兩 provider 的 Pilot／Full controls revisions、first dates、plans／members fingerprints，
以及 shared lock identity。以 CAS 停止四個 controls，等待 desired／observed stopped，
核對兩 provider owner 已釋放及在途 prepared／receipts 已排空；心跳過期不代表 owner 已釋放。
先執行 cutover script 的預設 dry-run，再以同一香港日期與具名 actor 執行 `--apply`，
保存 decision／audit 與上述 fingerprints 的前後比對。跨香港午夜須重做日期核對。

HK provider decision 不可改寫，應用 rollback 不代表把 HK 新抓取切回 Twelve Data。
候選 rollback 版本須維持目前部署的 provider-work owner 到期證據契約，以及 EOD v2、
cohort／partial、FactSet scopes 與十五 consumers；accepted 身分、migration compatibility
及停止／排空要求依 [部署維運](deployment.md) 核對，不只比較 image tag。

FactSet maintenance 成功可提供 staging 到 RDS 的 TLS／原生 catalogue SELECT 證據，須核對
實際 image 的 CA／hostname 必驗、read-only transaction、timeout 及持久原生 response。
行情驗收另須在正常 owner／授權流程下取得 frozen listings／日期的 prices SELECT，保存 raw
SHA-256、查詢範圍與 terminal 結果。只看到 container running、Secrets Manager 配置完成、
mock TLS 測試或本機快照重播，均不足以完成 staging 實際連線驗收。
密碼與完整 connection string 不得進入輸出。
