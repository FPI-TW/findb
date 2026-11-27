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
  - HSIL 的 PDF 在中繼資料中標示「不允許擷取文字」；pdfminer 會忽略這個標示。Owner 於 2026-11-24 確認維持「僅內部使用、可以擷取」。
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
- **PDF 解析**：文字以 NBSP（`\xa0`）分隔，負號是 U+2011，名稱與行業名稱會跨行。最後採用的做法見「Fetcher 解析（PR 3）」。
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
    所以要依字元位置判斷（見下方「Fetcher 解析」）。

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

## Fetcher 解析（PR 3）

- **PDF**：兩種報告都用 pdfminer.six 讀出每個字元的位置，不做版面分析（HSCI 182 頁約 3～6 秒，HSI 約 0.5 秒）。
  - pypdf 依繪製順序抽文字時會把相鄰欄位併成一段，HSI 的分類指數權重欄也無從分辨，所以不用。
  - 字元依「左緣在哪一欄之後」歸欄，依「上方最近的交易日」歸列；換行的儲存格保留換行處的空白，各行直接串接即可還原。
  - 每頁的表頭、頁尾都要和已知版面相同，否則整份拒收（`HsilFormatError`，work failed）。
  - 解析在子程序中執行，上限 120 秒，避免異常文件卡住 hkex 的日線。
  - 部分報告把表頭的「易」畫成相容字 U+F9E0，比對前先做 NFC 正規化。
  - HSCI 報告另列的大型／中型／小型股等 5 個指數不交付；不在已知清單內的指數一律拒收。
- **名稱**：名稱欄先印英文、空格，再印中文，中英文用同一字型，只能靠規則切分。
  - 中文名從「第一個全形或中日韓字元之前的最後一個空格」開始，例如 `TCL ELECTRONICS TCL電子`。
  - 沒有中日韓字元時，只有「同一名稱印兩次」（例如 `MANYCORE TECH MANYCORE TECH`）才切分，其他情況不交付名稱，不猜測。
  - 以 80 份歸檔報告比對 xlsx 的名稱，差異都是改名或大小寫不同，沒有切錯。
- **變動 xlsx**：新增共用的 `findb_fetcher.xlsx`，依 `workbook.xml` 的 sheet 順序讀出每個 sheet；HKEX 證券清單改用它的 `first_sheet_rows`。
  - sheet 標題要和凍結的指數中英文名稱相同；變動列從表頭下兩列開始，到第一列不是日期為止，之後只能是附註。
  - `document_date` 是整份 workbook 的最晚生效日，同一份 workbook 的 18 個成員一致。workbook 若已列出晚於交易日的生效日，該成員不交付（`document_later_than_date`）。
- **權重合計容許值**：580 檔 HSCI 權重各自四捨五入到小數兩位，合計曾為 99.85～99.88（40 份中有 6 份），超過 PR 2 原本的 ±0.1。
  - PR 3 改為依檔數放寬：`max(0.1, 0.0116 × √檔數)`，即四倍標準差；580 檔約 0.28，95 檔約 0.11。
- **Raw 格式**：新增 `pdf_gzip`（`.pdf.gz`）與 `xlsx_gzip`（`.xlsx.gz`），和 HKEX 報告一樣由 Fetcher 壓縮（`mtime=0`）。
- **真實資料驗證**：以本機歸檔 2026-10-02～11-27 的 80 份 PDF 與最新 xlsx 驗證。
  - 每份都解析成功，成員和先前驗證腳本的結果完全一致。
  - 以 11-26 的文件產生 20 個成員的請求，全部通過 Fetcher 契約、Backend pydantic 與 normalizer 的單份文件檢查；最大的是 HSCI 變動檔，2,061 列、約 417 KB。

## Backend plan 整合（PR 4）

- **成員鍵取自 batch，不取自資料列**：資料列的 `symbol` 是成分股代碼，不是 plan 成員。
  - 成分股報告的成員是 `document.series` 轉大寫（`HSI`、`HSCI`）；變動檔的成員是 `index.code`。
  - 共用的 `delivered_members()`（`app/schemas/ingress.py`）供 ingest 的 `associate_receipt` 與 prepared delivery 的 `register_prepared` 使用，期貨與逐檔資料集的推導不變。
- **`validate_ingest_part`**：移除 PR 1「參考文件不屬於 daily plan」的拒收，改為參考文件分支 `_validate_reference_member`。
  - 成員必須是這個 part 凍結的成員，且為 mapped；`series`／`index.code` 要等於名單的 `provider_symbol`。
  - 成分股報告的 `document_date` 要等於 plan 交易日；變動檔的 `document_date` 由契約保證不晚於觀察日。
  - 不套用 unlisted 規則與逐列幣別檢查。
- **完成判定**：`record_delivery_evidence` 對參考文件 run 記錄它代表的成員。
  - 只有 `completed`（整份文件沒被拒收）的 run 才算；重送相同文件、或較舊版本被略過時，文件仍已保存，照樣算數。
  - `classify_plan` 走既有的 `DeliveryMemberEvidence` 路徑，不需另外分支。
