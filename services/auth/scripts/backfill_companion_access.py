"""One-off backfill: grant the companion app to every existing (user, org) that
only ever had one of {mail-agent, voice-agent}.

scripts/seed_org.py grants the seeded admin both apps up front — this catches
everyone else: accounts created before that existed, or accounts invited into
only one app via the "Users & Roles" invite flow.

For each (user, org) with a membership in app A but none in app B, this
mirrors their exact role (Owner/Admin/User) into app B, reusing the same
find-or-create role logic the "Users & Roles" invite flow uses
(orgs.py's `_ensure_role`) so permissions stay consistent with a normal invite.

Safe to re-run: every insert is preceded by an existence check, so an already
backfilled (or already-dual-app) pair is left untouched.

Usage (from backend/services/auth with the venv active):
    python -m scripts.backfill_companion_access            # dry run — prints only
    python -m scripts.backfill_companion_access --apply     # actually writes
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.models import Application, Membership, Organization, User
from app.routers.orgs import INTERNAL_TO_DISPLAY, _ensure_role

COMPANION = {"mail-agent": "voice-agent", "voice-agent": "mail-agent"}


async def _apps(session: AsyncSession) -> dict[str, Application]:
    rows = list(await session.scalars(select(Application).where(Application.key.in_(COMPANION))))
    return {a.key: a for a in rows}


async def run(apply: bool) -> None:
    async with SessionLocal() as session:
        apps = await _apps(session)
        missing = [k for k in COMPANION if k not in apps]
        if missing:
            print(f"Application row(s) not found, nothing to backfill for them: {missing}")
        if len(apps) < 2:
            return

        memberships = list(
            await session.scalars(
                select(Membership).where(Membership.app_id.in_(a.id for a in apps.values()))
            )
        )
        by_user_org: dict[tuple[str, str], set[str]] = {}
        for m in memberships:
            by_user_org.setdefault((m.user_id, m.org_id), set()).add(m.app_id)

        id_to_key = {a.id: k for k, a in apps.items()}
        planned: list[tuple[str, str, str, str]] = []  # user_id, org_id, from_key, to_key
        for (user_id, org_id), app_ids in by_user_org.items():
            keys_present = {id_to_key[i] for i in app_ids}
            for have in keys_present:
                need = COMPANION[have]
                if need not in keys_present:
                    planned.append((user_id, org_id, have, need))

        if not planned:
            print("Nothing to backfill — every (user, org) already has both apps.")
            return

        print(f"{len(planned)} membership(s) to backfill:")
        for user_id, org_id, have, need in planned:
            user = await session.get(User, user_id)
            org = await session.get(Organization, org_id)
            print(f"  {user.email if user else user_id} @ {org.name if org else org_id}: has {have} -> add {need}")

        if not apply:
            print("\nDry run only — pass --apply to actually create these memberships.")
            return

        created = 0
        for user_id, org_id, have, need in planned:
            # Re-check right before writing — another process (or an earlier
            # iteration granting the reverse direction) may have already covered it.
            existing = await session.scalar(
                select(Membership).where(
                    Membership.user_id == user_id, Membership.org_id == org_id, Membership.app_id == apps[need].id
                )
            )
            if existing:
                continue
            have_membership = next(m for m in memberships if m.user_id == user_id and m.org_id == org_id and m.app_id == apps[have].id)
            role_names = [r.name for r in have_membership.roles] or ["user"]
            roles = [
                await _ensure_role(session, apps[need], org_id, INTERNAL_TO_DISPLAY.get(name, "User"))
                for name in role_names
            ]
            session.add(Membership(user_id=user_id, org_id=org_id, app_id=apps[need].id, roles=roles))
            created += 1
        await session.commit()
        print(f"\nCreated {created} membership(s).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="actually write changes (default: dry run)")
    args = parser.parse_args()
    asyncio.run(run(args.apply))
