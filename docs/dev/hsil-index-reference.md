# 港股產業分類與恒指成分股（HSIL 參考資料）

> 狀態：設計已於 2026-11-24 由 Owner 核准，正在分 PR 實作。
> 完成後依 `docs/dev/` 的規則，把仍有效的內容整併到 `docs/` 的現行文件，並刪除本檔。

## 已定事項（Owner，2026-11-24）

- **收兩種資料：**
  - 港股產業分類：HSICS 行業層，共 12 類。
  - 恒生指數各產業成分股名單：HSI 的四個分類指數，以及 HSCI 的 12 個行業指數。
- **HKEX 沒有可下載的產業清單。** HSICS 屬恒生指數公司（Hang Seng Indexes Company，下稱 HSIL），資料取自 hsi.com.hk 公開的檔案。
  - 曾否決 HKEX 個股報價頁的 widget：要用頁面內嵌的 token 逐檔查詢，HKEX 網站條款也禁止未經書面同意的自動擷取。
- **放在 `hkex` provider 內，不新增 provider。**
  - Source provider 名稱維持 `hkex`。
  - 實際發布者以 `document.publisher = "hsil"` 與文件 URL 記錄。
- **僅供內部使用。** HSIL 的指數資料若要對外轉發，需要另簽授權。
  - HSIL 的 PDF 在中繼資料中標示「不允許擷取文字」；pypdf 與 pdfminer 都會忽略這個標示。Owner 於 2026-11-24 確認維持「僅內部使用、可以擷取」。
- **產業分類只收 HSCI 成分股**（約 580 檔），只到行業層；非成分股沒有分類。
- **歷史採混合做法**（2026-11-24 Owner 決定）：
  - 有每日快照的日子以快照為準（本機歸檔從 2026-10-02 起）。
  - 更早的日子由變動 xlsx 往回推到 2008 年，並標為「推算」。
  - 兩份資料對不上的地方記為 DQ warning，不擋寫入。
- **不收檢討結果新聞稿**（2026-11-24 Owner 決定）：
  - 每日 PDF 與變動 xlsx 已涵蓋所有已生效的名單、權重與分類。
  - 新聞稿只多「尚未生效的預告」，下游目前用不到；而且它是自由格式文字，HSCI 行業指數的變動也不在其中（另見「指數通告」）。
  - 之後若需要預告，再另外加回。

## 來源（公開 HTTPS GET，不需認證）

API 前綴：`https://www.hsi.com.hk/api/wsit-hsi-ddoc-ea-public-website-proxy/v1`。

| 文件 | 取得方式 | 內容 |
| --- | --- | --- |
| 成分股表現 PDF（每個交易日一份，保留約 36 個交易日） | 清單：`/product-data/pub/series/metadata/v2?data=dailyReport&seriesCode=hsi\|hsci`（reportType `con-pdf-1`）；下載：`/content/gcs/download?resource=<url>` | HSI：95 檔，含權重與四個分類指數的權重。HSCI：580 檔，含 HSICS 行業代碼與中英文名稱、權重；另列行業指數與大中小型股指數。網站上的「月報」就是每月最後一份 |
| 成分股變動 xlsx | `/content/gcs/download?resource=/public-website/webapp/contents/en/indexes/hisConstituent/{hsi\|hsci}/hist_{hsi\|hsci}.xlsx` | 每個指數一個 sheet，sheet 名稱即 indexShortName。逐筆列出生效日、加入或刪除、變動後檔數。只列已生效的變動 |
| 目前 HSI 名單 JSON | `/product-data/pub/constituent/metadata/v2?data=rt&seriesCode=hsi&language=eng` | 只用來交叉核對；HSCI 系列只公開檔數 |

## 動工前實測（2026-11-24）

- **標頭**
  - GCS 下載沒有 ETag、Last-Modified 和 Content-Length。
  - 所以文件 evidence 的這兩個欄位可為空，歸檔另建 `reference_document_archive`，不改 HKEX 的 `exchange_report_archive`。
