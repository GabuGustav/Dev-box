import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.lifecycle import reconcile_once
from app.logging_config import configure_logging
from app.routers import health, workspaces

configure_logging(settings.log_level)
logger = logging.getLogger("devbox.http")


async def lifecycle_reconciliation_loop() -> None:
    interval = max(5, settings.lifecycle_reconcile_interval_seconds)
    while True:
        try:
            await asyncio.to_thread(reconcile_once)
        except Exception:
            logger.exception("Workspace lifecycle reconciliation cycle failed")
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(_: FastAPI):
    reconciler = None
    if settings.executor_service_token:
        reconciler = asyncio.create_task(lifecycle_reconciliation_loop())
    try:
        yield
    finally:
        if reconciler is not None:
            reconciler.cancel()
            await asyncio.gather(reconciler, return_exceptions=True)


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="Persistent, isolated development workspaces for AI agents.",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-DevBox-Key", "X-Request-ID"],
)


@app.middleware("http")
async def request_logging(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = request_id
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "Unhandled request error",
            extra={"request_id": request_id, "method": request.method, "path": request.url.path},
        )
        response = JSONResponse(status_code=500, content={"detail": "Internal server error"})
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "Request completed",
        extra={
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
        },
    )
    return response


app.include_router(health.router)
app.include_router(workspaces.router, prefix=settings.api_v1_prefix)


@app.get("/")
def root() -> dict[str, str]:
    return {"name": settings.app_name, "version": app.version, "docs": "/docs"}
