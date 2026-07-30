# 台灣一分鐘資料契約與後續架構

> 狀態：`market_minute.v1` ingress 與 `market_minute_archive.v1` archive manifest
> 已發布為契約，且 workflow/canonical DB 骨架已建；尚未實作 Source runtime 寫入、normalizer、monthly partition automation 或 Serve 查詢。

## 範圍與單位

目前 dataset 僅為 `tw_equity_minute` 與 `tw_etf_minute`，兩者使用
`market_minute` schema version `1`。每一列是 Taiwan-local `trade_date` 的一分鐘
OHLC bar，並帶有 UTC-aware、正規化 UTC 的 `bar_start_time`、`bar_end_time` 與
`signal_time`；v1 固定 `bar_end_time = bar_start_time + 1 minute`，且
`signal_time = bar_end_time`。同一 delivery 的自然鍵 `(symbol, bar_start_time)`
不得重複。

`market_timezone` 固定為 `Asia/Taipei`，`price_adjustment` 固定為 `none`，
`trade_count` 固定為 `null`（unavailable）。OHLC、volume、turnover 的含義是
canonical values：Shioaji 的 `Volume` 是 lots，Fetcher adapter 必須先乘以 1,000
轉為 shares 才能進入 contract；`Amount`/`turnover` 是 TWD。

Shioaji `ts` 不是 UTC instant，而是台灣 wall-clock nanoseconds。adapter 必須先以
`Asia/Taipei` localize，將它視為 bar end，再推導 start；不可直接把 provider
timestamp 當作 UTC。

## Delivery sequence

minute batch 必須帶固定的 `snapshot_id`、`daily_update_id`、`universe_id`、
`symbols_sha256`、`sequence` 與 `sequence_count`，且 delivery mode 固定為
`sequenced_snapshot`（不可誤用代表完整 universe 的 `full_snapshot`）。symbols checksum 是排序後的
symbol，以 LF 串接 UTF-8 bytes 的 SHA-256。每 sequence 的治理上限是 50 檔及
15,000 rows；實際 payload safety 上限仍由 runtime 的
`SOURCE_MAX_DATA_ITEMS` 與 `SOURCE_MAX_PAYLOAD_BYTES` 設定執行。

minute identity 必須精確重建。先以 UTF-8、compact、sorted-key JSON 編碼物件
`{"data_date":"YYYY-MM-DD","dataset_key":"…","sequence":N,"snapshot_id":"…"}`，
再取 SHA-256 hexadecimal digest。`request_key` 是單次 request identity，固定為
`mmr:{digest}`；`idempotency_key` 是可安全重送的 sequence identity，固定為
`mms:{digest}`。兩者共用相同 canonical components、以不同 prefix 區分，因此即使
`snapshot_id` 長達 100 字元，兩個 key 仍固定為 68 字元；producer 不得自行命名。

`symbol_statuses` 對每一檔明確記錄 `data`、`expected_no_data` 或 `error`，非
`data` 必須附 reason。Shioaji batch 必須保存 provider usage 的 before/after 計數。若 provider
volume 或 amount 無效，adapter 可以把 row 值轉成 `null`，並以不含 provider-specific
credential 的 anomaly 記錄 `field`、字串化原始值、reason 與原始值 checksum，供後續
DQ 使用。這是雙向證據：每個 `null` 的 `volume` 或 `turnover` 必須有且只能有一筆
相同 `(symbol, bar_start_time, field)` 的 anomaly；反過來 anomaly 所指欄位必須是
`null`，所以 non-null 欄位不可攜帶 anomaly。payload 不得包含 secret、API key、session
或 credential。

## Maintained universe

Production universe 採雙來源交集：

- Shioaji contracts 決定 provider eligibility；
- TWSE／TPEx 官方清單決定正式 membership 與股票／ETF 分類。

