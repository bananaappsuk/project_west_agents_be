from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import api, scheduler
from .db import engine
from .models import Base


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    scheduler.start()
    yield
    scheduler.stop()
    await engine.dispose()


app = FastAPI(title="west-agent", version="0.1.0", lifespan=lifespan)
app.include_router(api.router)
