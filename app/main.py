"""
FastAPI application entry point.
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.v1 import admin, serve, source
from app.config import get_settings
from app.models.base import init_db

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events."""
    # Startup
    if not settings.DEBUG and not settings.SOURCE_ALLOWLIST_CIDRS.strip():
        raise RuntimeError("SOURCE_ALLOWLIST_CIDRS is required when DEBUG is false")
    await init_db()
    yield
    # Shutdown
    pass


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="Financial Database - Normalize and Serve Layer",
    lifespan=lifespan,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
TEST_PAGE_PATH = STATIC_DIR / "test_page.html"
INSTRUMENT_LOOKUP_PAGE_PATH = STATIC_DIR / "instrument-lookup.html"

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Include routers
app.include_router(
    source.router,
    prefix=f"{settings.API_V1_PREFIX}/source",
    tags=["Source API"],
)

app.include_router(
    serve.router,
    prefix=f"{settings.API_V1_PREFIX}/serve",
    tags=["Serve API"],
)

app.include_router(
    admin.router,
    prefix=f"{settings.API_V1_PREFIX}/admin",
    tags=["Admin API"],
)


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "version": settings.APP_VERSION,
        "source_allowlist_configured": bool(settings.SOURCE_ALLOWLIST_CIDRS.strip()),
    }


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "docs": "/docs",
    }


@app.get("/test", include_in_schema=False)
async def test_page():
    """Test dashboard page."""
    return FileResponse(TEST_PAGE_PATH, media_type="text/html")


@app.get("/instrument-lookup", include_in_schema=False)
async def instrument_lookup_page():
    """Instrument lookup page."""
    return FileResponse(INSTRUMENT_LOOKUP_PAGE_PATH, media_type="text/html")
