# Production native 資料與回復驗收待辦

Production foundation 與既有 releases 的歷史由 Git 追溯。
Staging／production 已採用 [統一部署契約](../operations/deployment.md)，
不再沿用 GitHub Environment gate、bounded topology 或 schema v2 accepted replay。
現行操作契約見 [部署文件](../operations/deployment.md)、[production 維運](../operations/production.md)
與 [Full market runbook](../operations/full_market.md)。

尚未完成：

- Shioaji 空 Kbars 與 frozen mapping 缺口修復（[#378](https://github.com/FPI-TW/findb/issues/378)）：2026-10-30 唯讀確認五筆未結案 daily 工作，
  45 個失敗成員均保存空 Kbars；response journal 會重播同一份空內容，mapper 拒絕後
  消耗本地 failure budget，已有一個成員達 retry_exhausted。另有 12 個已遇到的 frozen
  mapping gap，與部署前的錯誤證據一致。須補空回應分類、保留原證據的受控快取更新、
  no_data 證據要求、frozen scope 修復及對應回歸測試；不得藉清 cache／reset attempts
  或退款已派送 quota 解套。Source 中斷修正無法單獨解決此問題；原始 SDK 空回應原因
  仍待證據。現場數量、時間軸及唯讀限制見 [production 維運](../operations/production.md)。

- Fetcher 對 Backend／Source 暫時不可用的恢復與重試額度修正：2026-10-30 已確認部署窗口
  HTTP 521 會穿過現行 Pilot／Full／historical 的控制迴圈而退出，Docker 因而自動重啟；
  521 亦未列入兩個 Source control clients 的 bounded retry statuses。另以零 provider I/O
  重現 canonical daily 在授權前增加本地 attempts 的風險。須補 startup／idle 可中止退避、
  active owner 停止／reap／重新取 fence、prepared／ACK-loss replay 與純等待不消耗 failure
  budget 的測試；不得放寬舊 generation、退款已派送 quota、清理 prepared 或重設 cooldown。
  本次尚未實作或部署此修正，根因、現場證據與限制見 [production 維運](../operations/production.md)。

- Production 五 provider 的 provider-work native／recovery 驗收：2026-10-29 已完成具名 Owner
  attestation、兩端 true 與 Backend／Fetcher v0.1.12 發布，原 DB／17 個 SQLite／prepared／receipts／
  預算／cooldown 保留；正式備份、版本與驗證證據已整併至 [production 維運](../operations/production.md)。
  切換驗收時 controls 全停止，後續啟停見 production 維運紀錄；尚需受控驗證同 provider daily → historical → daily 的 canonical proof、跨 provider
  並行、heartbeat-loss recovery、cancel／prepared drain 與 rollback。舊 Twelve Data HK scope 及既有
  failed／manual／raw_uploading 證據不得整批重試、清檔或重設預算。實際 native 抓取與 fault／rollback
  演練須另獲授權，不能以部署健康、原 rows 比對或本機測試代替。

- Owner 於台北時間 2026-10-16 將香港排除本次 native 驗收；本次 staging Pilot 目標為其餘六條，
  尚未全部通過，因此 production native 驗收保持未開始。七條 feed 架構與香港失敗紀錄仍保留。
  R2 診斷 prefix 的鎖定與相同 bytes 重試已驗證；不能據此宣稱整個 bucket 或七條 feeds 通過。
  Twelve Data 的美國 6,018 筆、香港 2,828 筆目錄已成功發布，413 問題已排除；
  香港凍結商品 `0001` 的受控單次診斷得到 HTTP／provider code 400、`no_data_response`，
  指定 `exchange=HKEX` 的追加診斷亦為相同結果；官方將 XHKG 列為需授權或未支援市場，
  香港另列未解項目，不計入本次分母，也不標示通過。既有兩項 retry exhausted 不重設。
  美股及 FinLab／Shioaji 四條的 native 證據已核對，本次範圍為 5／6（原七條 assembler 為 5／7）；
  已完成的美股日期修正、部署資料保留與 Source／R2／canonical／Serve 證據見
  [正式測試文件](../operations/testing.md)。Pilot 已停止並 drain，剩餘 TAIFEX 通過前不進行 production 驗收。
  FinLab FileStorage 與跨 feed 目錄缺欄隔離修復已部署至 `50cb51faadd9`，三輪目錄 refresh、
  記憶體與資料保留驗證見正式測試文件；仍需多輪日常抓取穩定性驗證。
  Pilot 全部停止，不將目錄診斷當作日常抓取驗收。
  最新股票 `8908` 與 ETF `00415A` 仍為 mapping gap，需核對 provider 資料可用性；
  不將已發布目錄等同所有成員有資料，也不修改既有凍結樣本。
  TAIFEX 2026-10-15 Pilot 計畫有兩筆 canonical 寫入，另兩項已 retry exhausted，整條 feed 未通過；
  未完成項目為前一日目錄的 MTX:202610W2 sessions。PR #307 已加入新計畫的同日期目錄要求，
  Backend／Fetcher CD 已將 `0a8d491a064d` 部署至 staging；Backend 八組資料指紋、
  Fetcher 的 16 份 SQLite 備份比對與服務／queue 健康檢查均已通過；
  下一個新日期計畫排程為台北 2026-10-16 17:00，須用新計畫完成 native 驗收；
  保留凍結成員、首次日期與 terminal checkpoint，不以重設狀態或無證據 no_data 掩蓋失敗。
- 新統一版在 production 的 accepted-record rollback 演練與可核對證據。
- 既有 missing／DQ alerts 的人工調查與修復；不得由部署成功自動結案。

完成項目需整併至正式維運文件並自此移除；native 驗收與部署健康 gate 各自報告。