- **指數代碼**
  - HSI 系列：HSI `00001.00`、FIN `.01`、UTI `.02`、PROP `.03`、C&I `.04`。
  - HSCI 系列：HSCI `00011.00`、ENG `.01`、MAT `.02`、IND `.03`、TEL `.06`、UTI `.07`、FIN `.08`、PROP `.09`、IT `.10`、CONG `.11`、CD `.12`、CS `.13`、H `.14`。
- **HSICS 代碼**
  - 00 Energy、05 Materials、10 Industrials、23 Consumer Discretionary、25 Consumer Staples、28 Healthcare。
  - 35 Telecommunications、40 Utilities、50 Financials、60 Properties and Construction、70 Information Technology、80 Conglomerates。
- **快照日期語意：D 日快照包含生效日 ≤ D 的變動。** 例如 02555 在 2026-10-23 生效刪除：10-22 的 HSCI 快照有 581 檔且含 02555，10-23 剩 580 檔。
- **變動 xlsx 的 `Count` 欄**是當日同一動作（加入或刪除）的筆數，同一生效日各列的「變動後檔數」相同。
- **變動 xlsx 的範圍**
  - HSI 從 2008-10-08 起，HSCI 從 2008-09-08 起。
  - 行業指數的起點各不相同，例如非必需性消費、必需性消費始於 2019-10，醫療保健始於 2020-03（HSICS 改制）。在各指數的起點之前不推算。
- **PDF 解析**
  - 用 pypdf 的純文字模式，182 頁約 1.7 秒；layout 模式超過 5 分鐘，不可用。
  - 文字以 NBSP（`\xa0`）分隔，名稱與行業名稱會跨行。
  - HSCI 權重合計為 100.0。
- **對應不到 instrument 的成分**：production 的 instruments 沒有 00823（領展 REIT），HSI 中這類 REIT 成分會記為對應不到。
- **本機歸檔**
  - 位於 Owner 本機的 `~/findb-hsil-archive/data/`，附 `manifest.json`。
    - 放在 Desktop 之外，因為 macOS 的隱私權限會擋下 launchd 讀寫 Desktop。
  - 2026-11-24 首次執行存了 169 個檔：HSI 與 HSCI 從 2026-10-02 起的每日 PDF 與 idx csv、兩份 xlsx，以及當時下載的 23 份新聞稿（不再更新）。
  - launchd 排程 `com.findb.hsil-archive` 每天 23:45（台北時間）補存當天的新檔。
  - **reference feed 在 production 上線、歸檔匯入完成後，移除排程：**
    `launchctl bootout gui/$(id -u)/com.findb.hsil-archive`，再刪除 `~/Library/LaunchAgents/com.findb.hsil-archive.plist`。

## 以真實資料驗證重建（2026-11-24）

以本機歸檔的 36 天快照與 2026-11-24 的兩份 xlsx 驗證：
- **HSI 系列**：5 個指數與所有快照完全一致，沒有任何 finding。
- **HSCI 系列**：13 個指數在快照期間全部一致；往回推算時有 3 個已知缺口。
  - 2026-03-09 xlsx 記了臨時代碼 02910、02911 的加入，之後換回正式代碼卻沒有紀錄；
    這兩檔的正式代碼因此被推成 2008 年起就是成分股。
  - 2022-03-07 的 06878 加入，和當時的成分對不上。
  - 這 3 個缺口讓更早日期推算出的檔數多 1～3 檔，記為 `count_after_change`。
- **HSIL 資料本身的特性**（重建時要處理）：
  - **臨時櫃台**：合股或拆股期間，股票改以 HKEX 保留的 02900–02999 代碼交易。
    - 快照照實列出臨時代碼，xlsx 只記正式代碼。例如 02915 換成 02670、02947 換成 02723、02963 換成 01440。
    - 重建時依快照中「同一天一出一入」的交換，把臨時代碼對回正式代碼；同一天有多組交換時，以同一行業指數配對。
  - **佔位代碼 99999**：例如 2026-11-19 起 HSCI 快照出現 99999，看來是為即將納入的新股預留，不算成分股。
  - **同一天以同一代碼刪除再加入**：例如 2015-03-18 長和集團重組時的 00001。這不是錯誤，成員區間在當天切成兩段。
  - **HSI 報告的分類指數權重欄**：只印出所屬的那一欄，純文字抽取無法分辨是哪一欄，
    所以 PR 3 要用 pdfminer 依字元位置判斷（10 頁約 0.7 秒）。

