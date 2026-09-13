"""FastAPI application entry point.

Run locally:

    python -m uvicorn app.main:app --reload
    # or: python run.py  (binds 0.0.0.0:8000)
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .config import settings
from .db import init_db
from .errors import APIError
from .routers import experiments as experiments_router
from .routers.cuped import plans_router as cuped_plans_router
from .routers.cuped import router as cuped_router
from .routers.exposures import router as exposures_router
from .routers.exposures import router_lookup as exposure_lookup_router
from .routers.metrics import router as metrics_router
from .routers.release import plans_router as release_plans_router
from .routers.release import router as release_router
from .routers.sequential import plans_router as sequential_plans_router
from .routers.sequential import router as sequential_router


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
        # jsonable_encoder is essential: pydantic error "ctx" can embed raw
        # ValueError objects (e.g. schedule end_at <= start_at), which are
        # not JSON serializable and would otherwise turn the response into
        # a 500.
        return JSONResponse(
            status_code=400,
            content=jsonable_encoder({
                "error": {"code": "bad_request",
                          "message": "request validation failed",
                          "details": {"errors": exc.errors()}},
            }),
        )

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    api = settings.api_prefix
    app.include_router(experiments_router.router, prefix=api)
    app.include_router(exposures_router, prefix=api)
    app.include_router(exposure_lookup_router, prefix=api)
    app.include_router(metrics_router, prefix=api)
    # global plan-key routes must be registered before the experiment-scoped
    # /experiments/... routers do not clash here; ordering is irrelevant since
    # prefixes differ, but plans_router is included first for clarity.
    app.include_router(sequential_plans_router, prefix=api)
    app.include_router(sequential_router, prefix=api)
    app.include_router(cuped_plans_router, prefix=api)
    app.include_router(cuped_router, prefix=api)
    app.include_router(release_plans_router, prefix=api)
    app.include_router(release_router, prefix=api)
    return app


app = create_app()
