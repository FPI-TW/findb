# 金融資料庫建置流程與開發先後順序（更新版：外部抓取服務 + Source API）

## 一、文件目的

說明金融資料庫的建置流程、開發先後順序與系統設計邏輯，

---

## 二、專案目標

1. 建立一套可長期維護、可逐步擴充的金融資料庫
2. 支援九大市場：
   - 美股宏觀與債券市場
   - 美股
   - 全球市場
   - 台股
   - 港股
   - 陸股
   - 全球外匯
   - 加密貨幣
   - 台指期
3. 資料用途：
   - 隔日分析報告
   - 投資研究與策略驗證
   - 圖表繪製
   - AI / RAG 資料正確性確認
4. 資料頻率以日 K（EOD）為主，收盤後更新
5. Raw 原始資料短期保存 14 天（主程式負責保存與清理）

---

## 三、核心設計原則

### 1) 三層解耦（更新版）：Fetch → Normalize → Serve

採用三層解耦架構，「來源層」改為外部抓取服務 + 主程式的 Source API 入口。

#### Fetch（資料抓取層｜外部服務｜特定設備）

- 運行位置：特定設備的外部服務
- 職責僅限於：
  - 向 API（或其他資料來源）抓取資料
  - **不儲存資料**
  - 抓取完成後呼叫主程式 API，傳送 raw data（原始回傳）
- 禁止事項：
  - 不得落地保存（避免授權/資料外洩風險）
  - 不得進行標準化與商業邏輯
  - 不得直接提供報告/圖表/AI 使用資料

#### Normalize（標準化層｜主程式）

- 職責：
  - 接收 Fetch 層傳入的 raw data
  - Raw 短期保存（14 天）以便追溯/除錯/對帳
  - 將 raw data 映射（mapping）為內部「通用金融資料模型」
  - 寫入 Canonical Database（長期保留）
  - 執行資料品質檢查（DQ）

#### Serve（服務層｜主程式）

- 職責：
  - 提供統一對外資料出入口（API/Query/Service）
  - 報告、策略、圖表、AI / RAG 一律只讀取 Canonical
- 禁止事項：
  - 不得直接接觸 Fetch 層或外部來源 API
  - 不得讀取 Raw 作為業務依據（Raw 僅用於追溯）

> 此架構確保：
>
> - 資料來源更換時，主要影響 Fetch 服務與 Normalize mapping
> - Serve 層與報告/AI pipeline 不需跟著變動
> - 可將「資料抓取」隔離於特定設備，符合環境/授權/安全需求

---

## 四、各市場資料範疇定義（含 Index）

### 重要原則：市場指標（Index）為一等資料類型

- Index 視為一種特殊的 Instrument
- 與個股（Equity）共用交易日曆與日 K 模型
- 不屬於宏觀指標（Macro Series）

### 各市場包含的 Instrument 類型

#### 美股市場

- Equity：
  - 自選美股個股（可擴展）
- Index（至少包含）：
  - S&P 500
  - NASDAQ Composite
  - Dow Jones Industrial Average

#### 台股市場

- Equity：
  - 上市 / 上櫃個股
- Index（至少包含）：
  - 加權股價指數
  - 櫃買指數

#### 港股市場

- Equity：
  - 主板 / 創業板個股
- Index（至少包含）：
  - 恆生指數
  - 國企指數

#### 陸股市場

- Equity：
  - 滬深 A 股
- Index（至少包含）：
  - 上證綜合指數
  - 深證成指
  - 滬深 300

#### 全球外匯

- 不設市場 Index
- 以貨幣對本身作為分析基準

#### 加密貨幣

- Index：
  - Total Market Cap Index

#### 台指期

- Underlying Index：
  - 台股加權指數
- 期貨商品視為衍生 Instrument

---

## 五、開發先後順序（Roadmap）

### Phase 0：共用底座

目的：建立完整的解耦結構，使任何市場都能直接接入

完成項目：

- Source API（接收 raw data 的入口）
- Raw 短期保存（14 天）與清理機制
- 標的主檔（Instrument Master）
- 代碼映射（Identifiers）
- 交易日曆（Trading Calendar）
- Dataset Registry（資料集定義與啟停）
- Ingestion Run（批次與對帳）
- 基礎資料品質檢查（DQ）
- Serve API（供報告/AI 使用的統一入口）

完成項目（外部抓取服務）：

- 最小抓取器：能抓取任一 dataset（例如美股指數日 K）並呼叫主程式 Source API

---

### Phase 1：優先市場（最快支援報告閉環）

- 加密貨幣
- 美股（Equity + Index）
- 全球外匯

每個市場最低交付：

- 日 K（OHLCV）
- （股市）Index（日 K）
- （股市）Corporate Actions（除權息等會影響股價的事件）

### Phase 2：區域股市擴展

- 台股（Equity + Index）
- 港股（Equity + Index）
- 陸股（Equity + Index）

### Phase 3：宏觀、債券與衍生品

- 美股宏觀與債券
- 台指期

---

## 六、上線與維護原則

各股票市場上線時，必須同時具備：

- Equity（日 K）
- Index（市場基準指標日 K）

- Fetch 層僅抓取與轉送，不落地、不提供查詢
- 主程式才是資料真實來源（Canonical）
- Raw 僅短期保存，僅用於追溯/除錯/對帳
- 所有報告/策略/AI 必須透過 Serve 層存取資料
