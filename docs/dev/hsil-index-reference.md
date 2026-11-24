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
- **產業分類只收 HSCI 成分股**（約 580 檔），只到行業層；非成分股沒有分類。
- **歷史用變動 xlsx 補到 2008 年**：以最新快照為錨點往回推算，並以每份快照交叉核對。
- **檢討結果新聞稿**作為公布但尚未生效的變動；生效後由 xlsx 確認。

## 來源（公開 HTTPS GET，不需認證）

API 前綴：`https://www.hsi.com.hk/api/wsit-hsi-ddoc-ea-public-website-proxy/v1`。

| 文件 | 取得方式 | 內容 |
| --- | --- | --- |
| 成分股表現 PDF（每個交易日一份，保留約 36 個交易日） | 清單：`/product-data/pub/series/metadata/v2?data=dailyReport&seriesCode=hsi\|hsci`（reportType `con-pdf-1`）；下載：`/content/gcs/download?resource=<url>` | HSI：95 檔，含權重與四個分類指數的權重。HSCI：580 檔，含 HSICS 行業代碼與中英文名稱、權重；另列行業指數與大中小型股指數。網站上的「月報」就是每月最後一份 |
| 成分股變動 xlsx | `/content/gcs/download?resource=/public-website/webapp/contents/en/indexes/hisConstituent/{hsi\|hsci}/hist_{hsi\|hsci}.xlsx` | 每個指數一個 sheet，sheet 名稱即 indexShortName。逐筆列出生效日、加入或刪除、變動後檔數。只列已生效的變動 |
| 檢討結果新聞稿 PDF | 清單嵌在 `https://www.hsi.com.hk/en-hk/media-room/` 的 JSON 中；檔案位於 `/content/dam/wpb/hsil/en_hk/press_releases/content/<時間>.pdf` | 2021-02 起共 23 份。內容含實施日與生效日，以及各指數的 Inclusion／Removal 與變動後檔數；HSCI 行業指數的變動另見「指數通告」 |
| 目前 HSI 名單 JSON | `/product-data/pub/constituent/metadata/v2?data=rt&seriesCode=hsi&language=eng` | 只用來交叉核對；HSCI 系列只公開檔數 |

## 動工前實測（2026-11-24）

- **標頭**
  - GCS 下載沒有 ETag、Last-Modified 和 Content-Length；新聞稿只有 Last-Modified。
  - 所以文件 evidence 的這兩個欄位可為空，歸檔另建 `reference_document_archive`，不改 HKEX 的 `exchange_report_archive`。
- **指數代碼**
  - HSI 系列：HSI `00001.00`、FIN `.01`、UTI `.02`、PROP `.03`、C&I `.04`。
  - HSCI 系列：HSCI `00011.00`、ENG `.01`、MAT `.02`、IND `.03`、TEL `.06`、UTI `.07`、FIN `.08`、PROP `.09`、IT `.10`、CONG `.11`、CD `.12`、CS `.13`、H `.14`。
- **HSICS 代碼**
  - 00 Energy、05 Materials、10 Industrials、23 Consumer Discretionary、25 Consumer Staples、28 Healthcare。
  - 35 Telecommunications、40 Utilities、50 Financials、60 Properties and Construction、70 Information Technology、80 Conglomerates。
- **快照日期語意：D 日快照包含生效日 ≤ D 的變動。** 例如 02555 在 2026-10-23 生效刪除：10-22 的 HSCI 快照有 581 檔且含 02555，10-23 剩 580 檔。
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
  - 2026-11-24 首次執行存了 169 個檔：HSI 與 HSCI 從 2026-10-02 起的每日 PDF 與 idx csv、兩份 xlsx、23 份新聞稿。
  - launchd 排程 `com.findb.hsil-archive` 每天 23:45（台北時間）補存當天的新檔。
  - **reference feed 在 production 上線、歸檔匯入完成後，移除排程：**
    `launchctl bootout gui/$(id -u)/com.findb.hsil-archive`，再刪除 `~/Library/LaunchAgents/com.findb.hsil-archive.plist`。

## 設計

- **新的 provider-work kind `reference`**
  - 成員是文件的部分，不是股票；不建 plan，也不需要 universe baseline，只受 `full_market_hkex_v1` control 管制。
  - 未變動的文件以 `reference_delivery_evidence` 結為 `no_data`。
- **三個契約**
  - `index_constituents.v1`：一份成分股表現 PDF 的完整名單。
  - `index_constituent_changes.v1`：一個 sheet，即一個指數的完整變動歷史。
  - `index_review_announcement.v1`：一份新聞稿中 HSI 與 HSCI 的變動。
  - 共用規則：`data_date` 是觀察日；`document.document_date` 是內容所屬日期，不得晚於觀察日。
- **Canonical 表**
  - `index_definition`、`index_change_event`、`index_change_announcement`、`index_snapshot`／`index_snapshot_constituent`。
  - 由重建產生的 `index_membership`、`index_membership_build`。
  - 產業分類由 HSCI 行業指數的成員推得，不另建表。
- **代碼對應**：一律保存 5 碼 symbol；`instrument_id` 依成員區間對應，可為空，不自動建立 instrument。

## PR 進度

| PR | 內容 | 狀態 |
| --- | --- | --- |
| 1 | 三個契約與 artifacts、canonical 與歸檔表、`reference_delivery_evidence`、migration `2bbe07b98293`；尚無 dataset 接收 | 進行中 |
| 2 | `reconstruct()`、三個 normalizer、DQ、`CONTRACT_NORMALIZER_MAP` | 未開始 |
| 3 | Fetcher 解析器、pypdf、讀取多個 sheet 的 xlsx、raw 格式 | 未開始 |
| 4 | `reference` work kind 與 known-parts 端點 | 未開始 |
| 5 | 啟用三條 feed、scheduler migration、Serve scope 分支、監控排除 | 未開始 |
| 6 | Serve 端點 | 未開始 |
| 7 | staging 驗收、整併文件並刪除本檔 | 未開始 |
