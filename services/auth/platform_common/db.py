"""Async SQLAlchemy helper for hosted Postgres (Neon, Supabase, or similar).

SQLAlchemy/asyncpg are imported lazily so stateless services (e.g. the gateway)
can depend on platform_common without installing a database driver.
"""


def make_db(database_url: str):
    """Return (engine, async_sessionmaker) for the given asyncpg URL.

    SSL is enabled automatically for `postgresql+asyncpg` URLs — every hosted
    Postgres provider we've used (Neon, Supabase) requires TLS on the wire.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    connect_args: dict = {}
    if database_url.startswith("postgresql+asyncpg"):
        # ssl="require" (not True/verify-full) -> encrypts the connection but
        #   doesn't verify the server's certificate chain. Needed for Supabase's
        #   Supavisor pooler specifically: its presented cert fails full chain
        #   verification (`CERTIFICATE_VERIFY_FAILED: self-signed certificate in
        #   certificate chain`) even from a clean network with no local
        #   interception — confirmed by hitting the identical error from a
        #   Railway-deployed service, not just a local machine. Still encrypted,
        #   just not pinned to a trusted CA — an accepted tradeoff for a pooled
        #   connection whose endpoint we already know we're deliberately
        #   connecting to.
        # statement_cache_size=0 -> required behind ANY PgBouncer-style
        #   transaction-mode connection pooler (Neon's "-pooler" endpoint,
        #   Supabase's Supavisor transaction-mode/6543 endpoint, ...) — asyncpg's
        #   server-side prepared statements don't survive a connection being
        #   handed to a different physical backend between statements. Harmless
        #   on a direct (non-pooled) endpoint.
        connect_args = {"ssl": "require", "statement_cache_size": 0}

    # pool_recycle(280) alone isn't enough behind Supabase's Supavisor pooler —
    # its idle-connection cutoff is shorter than the ~5-min one 280s was tuned for
    # (that number came from Neon), so requests intermittently hit "connection is
    # closed" / "connection was closed in the middle of operation" on a pooled
    # connection Supavisor already dropped server-side. pool_pre_ping adds one
    # cheap round-trip on checkout to catch that before the real query does;
    # pool_recycle stays as a second line of defense against connections going
    # stale between checkouts.
    engine = create_async_engine(database_url, connect_args=connect_args, pool_recycle=280, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, session_factory
