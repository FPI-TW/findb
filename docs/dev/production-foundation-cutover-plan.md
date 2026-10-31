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

- Shioaji 空 Kbars 與 frozen mapping 缺口修復（[#378](https://github.com/FPI-TW/findb/issues/378)）：
  程式／回歸測試及 staging 10/23 兩筆休市結清已完成，正式收據與保留性核對見
  [Full market runbook](../operations/full_market.md)。Issue 保持 open；production 版本、
  空回應根因、受控恢復、mapping 與端到端 native 證據仍待驗收，範圍見下方矩陣。
  本次只整理文件及 issue，不操作 production，不沿用 staging 的額度或休市結論。

- Production 五 provider 的 provider-work native／recovery 驗收：2026-10-29 已完成具名 Owner
  attestation、兩端 true 與 Backend／Fetcher v0.1.12 發布，原 DB／17 個 SQLite／prepared／receipts／
  預算／cooldown 保留；正式備份、版本與驗證證據已整併至 [production 維運](../operations/production.md)。
  切換驗收時 controls 全停止，後續啟停見 production 維運紀錄；尚需受控驗證同 provider daily → historical → daily 的 canonical proof、跨 provider
  並行、heartbeat-loss recovery、cancel／prepared drain 與 rollback。既有
  failed／manual／raw_uploading 證據不得整批重試、清檔或重設預算。實際 native 抓取與 fault／rollback
  演練須另獲授權，不能以部署健康、原 rows 比對或本機測試代替。

- Owner 於台北時間 2026-10-16 將香港排除本次 native 驗收；本次 staging Pilot 目標為其餘六條，
  尚未全部通過，因此 production native 驗收保持未開始。七條 feed 架構與香港失敗紀錄仍保留。
  R2 診斷 prefix 的鎖定與相同 bytes 重試已驗證；不能據此宣稱整個 bucket 或七條 feeds 通過。
  Twelve Data 的美國 6,018 筆目錄已成功發布，413 問題已排除；港股已改由 FactSet 負責，
  Twelve Data 舊 HK work 的結案核對見 [FactSet 港股日行情](../operations/factset_hk.md#twelve-data-舊-hk-work-結案)。
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

## Issue #378 production 驗收範圍

以下為待正式驗收範圍，production 寫入、native 觀測與控制操作須另行核准具體環境、
work／plan／member、額度與時段。10/31 已完成獲授權的原始 response／receipt 唯讀續查，
確認兩端 manifest 為 v0.1.14、DB revision `3a80c2e1d5f4`；五筆舊 failed 共 40 個耗盡
成員均有 checksum 正確的空 Kbars，另 14 個為 frozen mapping gaps。供應商空值原因仍
unknown，未執行受控恢復或新增 native 請求；完整分時快照見 [production 維運](../operations/production.md#原始-responsereceipt-與慢速路徑續查)。
調查基準為 2026-10-30 台北約 09:10–09:18 的唯讀快照：五筆 Full daily work
（10/27 equity、10/28 equity／ETF、10/29 equity／ETF）、45 份空 Kbars cache、
12 個已遇到的 mapping gap，以及本地標為 prepared／normalizing、但 Backend run 已 completed
的 1,063 個成員；這些是當時數量，正式恢復前須重新對帳。原始證據見
[production 維運](../operations/production.md)；10/23 staging 的颱風休市不能解釋其他日期。

| 範圍 | 待驗收行為與證據 |
| --- | --- |
| 版本與部署 | 核對 production 現版、精確 staging accepted digests、schema／API 與 checkpoint 相容性；另獲授權後依 Backend-first 發布，再更新 Fetcher。保留 DB／SQLite 備份與原始 fingerprints，確認 controls 停止、owner 閒置及服務健康。部署成功與 native 恢復分別驗收；已有恢復授權的相關工作須排空後才能退回不支援版本，已有 closure evidence 不得降版刪除。 |
| 空回應根因與舊快取 | 逐 work／symbol／date 保留 SDK 版本、向量長度、response origin、checksum、dispatch／usage 與 calendar／catalogue 證據。無證據的原因維持 unknown；有效 legacy cache 沿用、損壞 checksum／永久格式錯誤有界失敗。需新 catalogue 時使用正式 maintenance work，工具不直接呼叫 SDK。 |
| 既有成員受控恢復 | 先對帳 prepared／receipts／completed runs，已完成者只更新本地進度，ACK-loss 重用原 idempotency key 與 bytes。明確核准的耗盡成員才可取得每筆最多三次額外額度，原 attempts 保留；Backend 授權、安裝與 fenced ACK 不重複發放，授權不啟動 control。原 coordinator 在合法窗口續作，重新觀測至多三次且相隔至少 900 秒，跨回合／重啟不重設。 |
| Frozen mapping | 使用既有 catalogue 證據確認原 gap 的有效 provider mapping，Owner preview／apply 綁定 environment／profile／work／plan／member 與原值摘要。保留原 universe／manifest／成員身分與缺口，不更動已有 prepared／accepted delivery 的 mapping；更正後須取得 canonical evidence 才算完成。跨 scope、未授權、重複／過期預覽與安裝 ACK 必須符合協定。 |
| 日曆與 no_data | 逐日期驗證 durable calendar／no-trade evidence；新日曆與 frozen decision 衝突時另獲 Owner 更正授權並保存 audit。需要結清舊工作時另作 preview／apply，保留原 plan、所有 attempts、修復授權與帳務；空回應、mapping 更正或休市結清均不能自動算 canonical 成功或 no_data。 |
| 負向恢復與帳務 | 受控驗證停止／窗口、quota、cooldown、unknown debt、失效 fence、Source outage、ACK-loss、派送前預留及不確定派送、崩潰續作；純等待不耗一般 failure budget、已派送 native 用量不退款、不重複交付／發 grant。任何 fault injection、控制啟停及額外觀測均須在核准範圍內。 |
| 端到端收斂 | 核對 Source receipts→raw／run→normalization→canonical→Serve，完整 pagination 行數與 checksum 相符、無新增 DQ error。逐 plan 驗證 expected=data+no_data+missing+blocked，確認原 prepared bytes、cache checksum、attempts、授權／ACK 與 native 帳務完整；新增用量正常計費。記錄仍 missing／blocked／manual 的原因與處置，不因部署健康自動結案。 |

完成上述適用項目的原生證據、處理剩餘缺漏並更新 issue 後，才判定 #378 可否結案。
Historical 僅共用快取驗證，維持既有重試／恢復政策；本矩陣不擴大其它 provider 或日期的授權。
