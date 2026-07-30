import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from platform_common import configure_logging

configure_logging("voice_agent")
log = logging.getLogger("voice_agent")

from . import api, scheduler  # noqa: E402  (import after logging is configured)
from .db import engine
from .models import Base


async def _init_db(retries: int = 6, delay: float = 2.0) -> None:
    """Create tables, retrying through transient DNS/connection blips at startup."""
    for attempt in range(1, retries + 1):
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            return
        except Exception as exc:
            log.warning("DB connect attempt %d/%d failed: %s", attempt, retries, exc)
            if attempt == retries:
                raise
            await asyncio.sleep(delay)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("starting voice-agent: creating tables…")
    await _init_db()
    scheduler.start()
    log.info("voice-agent ready")
    yield
    scheduler.stop()
    await engine.dispose()


app = FastAPI(title="voice-agent", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "voice_agent"}


app.include_router(api.router)