來源不一致或分類衝突時不得自動加入，必須建立 universe DQ。每日
`14:00 Asia/Taipei` 產生 candidate，新版本從下一個交易日生效；`14:30` 的抓取固定
使用當日已生效版本。商品消失須連續兩個交易日確認才結束 membership，且只關閉
membership、不刪歷史。可證明為同一商品的代碼變更保留 `instrument_id` 並新增
identifier，否則建立新 instrument。

單一 dataset 異動超過 20 檔或 2% 任一門檻時 fail closed。每個 published universe
必須保留 Shioaji snapshot、官方來源版本、差異、checksum、判定與發布時間。

Staging 固定只使用 `2330`、`0050`、`0056`、`006201`，不隨 production universe
自動擴張；可以更新這四檔 metadata。

## Daily update 與 publication barrier

每日一個 `daily_update_id`，只綁定 environment、market 與 trade date；股票與 ETF
各自建立 snapshot，並各自 pin dataset、`universe_release_id`、排序後 symbols
checksum 與 `sequence_count`。每個 snapshot sequence 最多 50 檔，股票與 ETF
sequence 交錯啟動；不需等待上一個 normalization terminal 才開始下一分鐘的抓取。

Shioaji 治理上限是每 60 秒 50 requests，每個商品仍是一個單日 Kbars request。單一
provider request 最多三次嘗試，重試同樣計入 quota；當日 `17:00 Asia/Taipei`
截止。截止後不得自動做跨日 Shioaji catch-up，缺口只能由本地 archive 修復。假日依
published trading calendar 標記 `skipped_calendar`，不建立空 snapshot。

Publication revision 分為：

- `completed`：所有 sequence terminal 成功，且每檔有 data 或可解釋的
  `expected_no_data`；
- `completed_with_warnings`：可保存 immutable revision，只有全部 warning 都是
  `expected_no_data` 時才可提升為 `latest`；
- `unresolved_gap`：可留作稽核，但永遠不能成為 `latest`。

任一 sequence 遺失、terminal error、無法分類缺資料、usage 異常，或單批空回應
超過 5%，都使 daily update 失敗。5% 是 provider 熔斷門檻，不是資料品質容許率；
系統不人工補零量 K。Serve 與一般 export 只能讀取已發布 revision。

publication warning 中的 `expected_no_data` symbols 必須與 snapshot 的
`symbol_results` 逐一交叉核對，不能只信任 aggregate warning。Universe candidate 在
candidate → published transition 前必須已有 members；DB 會重算 source checksum 與
member checksum，提交的 checksum 不可取代 canonical evidence。

## Archive release

`market_minute_archive.v1` 是獨立、immutable 的 finalized release manifest，不是
Source ingest payload。它記錄 dataset、連續月份 coverage、instrument 清單、sequence /
chunk counts、總 rows、checksums、object keys 與 immutable object evidence。每個 chunk
帶 `snapshot_sequence`、`chunk_sequence` 與同一 snapshot 的 `chunk_count`；一個
snapshot sequence 可有多個 chunk，每個 chunk 最多 5,000 rows。object key 不得是
presigned URL 或含 credential 的 URL。

Archive Source identity 固定為 `tw_recorder_archive`，metadata 仍記錄原始上游
Shioaji，且 `overlap_precedence=direct_daily_shioaji`；重疊時 direct daily Shioaji
優先。manifest 必須帶 immutable `trading_calendar_checksum`、非空且嚴格遞增的
`expected_trading_dates`／`covered_trading_dates`，兩者必須逐日完全相同；
`trading_dates_sha256` 是 LF 串接 ISO expected dates 的 SHA-256。每個 covered month
必須至少有一個 expected trading date，這是 calendar-revision/checksum 支持的 strict
no-gap barrier。Release 必須涵蓋一個或多個連續月份，
chunks 可重試與亂序送達，但只能在 coverage、row counts、checksums、所有 sequences
與 continuity 全部驗證後原子 finalize。交易日 coverage 必須連續；不人工製造無成交
分鐘，`expected_no_data` 必須有原因。新 release 必須銜接目前最早 published
coverage，可保留少量交易日 overlap；overlap 不一致視為 correction conflict，archive
continuity 問題不得以 warning 放行。

