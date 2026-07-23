# Monorepo 環境變數與套件管理

## 邊界

- 根目錄 `package.json` 與 `pnpm-workspace.yaml` 管理所有 Node workspace、共用 scripts，以及未來 Husky hooks。
- 根目錄 `pnpm-lock.yaml` 是唯一 Node lockfile；子專案不得另建 lockfile。
- `backend/pyproject.toml` 與 `backend/uv.lock` 繼續管理 Python 依賴。
- 本機只使用根目錄 `.env`；不得建立 `dashboard/.env`。
- 生產環境不複製 `.env`，由 GitHub Actions Secrets 將值注入各 container。

## Dashboard secret 流向

```text
Local: root .env ────────┐
                         ├─> Dashboard Node server ──X-API-Key──> Admin API
Deploy: GitHub Secrets ──┘             │
                                       └─ HttpOnly signed session ─> Browser
```

`ADMIN_API_KEY`、`DASHBOARD_PASSWORD` 與 `DASHBOARD_SESSION_SECRET` 不可使用 `VITE_` 前綴，也不會進入 client bundle。瀏覽器只提交登入帳密並取得有期限的簽章 session cookie。

## 必填設定

| 變數 | 用途 |
| --- | --- |
| `ADMIN_API_KEY` | Dashboard server 呼叫 FastAPI Admin GET endpoints |
| `FINDB_API_BASE_URL` | Dashboard server 連至後端的內部位址 |
| `DASHBOARD_USERNAME` | 唯一操作帳號 |
| `DASHBOARD_PASSWORD` | 操作密碼 |
| `DASHBOARD_SESSION_SECRET` | Session HMAC 簽章密鑰，至少 32 字元 |

本機 `FINDB_API_BASE_URL` 通常是 `http://localhost:8080`；Compose container 使用 `http://app:8080`；正式環境使用 `http://ingest:8080`。

## 部署

Dashboard 使用獨立 image 與 `findb-dashboard` container，由 nginx 的 `/dashboard/` 路徑代理。Deploy workflow 會在啟動前檢查三個 Dashboard 認證 secrets 均非空，缺少任何一項即拒絕部署。
