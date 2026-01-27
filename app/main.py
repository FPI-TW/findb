"""
FastAPI application entry point.
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.config import get_settings
from app.api.v1 import source, serve
from app.models.base import init_db


settings = get_settings()


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
    description="Financial Database - Normalize and Serve Layer",
    lifespan=lifespan,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
TEST_PAGE_PATH = STATIC_DIR / "test_page.html"

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "version": settings.APP_VERSION}


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
