"""Async SQLAlchemy helper for Neon Postgres.

SQLAlchemy/asyncpg are imported lazily so stateless services (e.g. the gateway)
can depend on platform_common without installing a database driver.
"""


def make_db(database_url: str):
    """Return (engine, async_sessionmaker) for the given asyncpg URL.

    SSL is enabled automatically for `postgresql+asyncpg` URLs (Neon requires it).
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    connect_args: dict = {}
    if database_url.startswith("postgresql+asyncpg"):
        # ssl=True         -> Neon requires TLS.
        # statement_cache_size=0 -> required for Neon's pooled (-pooler / PgBouncer
        #   transaction-mode) endpoint; harmless on the direct endpoint.
        connect_args = {"ssl": True, "statement_cache_size": 0}

    # pool_recycle instead of pool_pre_ping: pre_ping adds a round-trip on every
    # checkout (painful on a high-latency link); recycling before Neon's ~5-min
    # idle cutoff keeps connections healthy without that per-request cost.
    engine = create_async_engine(database_url, connect_args=connect_args, pool_recycle=280)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, session_factory
