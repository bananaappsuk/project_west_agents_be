"""One-time seed: create the single Organization + the agent Applications for a
uni-org deployment.

This product has exactly one organization, ever (see
migrate_add_organizations_singleton.sql, which enforces that at the DB level
too). This script is that org's only creation path, meant to run exactly once
per deployment.

No users are created here. People sign in with Microsoft Entra ID and are
provisioned on first sign-in with the role from their Entra app role
(routers/entra.py) — so the first administrator is whoever the Entra admin
assigns Agents.Superadmin to.

Refuses to run if an Organization already exists, rather than silently
no-op'ing — a second run with different args should never be mistaken for
having worked.

Usage (from backend/services/auth with the venv active):
    python -m scripts.seed_org --org-name "Project West"
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Application, Organization
from app.routers.auth import _slug

APPS = ["mail-agent", "voice-agent"]


async def run(org_name: str) -> None:
    async with SessionLocal() as session:
        existing = await session.scalar(select(Organization))
        if existing:
            print(
                f"An organization already exists ({existing.name!r}, id={existing.id}) "
                "— refusing to create a second one."
            )
            return

        org = Organization(name=org_name, slug=_slug(org_name))
        session.add(org)
        for app_key in APPS:
            if not await session.scalar(select(Application).where(Application.key == app_key)):
                session.add(Application(key=app_key, name=app_key))

        await session.commit()
        print(f"Created organization {org.name!r} (id={org.id}) with apps: {', '.join(APPS)}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--org-name", required=True)
    args = parser.parse_args()
    asyncio.run(run(args.org_name))