- **只走 daily，不做 historical**（2026-11-28 調整）：
  - Fetcher 的 historical 路徑會把 batch 改成 `backfill` 並填 coverage 日期，參考文件契約只接受 `full_snapshot`；
    變動 xlsx 只有現行版本，回補過去日期只會重送同一份。
  - PR 5 兩條 feed 的 `capabilities.historical` 設 `unsupported`。過去日期的報告由 PR 7 的本機歸檔匯入補上（歸檔從 2026-10-02 起，比 HSIL 保留的約 36 個交易日更長）。
  - 因此不需要放寬 `EODRawEvidence`（只有 EOD v2／historical 證據用它），`MEMBER_EOD_SCHEMAS` 與 `ROW_DQ_ERRORS` 也不加參考文件。
- **已知限制**：Fetcher 回報 `document_later_than_date`、`missing_symbol` 的成員會是 blocked，plan 不會完成；這兩種情況只在 Fetcher 遲到或 HSIL 移除 sheet 時發生，上線觀察後再決定是否歸為 unavailable。

## 啟用（PR 5）

- 排程：香港 21:05 抓當天，期限隔日 09:05，和 `hk_equity_official_eod` 相同，HKEX control 時鐘不變。
  - 發布時間尚未逐日實測（下載服務不回 Last-Modified）；本機歸檔每天台北 23:45 都已抓到當天的報告。21:05 若尚未發布，
    依發布重試窗口重試到期限前 30 分鐘。
- 部署順序與上線步驟見 `docs/operations/deployment.md` 的「HSIL 恒指參考資料 feed」。

## Serve（PR 6）

- 四個唯讀端點，說明見 `docs/api/api_usage_guide.md`：指數清單、某日成分股（含最新一份報告的權重）、
  變動歷史（cursor 分頁）與 HSICS 分類。
- 原計畫另列的 `/indexes/{code}/snapshots/{trade_date}` 與 `/instruments/{id}/...` 不另做：
  前者等於 `constituents?as_of=<報告日>`，後者以 `classifications/hsics?symbols=` 查詢。
- 某日早於系列第一份報告時標 `derived: true`；早於指數最早的變動日回 `DATE_BEFORE_HISTORY`，不推算。

## 本機歸檔匯入（PR 7a）

HSIL 只保留約 36 個交易日的報告，本機歸檔從 2026-10-02 起，所以較早日期的成分股報告要從歸檔匯入。

- 設定 `HSIL_DOCUMENT_ARCHIVE_DIR` 時，HKEX runtime 讀取歸檔的 `manifest.json`：
  - plan 交易日在歸檔中有 `constituent_reports/{series}/{日期}_con_*.pdf` 時，改用歸檔的 PDF，不向 HSIL 下載。
  - 以 manifest 的 SHA-256 核對原檔，並要求 manifest 記錄的網址正好是同一個檔名的 HSIL 下載網址；
    不符時整份拒收（格式錯誤），不改向 HSIL 下載。同一日期有兩份報告時也拒收。
  - 歸檔沒有的日期照常向 HSIL 下載，所以設定可以一直保留到匯入結束。
  - 文件 evidence 的網址、ETag 與 Last-Modified 取自 manifest。
- 只用於成分股報告。變動 xlsx 只有現行版本，一律向 HSIL 下載；歸檔中的舊版 xlsx 不送。
- 匯入步驟（需要 Owner 決定執行方式與時間）：
  1. 把歸檔複製到 Fetcher 主機，唯讀掛進 `findb-full-market-hkex`，並設定 `HSIL_DOCUMENT_ARCHIVE_DIR`
     （目前的部署設定沒有這個掛載，要另外加上或手動執行一次）。
  2. `hk_index_constituents` 的首日設為 2026-10-02，Owner start 後 runtime 從首日逐日補 plan。
  3. `hk_index_changes` 的首日設為上線當天：過去日期的 plan 會因 xlsx 最新生效日晚於交易日
     （`document_later_than_date`）而受阻。
  4. 補完後移除掛載與環境變數。

## PR 進度

| PR | 內容 | 狀態 |
| --- | --- | --- |
| 1 | 兩個契約與 artifacts、canonical 與歸檔表、migration `2bbe07b98293`；尚無 dataset 接收 | #692 已合併並部署 staging |
| 2 | 重建演算法、歸檔服務、兩個 normalizer、DQ、`CONTRACT_NORMALIZER_MAP`、migration `ebb187773e30`（含刪除 `reference_delivery_evidence`） | #697 已合併 |
| 3 | Fetcher 解析器（pdfminer 依字元位置、多 sheet xlsx）、raw 格式、hkex adapter 的固定成員與 acquire；權重容許值依檔數放寬；尚未註冊 dataset | #773 已合併 |
| 4 | Backend plan 整合：batch 成員鍵、`validate_ingest_part`、delivery evidence | #778 已合併 |
| 5 | 啟用兩條 feed（香港 21:05、historical `unsupported`）、migration `21e8f89c680b`（dataset、綁定 HKEX control、擴大 `fetcher-hkex` 範圍、schedule inspection）、seed、Serve scope 分支、`_IMPLEMENTED_ADAPTERS`、端到端 plan 測試 | #780 已合併 |
| 6 | Serve 端點：`/indexes`、`/indexes/{code}/constituents`、`/indexes/{code}/changes`、`/classifications/hsics` | #782 已合併 |
| 7a | 本機歸檔匯入：`HSIL_DOCUMENT_ARCHIVE_DIR` 讀取 manifest 核對過的報告 | 進行中 |
| 7 | staging 驗收、執行歸檔匯入、移除 launchd 排程、整併文件並刪除本檔 | 未開始 |
