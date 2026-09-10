"""FastAPI application entry point.

Run locally:

    python -m uvicorn app.main:app --reload
    # or: python run.py  (binds 0.0.0.0:8000)
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .config import settings
from .db import init_db
from .errors import APIError
from .routers import experiments as experiments_router
from .routers.exposures import router as exposures_router
from .routers.exposures import router_lookup as exposure_lookup_router


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        init_db(settings.db_path)
        yield

    app = FastAPI(
        title="GrayLab — explainable gray-release experiment API",
        version="1.0.0",
        lifespan=lifespan,
        description=(
            "Versioned experiment configs, stable hash bucketing, audience "
            "rules, mutex namespaces, schedule windows, whitelist overrides, "
            "idempotent exposure records and full decision traces."
        ),
    )

    @app.exception_handler(APIError)
    async def _api_error(_: Request, exc: APIError) -> JSONResponse:
        body = {"error": {"code": exc.code, "message": exc.message}}
        if exc.details:
            body["error"]["details"] = exc.details
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request,
                                exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "bad_request",
                               "message": "request validation failed",
                               "details": {"errors": exc.errors()}}},
        )

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    api = settings.api_prefix
    app.include_router(experiments_router.router, prefix=api)
    app.include_router(exposures_router, prefix=api)
    app.include_router(exposure_lookup_router, prefix=api)
    return app


app = create_app()
