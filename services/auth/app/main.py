from contextlib import asynccontextmanager

from fastapi import FastAPI

from .db import engine
from .models import Base
from .routers import admin, auth, common, health, wellknown


@asynccontextmanager
async def lifespan(app: FastAPI):
    # MVP: auto-create tables. Replace with Alembic migrations later.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


app = FastAPI(title="Auth Service", version="0.1.0", lifespan=lifespan)

for router in (health.router, wellknown.router, auth.router, admin.router, common.router):
    app.include_router(router)
