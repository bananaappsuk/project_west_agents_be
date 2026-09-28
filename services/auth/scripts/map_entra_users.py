"""One-time: link existing (pre-Entra) users to their Microsoft Entra ID accounts.

After this, /auth/entra finds these users by Entra object id (`oid`) and keeps
their existing user id — so anything keyed to it carries over. It's the only place
email is used to match; sign-in itself never matches by email.

Input is the Entra user export: Entra admin center → Users → All users →
Download users (CSV). Uses the object id column (`id` or `objectId`),
`userPrincipalName` and `mail`. A local user matches an Entra user when its email
equals that user's mail or UPN (case-insensitive). Anything not matched exactly
once is reported, never guessed.

Dry run by default; pass --apply to write.

Usage (from backend/services/auth with the venv active):
    python -m scripts.map_entra_users --csv exportUsers.csv [--apply]
"""

from __future__ import annotations

import argparse
import asyncio
import csv
from collections import defaultdict

from sqlalchemy import select

from app.db import SessionLocal
from app.models import User


def _load_entra_users(path: str) -> dict[str, list[dict]]:
    """Map lower-cased mail/UPN -> the Entra users carrying it."""
    by_address: dict[str, list[dict]] = defaultdict(list)
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            oid = (row.get("id") or row.get("objectId") or "").strip()
            upn = (row.get("userPrincipalName") or "").strip()
            if not oid or not upn:
                continue
            entry = {"oid": oid, "upn": upn}
            for address in {upn.lower(), (row.get("mail") or "").strip().lower()} - {""}:
                by_address[address].append(entry)
    return by_address


async def run(csv_path: str, apply: bool) -> None:
    by_address = _load_entra_users(csv_path)
    async with SessionLocal() as session:
        users = list(await session.scalars(select(User).order_by(User.email)))
        taken = {u.entra_oid for u in users if u.entra_oid}

        linked, unmatched, ambiguous = [], [], []
        for user in users:
            if user.entra_oid:
                continue
            candidates = {e["oid"]: e for e in by_address.get(user.email.lower(), [])}
            if not candidates:
                unmatched.append(user.email)
            elif len(candidates) > 1:
                ambiguous.append(f"{user.email} -> {', '.join(e['upn'] for e in candidates.values())}")
            else:
                entra = next(iter(candidates.values()))
                if entra["oid"] in taken:
                    ambiguous.append(f"{user.email} -> {entra['upn']} (already linked to another user)")
                    continue
                taken.add(entra["oid"])
                linked.append(f"{user.email} -> {entra['upn']} ({entra['oid']})")
                if apply:
                    user.entra_oid = entra["oid"]
                    user.entra_upn = entra["upn"]

        if apply:
            await session.commit()

    print(f"{'Linked' if apply else 'Would link'} ({len(linked)}):")
    for line in linked:
        print(f"  {line}")
    print(f"Unmatched — no Entra user with this email ({len(unmatched)}):")
    for line in unmatched:
        print(f"  {line}")
    print(f"Ambiguous — not linked, resolve by hand ({len(ambiguous)}):")
    for line in ambiguous:
        print(f"  {line}")
    if not apply:
        print("\nDry run — nothing written. Re-run with --apply to link.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True, help="Entra 'Download users' CSV export")
    parser.add_argument("--apply", action="store_true", help="write the links (default: dry run)")
    args = parser.parse_args()
    asyncio.run(run(args.csv, args.apply))
