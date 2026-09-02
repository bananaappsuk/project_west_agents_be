"""One-time seed: create the single Organization + first admin User for a
uni-org deployment.

Self-serve registration (`/auth/register`) and the platform-admin "sell one
app to an org" endpoint (`/admin/provision`) have both been removed — this
product now has exactly one organization, ever (see
migrate_add_organizations_singleton.sql, which enforces that at the DB level
too). This script is that org's only creation path, meant to run exactly once
per deployment. Every additional user after this one is added via the normal
"Users & Roles" invite flow (routers/orgs.py's POST /orgs/members +
POST /auth/accept-invite) — not this script.

Grants the seeded admin an Owner membership in both "mail-agent" and
"voice-agent" (mirrors what /auth/register used to do via AUTO_SUBSCRIBE_APPS
before it was removed), reusing the same provisioning helper that endpoint
used (routers/auth.py's `_provision_owner_membership`) so permissions/roles
stay identical to what a normal owner would have gotten.

Refuses to run if an Organization already exists, rather than silently
no-op'ing — a second run with different args should never be mistaken for
having worked.

Usage (from backend/services/auth with the venv active):
    python -m scripts.seed_org --org-name "Acme" --email admin@acme.com --password "change-me-now" [--full-name "Ada Admin"]
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Organization, User
from app.routers.auth import _provision_owner_membership, _slug
from app.security import hash_password

APPS = ["mail-agent", "voice-agent"]


async def run(org_name: str, email: str, password: str, full_name: str | None) -> None:
    async with SessionLocal() as session:
        existing = await session.scalar(select(Organization))
        if existing:
            print(
                f"An organization already exists ({existing.name!r}, id={existing.id}) "
                "— refusing to create a second one."
            )
            return

        user = User(email=email, password_hash=hash_password(password), full_name=full_name)
        org = Organization(name=org_name, slug=_slug(org_name))
        session.add_all([user, org])
        await session.flush()

        for app_key in APPS:
            await _provision_owner_membership(session, user=user, org=org, app_key=app_key)

        await session.commit()
        print(
            f"Created organization {org.name!r} (id={org.id}) and admin user {user.email} "
            f"(id={user.id}), owner of: {', '.join(APPS)}."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--org-name", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--full-name", default=None)
    args = parser.parse_args()
    asyncio.run(run(args.org_name, args.email, args.password, args.full_name))
