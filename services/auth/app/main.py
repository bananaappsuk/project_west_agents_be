import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from platform_common import configure_logging

configure_logging("auth")
log = logging.getLogger("auth")

from .db import engine  # noqa: E402
from .models import Base  # noqa: E402
from .routers import admin, auth, common, health, orgs, wellknown  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):
    # MVP: auto-create tables. Replace with Alembic migrations later.
    log.info("starting auth: creating tables…")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("auth ready")
    yield
    await engine.dispose()


app = FastAPI(title="Auth Service", version="0.1.0", lifespan=lifespan)

for router in (health.router, wellknown.router, auth.router, admin.router, common.router, orgs.router):
    app.include_router(router)