發布順序是先寫完並驗證所有 immutable objects，再以單一原子 publish barrier 發布
`release_state=finalized` manifest；消費者只能看見 finalized manifest。archive schema 與
manifest 可用下列方式重建或檢查：

DB foundation 亦採 fail-closed finalization trigger：release 必須先保持 `staged`，待實際
chunks 的 rows、sequence continuity、月份 coverage、instrument／chunk／object checksums
與 finalized manifest 完全一致後才能轉為 `finalized`；finalized release 與其 chunks
之後皆不可修改或刪除。

```bash
uv --directory backend run python scripts/export_archive_contracts.py
uv --directory backend run python scripts/export_archive_contracts.py --check
```

archive staged → finalized transition 由 DB 重算 canonical manifest checksum，並驗證每個
covered month 都有實際 chunks；payload 提供的 counts、checksums 或月份清單不能取代
這些 canonical evidence。

## Hot／cold storage 與 API 邊界

RDS hot table 依 Taiwan-local `trade_date` 月分區，目標保存當月加前 60 個完整月份，
最多 61 個 partitions。最近五年 backfill 寫入 RDS 與 Canonical R2；更舊資料直接走
bounded staging 產生 Canonical R2，不得先永久灌入 primary RDS 再刪除。Workflow、
revision、manifest、DQ、correction 與 audit metadata 永久留在 RDS。

Canonical R2 使用獨立 bucket，不與 raw bucket 共用。月內先發布每日 immutable
objects，月底 compact 成按 market、dataset、year、month、revision 分區的 monthly
shards；shard 內依 `instrument_id, bar_start_time` 排序。Publisher 使用 write
credential，API signer 只使用 read/sign credential。Request-specific deliveries
保存七天，signed URL 一小時並可重新簽發。

Serve 必須維持唯讀：

```text
GET  /api/v1/serve/minute
POST /api/v1/exports/minute
POST /api/v1/exports/minute-market
GET  /api/v1/exports/{job_id}
GET  /api/v1/exports/{job_id}/manifest
```

Serve minute 一次只查一檔與 RDS hot range；超出時回 `export_required`，不得代為建立
job。單檔 export 可取得該檔全部 published R2 歷史；market export 使用當前
maintained universe、最多五年且僅提供 Parquet。Manifest 必須 pin canonical
revision、實際 instruments、universe version、row counts、checksums、coverage 與
`price_adjustment=none`，不足要求範圍時標記 `coverage_status=partial`。

## Credentials 與 provider compatibility

Staging、production 各用不同 Shioaji 帳號、limiter 與 Source client key。Credentials
只從環境 secrets/config 載入，不得進入 log、manifest、raw payload、文件或 generated
contracts。Production 歷史回補不得向 Shioaji 跨日取得。

Fetcher 實作前須 pin 並驗證 Shioaji SDK。既有參考專案使用 `1.3.3` legacy
`api.Contracts`；`1.7` 改為 `api.contracts`，登入參數與 contract detail 取得方式亦有
破壞性差異，因此必須用明確 compatibility layer 與真實 staging smoke，不能直接替換
版本。

## 後續工作（未實作）

以下是已界定但未在本里程碑完成的架構，文件不代表 runtime 已支援：

- provider adapter、credentialed login 與 live smoke；
- scheduler／acquisition runtime；
- deployment 與 staging rollout；
- production archive import；
- RDS hot storage 的 61 個 monthly partitions automation；
- 永久 Canonical R2 archive 與上述 publication barrier 的實際儲存流程；
- minute normalizer、DQ、canonical 寫入與 dataset registry/runtime routing；
- 保持唯讀的 Serve API minute query；
- 與 Serve API 分離的 Export API。
