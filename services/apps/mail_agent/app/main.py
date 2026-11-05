import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from platform_common import configure_logging

configure_logging("mail_agent")
log = logging.getLogger("mail_agent")

from platform_orchestration.models import OrchestrationBase  # noqa: E402

from . import api, scheduler, workflow_api  # noqa: E402  (import after logging is configured)
from .db import engine
from .models import Base


async def _init_db(retries: int = 6, delay: float = 2.0) -> None:
    """Create tables, retrying through transient DNS/connection blips at startup."""
    for attempt in range(1, retries + 1):
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                # The shared orchestration tables live in this service's own
                # database, on their own metadata -- the library does not reach
                # into this service's declarative base. Created here alongside
                # everything else, matching the MVP's create_all approach.
                await conn.run_sync(OrchestrationBase.metadata.create_all)
            return
        except Exception as exc:
            log.warning("DB connect attempt %d/%d failed: %s", attempt, retries, exc)
            if attempt == retries:
                raise
            await asyncio.sleep(delay)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("starting mail-agent: creating tables…")
    await _init_db()
    scheduler.start()
    log.info("mail-agent ready")
    yield
    scheduler.stop()
    await engine.dispose()


app = FastAPI(title="mail-agent", version="0.1.0", lifespan=lifespan)
app.include_router(api.router)
app.include_router(workflow_api.router)
