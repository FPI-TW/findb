"""
FastAPI application entry point.
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.v1 import admin, serve, source
from app.config import get_settings
from app.models.base import init_db

settings = get_settings()

OPENAPI_TAGS = [
    {"name": "Serve API", "description": "唯讀市場資料查詢端點。"},
    {"name": "Source API", "description": "需認證的資料寫入與匯入端點。"},
    {"name": "Admin API", "description": "需認證的系統管理端點。"},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events."""
    # Startup
    await init_db()
    yield
    # Shutdown
    pass


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="金融資料庫 - 正規化與查詢服務層",
    openapi_tags=OPENAPI_TAGS,
    lifespan=lifespan,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
TEST_PAGE_PATH = STATIC_DIR / "test_page.html"
INSTRUMENT_LOOKUP_PAGE_PATH = STATIC_DIR / "instrument-lookup.html"
SKILL_INSTALL_PAGE_PATH = STATIC_DIR / "skill-install.html"

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def enforce_source_payload_size(request, call_next):
    """Short-circuit oversized Source API requests before body parsing."""
    source_prefix = f"{settings.API_V1_PREFIX}/source"
    if request.url.path.startswith(source_prefix):
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > max(1, int(settings.SOURCE_MAX_PAYLOAD_BYTES)):
                    return JSONResponse(
                        status_code=413,
                        content={
                            "detail": (
                                "Payload exceeds maximum size of "
                                f"{settings.SOURCE_MAX_PAYLOAD_BYTES} bytes"
                            )
                        },
                    )
            except ValueError:
                pass

    return await call_next(request)


# Include routers
app.include_router(
    serve.router,
    prefix=f"{settings.API_V1_PREFIX}/serve",
    tags=["Serve API"],
)

app.include_router(
    source.router,
    prefix=f"{settings.API_V1_PREFIX}/source",
    tags=["Source API"],
)

app.include_router(
    admin.router,
    prefix=f"{settings.API_V1_PREFIX}/admin",
    tags=["Admin API"],
)


@app.get("/health")
async def health_check():
    """健康檢查端點。"""
    return {
        "status": "healthy",
        "version": settings.APP_VERSION,
    }


@app.get("/")
async def root():
    """根端點。"""
    return {
        "name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "docs": "/docs",
    }


@app.get("/test", include_in_schema=False)
async def test_page():
    """測試儀表板頁面。"""
    return FileResponse(TEST_PAGE_PATH, media_type="text/html")


@app.get("/instrument-lookup", include_in_schema=False)
async def instrument_lookup_page():
    """商品查詢頁面。"""
    return FileResponse(INSTRUMENT_LOOKUP_PAGE_PATH, media_type="text/html")


@app.get("/skill-install", include_in_schema=False)
async def skill_install_page():
    """AI Skill 安裝說明頁。"""
    return FileResponse(SKILL_INSTALL_PAGE_PATH, media_type="text/html")
