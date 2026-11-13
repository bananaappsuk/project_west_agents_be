"""One-off backfill: grant the companion app to an existing (user, org) that only
ever had one of {mail-agent, voice-agent}.

scripts/seed_org.py grants the seeded admin both apps up front — this catches
everyone else: accounts created before that existed, or accounts invited into
only one app via the "Users & Roles" invite flow.

For each (user, org) with a membership in app A but none in app B, this
mirrors their exact role (Owner/Admin/User) into app B, reusing the same
find-or-create role logic the "Users & Roles" invite flow uses
(orgs.py's `_ensure_role`) so permissions stay consistent with a normal invite.

Safe to re-run: every insert is preceded by an existence check, so an already
backfilled (or already-dual-app) pair is left untouched.

⚠️ voice-agent is NO LONGER BACKFILLED — see `NEVER_BACKFILL` below
----------------------------------------------------------------------
This script predates per-application entitlement. Written as it was, one
`--apply` run would grant `voice-agent` to every Email user and silently undo
the access control in `app/routers/entra.py`, because Voice entitlement is now
membership of the PW-Voice-Users Entra group and nothing else.

The script is kept rather than deleted: the `voice-agent -> mail-agent`
direction is still consistent with the model (Email entitlement is the Agents
app role, which any existing member already holds), and the dry-run inventory it
prints is useful on its own. Only the direction that would grant Voice is
refused.

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

#: Apps this script must never create a membership for, however the plan was built.
#:
#: Voice entitlement is membership of the PW-Voice-Users Entra security group,
#: resolved at sign-in by `app/routers/entra.py::_entitled_apps`. A membership
#: created here would not merely duplicate that decision -- it would *override* it,
#: because `POST /auth/refresh` mints a token from whatever membership row exists.
#: So a single `--apply` would hand Voice back to everyone it was just taken from,
#: and nothing in the sign-in path would notice.
#:
#: Granting Voice is therefore a group change in Entra, never a database write.
NEVER_BACKFILL = frozenset({"voice-agent"})


def _permitted(planned: list[tuple[str, str, str, str]]) -> tuple[list, list]:
    """Split a plan into (allowed, refused) on `NEVER_BACKFILL`.

    Pure, so the guard is testable without a database -- which matters, because the
    thing being asserted is a negative ("this never happens") and the natural place
    to discover it otherwise is in production data.
    """
    allowed, refused = [], []
    for item in planned:
        (refused if item[3] in NEVER_BACKFILL else allowed).append(item)
    return allowed, refused


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

        planned, refused = _permitted(planned)

        if refused:
            print(
                f"REFUSED {len(refused)} grant(s) into {'/'.join(sorted(NEVER_BACKFILL))} — "
                "entitlement for that app comes from an Entra group at sign-in, not from "
                "this script. Add the user to PW-Voice-Users instead."
            )
            for user_id, org_id, have, need in refused:
                user = await session.get(User, user_id)
                print(f"  refused: {user.email if user else user_id}: has {have} -> {need}")

        if not planned:
            print("Nothing to backfill.")
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
            # Second, unconditional check at the write site. `_permitted` above has
            # already filtered the plan; this refuses again immediately before the
            # INSERT so that a future edit to the planning code cannot reach the
            # write without passing a guard. Cheap, and the failure it prevents is
            # one nothing downstream would report.
            if need in NEVER_BACKFILL:
                raise SystemExit(
                    f"refusing to create a {need} membership: entitlement for that app "
                    "is an Entra group membership resolved at sign-in, not a database row"
                )
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
