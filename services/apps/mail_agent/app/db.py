from platform_common.db import make_db

from .config import settings

if not settings.database_url:
    raise RuntimeError("DATABASE_URL is required for the mail-agent service")

engine, SessionLocal = make_db(settings.database_url)


async def get_session():
    async with SessionLocal() as session:
        yield session
