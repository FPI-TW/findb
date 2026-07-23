# Dashboard 路由與公開資料邊界

## 目標

Dashboard 同時承載公開查詢工具與需要登入的營運功能。所有頁面都由同一個
TanStack Start application 提供，部署在 `/dashboard/`，但依資料敏感度切分路由與
資料存取邊界。

## 路由

| 對外路徑 | Dashboard route | 權限 | 用途 |
| --- | --- | --- | --- |
| `/dashboard/` | `/` | 公開 | 首頁與功能入口 |
| `/dashboard/lookup` | `/lookup` | 公開 | 金融商品與宏觀序列查詢 |
| `/dashboard/skill` | `/skill` | 公開 | FinDB API Skill 下載與安裝教學 |
| `/dashboard/login` | `/login` | 公開 | 營運台登入 |
| `/dashboard/operations` | `/_authenticated/operations` | 需登入 | 導入、DQ、完整度與稽核資料 |

`/_authenticated` 是 pathless layout，只負責 session guard，不會出現在 URL。
登入成功後導向 `/operations`；公開首頁、Lookup 與 Skill 不得依賴 Dashboard
session 或 Admin credentials。

舊網址 `/instrument-lookup` 與 `/skill-install` 仍由 FastAPI 公開提供，避免破壞
沒有 Nginx 的本機環境與既有書籤。新功能以 `/dashboard/lookup` 與
`/dashboard/skill` 為主要入口；舊 HTML 暫時保留在 `backend/app/static/`，
後續若要移除需另行規劃跨 host 的 redirect。

## 公開資料

Lookup 的列表、搜尋、篩選、排序、分頁與 facets 由專用的唯讀後端 API 提供：

- `/api/v1/serve/lookup/instruments`
- `/api/v1/serve/lookup/macro-series`

後端負責輸入驗證、資料庫查詢與穩定排序；Dashboard 只透過 typed API client
轉換 request/response 並維護畫面狀態，不讀取後端檔案或 generated cache。

商品與宏觀序列明細只呼叫固定的唯讀 Serve API：

- `/api/v1/serve/eod/{instrument_id}?page_size=10`
- `/api/v1/serve/corporate-actions/{instrument_id}?page_size=5`
- `/api/v1/serve/macro/observations/{series_id}?page_size=10`

瀏覽器不會收到 `ADMIN_API_KEY`。生產 Nginx 只對 FinDB 同源 Lookup Referer
注入 Serve key；其他 caller 仍需自行提供 `X-API-Key`。

Skill archive 保留在 `/static/findb-api.skill`，不需要登入。

## 私有資料

`/operations` 的 route guard 只負責導覽體驗。真正的資料邊界仍位於 Dashboard
server functions：每個 Admin 查詢都必須在 server 端驗證 session，再從環境變數
讀取 `ADMIN_API_KEY` 呼叫 Admin API。Admin key 不得出現在 client bundle、HTML、
loader payload 或公開 server function。

## 前端模組

```text
dashboard/src/
├── features/
│   ├── landing/       # 公開首頁
│   ├── lookup/        # API client、filters、table、CSV、detail drawer
│   ├── operations/    # 受保護營運台
│   └── skill/         # 安裝 tabs、copy、下載與內容
├── routes/
│   ├── _authenticated.tsx
│   ├── _authenticated/operations.tsx
│   ├── index.tsx
│   ├── login.tsx
│   ├── lookup.tsx
│   └── skill.tsx
└── components/        # 全站 shell 與共用 UI
```

`routeTree.gen.ts` 永遠由 TanStack Router CLI 產生，不手動修改。

## 測試邊界

- Backend tests 以資料庫 fixtures 驗證 lookup API 的查詢、驗證、排序、分頁與 facets，
  不讀取 Dashboard source。
- Dashboard tests mock HTTP API response，驗證 request mapping、互動與呈現，
  不讀取 backend source、cache 或 fixtures。
- 部署拓撲與 Nginx routing 屬於 infra integration checks，不拿來替代任一端的
  contract/unit tests。
