"""One-off cleanup: keep exactly one user's organization(s) and delete every
other organization's data across every service that shares this Postgres
database (auth, mail_agent, voice_agent, billing).

Scope, precisely:
  - Find the user by --keep-email, and every org they're a member of.
  - Keep those orgs, every user who belongs to them (teammates included, not
    just the named email), and every row any of those orgs own.
  - Delete every other organization, user, membership, role, invite, refresh
    token, password reset, email, folder, mailbox, recording, voice setting,
    subscription, usage event, and agent-run history.
  - Left untouched entirely: `applications`, `permissions`, `plans` — shared
    platform/catalog tables, not per-org data.

Deletion order matters: children before parents, so FK constraints that
aren't ON DELETE CASCADE (Role -> Organization) don't reject the parent
delete.

Usage (from backend/ with any service's venv active — asyncpg is a shared dep):
    python scripts/prune_to_one_org.py --keep-email you@example.com              # dry run
    python scripts/prune_to_one_org.py --keep-email you@example.com --apply       # actually deletes
"""

from __future__ import annotations

import argparse
import asyncio
import re

import asyncpg

# Every service in this repo points at the same Postgres database (see each
# service's .env DATABASE_URL) — read from auth's since it's guaranteed present.
_ENV_PATH = "services/auth/.env"


def _load_database_url() -> str:
    with open(_ENV_PATH, encoding="utf-8") as f:
        for line in f:
            if line.startswith("DATABASE_URL="):
                url = line.split("=", 1)[1].strip()
                # asyncpg doesn't understand SQLAlchemy's "+asyncpg" dialect suffix.
                return re.sub(r"^postgresql\+asyncpg://", "postgresql://", url)
    raise RuntimeError(f"DATABASE_URL not found in {_ENV_PATH}")


# (table, org_id column) for every straightforwardly org-scoped table — no FK
# to `organizations`, so order among these doesn't matter and CASCADE isn't a
# factor. Auth's own tables (organizations/users/roles/memberships/invites/
# refresh_tokens/password_resets) are handled separately below since they DO
# have FK relationships that dictate ordering.
ORG_SCOPED_TABLES = [
    ("emails", "org_id"),
    ("folders", "org_id"),
    ("agent_runs", "org_id"),
    ("mailboxes", "org_id"),
    ("agent_config", "org_id"),
    ("recordings", "org_id"),
    ("voice_settings", "org_id"),
    ("voice_notifications", "org_id"),
    ("voice_agent_runs", "org_id"),
    ("subscriptions", "org_id"),
    ("usage_events", "org_id"),
]


async def run(keep_email: str, apply: bool) -> None:
    conn = await asyncpg.connect(_load_database_url())
    try:
        user = await conn.fetchrow("SELECT id, email FROM users WHERE email = $1", keep_email)
        if not user:
            print(f"No user found with email {keep_email!r} — aborting, nothing touched.")
            return

        keep_org_rows = await conn.fetch("SELECT DISTINCT org_id FROM memberships WHERE user_id = $1", user["id"])
        keep_org_ids = [r["org_id"] for r in keep_org_rows]
        if not keep_org_ids:
            print(f"{keep_email} has no organization memberships — aborting rather than deleting everything.")
            return

        keep_user_rows = await conn.fetch(
            "SELECT DISTINCT user_id FROM memberships WHERE org_id = ANY($1::text[])", keep_org_ids
        )
        keep_user_ids = list({r["user_id"] for r in keep_user_rows} | {user["id"]})

        org_names = await conn.fetch("SELECT id, name FROM organizations WHERE id = ANY($1::text[])", keep_org_ids)
        print(f"Keeping: {keep_email}")
        for r in org_names:
            print(f"  org {r['id']} — {r['name']}")
        print(f"  ({len(keep_user_ids)} user(s) total across kept org(s))")
        print()

        # ---- preview counts ----
        counts: list[tuple[str, str, int]] = []
        for table, col in ORG_SCOPED_TABLES:
            n = await conn.fetchval(f"SELECT count(*) FROM {table} WHERE {col} != ALL($1::text[])", keep_org_ids)
            if n:
                counts.append((table, "org_id", n))

        n = await conn.fetchval("SELECT count(*) FROM invites WHERE org_id != ALL($1::text[])", keep_org_ids)
        if n:
            counts.append(("invites", "org_id", n))
        n = await conn.fetchval("SELECT count(*) FROM memberships WHERE org_id != ALL($1::text[])", keep_org_ids)
        if n:
            counts.append(("memberships", "org_id", n))
        n = await conn.fetchval(
            "SELECT count(*) FROM roles WHERE org_id IS NOT NULL AND org_id != ALL($1::text[])", keep_org_ids
        )
        if n:
            counts.append(("roles", "org_id", n))
        n = await conn.fetchval("SELECT count(*) FROM refresh_tokens WHERE user_id != ALL($1::text[])", keep_user_ids)
        if n:
            counts.append(("refresh_tokens", "user_id", n))
        n = await conn.fetchval("SELECT count(*) FROM password_resets WHERE user_id != ALL($1::text[])", keep_user_ids)
        if n:
            counts.append(("password_resets", "user_id", n))
        n = await conn.fetchval("SELECT count(*) FROM organizations WHERE id != ALL($1::text[])", keep_org_ids)
        if n:
            counts.append(("organizations", "id", n))
        n = await conn.fetchval("SELECT count(*) FROM users WHERE id != ALL($1::text[])", keep_user_ids)
        if n:
            counts.append(("users", "id", n))

        if not counts:
            print("Nothing to delete — every row already belongs to the kept org(s).")
            return

        print("Rows that would be deleted:")
        total = 0
        for table, _col, n in counts:
            print(f"  {table:<20} {n}")
            total += n
        print(f"  {'TOTAL':<20} {total}")

        if not apply:
            print("\nDry run only — pass --apply to actually delete these rows.")
            return

        # ---- delete, children before parents ----
        async with conn.transaction():
            for table, col in ORG_SCOPED_TABLES:
                await conn.execute(f"DELETE FROM {table} WHERE {col} != ALL($1::text[])", keep_org_ids)
            await conn.execute("DELETE FROM invites WHERE org_id != ALL($1::text[])", keep_org_ids)
            await conn.execute("DELETE FROM memberships WHERE org_id != ALL($1::text[])", keep_org_ids)
            await conn.execute(
                "DELETE FROM roles WHERE org_id IS NOT NULL AND org_id != ALL($1::text[])", keep_org_ids
            )
            await conn.execute("DELETE FROM refresh_tokens WHERE user_id != ALL($1::text[])", keep_user_ids)
            await conn.execute("DELETE FROM password_resets WHERE user_id != ALL($1::text[])", keep_user_ids)
            await conn.execute("DELETE FROM organizations WHERE id != ALL($1::text[])", keep_org_ids)
            await conn.execute("DELETE FROM users WHERE id != ALL($1::text[])", keep_user_ids)

        print(f"\nDeleted. {keep_email} and {len(org_names)} org(s) remain.")
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-email", required=True)
    parser.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    args = parser.parse_args()
    asyncio.run(run(args.keep_email, args.apply))
