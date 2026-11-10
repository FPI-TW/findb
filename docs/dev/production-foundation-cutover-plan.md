# Production native 資料與回復驗收待辦

Production foundation 與既有 releases 的歷史由 Git 追溯。
Staging／production 已採用 [統一部署契約](../operations/deployment.md)，
不再沿用 GitHub Environment gate、bounded topology 或 schema v2 accepted replay。
現行操作契約見 [部署文件](../operations/deployment.md)、[production 維運](../operations/production.md)
與 [Full market runbook](../operations/full_market.md)。

尚未完成：

- Provider-work 控制／交付路徑效能：repository 已實作抓取前日曆防護、控制路徑只讀
  key／descriptor、enqueue 重播免鎖與 Fetcher 每程序單次 enqueue，以及 migration
  `9c1e4f7a2b6d` 的逐筆 prepared 資料列（[provider-work 架構](../architecture/provider_work.md#api-與營運觀測)）。
  尚未部署；須依 Backend-first 發布 staging／production，保留 DB／SQLite 備份與 prepared
  bytes／receipts 指紋，再以成員／回合分段耗時、enqueue 與 SQL 次數、TOAST 寫入量、
  receipt／bytes 保留及原生帳本驗收。減少 preparation 往返（authorize／intent／confirm）
  僅在部署後量測仍為瓶頸時再評估。原始證據見 [production 維運](../operations/production.md#其他-provider-複查與控制路徑實測2026-11-01)。

- 舊 `planned` 休市日工作：部署上述防護後，production Twelve Data 美股 10/17、10/18、10/24
  與 FinLab 10/24、10/25 會在不呼叫 provider、不扣 member attempts 的情況下以
  `CALENDAR_DECISION_REPAIR_REQUIRED` 結算 failed。Frozen decision 更正仍走人工流程；
  休市結清工具目前只接受 Shioaji，需依各 provider 的交付／帳務盤點推廣後，才能對上述
  工作結清及處理對應告警。

- 11/01 唯讀查核的其他後續：FactSet daily 每成員同步等待 normalization（每 2 秒輪詢、
  最多 30 秒）限制吞吐；同一份有效快取被 mapper 確定拒絕時仍會用滿一般失敗額度，需依
  永久錯誤分類提早轉 manual；TAIFEX 健康輸出仍計入 Backend 已 cancelled 的 10/13～10/27
  本地 plan（944 個 manual）。另待 Owner 決定 FinLab（change ratio 1.003）與 TAIFEX（0.397）
  待審 universe candidate，以及 production FactSet 港股啟用時程。

- Production 四個現役 providers 的 provider-work native／recovery 驗收：2026-10-29 已完成具名 Owner
  attestation、兩端 true 與 Backend／Fetcher v0.1.12 發布，原 DB／17 個 SQLite／prepared／receipts／
  預算／cooldown 保留；正式備份、版本與驗證證據已整併至 [production 維運](../operations/production.md)。
  切換驗收時 controls 全停止，後續啟停見 production 維運紀錄；尚需受控驗證同 provider daily → historical → daily 的 canonical proof、跨 provider
  並行、heartbeat-loss recovery、cancel／prepared drain 與 rollback。既有
  failed／manual／raw_uploading 證據不得整批重試、清檔或重設預算。實際 native 抓取與 fault／rollback
  演練須另獲授權，不能以部署健康、原 rows 比對或本機測試代替。

- Owner 於台北時間 2026-10-16 將香港排除本次 native 驗收；當時 staging Pilot 目標為其餘六條，
  尚未全部通過，因此 production native 驗收保持未開始。Pilot 已退役，staging native 驗收改由
  Full daily 首日驗收承接（見 [backlog](backlog.md)）。七條 feed 架構與香港失敗紀錄仍保留。
  R2 診斷 prefix 的鎖定與相同 bytes 重試已驗證；不能據此宣稱整個 bucket 或七條 feeds 通過。
  Twelve Data 的美國 6,018 筆目錄已成功發布，413 問題已排除；港股已改由 FactSet 負責，
  Twelve Data HK 已永久退役，舊 work 的結案核對見 [FactSet 港股日行情](../operations/factset_hk.md#twelve-data-舊-hk-work-結案)。
  既有美股及 FinLab／Shioaji native 證據保留；Shioaji 已退役，不列入現役抓取驗收，
  不可沿用舊 scope 或授權重啟。其餘現役 feeds 的未完成 native 驗收仍獨立追蹤；
  已完成的美股日期修正、部署資料保留與 Source／R2／canonical／Serve 證據見
  [正式測試文件](../operations/testing.md)。剩餘 TAIFEX 改以 staging Full 驗收，通過前不進行 production 驗收。
  FinLab FileStorage 與跨 feed 目錄缺欄隔離修復已部署至 `50cb51faadd9`，三輪目錄 refresh、
  記憶體與資料保留驗證見正式測試文件；仍需多輪日常抓取穩定性驗證。
  不將目錄診斷當作日常抓取驗收。
  Shioaji 股票 `8908` 與 ETF `00415A` 的 mapping gap 保留為歷史缺口，
  不重開舊抓取或修改凍結樣本；替代 provider 需另建 scope 與驗收證據。
  TAIFEX 2026-10-15 Pilot 計畫有兩筆 canonical 寫入，另兩項已 retry exhausted，整條 feed 未通過；
  未完成項目為前一日目錄的 MTX:202610W2 sessions。PR #307 已加入新計畫的同日期目錄要求，
  Backend／Fetcher CD 已將 `0a8d491a064d` 部署至 staging；Backend 八組資料指紋、
  Fetcher 的 16 份 SQLite 備份比對與服務／queue 健康檢查均已通過；
  下一個新日期計畫排程為台北 2026-10-16 17:00，須用新計畫完成 native 驗收；
  保留凍結成員、首次日期與 terminal checkpoint，不以重設狀態或無證據 no_data 掩蓋失敗。
- 新統一版在 production 的 accepted-record rollback 演練與可核對證據。
- 既有 missing／DQ alerts 的人工調查與修復；不得由部署成功自動結案。

完成項目需整併至正式維運文件並自此移除；native 驗收與部署健康 gate 各自報告。

Shioaji 已退役，#378 的舊恢復／重新抓取驗收計畫已由退役契約取代；缺口不因此視為已補齊。
保留資料與權限驗收見 [production 維運](../operations/production.md#shioaji-backend-退役部署基準)，
舊 repair inventory／preview 仍可查，禁止沿用舊授權重開 work。