## 設計

- **排程：固定成員的 daily plan**（2026-11-26 Owner 決定，取代原本的 `reference` work kind；做法和 #647 三大法人的整張表相同）
  - 每個港股交易日一個 plan。
  - `hk_index_constituents` 的成員是 `HSI`、`HSCI` 兩份報告。
  - `hk_index_changes` 的成員是 18 個指數的 sheet。
  - 每個成員每天送一次；變動檔內容沒變時，重建會略過。
- **兩個契約**
  - `index_constituents.v1`：一份成分股表現 PDF 的完整名單。
  - `index_constituent_changes.v1`：一個 sheet，即一個指數的完整變動歷史。
  - 共用規則：`data_date` 是觀察日；`document.document_date` 是內容所屬日期，不得晚於觀察日。
- **Canonical 表**
  - `index_definition`、`index_change_event`、`index_snapshot`／`index_snapshot_constituent`。
  - 由重建產生的 `index_membership`、`index_membership_build`。
  - 產業分類由 HSCI 行業指數的成員推得，不另建表。
- **代碼對應**：一律保存 5 碼 symbol；`instrument_id` 依成員區間對應，可為空，不自動建立 instrument。
  - 只有在區間延續到快照期間，或區間結束時該 instrument 已有日線時，才對應到 instrument，以免代碼重複使用造成錯配。
- **重建**：每次交付都以該系列已存的全部快照與變動重建一次，所以交付順序不影響結果。
  - 輸入和上次重建相同時略過，重送已存的文件不會觸發重建。
  - 同一系列的歸檔、寫入與重建都在同一把 advisory lock 下進行。
  - 臨時代碼每「一段出現期間」各自配對，代碼日後被別的股票沿用也不會配錯。
- **舊版文件**：同一天的報告或同一指數的變動檔，若晚到的是較舊的版本，不會取代較新的版本，只記 `INDEX_DOCUMENT_STALE` warning。
  - 文件沒寫父指數或行業時，保留已存的值。
  - 成員區間 `valid_from` 為空，代表「在 HSIL 最早紀錄之前」。
  - `index_membership_build.observed_from` 是第一份快照日，在它之前的成員為推算。
- **DQ**
  - 單份文件自相矛盾時整份擋下，不寫入任何資料，例如權重合計不是 100、成員不在父指數內、行業欄與行業指數不符。
  - 文件之間不一致時記 warning，每個規則與指數各一筆，附前 20 例。

## PR 進度

| PR | 內容 | 狀態 |
| --- | --- | --- |
| 1 | 兩個契約與 artifacts、canonical 與歸檔表、migration `2bbe07b98293`；尚無 dataset 接收 | #692 已合併並部署 staging |
| 2 | 重建演算法、歸檔服務、兩個 normalizer、DQ、`CONTRACT_NORMALIZER_MAP`、migration `ebb187773e30`（含刪除 `reference_delivery_evidence`） | #697 |
| 3 | Fetcher 解析器（HSI 用 pdfminer、HSCI 用 pypdf、多 sheet xlsx）、raw 格式、hkex adapter 的固定成員與 acquire | 未開始 |
| 4 | Backend plan 整合：成員鍵、`validate_ingest_part`、完成判定、delivery evidence | 未開始 |
| 5 | 啟用兩條 feed、dataset／scheduler migration、Serve scope 分支、`historical_lookback_days` | 未開始 |
| 6 | Serve 端點 | 未開始 |
| 7 | staging 驗收、本機歸檔匯入、移除 launchd 排程、整併文件並刪除本檔 | 未開始 |
