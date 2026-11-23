"""
FastAPI application entry point.
"""

import asyncio
import logging
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1 import admin, serve, source
from app.config import get_settings
from app.models.base import async_session_maker, init_db
from app.services.credential_usage import flush_usage, periodic_flush, record_usage
from app.services.serve_errors import fallback_code

settings = get_settings()
APP_ROLE = settings.APP_ROLE.lower().strip()
SOURCE_UNIVERSE_MAX_PAYLOAD_BYTES = 16 * 1024 * 1024

SERVE_API_DESCRIPTION = (
    "Read-only market data: instruments, daily bars, intraday bars and ticks, TAIFEX futures "
    "quotes, trading calendars and data freshness.\n\n"
    "**Auth**: send a Serve API key in the `X-API-Key` header. Keys are issued on request; "
    "each key has its own rate limit and maximum `page_size`.\n\n"
    "**Suggested flow**: `list_datasets` to see what exists; `resolve_instruments` (known "
    "market and symbol) or `list_instruments` (search) to get an `instrument_id`; then the "
    "data endpoint named by its `coverage`.\n\n"
    "**Conventions**: trade dates are exchange-local and date ranges are inclusive; "
    "timestamps are UTC; prices are as traded (not adjusted). Daily volume is in shares, "
    "intraday volume in lots of 1,000 shares. Prices and other decimals are JSON strings "
    '(e.g. `"1300.00000000"`) so no precision is lost. List endpoints page with an opaque '
    "`pagination.next_cursor` (or `page` where totals are returned); a null cursor means no "
    "more rows.\n\n"
    '**Errors**: every error body is `{"detail": {"code", "message", "details"}}`; '
    "branch on `code`. On `429` wait for `Retry-After` seconds."
)

OPENAPI_TAGS = [
    {"name": "Serve API", "description": SERVE_API_DESCRIPTION},
    {"name": "Source API", "description": "需認證的資料寫入與匯入端點。"},
    {"name": "Admin API", "description": "需認證的系統管理端點。"},
]


@contextmanager
def app_logging() -> Iterator[None]:
    """Emit ``app.*`` INFO records from API roles for the app's lifetime.

    Uvicorn configures only its own loggers, so without this the root logger
    stays at WARNING and drops them. Celery workers configure the root logger
    themselves and never run the lifespan. Records still propagate to root.
    """
    logger = logging.getLogger("app")
    formatter = logging.Formatter(
        "%(asctime)s.%(msecs)03dZ %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    formatter.converter = time.gmtime
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events."""
    with app_logging():
        # Startup
        await init_db()
        flush_task = asyncio.create_task(
            periodic_flush(async_session_maker, settings.CREDENTIAL_USAGE_FLUSH_SECONDS)
        )
        try:
            yield
        finally:
            flush_task.cancel()
            try:
                await flush_task
            except asyncio.CancelledError:
                pass
            async with async_session_maker() as db:
                await flush_usage(db)


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description=(
        "FinDB financial market data. The Serve API is the read-only query surface; see the "
        "Serve API tag for auth, conventions and error codes."
    ),
    openapi_tags=OPENAPI_TAGS,
    lifespan=lifespan,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


SERVE_PREFIX = f"{settings.API_V1_PREFIX}/serve"


def _is_serve_request(request: Request) -> bool:
    return request.url.path == SERVE_PREFIX or request.url.path.startswith(f"{SERVE_PREFIX}/")


@app.exception_handler(RequestValidationError)
async def serve_validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Serve reports bad parameters with a code; Source and Admin keep FastAPI's list."""
    if not _is_serve_request(request):
        return await request_validation_exception_handler(request, exc)
    errors = [
        {
            "location": str(error["loc"][0]) if error.get("loc") else "request",
            "param": ".".join(str(part) for part in error.get("loc", ())[1:]) or None,
            "reason": error.get("msg", "invalid value"),
        }
        for error in exc.errors()
    ]
    names = sorted({error["param"] for error in errors if error["param"]})
    message = f"Invalid parameter: {', '.join(names)}" if names else "Invalid request"
    return JSONResponse(
        status_code=422,
        content={
            "detail": {
                "code": "INVALID_PARAMETER",
                "message": message,
                "details": {"errors": errors},
            }
        },
    )


@app.exception_handler(StarletteHTTPException)
async def serve_http_error_handler(request: Request, exc: StarletteHTTPException):
    """Give Serve errors raised without a code (e.g. an unknown path) a code of their own."""
    if _is_serve_request(request) and not isinstance(exc.detail, dict):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "detail": {"code": fallback_code(exc.status_code), "message": str(exc.detail)}
            },
            headers=getattr(exc, "headers", None),
        )
    return await http_exception_handler(request, exc)


@app.middleware("http")
async def enforce_source_payload_size(request, call_next):
    """Short-circuit oversized Source API requests before body parsing."""
    source_prefix = f"{settings.API_V1_PREFIX}/source"
    if request.url.path.startswith(source_prefix):
        limit = (
            SOURCE_UNIVERSE_MAX_PAYLOAD_BYTES
            if request.method == "POST"
            and (
                request.url.path == f"{source_prefix}/universes"
                or (
                    request.url.path.startswith(f"{source_prefix}/provider-work/")
                    and request.url.path.endswith("/manifest")
                )
            )
            else max(1, int(settings.SOURCE_MAX_PAYLOAD_BYTES))
        )
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > limit:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": f"Payload exceeds maximum size of {limit} bytes"},
                    )
            except ValueError:
                pass

    response = await call_next(request)
    credential_ref = getattr(request.state, "credential_ref", None)
    if credential_ref:
        record_usage(
            credential_ref[0],
            credential_ref[1],
            request.url.path,
            response.status_code,
        )
    return response


if APP_ROLE not in {"serve", "ingest", "all"}:
    raise RuntimeError("APP_ROLE must be one of: serve, ingest, all")

if APP_ROLE in {"serve", "all"}:
    app.include_router(
        serve.router,
        prefix=f"{settings.API_V1_PREFIX}/serve",
        tags=["Serve API"],
    )

if APP_ROLE in {"ingest", "all"}:
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
        "role": APP_ROLE,
        "docs": "/docs",
    }
